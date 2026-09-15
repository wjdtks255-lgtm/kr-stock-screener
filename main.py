import datetime
import os
import requests
import FinanceDataReader as fdr
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    requests.post(url, data=payload)

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
            
        stop_loss = max(low, ma5 * 0.99)
        loss_rate = round(((stop_loss - close) / close) * 100, 2)
        
        high_20 = df_20['High'].max()
        target_1 = high_20 if high_20 > close * 1.02 else close * 1.04
        target_1_rate = round(((target_1 - close) / close) * 100, 2)
        
        target_2 = target_1 * 1.05
        target_2_rate = round(((target_2 - close) / close) * 100, 2)
        
        chart_link = f"https://finance.naver.com/item/main.naver?code={ticker}"
        reason = "20일 이평선 돌파 및 5일선 지지확인 / 전일 대비 거래량 200% 이상 유입 / 위꼬리 짧은 강한 장대양봉"

        msg = f"<b>{name}({ticker})</b>\n" \
              f"현재가: {int(close):,}원\n" \
              f"1차목표가: {int(target_1):,}원 ({target_1_rate:+}%)\n" \
              f"2차목표가: {int(target_2):,}원 ({target_2_rate:+}%)\n" \
              f"손절가: {int(stop_loss):,}원 ({loss_rate:+}%)\n" \
              f"간단한상승근거: {reason}\n" \
              f"주식차트링크: {chart_link}"
        return msg
    except Exception:
        return None

def run_screener():
    df_krx = fdr.StockListing('KRX')
    top_300 = df_krx.sort_values(by='Amount', ascending=False).head(300)
    
    today = datetime.datetime.now()
    start_date = (today - datetime.timedelta(days=60)).strftime('%Y-%m-%d')

    signals = []
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(analyze_stock, row, start_date) for _, row in top_300.iterrows()]
        for future in as_completed(futures):
            result = future.result()
            if result:
                signals.append(result)

    if not signals:
        send_telegram("현재 조건에 부합하는 종가매매 종목이 없습니다.")
    else:
        for signal in signals[:5]:
            send_telegram(signal)

if __name__ == "__main__":
    # 비공개 채널 ID 확인용 로그 출력
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        res = requests.get(url).json()
        print("=== 텔레그램 채널/유저 ID 확인 로그 ===")
        print(res)
    except Exception as e:
        print("ID 확인 로그 출력 실패:", e)

    run_screener()
