# KOREA STOCK HUNTER SCREENER & SIGNAL BOT (8 SLOTS ENGINE)
import os, json, time, requests
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import FinanceDataReader as fdr

V = "20.1.6"
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

def main():
    print("====================================")
    print(f" KOREA STOCK HUNTER SCREENER V{V}")
    print("====================================")

    state = get_state()
    positions = state.get("positions", [])

    # 실행 시 텔레그램 상태 알림 전송 (연동 테스트 겸용)
    tg(f"🤖 <b>[국장 스크리너] 스캔 시작</b>\n시간: {ts()}\n현재 포지션: {len(positions)} / {MAX_SLOTS} 개")

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
            hit = pos.setdefault("hit", [])

            if current_price <= sl:
                tg(f"🛡 <b>[국장 시그널] 손절가 도달</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n종목: <b>{pos.get('name')} ({code})</b>\n청산가: <code>{m(current_price)}</code>")
                continue

            if current_price >= tp1 and "TP1" not in hit:
                hit.append("TP1")
                pos["sl"] = pos["entry"]
                tg(f"🎯 <b>[국장 시그널] 1차 목표가 도달</b>\n종목: <b>{pos.get('name')} ({code})</b>\n본전 스탑으로 손절가 상향")

            updated_positions.append(pos)
        except Exception as e:
            print(f"[ERROR] 종목 {code} 체크 중 오류:", e)
            updated_positions.append(pos)

    # 2. 빈 슬롯 존재 시 신규 종목 스캔
    active_codes = [p.get("code") for p in updated_positions]
    
    if len(updated_positions) < MAX_SLOTS:
        print(f"[SCAN] 빈 슬롯 존재 ({len(updated_positions)}/{MAX_SLOTS}). 신규 종목 검색 시작")
        
        try:
            krx = fdr.StockListing('KRX')
            candidates = krx.dropna(subset=['Code', 'Name']).head(300)
            
            scanned_count = 0
            for _, row in candidates.iterrows():
                if len(updated_positions) >= MAX_SLOTS:
                    break
                
                code = str(row['Code'])
                name = str(row['Name'])
                
                if code in active_codes:
                    continue
                
                df = fdr.DataReader(code)
                if len(df) < 60:
                    continue
                
                close = float(df['Close'].iloc[-1])
                ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
                ma60 = float(df['Close'].rolling(60).mean().iloc[-1])
                volume = float(df['Volume'].iloc[-1])
                vol_ma20 = float(df['Volume'].rolling(20).mean().iloc[-1])

                # 완화된 조건: 20일선 상회 및 평균 거래량 이상
                if close > ma20 and volume > vol_ma20:
                    entry = close
                    sl = entry * 0.95
                    tp1 = entry * 1.05
                    tp2 = entry * 1.10

                    new_pos = {
                        "code": code,
                        "name": name,
                        "entry": entry,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "hit": [],
                        "time": ts()
                    }
                    
                    updated_positions.append(new_pos)
                    active_codes.append(code)
                    scanned_count += 1

                    tg(f"""🚨 <b>[국장 시그널] 신규 추천 종목 포착</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>슬롯 현황: [ {len(updated_positions)} / {MAX_SLOTS} ]</b>
🪙 <b>종목명:</b> <b>{name} ({code})</b>
💰 <b>진입가:</b> <code>{m(entry)}</code>
🛡 <b>손절가 (SL):</b> <code>{m(sl)}</code> (-5.0%)
🎯 <b>1차 목표가 (TP1):</b> <code>{m(tp1)}</code> (+5.0%)
🎯 <b>2차 목표가 (TP2):</b> <code>{m(tp2)}</code> (+10.0%)
━━━━━━━━━━━━━━━━━━━━━━━""")
        except Exception as e:
            print("[SCAN ERROR] 종목 스캔 중 오류 발생:", e)
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
