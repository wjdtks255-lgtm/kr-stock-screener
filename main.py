import os
import json
import requests
from datetime import datetime
from pykrx import stock

# ==================== [설정 영역] ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
POSITIONS_FILE = "active_positions.json"

# 전략 파라미터 설정
TARGET_VOLUME_RATIO = 2.0      # 전일 거래량 대비 최소 2배 이상
MIN_PRICE = 1000               # 동전주 제외 (1,000원 이상)
MAX_PRICE = 50000              # 5만 원 이하 종목 선호 (조절 가능)
STOP_LOSS_PCT = -0.03          # 손절가: 진입가 대비 -3%
TP1_PCT = 0.03                 # 1차 목표가: +3% (절반 매도 구간)
TP2_PCT = 0.07                 # 2차 목표가: +7% (나머지 전량 매도)
# ====================================================

def send_telegram_message(message):
    """텔레그램 메시지 전송 함수"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("텔레그램 토큰 또는챗 ID가 설정되지 않았습니다.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

def load_positions():
    """저장된 보유 포지션 불러오기"""
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_positions(positions):
    """보유 포지션 파일 저장"""
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=4)

def run_screener():
    """종가베팅 조건 검색 (오후 3시 15분 실행 추천)"""
    today = datetime.now().strftime("%Y%m%d")
    try:
        df = stock.get_market_ohlcv_by_ticker(today, market="ALL")
    except Exception as e:
        print(f"데이터를 불러오는 중 오류 발생 (휴일일 수 있음): {e}")
        return

    selected_stocks = []
    positions = load_positions()

    for ticker, row in df.iterrows():
        close = row['종가']
        volume = row['거래량']
        
        # 기본 필터링 (가격 및 거래량 조건)
        if not (MIN_PRICE <= close <= MAX_PRICE):
            continue
        if volume < 100000: # 최소 거래량 필터
            continue

        try:
            # 최근 20일 거래량 데이터 비교를 위해 기간 조회
            df_hist = stock.get_market_ohlcv_by_date(
                (datetime.now().date().replace(day=1)).strftime("%Y%m%d"), 
                today, 
                ticker
            )
            if len(df_hist) < 5:
                continue
            
            avg_volume_5d = df_hist['거래량'].iloc[-6:-1].mean() # 직전 5일 평균 거래량
            
            # 조건: 오늘 거래량이 직전 5일 평균 거래량의 N배 이상이고 양봉인 경우
            if avg_volume_5d > 0 and (volume / avg_volume_5d >= TARGET_VOLUME_RATIO) and (row['대비'] > 0):
                name = stock.get_market_ticker_name(ticker)
                
                # 이미 보유 중이거나 오늘 이미 추가된 종목이 아니라면 신규 편입
                if ticker not in positions:
                    positions[ticker] = {
                        "name": name,
                        "entry_price": close,
                        "stop_loss": close * (1 + STOP_LOSS_PCT),
                        "tp1": close * (1 + TP1_PCT),
                        "tp2": close * (1 + TP2_PCT),
                        "tp1_hit": False,
                        "date": today
                    }
                    selected_stocks.append(f"🟢 **[종가베팅 포착]** {name} ({ticker})\n- 진입가: {close:,}원\n- 손절가: {int(close * (1 + STOP_LOSS_PCT)):,}원\n- 1차목표: {int(close * (1 + TP1_PCT)):,}원")
        except Exception:
            continue

    save_positions(positions)

    if selected_stocks:
        msg = "🚀 **[오늘의 종가베팅 스크리닝 결과]**\n\n" + "\n\n".join(selected_stocks)
        send_telegram_message(msg)
    else:
        print("조건에 부합하는 종목이 없습니다.")

def monitor_positions():
    """보유 포지션 실시간 감시 (손절가 / 목표가 도달 체크)"""
    positions = load_positions()
    if not positions:
        print("감시할 보유 포지션이 없습니다.")
        return

    today = datetime.now().strftime("%Y%m%d")
    try:
        df_current = stock.get_market_ohlcv_by_ticker(today, market="ALL")
    except Exception as e:
        print(f"현재가 조회 오류: {e}")
        return

    updated_positions = {}
    alerts = []

    for ticker, pos in positions.items():
        if ticker not in df_current.index:
            updated_positions[ticker] = pos
            continue

        current_price = df_current.loc[ticker, '종가']
        name = pos['name']
        entry_price = pos['entry_price']
        stop_loss = pos['stop_loss']
        tp1 = pos['tp1']
        tp2 = pos['tp2']
        tp1_hit = pos['tp1_hit']

        # 1. 손절(SL) 조건 이탈 체크
        if current_price <= stop_loss:
            loss_pct = ((current_price - entry_price) / entry_price) * 100
            alerts.append(f"🔴 **[손절(SL) 도달]** {name} ({ticker})\n- 진입가: {entry_price:,}원\n- 현재가: {current_price:,}원 ({loss_pct:.2f}%)")
            # 포지션 청산으로 목록에서 제거
            continue

        # 2. 2차 목표가(TP2) 도달 체크
        elif current_price >= tp2:
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            alerts.append(f"🎯 **[2차 목표가 달성 (전량익절)]** {name} ({ticker})\n- 진입가: {entry_price:,}원\n- 현재가: {current_price:,}원 (+{profit_pct:.2f}%)")
            # 전량 익절이므로 목록에서 제거
            continue

        # 3. 1차 목표가(TP1) 도달 체크 (절반 익절 후 목표가를 TP2로 상향)
        elif not tp1_hit and current_price >= tp1:
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            alerts.append(f"📈 **[1차 목표가 달성 (절반 익절)]** {name} ({ticker})\n- 진입가: {entry_price:,}원\n- 현재가: {current_price:,}원 (+{profit_pct:.2f}%)\n👉 물량의 50% 익절을 진행하세요!")
            pos['tp1_hit'] = True
            updated_positions[ticker] = pos
        else:
            # 아직 조건에 해당하지 않으면 유지
            updated_positions[ticker] = pos

    save_positions(updated_positions)

    if alerts:
        send_telegram_message("\n\n".join(alerts))

if __name__ == "__main__":
    # 실행 인자에 따라 모니터링 혹은 스크리닝 분기 (GitHub Actions에서 인자로 조절 가능)
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        monitor_positions()
    else:
        run_screener()

