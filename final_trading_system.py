import requests
import time
from datetime import datetime, timedelta
import pytz

# ===== TIMEZONE =====
IST = pytz.timezone('Asia/Kolkata')


# ===== CONFIG =====
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiJFVTkzNDciLCJqdGkiOiI2YTAzZWE1NzE1MDY2NDBmYzFmM2E0YjMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzc4NjQxNDk1LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3Nzg3MDk2MDB9.eIPAqIyszhIlpHdf6fGKt55Razan4GoLLrIAPSUkEXY"
BOT_TOKEN = "8726435378:AAEhAviD-pwjF-IY-wYcUVlPBKYZIjpBXB4"
CHAT_ID = "-1003724403519"

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

# ===== GLOBAL =====
prev_support = 0
prev_resistance = 0
prev_total_pe = 0
prev_total_ce = 0
mode = "TREND"
prev_data = {}
fixed_support = None
fixed_resistance = None
prev_price = 0

last_heartbeat = None
last_sr_update = None

active_trade = None
reentry_ready = False
last_direction = None

market_started = False
market_closed_sent = False

# ===== TELEGRAM =====
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except:
        print("Telegram error")

# ===== EXPIRY =====
def get_expiry(chain):
    expiries = list(set([d['expiry'] for d in chain]))
    expiries.sort()
    return expiries[0]   # nearest expiry

# ===== SAFE API =====
def safe_request(url, params=None):
    try:
        res = requests.get(url, headers=HEADERS, params=params)
        data = res.json()
        if "data" not in data:
            return None
        return data["data"]
    except:
        return None

# ===== LTP =====
def get_ltp():
    url = "https://api.upstox.com/v2/market-quote/ltp"
    params = {"instrument_key": "NSE_INDEX|Nifty 50"}
    data = safe_request(url, params)
    if not data:
        return None
    return list(data.values())[0]['last_price']

# ===== OPTION CHAIN =====
def get_chain():
    url = "https://api.upstox.com/v2/option/chain"
    params = {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-05-19"
    }
    return safe_request(url, params) or []

# ===== ATM =====
def get_atm(price):
    return int(round(price / 50) * 50)

# ===== DATA =====
def get_data(chain, atm):
    global prev_data
    strikes = list(range(atm-150, atm+150, 50))
    data = []

    for item in chain:
        if item['strike_price'] in strikes:
            strike = item['strike_price']

            ce = item['call_options']['market_data']['oi']
            pe = item['put_options']['market_data']['oi']

            prev_ce = prev_data.get(strike, {}).get("ce", ce)
            prev_pe = prev_data.get(strike, {}).get("pe", pe)

            data.append({
                "strike": strike,
                "ce": ce,
                "pe": pe,
                "ce_chg": ce - prev_ce,
                "pe_chg": pe - prev_pe,
                "ce_price": item['call_options']['market_data'].get('ltp', 0),
                "pe_price": item['put_options']['market_data'].get('ltp', 0)
            })

            prev_data[strike] = {"ce": ce, "pe": pe}

    return data

# ===== SR =====
def get_sr(data):
    support = max(data, key=lambda x: x['pe'])['strike']
    resistance = max(data, key=lambda x: x['ce'])['strike']
    return support, resistance

def detect_mode(support, resistance, prev_support, prev_resistance):
    if abs(support - prev_support) < 20 and abs(resistance - prev_resistance) < 20:
        return "RANGE"
    else:
        return "TREND"

         

# ===== SIGNAL =====
def smart_oi_signal(data, atm):
    top_pe = sorted(data, key=lambda x: x['pe_chg'], reverse=True)[:3]
    top_ce = sorted(data, key=lambda x: x['ce_chg'], reverse=True)[:3]

    pe_score = sum(d['pe_chg'] for d in top_pe)
    ce_score = sum(d['ce_chg'] for d in top_ce)

    pe_near = sum(1 for d in top_pe if abs(d['strike'] - atm) <= 100)
    ce_near = sum(1 for d in top_ce if abs(d['strike'] - atm) <= 100)

    return pe_score, ce_score, pe_near, ce_near


def weighted(data, atm):
    w_bull = sum(2 for d in data if d['strike'] < atm and d['pe_chg'] > 0)
    w_bear = sum(2 for d in data if d['strike'] > atm and d['ce_chg'] > 0)
    return w_bull, w_bear

def strength(bull, bear, w_bull, w_bear):
    if w_bull >= 6:
        return "SUPER STRONG BULLISH 🔥"
    if w_bear >= 6:
        return "SUPER STRONG BEARISH 🔥"
    if bull >= 5:
        return "STRONG BULLISH"
    if bear >= 5:
        return "STRONG BEARISH"
    return "WEAK"

def confidence(data):
    score = sum(1 for d in data if d['pe'] > d['ce'])
    return round((score / len(data)) * 100, 2)

def best_strike(data, signal, atm):
    best = None
    max_score = -999999

    for d in data:
        distance = abs(d['strike'] - atm)

        if distance > 100:
            continue   # far strike skip

        if signal == "BUY CALL":
            score = d['ce_chg'] - distance * 5
        else:
            score = d['pe_chg'] - distance * 5

        if score > max_score:
            max_score = score
            best = d['strike']

    return best if best else atm

def get_option_price(data, strike, signal):
    for d in data:
        if d['strike'] == strike:
            return d['ce_price'] if signal == "BUY CALL" else d['pe_price']
    return 0

def sl_target(price):
    sl = price - 10
    t1 = price * 1.10   # +10%
    t2 = price * 1.20   # +20%
    t3 = price * 1.30   # +30%
    t4 = price * 1.40   # +40%
    return sl, t1, t2, t3, t4

    
# ===== MAIN =====
def run():
    global fixed_support, fixed_resistance, prev_price
    global last_heartbeat, last_sr_update
    global active_trade, reentry_ready, last_direction
    global market_started, market_closed_sent

    print("🚀 SYSTEM STARTED")
    send_telegram("✅ SYSTEM STARTED")

    while True:
        try:
            now = datetime.now(IST)
            current_time = now.strftime("%H:%M")

            # HEARTBEAT
            if now.minute // 10 != last_heartbeat:
                send_telegram(f"💓 SYSTEM RUNNING {current_time}")
                last_heartbeat = now.minute // 10
                
            # MARKET START
            if current_time >= "09:15" and not market_started:
                send_telegram("🚀 Market Started")
                market_started = True
                market_closed_sent = False

            # BEFORE MARKET
            if current_time < "09:15":
                time.sleep(30)
                continue

            # MARKET CLOSED
            if current_time > "15:30":
                if not market_closed_sent:
                    print("🛑 Market Closed")
                    send_telegram("🛑 Market Closed")
                    market_closed_sent = True
                time.sleep(60)
                continue

            # SR RESET
            if current_time in ["10:20", "13:45"]:
                if last_sr_update != current_time:
                    send_telegram(f"🔄 SR RESET {current_time}")
                    fixed_support = None
                    fixed_resistance = None
                    last_sr_update = current_time

            ltp = get_ltp()
            ema_fast = (ltp + prev_price) / 2
            if not ltp:
                continue

            atm = get_atm(ltp)
            chain = get_chain()

            if not chain:
                continue

            data = get_data(chain, atm)
            support, resistance = get_sr(data)

            global prev_support, prev_resistance, mode
            mode = detect_mode(support, resistance, prev_support, prev_resistance)

            if fixed_support is not None and fixed_resistance is not None:
                if ltp > fixed_resistance + 10 or ltp < fixed_support - 10:
                    mode = "TREND"

            prev_support = support
            prev_resistance = resistance

            if fixed_support is None:
                fixed_support = support
                fixed_resistance = resistance


            # ===== EXIT =====
            if active_trade:

                strike = active_trade['strike']
                signal = active_trade['signal']
                entry = active_trade['entry']
                price = get_option_price(data, strike, signal)
            
                sl = active_trade['sl']
                t1 = active_trade['t1']
                t2 = active_trade['t2']
                t3 = active_trade['t3']
                t4 = active_trade['t4']
                stage = active_trade.get('stage', 0)   # 🔥 safe

               # 🔹 T1 hit → SL to cost
                if stage == 0 and price >= t1:
                    active_trade['sl'] = entry
                    active_trade['stage'] = 1
                    send_telegram(f"🎯 T1 HIT → SL moved to COST")
     
               # 🔹 T2 hit → SL to T1
                elif stage == 1 and price >= t2:
                    active_trade['sl'] = t1
                    active_trade['stage'] = 2
                    send_telegram(f"🎯 T2 HIT → SL moved to T1")
              
                # 🔹 T3 hit → SL to T2
                elif stage == 2 and price >= t3:
                    active_trade['sl'] = t2
                    active_trade['stage'] = 3
                    send_telegram(f"🎯 T3 HIT → SL moved to T2")
          
                # 🔹 T4 hit → full exit

                elif stage == 3 and price >= t4:
                    send_telegram(f"🚀 T4 HIT FULL PROFIT {strike} @ {price}")
                    active_trade = None
                    reentry_ready = True
                    continue

                # 🔹 SL hit
                elif price <= active_trade['sl']:
                    send_telegram(f"❌ SL HIT {strike} @ {price}")
                    active_trade = None
                    reentry_ready = True
                    continue               

            if current_time < "09:45":
                continue

            pe_score, ce_score, pe_near, ce_near = smart_oi_signal(data, atm)
            if pe_score > abs(ce_score) and ce_score < 0 and pe_near >= 2:
                st = "🔥 BULLISH"
            elif ce_score > abs(pe_score) and pe_score < 0 and ce_near >= 2:
                st = "🔻 BEARISH"
            else:
                st = "⚪ WEAK"

            conf = confidence(data)

            print("\n" + "="*40)

            print(f"⏰ TIME: {current_time}")
            print(f"📊 LTP: {ltp}")
            print(f"📊 Chain: {len(chain)} | Expiry: {get_expiry(chain)}")

            print(f"\n📈 LIVE SR → {support} | {resistance}")
            print(f"🔒 FIXED SR → {fixed_support} | {fixed_resistance}")

            print(f"\n📊 Strength: {st} | Confidence: {conf}%")
            print(f"🧠 Mode: {mode}")
        

            print("="*40)

            if "WEAK" in st and mode == "TREND":
                continue
           
            move = abs(ltp - prev_price)

            if move < 1.5:
                continue

            if conf < 40:
                continue


            signal = ""
            signal_type = ""
            # ===== COMBO LOGIC (TREND + RANGE) =====
            buffer = 35
            total_ce = sum(d['ce_chg'] for d in data)
            total_pe = sum(d['pe_chg'] for d in data)

            # ===== MARKET TYPE (OI BUILDUP) =====
            market_type = "UNKNOWN"
            if prev_price != 0:
                 if ltp > prev_price and total_pe > prev_total_pe:
                     market_type = "LONG BUILDUP"
                 elif ltp < prev_price and total_ce > prev_total_ce:
                     market_type = "SHORT BUILDUP"
            # store previous OI
            prev_total_pe = total_pe
            prev_total_ce = total_ce




            oi_bias = "NEUTRAL"
            if total_pe > total_ce:
                oi_bias = "BULLISH"
            elif total_ce > total_pe:
                oi_bias = "BEARISH"
            print(f"📊 OI CHANGE → CE: {int(total_ce)} | PE: {int(total_pe)} | Bias: {oi_bias}")

            # 🔵 TREND BREAKOUT
            if "BULLISH" in st and ltp > fixed_resistance + 10 and total_pe > total_ce:
                signal = "BUY CALL"
                signal_type = "TREND"

            elif "BEARISH" in st and ltp < fixed_support - 10 and total_ce > total_pe:
                signal = "BUY PUT"
                signal_type = "TREND"
            
            # 🟢 TREND CONTINUATION
            elif "BEARISH" in st and ltp < resistance and total_ce > total_pe and ltp < ema_fast and ltp < prev_price - 5:
                signal = "BUY PUT"
                signal_type = "TREND CONT"

            elif "BULLISH" in st and ltp > support and total_pe > total_ce and ltp > ema_fast and ltp > prev_price + 5:
                signal = "BUY CALL"
                signal_type = "TREND CONT"

            # 🔴 REVERSAL CATCH
            elif (
                "BULLISH" in st
                and ltp > prev_price
                and total_pe > total_ce
                and abs(ltp - support) < 50
                and abs(ltp - prev_price) > 2
                ):
                signal = "BUY CALL"
                signal_type = "REVERSAL"

            elif (
                "BEARISH" in st
                and ltp < prev_price
                and total_ce > total_pe
                and abs(ltp - resistance) < 50
                and abs(ltp - prev_price) > 2
                ):
                signal = "BUY PUT"
                signal_type = "REVERSAL"
           
            # 🟡 RANGE REVERSAL
            elif fixed_support <= ltp <= fixed_resistance:
                # Support bounce
                if abs(ltp - support) <= buffer and total_pe > total_ce and ltp > ema_fast:
                    signal = "BUY CALL"
                    signal_type = "RANGE"

                # Resistance rejection
                elif abs(ltp - resistance) <= buffer and total_ce > total_pe and ltp < ema_fast:
                    signal = "BUY PUT"
                    signal_type = "RANGE"

            # ✅ 🔥 OI FILTER
            if signal_type in ["TREND", "TREND CONT",]:
                if signal == "BUY CALL" and market_type != "LONG BUILDUP":
                    continue
                if signal == "BUY PUT" and market_type != "SHORT BUILDUP":
                    continue

        
            # ===== ENTRY / RE-ENTRY =====
            if signal != "" and active_trade is None:

                # normal entry
                if not reentry_ready:
                    pass

                # re-entry
                elif last_direction == signal:
                    print("🔁 RE-ENTRY SIGNAL")

                else:
                    continue

                strike = best_strike(data, signal, atm)
                price = get_option_price(data, strike, signal)

                if price == 0:
                    continue

                sl, t1, t2, t3, t4 = sl_target(price)

                active_trade = {
                    "strike": strike,
                    "signal": signal,
                    "entry": price,
                    "sl": sl,
                    "t1": t1,
                    "t2": t2,
                    "t3": t3,
                    "t4": t4,
                    "stage": 0   # tracking stage
                }

                last_direction = signal

                send_telegram(f"""
🔥 NIFTY {'RE-ENTRY' if reentry_ready else 'ENTRY'}
Mode: {signal_type}
{signal}

OI:
CE: {int(total_ce)}
PE: {int(total_pe)}
Bias: {oi_bias}

Strike: {strike}
Price: {price}
SL: {sl}
T1: {round(t1,1)}, T2: {round(t2,1)}, T3: {round(t3,1)}, T4: {round(t4,1)}

Conf: {conf}%
Time: {current_time}
""")

            reentry_ready = False
            prev_price = ltp

        except Exception as e:
            print("ERROR:", e)
            time.sleep(1)

            time.sleep(1)

run()