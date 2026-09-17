import datetime
import os
import json
import requests
import FinanceDataReader as fdr
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
STATE_FILE = "active_positions.json"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    requests.post(url, data=payload)

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
        return False

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
            
            # SL(손절가) 이탈 체크
            if low_price <= sl:
                msg = (
                    f"🛡️ <b>[손절가(SL) 도달/이탈 알림]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                    f"❌ 이탈 가격: <code>{int(sl):,}원 이하</code>\n"
                    f"⚠️ 손절 라인을 터치하여 포지션을 정리합니다.\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_telegram(msg)
                continue 
                
            # TP2(2차 목표가) 달성 체크
            if high_price >= tp2:
                msg = (
                    f"🎯 <b>[2차 목표가(TP2) 달성! - 전량 익절]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                    f"🔥 달성 가격: <code>{int(tp2):,}원 돌파!</code>\n"
                    f"💰 아침 슈팅 구간 목표가 달성을 축하드립니다!\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_telegram(msg)
                continue 
                
            # TP1(1차 목표가) 달성 체크
            if high_price >= tp1 and not pos.get('tp1_hit', False):
                msg = (
                    f"🎯 <b>[1차 목표가(TP1) 달성! - 분할 익절]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                    f"✨ 달성 가격: <code>{int(tp1):,}원 도달!</code>\n"
                    f"📈 아침장 강세 속 일부 물량 익절 및 본절가 대응을 권장합니다.\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_telegram(msg)
                pos['tp1_hit'] = True 
                
            updated_positions[ticker] = pos
            
        except Exception:
            updated_positions[ticker] = pos

    save_positions(updated_positions)
    return True

def analyze_stock(row, start_date):
    ticker = row['Code']
    name = row['Name']
    
    try:
        df = fdr.DataReader(ticker, start_date)
        if len(df) < 20:
            return None
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        df_20 = df.iloc[-20:]
        
        close = latest['Close']
        open_p = latest['Open']
        high = latest['High']
        volume = latest['Volume']
        avg_vol_20 = df_20['Volume'].mean()
        
        # [강화된 캔들 조건] 
        candle_body = close - open_p
        if candle_body <= 0:
            return None
            
        if (close - prev['Close']) / prev['Close'] < 0.025:
            return None
            
        upper_tail = high - close
        if upper_tail > candle_body * 0.15:
            return None
            
        # [강화된 거래량 조건]
        if volume < prev['Volume'] * 2.5 or volume < avg_vol_20 * 2.0:
            return None
            
        # [이평선 조건]
        ma5 = df['Close'].rolling(5).mean().iloc[-1]
        ma20 = df['Close'].rolling(20).mean().iloc[-1]
        if close < ma5 or close < ma20:
            return None
            
        stop_loss = close * 0.96
        loss_rate = -4.0
        
        high_20 = df_20['High'].max()
        target_1 = high_20 if high_20 > close * 1.02 else close * 1.04
        target_1_rate = round(((target_1 - close) / close) * 100, 2)
        
        target_2 = target_1 * 1.05
        target_2_rate = round(((target_2 - close) / close) * 100, 2)
        
        chart_link = f"https://finance.naver.com/item/main.naver?code={ticker}"
        
        msg = (
            f"🚨 <b>[종가베팅 포착 - 15분 후 종가 매수 준비]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{name}</b> <code>({ticker})</code>\n\n"
            f"💰 <b>현재가(추정):</b> <code>{int(close):,}원</code>\n\n"
            f"🎯 <b>익절 Target 1:</b> <code>{int(target_1):,}원</code> <code>({target_1_rate:+.2f}%)</code>\n"
            f"🎯 <b>익절 Target 2:</b> <code>{int(target_2):,}원</code> <code>({target_2_rate:+.2f}%)</code>\n"
            f"🛡️ <b>손절 Stop Loss:</b> <code>{int(stop_loss):,}원</code> <code>({loss_rate:+.2f}%)</code>\n\n"
            f"💡 <b>종가베팅 포착 근거</b>\n"
            f"• 2.5% 이상 강한 장대양봉 및 고가 마감 (윗꼬리 최소화)\n"
            f"• 전일 거래량 250% 폭증 & 20일 이평선 정배열 지지\n"
            f"• 15분 뒤 3시 30분 종가 매수 후 내일 아침 슈팅 대비\n\n"
            f"📈 <a href='{chart_link}'>네이버 금융 차트 바로가기</a>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        
        pos_data = {
            "name": name,
            "target_1": int(target_1),
            "target_2": int(target_2),
            "stop_loss": int(stop_loss),
            "tp1_hit": False
        }
        
        return msg, ticker, pos_data
    except Exception:
        return None

def run_screener():
    now = datetime.datetime.now()
    start_date = (now - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    
    current_hour = now.hour
    current_minute = now.minute
    
    print(f"=== 모니터링 수행 ({current_hour}:{current_minute}) ===")
    monitor_positions(start_date)

    # 💡 신규 탐색 시간을 오후 3시 15분 ~ 3시 19분 사이로 변경 (15분 여유 확보)
    if not (current_hour == 15 and 15 <= current_minute <= 19):
        print("장 마감 15분 전(15:15~15:19)이 아니므로 신규 종가베팅 종목 탐색은 건너뜁니다.")
        return

    print("=== [장 마감 15분 전] 종가베팅 신규 종목 스크리닝 시작 ===")
    df_krx = fdr.StockListing('KRX')
    top_300 = df_krx.sort_values(by='Amount', ascending=False).head(300)

    signals = []
    new_positions = {} 
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(analyze_stock, row, start_date) for _, row in top_300.iterrows()]
        for future in as_completed(futures):
            result = future.result()
            if result:
                msg, ticker, pos_data = result
                signals.append(msg)
                new_positions[ticker] = pos_data

    if not signals:
        send_telegram("⚠️ 현재 조건에 부합하는 종가베팅 종목이 없습니다.")
    else:
        for signal in signals[:5]:
            send_telegram(signal)

    save_positions(new_positions)
    send_telegram("🏁 <b>[국장 종가베팅] 종목 선정 완료. 15분 동안 차트를 검토하고 3시 30분 종가에 매수하세요!</b>")

if __name__ == "__main__":
    run_screener()
