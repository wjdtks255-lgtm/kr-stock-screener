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
        print("현재 추적 중인 보유 포지션이 없습니다.")
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
    
    print("=== 보유 포지션 모니터링 수행 ===")
    monitor_positions(start_date)

    print("=== [주도주 무제한 완화버전] 저항돌파 스크리닝 시작 ===")
    try:
        df_krx = fdr.StockListing('KRX')
        top_300 = df_krx.sort_values(by='Amount', ascending=False).head(300)
    except Exception as e:
        print(f"KRX 종목 리스트 불러오기 실패: {e}")
        return

    closing_signals = []  
    morning_signals = []  
    new_positions = load_positions() 

    for _, row in top_300.iterrows():
        ticker = row['Code']
        name = row['Name']
        
        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) < 30:
                continue
            
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            df_30 = df.iloc[-30:]
            
            close = latest['Close'] 
            open_p = latest['Open']
            high = latest['High']
            volume = latest['Volume']
            amount = latest['Amount']
            
            # 당일 거래대금 100억 이상
            if amount < 100_0000_000:
                continue

            avg_vol_20 = df_30['Volume'].mean()
            
            # 1. 기본 상승률 조건 (2.5% 이상 상승으로 살짝 완화)
            candle_body = close - open_p
            if candle_body <= 0:
                continue
            if (close - prev['Close']) / prev['Close'] < 0.025:
                continue
                
            # 2. 윗꼬리 제한 완화 (고가 대비 -2.5% 이내 마감)
            if high > close and (high - close) / high > 0.025:
                continue
                
            # 3. 거래량 폭증 조건 완화 (전일 대비 1.5배 또는 20일 평균 대비 1.5배 이상)
            if volume < prev['Volume'] * 1.5 and volume < avg_vol_20 * 1.5:
                continue
                
            # 4. 이평선 정배열 (5일 > 20일 > 60일 우상향)
            ma5 = df['Close'].rolling(5).mean().iloc[-1]
            ma20 = df['Close'].rolling(20).mean().iloc[-1]
            ma60 = df['Close'].rolling(60).mean().iloc[-1]
            
            if not (close > ma5 > ma20 > ma60):
                continue
                
            # 5. 전고점/저항선 돌파 임박 조건 완화 (-6% 이내로 확장)
            high_30 = df_30['High'].max()
            if close < high_30 * 0.94:
                continue 
                
            stop_loss = round(close * 0.95, -1) # 손절폭도 살짝 여유있게 -5%로 조정
            raw_target_1 = high_30 if high_30 > close * 1.02 else close * 1.04
            target_1 = round(raw_target_1, -1)
            target_2 = round(target_1 * 1.06, -1)
            
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
        closing_text = "🚨 <b>[1] 주도주 완화버전 저항돌파 종가베팅</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(closing_signals[:5])
        send_telegram(closing_text)
        
        morning_text = "🌅 <b>[2] 내일 아침 시초가 매매 (오전 갭 공략)</b>\n━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(morning_signals[:5])
        send_telegram(morning_text)
    else:
        send_telegram("⚠️ 오늘 완화된 조건에 부합하는 주도주가 없습니다.")

    save_positions(new_positions)
    send_telegram("🏁 <b>[국장 자동화] 스크리닝 완료!</b>")

if __name__ == "__main__":
    run_screener()

