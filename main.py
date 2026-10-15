# KOREA STOCK HUNTER SCREENER & SIGNAL BOT (DYNAMIC VOLATILITY & TP3 ENGINE)
import os, json, time, requests
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import FinanceDataReader as fdr

V = "20.1.8"
STATE = "active_positions.json"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))

MAX_SLOTS = 8

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, (np.bool_, bool)): return bool(obj)
        return super(NpEncoder, self).default(obj)

def now():
    return datetime.now(KST)

def ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")

def m(x):
    try:
        val = float(x)
        if np.isnan(val) or np.isinf(val): return "0원"
        return f"{int(val):,}원"
    except:
        return "0원"

def save(p, x):
    q = p + ".tmp"
    with open(q, "w", encoding="utf8") as f:
        json.dump(x, f, ensure_ascii=False, indent=2, cls=NpEncoder)
    os.replace(q, p)

def load(p, d):
    try:
        with open(p, encoding="utf8") as f: return json.load(f)
    except: return d

def tg(x):
    if not TOKEN or not CHAT: 
        print("[TG CONFIG ERROR] Token or Chat ID is missing.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": x, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        print("[TELEGRAM]", "SENT" if r.ok else f"FAIL: {r.text}")
        return r.ok
    except Exception as e:
        print("[TG ERROR]", e)
        return False

def get_state():
    s = load(STATE, {"positions": [], "sent": []})
    if not isinstance(s, dict):
        s = {"positions": [], "sent": []}
    s.setdefault("positions", [])
    s.setdefault("sent", [])
    return s

def calculate_atr(df, period=14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def main():
    print("====================================")
    print(f" KOREA STOCK HUNTER SCREENER V{V}")
    print("====================================")

    state = get_state()
    positions = state.get("positions", [])

    print(f"[STATUS] 현재 유지 중인 포지션: {len(positions)} / {MAX_SLOTS}")

    # 1. 기존 유지 중인 포지션 청산 여부 및 TP/SL 체크
    updated_positions = []
    for pos in positions:
        code = pos.get("code")
        try:
            df = fdr.DataReader(code)
            if df.empty:
                updated_positions.append(pos)
                continue
            
            current_price = float(df['Close'].iloc[-1])
            sl = pos.get("sl", 0)
            tp1 = pos.get("tp1", 0)
            tp2 = pos.get("tp2", 0)
            tp3 = pos.get("tp3", 0)
            hit = pos.setdefault("hit", [])

            if current_price <= sl:
                tg(f"🛡 <b>[국장 시그널] 손절가 도달</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n종목: <b>{pos.get('name')} ({code})</b>\n청산가: <code>{m(current_price)}</code>")
                continue

            if current_price >= tp3:
                if "TP1" not in hit: hit.append("TP1")
                if "TP2" not in hit: hit.append("TP2")
                if "TP3" not in hit: hit.append("TP3")
                tg(f"🏆 <b>[국장 시그널] 3차 목표가 도달 (완료)</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n종목: <b>{pos.get('name')} ({code})</b>\n최종 청산가: <code>{m(current_price)}</code>")
                continue

            if current_price >= tp2 and "TP2" not in hit:
                if "TP1" not in hit: hit.append("TP1")
                hit.append("TP2")
                pos["sl"] = tp1
                tg(f"🎯 <b>[국장 시그널] 2차 목표가 도달</b>\n종목: <b>{pos.get('name')} ({code})</b>\n손절가를 1차 목표가로 상향")
            elif current_price >= tp1 and "TP1" not in hit:
                hit.append("TP1")
                pos["sl"] = pos["entry"]
                tg(f"🎯 <b>[국장 시그널] 1차 목표가 도달</b>\n종목: <b>{pos.get('name')} ({code})</b>\n본전 스탑으로 손절가 상향")

            updated_positions.append(pos)
        except Exception as e:
            print(f"[ERROR] 종목 {code} 체크 중 오류:", e)
            updated_positions.append(pos)

    # 2. 8개 슬롯 중 빈 자리가 있을 경우 동적 변동성 기반 신규 종목 스캔
    active_codes = [p.get("code") for p in updated_positions]
    
    if len(updated_positions) < MAX_SLOTS:
        print(f"[SCAN] 빈 슬롯 존재 ({len(updated_positions)}/{MAX_SLOTS}). 동적 변동성 스캔 시작")
        
        try:
            krx = fdr.StockListing('KRX')
            candidates = krx.dropna(subset=['Code', 'Name']).head(300)
            
            for _, row in candidates.iterrows():
                if len(updated_positions) >= MAX_SLOTS:
                    break
                
                code = str(row['Code'])
                name = str(row['Name'])
                
                if code in active_codes:
                    continue
                
                df = fdr.DataReader(code)
                if len(df) < 30:
                    continue
                
                close = float(df['Close'].iloc[-1])
                ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
                volume = float(df['Volume'].iloc[-1])
                vol_ma20 = float(df['Volume'].rolling(20).mean().iloc[-1])

                if close > ma20 and volume > vol_ma20:
                    # 종목별 변동성(ATR) 계산
                    atr_series = calculate_atr(df)
                    atr_val = float(atr_series.iloc[-1]) if not atr_series.empty and not np.isnan(atr_series.iloc[-1]) else close * 0.04
                    
                    # 변동성에 따른 동적 비율 산정 (최소 3%, 최대 8% 안전장치)
                    volatility_pct = max(0.03, min(0.08, atr_val / close))
                    
                    entry = close
                    sl = entry * (1 - volatility_pct)
                    tp1 = entry * (1 + volatility_pct * 1.2)
                    tp2 = entry * (1 + volatility_pct * 2.2)
                    tp3 = entry * (1 + volatility_pct * 3.2)

                    sl_pct_str = f"-{volatility_pct*100:.1f}%"
                    tp1_pct_str = f"+{volatility_pct*1.2*100:.1f}%"
                    tp2_pct_str = f"+{volatility_pct*2.2*100:.1f}%"
                    tp3_pct_str = f"+{volatility_pct*3.2*100:.1f}%"

                    new_pos = {
                        "code": code,
                        "name": name,
                        "entry": entry,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "hit": [],
                        "time": ts()
                    }
                    
                    updated_positions.append(new_pos)
                    active_codes.append(code)

                    tg(f"""🚨 <b>[국장 시그널] 맞춤형 변동성 종목 포착</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>슬롯 현황: [ {len(updated_positions)} / {MAX_SLOTS} ]</b>
🪙 <b>종목명:</b> <b>{name} ({code})</b>
💰 <b>진입가:</b> <code>{m(entry)}</code>
🛡 <b>손절가 (SL):</b> <code>{m(sl)}</code> ({sl_pct_str})
🎯 <b>1차 목표가 (TP1):</b> <code>{m(tp1)}</code> ({tp1_pct_str})
🎯 <b>2차 목표가 (TP2):</b> <code>{m(tp2)}</code> ({tp2_pct_str})
🎯 <b>3차 목표가 (TP3):</b> <code>{m(tp3)}</code> ({tp3_pct_str})
━━━━━━━━━━━━━━━━━━━━━━━""")
        except Exception as e:
            print("[SCAN ERROR] 종목 스캔 중 오류:", e)
    else:
        print("[SCAN SKIP] 8개 슬롯이 모두 채워져 있습니다.")

    state["positions"] = updated_positions
    save(STATE, state)
    print(f"[DONE] 최신 상태가 {STATE} 파일에 저장되었습니다.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", repr(e))
        raise
