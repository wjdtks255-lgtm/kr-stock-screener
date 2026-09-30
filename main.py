import os
import json
import requests
from datetime import datetime, timedelta
import FinanceDataReader as fdr
import pytz

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
                data = json.load(f)
                if isinstance(data, dict):
                    return data
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
        if not isinstance(pos, dict):
            continue

        status = pos.get('status', 'ACTIVE')
        if status in ['STOP', 'TP2']:
            continue

        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) < 2:
                updated_positions[ticker] = pos
                continue
                
            latest = df.iloc[-1]
            high_price = latest['High']
            low_price = latest['Low']
            
            name = pos.get('name', ticker)
            tp1 = pos.get('target_1', 0)
            tp2 = pos.get('target_2', 0)
            sl = pos.get('stop_loss', 0)

            if low_price <= sl:
                msg = f"🔴 <b>STOP LOSS</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n❌ 이탈 가격: <code>{int(sl):,}원 이하</code>"
                send_telegram(msg)
                pos['status'] = 'STOP'
                continue 
                
            if high_price >= tp2:
                msg = f"🎯 <b>2차 목표가(TP2) 달성</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n🔥 달성 가격: <code>{int(tp2):,}원 돌파!</code>"
                send_telegram(msg)
                pos['status'] = 'TP2'
                continue 
                
            if high_price >= tp1 and not pos.get('tp1_hit', False):
                msg = f"🎯 <b>1차 목표가(TP1) 달성</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n✨ 달성 가격: <code>{int(tp1):,}원 도달!</code>"
                send_telegram(msg)
                pos['tp1_hit'] = True 
                
            updated_positions[ticker] = pos
        except Exception:
            updated_positions[ticker] = pos

    save_positions(updated_positions)

def run_screener():
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.now(kst)
    current_hour = now_kst.hour
    current_minute = now_kst.minute
    
    start_date = (now_kst - timedelta(days=60)).strftime('%Y-%m-%d')
    
    # 1. 기존 포지션 손절/목표가 모니터링 먼저 실행
    monitor_positions(start_date)

    print(f"=== [국장 스크리닝 실행] 현재 시각: {now_kst.strftime('%H:%M')} ===")

    # 2. 시간대별 분기 처리
    # [A] 아침 시초가 공략 시간대 (예: 09:00 ~ 09:30)
    if current_hour == 9 and current_minute <= 30:
        send_telegram("🌅 <b>[오전장] 시초가 갭 공략 종목 탐색 중...</b>")

    # [B] 마감 전 종가베팅 종목 탐색 시간대 (15시 15분 경 실행하여 15시 20분 동시호가 전 준비)
    elif current_hour == 15 and 10 <= current_minute <= 20:
        print("=== [종가베팅 탑픽 스크리닝 시작] ===")
        try:
            df_krx = fdr.StockListing('KRX')
            top_500 = df_krx.sort_values(by='Amount', ascending=False).head(500)
        except Exception as e:
            print(f"KRX 종목 리스트 불러오기 실패: {e}")
            return

        candidates = []
        for _, row in top_500.iterrows():
            ticker = row['Code']
            name = row['Name']
            try:
                df = fdr.DataReader(ticker, start_date)
                if len(df) < 10:
                    continue
                latest = df.iloc[-1]
                close = latest['Close']
                amount = latest['Amount']
                if amount < 1_000_000_000:
                    continue
                candidates.append({'ticker': ticker, 'name': name, 'close': close, 'amount': amount})
            except Exception:
                continue

        candidates = sorted(candidates, key=lambda x: x['amount'], reverse=True)
        top_picks = candidates[:3]

        closing_signals = []  
        new_positions = load_positions() 

        for item in top_picks:
            ticker = item['ticker']
            name = item['name']
            close = item['close']
            
            stop_loss = round(close * 0.95, -1) 
            target_1 = round(close * 1.03, -1)  
            target_2 = round(close * 1.06, -1)  
            
            chart_link = f"https://finance.naver.com/item/main.naver?code={ticker}"
            
            closing_msg = (
                f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                f"💰 <b>기준가(현재가):</b> <code>{int(close):,}원</code>\n"
                f"🛡️ <b>손절가(SL):</b> <code>{int(stop_loss):,}원</code>\n"
                f"🎯 <b>1차 목표가(TP1):</b> <code>{int(target_1):,}원</code>\n"
                f"🎯 <b>2차 목표가(TP2):</b> <code>{int(target_2):,}원</code>\n"
                f"📈 <a href='{chart_link}'>네이버 차트</a>"
            )
            closing_signals.append(closing_msg)
            
            existing_pos = new_positions.get(ticker, {})
            if not isinstance(existing_pos, dict) or existing_pos.get('status') not in ['ACTIVE']:
                new_positions[ticker] = {
                    "name": name,
                    "target_1": int(target_1),
                    "target_2": int(target_2),
                    "stop_loss": int(stop_loss),
                    "tp1_hit": False,
                    "status": "ACTIVE"
                }

        if closing_signals:
            closing_text = "🚨 <b>[종가베팅] 동시호가 전 최상위 주도주 포착</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(closing_signals)
            send_telegram(closing_text)

        save_positions(new_positions)
        send_telegram("🏁 <b>[국장 자동화] 종가 스크리닝 완료!</b>")

if __name__ == "__main__":
    run_screener()
