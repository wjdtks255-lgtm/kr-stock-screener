import os
import json
import requests
from datetime import datetime, timedelta
import FinanceDataReader as fdr

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
STATE_FILE = "active_positions.json"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

def load_positions():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_positions(positions):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=4)

def monitor_positions(start_date):
    positions = load_positions()
    if not positions:
        return

    updated_positions = {}
    for ticker, pos in positions.items():
        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) < 2:
                updated_positions[ticker] = pos
                continue
                
            latest = df.iloc[-1]
            high_price = latest['High']
            low_price = latest['Low']
            
            name = pos['name']
            tp1 = pos['target_1']
            tp2 = pos['target_2']
            sl = pos['stop_loss']
            
            if pos.get('status') in ['STOP', 'TP2']:
                continue

            if low_price <= sl:
                msg = f"🛡️ <b>[손절가(SL) 도달]</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n❌ 이탈 가격: <code>{int(sl):,}원 이하</code>"
                send_telegram(msg)
                pos['status'] = 'STOP'
                updated_positions[ticker] = pos
                continue 
                
            if high_price >= tp2:
                msg = f"🎯 <b>[2차 목표가(TP2) 달성]</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n🔥 달성 가격: <code>{int(tp2):,}원 돌파!</code>"
                send_telegram(msg)
                pos['status'] = 'TP2'
                updated_positions[ticker] = pos
                continue 
                
            if high_price >= tp1 and not pos.get('tp1_hit', False):
                msg = f"🎯 <b>[1차 목표가(TP1) 달성]</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n✨ 달성 가격: <code>{int(tp1):,}원 도달!</code>"
                send_telegram(msg)
                pos['tp1_hit'] = True 
                
            updated_positions[ticker] = pos
        except Exception:
            updated_positions[ticker] = pos

    save_positions(updated_positions)

def run_screener():
    now = datetime.now()
    start_date = (now - timedelta(days=60)).strftime('%Y-%m-%d')
    
    monitor_positions(start_date)

    print("=== [주도주 실전형 긴급완화버전] 스크리닝 시작 ===")
    try:
        df_krx = fdr.StockListing('KRX')
        top_400 = df_krx.sort_values(by='Amount', ascending=False).head(400)
    except Exception as e:
        print(f"KRX 종목 리스트 불러오기 실패: {e}")
        return

    closing_signals = []  
    morning_signals = []  
    new_positions = load_positions() 

    for _, row in top_400.iterrows():
        ticker = row['Code']
        name = row['Name']
        
        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) < 20:
                continue
            
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            df_30 = df.iloc[-30:]
            
            close = latest['Close'] 
            open_p = latest['Open']
            high = latest['High']
            volume = latest['Volume']
            amount = latest['Amount']
            
            # 거래대금 기준 5천만 원 이상 (오타 수정완료)
            if amount < 50_000_000:
                continue

            if close <= open_p:
                continue
            if (close - prev['Close']) / prev['Close'] < 0.015:
                continue
                
            high_30 = df_30['High'].max()
            if close < high_30 * 0.90:
                continue 
                
            stop_loss = round(close * 0.94, -1)
            raw_target_1 = high_30 if high_30 > close * 1.02 else close * 1.03
            target_1 = round(raw_target_1, -1)
            target_2 = round(target_1 * 1.05, -1)
            
            chart_link = f"https://finance.naver.com/item/main.naver?code={ticker}"
            
            closing_msg = (
                f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                f"💰 <b>진입가(종가):</b> <code>{int(close):,}원</code>\n"
                f"🛡️ <b>손절가(SL):</b> <code>{int(stop_loss):,}원</code>\n"
                f"🎯 <b>1차 목표가(TP1):</b> <code>{int(target_1):,}원</code>\n"
                f"🎯 <b>2차 목표가(TP2):</b> <code>{int(target_2):,}원</code>\n"
                f"📈 <a href='{chart_link}'>네이버 차트</a>"
            )
            closing_signals.append(closing_msg)
            
            morning_msg = (
                f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                f"💰 <b>기준가(오늘종가):</b> <code>{int(close):,}원</code>\n"
                f"🎯 <b>1차 목표가(TP1):</b> <code>{int(target_1):,}원</code>\n"
                f"🎯 <b>2차 목표가(TP2):</b> <code>{int(target_2):,}원</code>\n"
                f"💡 <i>내일 아침 시초가 갭 공략</i>"
            )
            morning_signals.append(morning_msg)
            
            new_positions[ticker] = {
                "name": name,
                "target_1": int(target_1),
                "target_2": int(target_2),
                "stop_loss": int(stop_loss),
                "tp1_hit": False,
                "status": "ACTIVE"
            }
        except Exception:
            continue

    if closing_signals:
        closing_text = "🚨 <b>[1] 주도주 실전형 저항돌파 종가베팅</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(closing_signals[:5])
        send_telegram(closing_text)
        
        morning_text = "🌅 <b>[2] 내일 아침 시초가 매매 (오전 갭 공략)</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(morning_signals[:5])
        send_telegram(morning_text)
    else:
        send_telegram("⚠️ 오늘 완화된 조건에도 부합하는 종목이 없습니다.")

    save_positions(new_positions)
    send_telegram("🏁 <b>[국장 자동화] 스크리닝 완료!</b>")

if __name__ == "__main__":
    run_screener()
