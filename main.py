import datetime
import os
import requests
from pykrx import stock

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    requests.post(url, data=payload)

def run_screener():
    today = datetime.datetime.now().strftime("%Y%m%d")
    
    # 당일 거래대금 상위 300개 종목 필터링
    df_market = stock.get_market_ohlcv_by_ticker(today, market="ALL")
    top_trading_value = df_market.sort_values(by="거래대금", ascending=False).head(300)
    tickers = top_trading_value.index.tolist()
    
    signals = []

    for ticker in tickers:
        name = stock.get_market_ticker_name(ticker)
        # 데이터 조회
        df = stock.get_market_ohlcv_by_date("20260801", today, ticker)
        if len(df) < 20:
            continue
            
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        df_20 = df.iloc[-20:]
        
        close = latest['종가']
        open_p = latest['시가']
        high = latest['고가']
        low = latest['저가']
        volume = latest['거래량']
        avg_vol_20 = df_20['거래량'].mean()
        
        # 조건 1: 양봉 & 위꼬리 30% 이하
        candle_body = close - open_p
        upper_tail = high - close
        if candle_body <= 0 or (upper_tail > candle_body * 0.3):
            continue
            
        # 조건 2: 거래량 급증 (전일 대비 200% 이상, 20일 평균 대비 150% 이상)
        if volume < prev['거래량'] * 2.0 or volume < avg_vol_20 * 1.5:
            continue
            
        # 조건 3: 5일선 / 20일선 위 위치
        ma5 = df['종가'].rolling(5).mean().iloc[-1]
        ma20 = df['종가'].rolling(20).mean().iloc[-1]
        if close < ma5 or close < ma20:
            continue
            
        # 지지·저항 기반 손절가 / 목표가 산출
        stop_loss = max(low, ma5 * 0.99)
        loss_rate = round(((stop_loss - close) / close) * 100, 2)
        
        high_20 = df_20['고가'].max()
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
              
        signals.append(msg)

    # 텔레그램 전송 (상위 5개)
    for signal in signals[:5]:
        send_telegram(signal)

if __name__ == "__main__":
    run_screener()

