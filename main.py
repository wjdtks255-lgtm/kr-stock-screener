import os
import json
import requests
from datetime import datetime
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
        print("현재 추적 중인 보유 포지션이 없습니다.")
        return

    updated_positions = {}
    for ticker, pos in positions.items():
        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) == 0:
                updated_positions[ticker] = pos
                continue
                
            latest = df.iloc[-1]
            high_price = latest['High']
            low_price = latest['Low']
            
            name = pos['name']
            tp1 = pos['target_1']
            tp2 = pos['target_2']
            sl = pos['stop_loss']
            
            # SL(손절가) 체크
            if low_price <= sl:
                msg = f"🛡️ <b>[손절가(SL) 도달]</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n❌ 이탈 가격: <code>{int(sl):,}원 이하</code>"
                send_telegram(msg)
                continue 
                
            # TP2(2차 목표가) 체크
            if high_price >= tp2:
                msg = f"🎯 <b>[2차 목표가(TP2) 달성]</b>\n📌 <b>{name}</b> <code>({ticker})</code>\n🔥 달성 가격: <code>{int(tp2):,}원 돌파!</code>"
                send_telegram(msg)
                continue 
                
            # TP1(1차 목표가) 체크
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
    start_date = (now - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    
    print("=== 보유 포지션 모니터링 수행 ===")
    monitor_positions(start_date)

    print("=== [종가베팅] 신규 종목 스크리닝 시작 ===")
    try:
        df_krx = fdr.StockListing('KRX')
        top_300 = df_krx.sort_values(by='Amount', ascending=False).head(300)
    except Exception as e:
        print(f"KRX 종목 리스트 불러오기 실패: {e}")
        return

    signals = []
    new_positions = load_positions() 

    for _, row in top_300.iterrows():
        ticker = row['Code']
        name = row['Name']
        
        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) < 20:
                continue
            
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            df_20 = df.iloc[-20:]
            
            close = latest['Close']
            open_p = latest['Open']
            high = latest['High']
            volume = latest['Volume']
            avg_vol_20 = df_20['Volume'].mean()
            
            # 조건 검증 (양봉, 2.5% 이상 상승, 윗꼬리 최소화, 거래량 폭증)
            candle_body = close - open_p
            if candle_body <= 0:
                continue
            if (close - prev['Close']) / prev['Close'] < 0.025:
                continue
            if (high - close) > candle_body * 0.15:
                continue
            if volume < prev['Volume'] * 2.5 or volume < avg_vol_20 * 2.0:
                continue
                
            ma5 = df['Close'].rolling(5).mean().iloc[-1]
            ma20 = df['Close'].rolling(20).mean().iloc[-1]
            if close < ma5 or close < ma20:
                continue
                
            stop_loss = close * 0.96
            high_20 = df_20['High'].max()
            target_1 = high_20 if high_20 > close * 1.02 else close * 1.04
            target_2 = target_1 * 1.05
            
            msg = (
                f"🚨 <b>[종가베팅 포착]</b>\n"
                f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                f"💰 <b>현재가:</b> <code>{int(close):,}원</code>\n"
                f"🎯 <b>Target 1:</b> <code>{int(target_1):,}원</code>\n"
                f"🎯 <b>Target 2:</b> <code>{int(target_2):,}원</code>\n"
                f"🛡️ <b>Stop Loss:</b> <code>{int(stop_loss):,}원</code>"
            )
            
            signals.append(msg)
            new_positions[ticker] = {
                "name": name,
                "target_1": int(target_1),
                "target_2": int(target_2),
                "stop_loss": int(stop_loss),
                "tp1_hit": False
            }
        except Exception:
            continue

    if signals:
        for signal in signals[:5]:
            send_telegram(signal)
    else:
        send_telegram("⚠️ 오늘 조건에 부합하는 종가베팅 종목이 없습니다.")

    save_positions(new_positions)
    send_telegram("🏁 <b>[국장 종가베팅] 모니터링 및 스크리닝 완료!</b>")

if __name__ == "__main__":
    run_screener()

