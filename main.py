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
        
        # 1. 캔들 조건: 양봉이면서 위꼬리가 몸통의 30% 이하인 강한 캔들
        candle_body = close - open_p
        upper_tail = high - close
        if candle_body <= 0 or (upper_tail > candle_body * 0.3):
            return None
            
        # 2. 거래량 조건: 전일 대비 200% 이상, 20일 평균 대비 150% 이상
        if volume < prev['Volume'] * 2.0 or volume < avg_vol_20 * 1.5:
            return None
            
        # 3. 이평선 조건: 5일선, 20일선 위에 위치
        ma5 = df['Close'].rolling(5).mean().iloc[-1]
        ma20 = df['Close'].rolling(20).mean().iloc[-1]
        if close < ma5 or close < ma20:
            return None
            
        # 4. 현실적인 손절가 설정 (당일 시가 또는 5일선 부근 중 합리적인 선)
        stop_loss = max(low, open_p * 0.98)
        loss_rate = round(((stop_loss - close) / close) * 100, 2)
        
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
            f"• 위꼬리 짧은 강한 장대양봉 형성\n\n"
            f"📈 <a href='{chart_link}'>네이버 금융 차트 바로가기</a>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
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
        send_telegram("⚠️ 현재 조건에 부합하는 종가매매 종목이 없습니다.")
    else:
        for signal in signals[:5]:
            send_telegram(signal)

if __name__ == "__main__":
    run_screener()
