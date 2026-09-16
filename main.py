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

# 1. 기존 포지션 상태 관리 파일 로드/저장
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

# 2. 진행 중인 포지션 가격 모니터링 및 TP/SL 판정
def monitor_positions(start_date):
    positions = load_positions()
    if not positions:
        return

    updated_positions = {}
    
    for ticker, pos in positions.items():
        try:
            df = fdr.DataReader(ticker, start_date)
            if len(df) == 0:
                updated_positions[ticker] = pos
                continue
                
            latest = df.iloc[-1]
            current_price = latest['Close']
            high_price = latest['High']
            low_price = latest['Low']
            
            name = pos['name']
            tp1 = pos['target_1']
            tp2 = pos['target_2']
            sl = pos['stop_loss']
            
            # SL(손절가) 이탈 체크
            if low_price <= sl:
                msg = (
                    f"🛡️ <b>[손절가(SL) 도달 알림]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                    f"❌ 이탈 가격: <code>{int(sl):,}원 이하</code>\n"
                    f"⚠️ 설정된 손절 라인을 터치하여 포지션을 정리합니다.\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_telegram(msg)
                continue # 포지션 종료 (추적 목록에서 제외)
                
            # TP2(2차 목표가) 도달 체크
            if high_price >= tp2:
                msg = (
                    f"🎯 <b>[2차 목표가(TP2) 달성!]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                    f"🔥 달성 가격: <code>{int(tp2):,}원 돌파!</code>\n"
                    f"💰 대세 상승 구간 완결, 잔여 물량 전량 익절을 축하드립니다!\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_telegram(msg)
                continue # 포지션 종료
                
            # TP1(1차 목표가) 도달 체크 (아직 TP1 도달 전인 경우에만)
            if high_price >= tp1 and not pos.get('tp1_hit', False):
                msg = (
                    f"🎯 <b>[1차 목표가(TP1) 달성!]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{name}</b> <code>({ticker})</code>\n"
                    f"✨ 달성 가격: <code>{int(tp1):,}원 도달!</code>\n"
                    f"📈 일부 물량 분할 익절 및 본절가(스탑로스) 상향 조정을 권장합니다.\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                send_telegram(msg)
                pos['tp1_hit'] = True # TP1 달성 기록
                
            # 아직 청산 조건에 안 걸린 경우 계속 추적
            updated_positions[ticker] = pos
            
        except Exception as e:
            updated_positions[ticker] = pos

    save_positions(updated_positions)

# 3. 신규 종목 분석
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
        low = latest['Low']
        volume = latest['Volume']
        avg_vol_20 = df_20['Volume'].mean()
        
        candle_body = close - open_p
        upper_tail = high - close
        if candle_body <= 0 or (upper_tail > candle_body * 0.3):
            return None
            
        if volume < prev['Volume'] * 2.0 or volume < avg_vol_20 * 1.5:
            return None
            
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
            f"🚨 <b>[국장 종가베팅 포착]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{name}</b> <code>({ticker})</code>\n\n"
            f"💰 <b>현재가:</b> <code>{int(close):,}원</code>\n\n"
            f"🎯 <b>Target 1:</b> <code>{int(target_1):,}원</code> <code>({target_1_rate:+.2f}%)</code>\n"
            f"🎯 <b>Target 2:</b> <code>{int(target_2):,}원</code> <code>({target_2_rate:+.2f}%)</code>\n"
            f"🛡️ <b>Stop Loss:</b> <code>{int(stop_loss):,}원</code> <code>({loss_rate:+.2f}%)</code>\n\n"
            f"💡 <b>포착 근거</b>\n"
            f"• 20일 이평선 돌파 및 5일선 지지\n"
            f"• 전일 대비 거래량 200% 이상 급증\n"
            f"• 칼같은 리스크 관리 (-4% 고정 손절)\n\n"
            f"📈 <a href='{chart_link}'>네이버 금융 차트 바로가기</a>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        
        # 신규 포지션 정보 반환 (추적 등록용 데이터 포함)
        pos_data = {
            "name": name,
            "target_1": target_1,
            "target_2": target_2,
            "stop_loss": stop_loss,
            "tp1_hit": False
        }
        
        return msg, ticker, pos_data
    except Exception:
        return None

def run_screener():
    today = datetime.datetime.now()
    start_date = (today - datetime.timedelta(days=60)).strftime('%Y-%m-%d')

    # 1단계: 기존 포지션 TP/SL 모니터링 먼저 실행
    print("=== 기존 포지션 모니터링 시작 ===")
    monitor_positions(start_date)

    # 2단계: 신규 종목 스크리닝 실행
    print("=== 신규 종목 스크리닝 시작 ===")
    df_krx = fdr.StockListing('KRX')
    top_300 = df_krx.sort_values(by='Amount', ascending=False).head(300)

    signals = []
    new_positions = load_positions() # 기존 포지션 불러오기 유지
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(analyze_stock, row, start_date) for _, row in top_300.iterrows()]
        for future in as_completed(futures):
            result = future.result()
            if result:
                msg, ticker, pos_data = result
                signals.append(msg)
                new_positions[ticker] = pos_data # 신규 포지션 추적 리스트에 추가

    if not signals:
        send_telegram("⚠️ 현재 조건에 부합하는 종가매매 종목이 없습니다.")
    else:
        for signal in signals[:5]:
            send_telegram(signal)

    # 업데이트된 포지션 상태 저장
    save_positions(new_positions)
    
    send_telegram("🏁 <b>[국장 종가베팅] 금일 종목 탐색 및 포지션 모니터링이 완료되었습니다.</b>")

if __name__ == "__main__":
    run_screener()
