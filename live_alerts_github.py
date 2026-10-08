"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   Live Alert System — GitHub Actions Version                               ║
║   Runs every 5 min on GitHub servers                                       ║
║   State stored in GitHub Gist (survives PC shutdown)                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Morning setup (on your PC, 1 min):                                        ║
║    Double-click 1_MORNING_SETUP.bat                                        ║
║    → Fyers auth → token pushed to GitHub Secret automatically              ║
║    → GitHub starts running alerts all day                                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, re, time, logging
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, date
import warnings
warnings.filterwarnings("ignore")

# ── LOGGER ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("alerts")
def L(msg):
    for line in str(msg).split("\n"): log.info(line)

# ═════════════════════════════════════════════════════════════════════════════
# CONFIG — loaded from environment (GitHub Secrets)
# ═════════════════════════════════════════════════════════════════════════════
CONFIG = {
    "client_id":         os.environ.get("FYERS_CLIENT_ID", "ALTBAEV1JI-200"),
    "fyers_token":       os.environ.get("FYERS_TOKEN", ""),
    "telegram_token":    os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id":  os.environ.get("TELEGRAM_CHAT_ID", ""),
    "gist_id":           os.environ.get("GIST_ID", ""),
    "github_token":      os.environ.get("GH_PAT", ""),

    "nifty_symbol":      "NSE:NIFTY50-INDEX",
    "crude_symbol":      "MCX:CRUDEOIL26OCTFUT",
    "nifty_expiry":      "26OCT",
    "strike_gap":        50,

    # Alert proximity
    "nifty_prox":        3,
    "crude_prox":        5,

    # Gann degrees
    "nifty_degree":      45,
    "crude_degree":      90,
    "crude_adj_degree":  135,
    "crude_adj_thresh":  30,

    # Swing lock times
    "nifty_swing_lock":  "10:30",
    "crude_swing_lock":  "10:30",

    # POB
    "nifty_pob_mins":    15,
    "crude_pob_mins":    15,
    "crude_pob_lock":    90,

    # Auto trade
    "auto_trade":        True,
    "auto_lots":         3,
    "auto_tsl":          25,
    "auto_sl":           25,
    "auto_min_premium":  50,
    "auto_max_per_day":  1,     # 1 for live, 3 for testing
    "auto_triggers":     [1, 2],

    # Market hours (IST)
    "nifty_start":       "09:15",
    "nifty_end":         "15:30",
    "crude_start":       "09:00",
    "crude_end":         "23:30",
}


# ═════════════════════════════════════════════════════════════════════════════
# GIST STATE — persistent memory between GitHub Action runs
# ═════════════════════════════════════════════════════════════════════════════
class GistState:
    """Read/write trading state from GitHub Gist."""

    def __init__(self, gist_id: str, token: str):
        self.gist_id = gist_id
        self.token   = token
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "trading-alerts"
        }

    def load(self) -> dict:
        try:
            url  = f"https://api.github.com/gists/{self.gist_id}"
            req  = urllib.request.Request(url, headers=self.headers)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            content = data["files"]["trading_state.json"]["content"]
            return json.loads(content)
        except Exception as e:
            L(f"  Gist load error: {e} — starting fresh")
            return {}

    def save(self, state: dict):
        try:
            url     = f"https://api.github.com/gists/{self.gist_id}"
            payload = json.dumps({
                "files": {
                    "trading_state.json": {
                        "content": json.dumps(state, indent=2, default=str)
                    }
                }
            }).encode()
            req = urllib.request.Request(
                url, data=payload, headers=self.headers, method="PATCH")
            urllib.request.urlopen(req, timeout=15)
            L("  State saved to Gist ✅")
        except Exception as e:
            L(f"  Gist save error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═════════════════════════════════════════════════════════════════════════════
class Telegram:
    def __init__(self, token, chat_id):
        self.token = token; self.chat_id = str(chat_id)
        self.enabled = bool(token and chat_id)
        self.offset  = 0

    def send(self, msg: str):
        if not self.enabled: print(f"TG: {msg[:80]}"); return
        try:
            url  = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id":    self.chat_id,
                "text":       msg,
                "parse_mode": "HTML"
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(url, data=data), timeout=10)
        except Exception as e:
            L(f"  TG error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# FYERS DATA
# ═════════════════════════════════════════════════════════════════════════════
class FyersData:
    def __init__(self, client_id, token):
        from fyers_apiv3 import fyersModel
        self.fyers = fyersModel.FyersModel(
            client_id=client_id, token=token, log_path="", is_async=False)

    def fetch(self, symbol, days=3) -> list:
        """Fetch last N days of 5-min candles."""
        from datetime import timedelta
        end   = datetime.now()
        start = (end - timedelta(days=days+3)).strftime("%Y-%m-%d")
        end_s = end.strftime("%Y-%m-%d")
        resp  = self.fyers.history(data={
            "symbol": symbol, "resolution": "5",
            "date_format": "1", "cont_flag": "1",
            "range_from": start, "range_to": end_s})
        if resp.get("code") == 200 and resp.get("candles"):
            return resp["candles"]   # [[ts, o, h, l, c, v], ...]
        return []

    def fetch_daily(self, symbol, days=5) -> list:
        from datetime import timedelta
        end   = datetime.now()
        start = (end - timedelta(days=days+5)).strftime("%Y-%m-%d")
        resp  = self.fyers.history(data={
            "symbol": symbol, "resolution": "D",
            "date_format": "1", "cont_flag": "1",
            "range_from": start, "range_to": end.strftime("%Y-%m-%d")})
        if resp.get("code") == 200 and resp.get("candles"):
            return resp["candles"]
        return []

    def get_ltp_batch(self, symbols: list) -> dict:
        if not symbols: return {}
        try:
            resp = self.fyers.quotes(data={"symbols": ",".join(symbols)})
            out  = {}
            if resp.get("code") == 200:
                for item in resp.get("d", []):
                    try: out[item["n"]] = float(item["v"]["lp"])
                    except: pass
            return out
        except: return {}


# ═════════════════════════════════════════════════════════════════════════════
# LEVEL CALCULATORS (same as live_alerts.py)
# ═════════════════════════════════════════════════════════════════════════════
def calc_gann(open_px, degree=45, num=8):
    mult = degree / 360.0; sq = open_px ** 0.5
    r = [round((sq + i*mult)**2, 2) for i in range(1, num+1)]
    s = [round(max((sq - i*mult)**2, 0), 2) for i in range(1, num+1)]
    return {"R": r, "S": s, "SL": round((r[0]+s[0])/2, 2), "degree": degree}

def calc_pob(or_high, or_low):
    rng = or_high - or_low; onm = rng*0.146; dec = rng*0.236; stop = onm+dec
    def side(bo, bd, sign):
        d = {"ONM":round(bo,2),"DEC":round(bd,2),"SL":round(bo-sign*stop,2),
             "T1":round(bo+sign*onm*5,2),"T2":round(bd+sign*dec*5,2),
             "T3":round(bo+sign*onm*9,2),"T4":round(bo+sign*onm*14,2),
             "T5":round(bd+sign*dec*9,2),"T6":round(bo+sign*onm*18,2),
             "T7":round(bo+sign*onm*23.25,2),"T8":round(bd+sign*dec*18,2)}
        d["T23_zone"] = (min(d["T2"],d["T3"]), max(d["T2"],d["T3"]))
        d["T45_zone"] = (min(d["T4"],d["T5"]), max(d["T4"],d["T5"]))
        return d
    return {"UP":   side(or_high+onm, or_high+dec, +1),
            "DOWN": side(or_low -onm, or_low -dec, -1),
            "or_high": or_high, "or_low": or_low}

def calc_swing(candles_today, min_sep=10):
    highs = sorted(set(c[2] for c in candles_today), reverse=True)
    lows  = sorted(set(c[3] for c in candles_today))
    sh1=sh2=sl1=sl2=None
    for h in highs:
        if sh1 is None: sh1=h
        elif abs(h-sh1)>=min_sep: sh2=h; break
    for l in lows:
        if sl1 is None: sl1=l
        elif abs(l-sl1)>=min_sep: sl2=l; break
    return {"SH1":sh1,"SH2":sh2,"SL1":sl1,"SL2":sl2}

def calc_cpr(pdh, pdl, pdc):
    pivot=(pdh+pdl+pdc)/3; bc=(pdh+pdl)/2; tc=(pivot-bc)+pivot
    return {"pivot":round(pivot,2),"tc":round(tc,2),"bc":round(bc,2),
            "pdh":round(pdh,2),"pdl":round(pdl,2),"pdc":round(pdc,2)}

def calc_wick(c1, c2):
    if not c1 or not c2: return None
    _,o1,h1,l1,c1c,_ = c1; _,o2,h2,l2,c2c,_ = c2
    fc="green" if c1c>o1 else "red"; sc="green" if c2c>o2 else "red"
    inside = h2<=h1 and l2>=l1
    top=bot=None
    if inside:
        if fc=="green" and sc=="green": top,bot=o2,l2
        elif fc=="red" and sc=="green": top,bot=h2,o2
        else: top,bot=h2,l2
    else:
        if sc=="red": top,bot=h2,max(o2,c2c)
        else: top,bot=min(o2,c2c),l2
    return {"top":round(top,2),"bot":round(bot,2)} if top and bot else None

def calc_trend(candles_all, sess_open):
    if len(candles_all) < 22:
        return {"score":0,"label":"NEUTRAL","rsi":50,"bull":True,"close":sess_open}
    closes = [c[4] for c in candles_all]
    vols   = [c[5] for c in candles_all]
    c = closes[-1]

    def ema(data, span):
        k = 2/(span+1); e = data[0]
        for x in data[1:]: e = x*k + e*(1-k)
        return e

    ema9  = ema(closes[-30:], 9)
    ema21 = ema(closes[-40:], 21)
    vwap  = sum(closes[i]*vols[i] for i in range(-30,0)) / max(sum(vols[-30:]),1)

    delta = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    gains = [max(d,0) for d in delta[-14:]]
    losses= [-min(d,0) for d in delta[-14:]]
    ag = sum(gains)/14; al = sum(losses)/14
    rsi = 100 - 100/(1+ag/max(al,0.001))

    score = 0
    score += 2 if c > sess_open else -2
    score += 2 if ema9 > ema21  else -2
    score += 2 if c > vwap      else -2
    score += 1 if rsi > 60 else (-1 if rsi < 40 else 0)

    label = ("STRONG BULL" if score>=7 else "BULLISH" if score>=4 else
             "NEUTRAL" if score>=-3 else "BEARISH" if score>=-6 else "STRONG BEAR")
    return {"score":score,"label":label,"rsi":round(rsi,1),
            "close":c,"ema9":round(ema9,2),"ema21":round(ema21,2),"bull":c>sess_open and ema9>ema21}


# ═════════════════════════════════════════════════════════════════════════════
# ALERT ENGINE — stateless (reads/writes state dict)
# ═════════════════════════════════════════════════════════════════════════════
def tz_ts(ts_unix):
    """Convert unix timestamp to IST datetime."""
    from datetime import timezone, timedelta
    return datetime.fromtimestamp(ts_unix, tz=timezone(timedelta(hours=5, minutes=30)))

def in_session(t, start_str, end_str):
    start = list(map(int, start_str.split(":")))
    end   = list(map(int, end_str.split(":")))
    h,m   = t.hour, t.minute
    return (h*60+m) >= (start[0]*60+start[1]) and (h*60+m) <= (end[0]*60+end[1])

def process_instrument(candles_all, daily_candles, cfg, inst_name,
                       inst_state: dict, tg: Telegram, fd: FyersData,
                       global_state: dict) -> dict:
    """
    Process one instrument for this 5-min run.
    Returns updated inst_state.
    """
    now   = datetime.now()
    today = str(now.date())
    prox  = cfg[f"{inst_name.lower()}_prox"] if inst_name=="NIFTY" else cfg["crude_prox"]
    start = cfg[f"{inst_name.lower()}_start"]
    end   = cfg[f"{inst_name.lower()}_end"]

    # Filter today's candles
    candles_today = [c for c in candles_all
                     if str(tz_ts(c[0]).date()) == today]
    if not candles_today: return inst_state

    # Check if in session
    if not in_session(now, start, end):
        L(f"  [{inst_name}] Outside session")
        return inst_state

    # Reset state on new day
    if inst_state.get("date") != today:
        L(f"  [{inst_name}] New day — resetting state")
        inst_state = {"date": today, "alerted": {}, "break_alerted": [],
                      "auto_trades_taken": 0, "active_trade": None}

    # Session open + Gann
    if "sess_open" not in inst_state:
        inst_state["sess_open"] = candles_today[0][1]  # open of first candle
        degree = cfg.get(f"{inst_name.lower()}_degree",
                         cfg["nifty_degree"] if inst_name=="NIFTY" else cfg["crude_degree"])
        gann   = calc_gann(inst_state["sess_open"], degree)
        # Auto-adjust crude
        if inst_name == "CRUDE":
            if abs(gann["R"][0] - gann["S"][0]) < cfg["crude_adj_thresh"]:
                gann = calc_gann(inst_state["sess_open"], cfg["crude_adj_degree"])
                L(f"  [{inst_name}] Gann auto-adjusted to {cfg['crude_adj_degree']}°")
        inst_state["gann"] = gann
        L(f"  [{inst_name}] Open={inst_state['sess_open']} Gann {gann['degree']}° "
          f"R1={gann['R'][0]} S1={gann['S'][0]}")

    # Wick zone
    if "wick" not in inst_state and len(candles_today) >= 2:
        inst_state["wick"] = calc_wick(candles_today[0], candles_today[1])
        if inst_state["wick"]:
            L(f"  [{inst_name}] Wick: Top={inst_state['wick']['top']} Bot={inst_state['wick']['bot']}")

    # CPR from previous day
    if "cpr" not in inst_state and len(daily_candles) >= 2:
        prev = daily_candles[-2]  # [ts, o, h, l, c, v]
        inst_state["cpr"] = calc_cpr(prev[2], prev[3], prev[4])
        L(f"  [{inst_name}] PDH={inst_state['cpr']['pdh']} PDL={inst_state['cpr']['pdl']}")

    # POB phase 1 (after or_mins)
    or_mins = cfg["nifty_pob_mins"] if inst_name=="NIFTY" else cfg["crude_pob_mins"]
    s_start = list(map(int, start.split(":")))
    elapsed = (now.hour - s_start[0])*60 + (now.minute - s_start[1])
    if elapsed >= or_mins and "pob" not in inst_state:
        or_c = [c for c in candles_today if elapsed_from_open(c[0], candles_today[0][0]) <= or_mins*60]
        if or_c:
            inst_state["pob"] = calc_pob(max(c[2] for c in or_c), min(c[3] for c in or_c))
            L(f"  [{inst_name}] POB set OR={inst_state['pob']['or_high']}/{inst_state['pob']['or_low']}")

    # Swing zones lock at swing_lock time
    sw_lock = cfg["nifty_swing_lock"] if inst_name=="NIFTY" else cfg["crude_swing_lock"]
    sw_lock_mins = int(sw_lock.split(":")[0])*60 + int(sw_lock.split(":")[1])
    now_mins = now.hour*60 + now.minute
    if now_mins >= sw_lock_mins and "swing" not in inst_state:
        window = [c for c in candles_today if tz_ts(c[0]).hour*60+tz_ts(c[0]).minute <= sw_lock_mins]
        if window:
            inst_state["swing"] = calc_swing(window)
            L(f"  [{inst_name}] Swing locked: {inst_state['swing']}")
            tg.send(
                f"📐 <b>{inst_name} Levels Set</b>\n"
                f"🔴 Supply: {inst_state['swing'].get('SH2')}–{inst_state['swing'].get('SH1')}\n"
                f"🟢 Demand: {inst_state['swing'].get('SL1')}–{inst_state['swing'].get('SL2')}\n"
                f"📐 Gann R1:{inst_state['gann']['R'][0]} R2:{inst_state['gann']['R'][1]}\n"
                f"📐 Gann S1:{inst_state['gann']['S'][0]} S2:{inst_state['gann']['S'][1]}\n"
                f"📌 PDH:{inst_state['cpr']['pdh'] if 'cpr' in inst_state else 'N/A'} "
                f"PDL:{inst_state['cpr']['pdl'] if 'cpr' in inst_state else 'N/A'}")

    # Latest completed candle (second to last)
    if len(candles_today) < 2: return inst_state
    latest = candles_today[-2]
    prev_c = candles_today[-3] if len(candles_today) >= 3 else candles_today[-2]
    price       = latest[4]   # close
    candle_open = latest[1]
    candle_bull = price > candle_open
    candle_bear = price < candle_open
    ts_str      = tz_ts(latest[0]).strftime("%H:%M")

    trend = calc_trend(candles_all, inst_state["sess_open"])
    L(f"  [{inst_name}] [{ts_str}] price={price} | {trend['label']}({trend['score']}) RSI:{trend['rsi']}")

    # Check active paper trade
    inst_state = check_active_trade(inst_state, fd, tg, cfg)

    # ── BUILD ALERTS ─────────────────────────────────────────────────────────
    alerted      = inst_state.get("alerted", {})
    break_alerted= inst_state.get("break_alerted", [])
    gann         = inst_state.get("gann", {})
    pob          = inst_state.get("pob")
    swing        = inst_state.get("swing", {})
    wick         = inst_state.get("wick")
    cpr          = inst_state.get("cpr")
    alerts       = []

    # Gann near
    gann_near = []
    for i, r in enumerate(gann.get("R",[])[:8], 1):
        if abs(price-r) <= prox: gann_near.append((f"R{i}", r, "PE"))
    for i, s in enumerate(gann.get("S",[])[:8], 1):
        if abs(price-s) <= prox: gann_near.append((f"S{i}", s, "CE"))

    # POB near
    pob_near = []
    if pob:
        for sk, pob_side, sign in [("UP",pob["UP"],"CE"),("DOWN",pob["DOWN"],"PE")]:
            for lbl in ["ONM","DEC","SL","T1","T6","T7","T8"]:
                lvl = pob_side.get(lbl)
                if lvl and abs(price-lvl) <= prox:
                    pob_near.append((f"{sk} {lbl}", lvl, sign))
            for zk,zl in [("T23_zone","T2-T3"),("T45_zone","T4-T5")]:
                z = pob_side.get(zk)
                if z and z[0] <= price <= z[1]:
                    pob_near.append((f"{sk} {zl} zone", (z[0]+z[1])/2, sign))

    # Confluence
    for g_lbl,g_lvl,g_dir in gann_near:
        for p_lbl,p_lvl,p_dir in pob_near:
            if abs(g_lvl-p_lvl) <= 8:
                cc = (g_dir=="PE" and candle_bear) or (g_dir=="CE" and candle_bull)
                alerts.append({"key":f"CONF_{g_lbl}_{p_lbl}","priority":1,
                    "label":f"{'🔴🔴 STRONG' if cc else '⚠️ WATCH'} Gann+POB",
                    "level":g_lvl,"direction":g_dir,"close_confirms":cc,
                    "msg":f"Gann {g_lbl}={g_lvl} + POB {p_lbl}={p_lvl:.2f}"})

    # Gann alone
    for g_lbl,g_lvl,g_dir in gann_near:
        if any(f"CONF_{g_lbl}_" in a["key"] for a in alerts): continue
        cc = (g_dir=="PE" and candle_bear) or (g_dir=="CE" and candle_bull)
        alerts.append({"key":f"Gann_{g_lbl}","priority":2,
            "label":f"📐 Gann {g_lbl}","level":g_lvl,"direction":g_dir,
            "close_confirms":cc,
            "msg":f"Gann {g_lbl}={g_lvl} | {'✅ confirmed' if cc else '⏳ watch close'}"})

    # PDH/PDL/PDC
    if cpr:
        pdh=cpr["pdh"]; pdl=cpr["pdl"]; pdc=cpr["pdc"]
        if abs(price-pdh) <= prox:
            cc = candle_bear
            alerts.append({"key":"PDH_touch","priority":2,"label":"📌 PDH",
                "level":pdh,"direction":"PE","close_confirms":cc,
                "msg":f"PDH={pdh} | {'✅ bearish' if cc else '⏳ watch'}"})
        elif price > pdh and candle_bull and "PDH_break" not in break_alerted:
            break_alerted.append("PDH_break")
            alerts.append({"key":"PDH_break","priority":2,"label":"🚀 PDH Broken",
                "level":pdh,"direction":"CE","close_confirms":True,
                "msg":f"PDH={pdh} BROKEN — strong bull"})
        if abs(price-pdl) <= prox:
            cc = candle_bull
            alerts.append({"key":"PDL_touch","priority":2,"label":"📌 PDL",
                "level":pdl,"direction":"CE","close_confirms":cc,
                "msg":f"PDL={pdl} | {'✅ bullish' if cc else '⏳ watch'}"})
        elif price < pdl and candle_bear and "PDL_break" not in break_alerted:
            break_alerted.append("PDL_break")
            alerts.append({"key":"PDL_break","priority":2,"label":"📉 PDL Broken",
                "level":pdl,"direction":"PE","close_confirms":True,
                "msg":f"PDL={pdl} BROKEN — strong bear"})
        if abs(price-pdc) <= prox:
            d="CE" if price<=pdc else "PE"
            cc=(d=="CE" and candle_bull) or (d=="PE" and candle_bear)
            alerts.append({"key":"PDC_touch","priority":2,"label":"📌 PDC",
                "level":pdc,"direction":d,"close_confirms":cc,
                "msg":f"PDC={pdc} decision | {'✅' if cc else '⏳'}"})

    # Swing zones
    sh1=swing.get("SH1"); sh2=swing.get("SH2")
    sl1=swing.get("SL1"); sl2=swing.get("SL2")
    if sh1 and sh2:
        bt=max(sh1,sh2); bb=min(sh1,sh2)
        if bb <= price <= bt:
            alerts.append({"key":"Swing_SH","priority":4,"label":"🔴 Supply Zone",
                "level":bb,"direction":"PE","close_confirms":candle_bear,
                "msg":f"Supply {bb}–{bt} | {'✅' if candle_bear else '⏳'}"})
    if sl1 and sl2:
        bt=max(sl1,sl2); bb=min(sl1,sl2)
        if bb <= price <= bt:
            alerts.append({"key":"Swing_SL","priority":4,"label":"🟢 Demand Zone",
                "level":bt,"direction":"CE","close_confirms":candle_bull,
                "msg":f"Demand {bb}–{bt} | {'✅' if candle_bull else '⏳'}"})

    # Wick zone
    if wick:
        if abs(price-wick["top"]) <= prox:
            alerts.append({"key":"Wick_Top","priority":5,"label":"🕯️ Wick Top",
                "level":wick["top"],"direction":"PE","close_confirms":candle_bear,
                "msg":f"Wick Top={wick['top']} | {'✅' if candle_bear else '⏳'}"})
        if abs(price-wick["bot"]) <= prox:
            alerts.append({"key":"Wick_Bot","priority":5,"label":"🕯️ Wick Bottom",
                "level":wick["bot"],"direction":"CE","close_confirms":candle_bull,
                "msg":f"Wick Bot={wick['bot']} | {'✅' if candle_bull else '⏳'}"})

    # POB alone
    for p_lbl,p_lvl,p_dir in pob_near:
        if any(f"_{p_lbl}" in a["key"] for a in alerts if a["priority"]==1): continue
        cc=(p_dir=="PE" and candle_bear) or (p_dir=="CE" and candle_bull)
        alerts.append({"key":f"POB_{p_lbl.replace(' ','_')}","priority":3,
            "label":f"🎯 POB {p_lbl}","level":p_lvl,"direction":p_dir,
            "close_confirms":cc,"msg":f"POB {p_lbl}={p_lvl:.2f} | {'✅' if cc else '⏳'}"})

    # ── FIRE ALERTS ───────────────────────────────────────────────────────────
    inst_state["break_alerted"] = break_alerted
    for a in sorted(alerts, key=lambda x: x["priority"]):
        key = a["key"]; lvl = a["level"]
        prev_alert = alerted.get(key, 0)
        if prev_alert and abs(price - prev_alert) < 5: continue

        alerted[key] = price
        direction     = a["direction"]
        dist = round(price - lvl, 2)
        dist_str = f"{'+' if dist>=0 else ''}{dist}"

        if a["priority"]==1 and a["close_confirms"]: hdr="🔴🔴 STRONG SIGNAL 🔴🔴"
        elif a["priority"]==1: hdr="⚠️ CONFLUENCE WATCH"
        elif a["priority"]==2 and a["close_confirms"]: hdr=f"✅ {a['label']} CONFIRMED"
        elif a["priority"]==2: hdr=f"👀 {a['label']} — Watch"
        else: hdr = a["label"]

        msg = (f"<b>{hdr}</b> — {inst_name}\n"
               f"Time : {ts_str}\n"
               f"Price: {price:.2f}  ({dist_str} pts)\n"
               f"Level: {lvl:.2f}\n"
               f"Trend: {trend['label']}({trend['score']}) RSI:{trend['rsi']}\n"
               f"Signal: <b>{direction}</b>\n"
               f"━━━━━━━━━━━━━━━━━━━━\n{a['msg']}")
        tg.send(msg)
        L(f"  🔔 ALERT P{a['priority']} | {inst_name} | {a['label']} | "
          f"Lvl:{lvl} | {price} | {direction} | Conf:{a['close_confirms']}")

        # Auto trade for Nifty
        if (inst_name == "NIFTY" and
            a["priority"] in cfg.get("auto_triggers",[1,2]) and
            a["close_confirms"] and
            abs(trend["score"]) >= 4 and
            inst_state.get("auto_trades_taken",0) < cfg["auto_max_per_day"] and
            inst_state.get("active_trade") is None):

            # Direction must align with trend
            if (direction=="PE" and trend["score"] <= -3) or \
               (direction=="CE" and trend["score"] >= 3):
                trade = auto_enter_trade(fd, tg, price, direction, a["label"],
                                         cfg, inst_name)
                if trade:
                    inst_state["active_trade"]       = trade
                    inst_state["auto_trades_taken"]  = inst_state.get("auto_trades_taken",0) + 1

    inst_state["alerted"] = alerted
    return inst_state


def elapsed_from_open(ts, open_ts):
    return ts - open_ts


def auto_enter_trade(fd, tg, price, cp, signal, cfg, instrument):
    """Auto enter paper trade, returns trade dict or None."""
    gap = cfg["strike_gap"]
    atm = round(price / gap) * gap
    sym = f"NSE:NIFTY{cfg['nifty_expiry']}{atm}{cp}"
    ltps = fd.get_ltp_batch([sym])
    ltp  = ltps.get(sym, 0)

    if ltp < cfg["auto_min_premium"]:
        L(f"  [AUTO] Skip — {sym} LTP=₹{ltp} < min ₹{cfg['auto_min_premium']}")
        tg.send(f"⚠️ Auto trade skipped\n{sym}\nPremium ₹{ltp:.0f} too low")
        return None

    sl  = round(ltp - cfg["auto_sl"], 2)
    units = cfg["auto_lots"] * 25
    trade = {
        "sym": sym, "cp": cp, "buy": ltp, "sl": sl,
        "cur_sl": sl, "tsl": cfg["auto_tsl"],
        "peak": ltp, "lots": cfg["auto_lots"], "units": units,
        "signal": signal, "active": True, "entry_time": str(datetime.now())
    }
    L(f"  🤖 AUTO TRADE: {sym} @ ₹{ltp} | SL:₹{sl} | {cfg['auto_lots']} lots")
    tg.send(
        f"🤖 <b>AUTO Trade — {sym}</b>\n"
        f"Signal : {signal}\n"
        f"Entry  : ₹{ltp:.0f}\n"
        f"SL     : ₹{sl:.0f} (-₹{ltp-sl:.0f})\n"
        f"TSL    : ₹{cfg['auto_tsl']:.0f} staircase\n"
        f"Lots   : {cfg['auto_lots']} ({units} units)\n"
        f"Max risk: ₹{(ltp-sl)*units:.0f}")
    return trade


def check_active_trade(inst_state, fd, tg, cfg):
    """Check and update active paper trade."""
    trade = inst_state.get("active_trade")
    if not trade or not trade.get("active"): return inst_state

    sym  = trade["sym"]
    ltps = fd.get_ltp_batch([sym])
    ltp  = ltps.get(sym, 0)
    if ltp <= 0: return inst_state

    units = trade["units"]

    if ltp > trade["peak"]:
        trade["peak"] = ltp
        steps  = int((ltp - trade["buy"]) / trade["tsl"])
        new_sl = trade["buy"] + (steps-1)*trade["tsl"] if steps > 0 else trade["sl"]
        if new_sl > trade["cur_sl"]:
            old_sl = trade["cur_sl"]; trade["cur_sl"] = new_sl
            tg.send(
                f"⚡ <b>TSL → ₹{new_sl:.0f}</b> — {sym}\n"
                f"LTP:₹{ltp:.0f} | Peak:₹{ltp:.0f}\n"
                f"SL: ₹{old_sl:.0f} → ₹{new_sl:.0f}\n"
                f"Locked: +₹{(new_sl-trade['buy'])*units:.0f}")
            L(f"  TSL: {sym} SL ₹{old_sl}→₹{new_sl}")

    if ltp <= trade["cur_sl"]:
        pnl     = ltp - trade["buy"]
        pnl_tot = pnl * units
        e = "✅" if pnl >= 0 else "⛔"
        tg.send(
            f"{e} <b>EXIT — {sym}</b>\n"
            f"Entry:₹{trade['buy']:.0f} → ₹{ltp:.0f}\n"
            f"Per unit: {'+'if pnl>=0 else ''}₹{pnl:.0f}\n"
            f"<b>{trade['lots']} lots: {'+'if pnl_tot>=0 else ''}₹{pnl_tot:.0f}</b>\n"
            f"Peak: ₹{trade['peak']:.0f}")
        L(f"  EXIT {sym}: ₹{trade['buy']}→₹{ltp} | P&L ₹{pnl_tot:.0f}")
        trade["active"] = False
        inst_state["active_trade"] = None

    inst_state["active_trade"] = trade
    return inst_state


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════
def main():
    L("=" * 60)
    L(f"  Alert run started: {datetime.now()}")
    L("=" * 60)

    token = CONFIG["fyers_token"]
    if not token:
        L("❌ No FYERS_TOKEN in environment"); return

    tg   = Telegram(CONFIG["telegram_token"], CONFIG["telegram_chat_id"])
    fd   = FyersData(CONFIG["client_id"], token)
    gist = GistState(CONFIG["gist_id"], CONFIG["github_token"])

    # Load state
    state = gist.load()
    L(f"  State loaded: {list(state.keys())}")

    # Fetch data
    L("  Fetching Nifty...")
    nifty_candles = fd.fetch(CONFIG["nifty_symbol"])
    nifty_daily   = fd.fetch_daily(CONFIG["nifty_symbol"])

    L("  Fetching Crude...")
    crude_candles = fd.fetch(CONFIG["crude_symbol"])
    crude_daily   = fd.fetch_daily(CONFIG["crude_symbol"])

    # Process Nifty
    if in_session(datetime.now(), CONFIG["nifty_start"], CONFIG["nifty_end"]):
        nifty_state = state.get("nifty", {})
        nifty_state = process_instrument(
            nifty_candles, nifty_daily, CONFIG, "NIFTY",
            nifty_state, tg, fd, state)
        state["nifty"] = nifty_state
    else:
        L("  Nifty: outside session")

    # Process Crude
    if in_session(datetime.now(), CONFIG["crude_start"], CONFIG["crude_end"]):
        crude_state = state.get("crude", {})
        crude_state = process_instrument(
            crude_candles, crude_daily, CONFIG, "CRUDE",
            crude_state, tg, fd, state)
        state["crude"] = crude_state
    else:
        L("  Crude: outside session")

    # Save state
    gist.save(state)
    L("  Run complete ✅")


if __name__ == "__main__":
    main()
