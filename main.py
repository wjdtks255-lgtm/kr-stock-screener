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
        # 데이터 형식이 딕셔너리가 아니면 에러 방지를 위해 건너뜀
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

            # 1. 손절가 도달 체크
            if low_price <= sl:
                msg = f"🔴 <b>STOP LOSS</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n❌ 이탈 가격: <code>{int(sl):,}원 이하</code> (현재가 저가 기준)"
                send_telegram(msg)
                pos['status'] = 'STOP'
                continue 
                
            # 2. 2차 목표가 달성 체크
            if high_price >= tp2:
                msg = f"🎯 <b>2차 목표가(TP2) 달성</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n🔥 달성 가격: <code>{int(tp2):,}원 돌파!</code>"
                send_telegram(msg)
                pos['status'] = 'TP2'
                continue 
                
            # 3. 1차 목표가 달성 체크
            if high_price >= tp1 and not pos.get('tp1_hit', False):
                msg = f"🎯 <b>1차 목표가(TP1) 달성</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n✨ 달성 가격: <code>{int(tp1):,}원 도달!</code>"
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

    print("=== [주도주 절대 거래대금 탑픽 스크리닝 시작] ===")
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
                
            candidates.append({
                'ticker': ticker,
                'name': name,
                'close': close,
                'amount': amount
            })
        except Exception:
            continue

    candidates = sorted(candidates, key=lambda x: x['amount'], reverse=True)
    top_picks = candidates[:3]

    closing_signals = []  
    morning_signals = []  
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
        closing_text = "🚨 <b>[1] 오늘의 거래대금 최상위 주도주 (종가베팅)</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(closing_signals)
        send_telegram(closing_text)
        
        morning_text = "🌅 <b>[2] 내일 아침 시초가 매매 (오전 갭 공략)</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(morning_signals)
        send_telegram(morning_text)

    save_positions(new_positions)
    send_telegram("🏁 <b>[국장 자동화] 스크리닝 완료!</b>")

if __name__ == "__main__":
    run_screener()
