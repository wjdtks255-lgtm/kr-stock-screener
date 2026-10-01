import os
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import FinanceDataReader as fdr
import pytz


# ============================================================
# KOREA STOCK HUNTER V6
# ============================================================
# 1) 09:00 ~ 09:30  : 시초 상승 후보
# 2) 09:35 ~ 15:00  : 장중 상승 후보
# 3) 15:10 ~ 15:20  : 종가 후보
# 4) 전체 시간       : 기존 포지션 TP / SL 추적
#
# DATA
# - FinanceDataReader : 종목 목록 / 과거 가격
# - Naver 공개 실시간 조회 : 현재가 / 등락률 / 거래량 등
#
# STATE
# - active_positions.json
# ============================================================


# =========================
# 환경설정
# =========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

STATE_FILE = "active_positions.json"

KST = pytz.timezone("Asia/Seoul")

NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime"

NAVER_CHART_URL = "https://finance.naver.com/item/main.naver?code={}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    )
}


# =========================
# 스크리닝 설정
# =========================

UNIVERSE_SIZE = 350

MIN_TURNOVER = 1_000_000_000

MORNING_MIN_GAP = 0.5
MORNING_MAX_GAP = 7.0

INTRADAY_MIN_CHANGE = 1.0

CLOSE_MIN_CHANGE = 0.0

MIN_VOLUME_RATIO = 1.30

TOP_SIGNAL_COUNT = 3

HISTORY_DAYS = 90


# =========================
# 포지션 설정
# =========================

SL_PERCENT = 0.05
TP1_PERCENT = 0.03
TP2_PERCENT = 0.06


# =========================
# HTTP SESSION
# =========================

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 환경변수가 없습니다.")
        print(message)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        response = SESSION.post(
            url,
            data=payload,
            timeout=15
        )

        if response.status_code != 200:
            print(
                f"텔레그램 오류: "
                f"{response.status_code} / {response.text[:300]}"
            )
            return False

        return True

    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")
        return False


# ============================================================
# STATE
# ============================================================

def load_positions():

    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

            if isinstance(data, dict):
                return data

    except Exception as e:
        print(f"STATE LOAD ERROR: {e}")

    return {}


def save_positions(positions):

    try:
        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                positions,
                f,
                ensure_ascii=False,
                indent=4
            )

    except Exception as e:
        print(f"STATE SAVE ERROR: {e}")


# ============================================================
# 날짜
# ============================================================

def get_now():

    return datetime.now(KST)


def get_start_date():

    return (
        get_now() - timedelta(days=HISTORY_DAYS)
    ).strftime("%Y-%m-%d")


# ============================================================
# 숫자 안전 변환
# ============================================================

def safe_float(value, default=0.0):

    try:
        if value is None:
            return default

        return float(value)

    except Exception:
        return default


def safe_int(value, default=0):

    try:
        if value is None:
            return default

        return int(float(value))

    except Exception:
        return default


# ============================================================
# Naver 실시간 데이터
# ============================================================

def get_realtime_quotes(codes):

    """
    Naver 공개 실시간 조회.

    return:
    {
        "005930": {
            "price": ...,
            "change": ...,
            "volume": ...,
            "amount": ...,
            "open": ...,
            "high": ...,
            "low": ...,
            "prev_close": ...
        }
    }
    """

    result = {}

    if not codes:
        return result

    # 너무 긴 URL 방지
    chunk_size = 50

    for start in range(0, len(codes), chunk_size):

        chunk = codes[start:start + chunk_size]

        query = "|".join(
            f"SERVICE_ITEM:{code}"
            for code in chunk
        )

        try:

            response = SESSION.get(
                NAVER_REALTIME_URL,
                params={"query": query},
                timeout=15
            )

            response.raise_for_status()

            data = response.json()

            datas = data.get("result", {}).get("areas", [])

            for area in datas:

                items = area.get("datas", [])

                for item in items:

                    code = str(
                        item.get("cd", "")
                    ).zfill(6)

                    if not code:
                        continue

                    price = safe_float(
                        item.get("nv")
                    )

                    change_rate = safe_float(
                        item.get("cr")
                    )

                    volume = safe_float(
                        item.get("aq")
                    )

                    amount = safe_float(
                        item.get("aa")
                    )

                    open_price = safe_float(
                        item.get("op")
                    )

                    high_price = safe_float(
                        item.get("hp")
                    )

                    low_price = safe_float(
                        item.get("lp")
                    )

                    prev_close = safe_float(
                        item.get("pcv")
                    )

                    result[code] = {
                        "price": price,
                        "change": change_rate,
                        "volume": volume,
                        "amount": amount,
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "prev_close": prev_close
                    }

        except Exception as e:

            print(
                f"Naver 실시간 조회 실패: {e}"
            )

        # API 부담 감소
        time.sleep(0.15)

    return result


# ============================================================
# KRX 종목 목록
# ============================================================

def get_universe():

    print("KRX 종목 목록 다운로드...")

    try:

        df = fdr.StockListing("KRX")

        if df is None or df.empty:
            print("KRX 목록이 비어 있습니다.")
            return pd.DataFrame()

        required = ["Code", "Name"]

        for col in required:

            if col not in df.columns:

                print(
                    f"KRX 목록에 {col} 컬럼 없음"
                )

                return pd.DataFrame()

        # 숫자형 거래대금이 있으면 우선 사용
        if "Amount" in df.columns:

            df["Amount"] = pd.to_numeric(
                df["Amount"],
                errors="coerce"
            ).fillna(0)

            df = df.sort_values(
                "Amount",
                ascending=False
            )

        # 거래대금 정보가 없는 경우 종목 수 기준
        df = df.head(UNIVERSE_SIZE).copy()

        df["Code"] = (
            df["Code"]
            .astype(str)
            .str.extract(r"(\d{6})")[0]
        )

        df = df.dropna(
            subset=["Code", "Name"]
        )

        df = df.drop_duplicates(
            subset=["Code"]
        )

        print(
            f"KRX universe: {len(df)}개"
        )

        return df

    except Exception as e:

        print(
            f"KRX 목록 다운로드 실패: {e}"
        )

        return pd.DataFrame()


# ============================================================
# 과거 데이터
# ============================================================

def get_history(code, start_date):

    try:

        df = fdr.DataReader(
            code,
            start_date
        )

        if df is None or df.empty:
            return None

        df = df.copy()

        required = [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

        for col in required:

            if col not in df.columns:
                return None

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df = df.dropna(
            subset=required
        )

        if len(df) < 25:
            return None

        return df

    except Exception:

        return None


# ============================================================
# 기술지표
# ============================================================

def calculate_metrics(df):

    data = df.copy()

    data["MA5"] = (
        data["Close"]
        .rolling(5)
        .mean()
    )

    data["MA20"] = (
        data["Close"]
        .rolling(20)
        .mean()
    )

    data["VOL20"] = (
        data["Volume"]
        .rolling(20)
        .mean()
    )

    data["HIGH20"] = (
        data["High"]
        .rolling(20)
        .max()
        .shift(1)
    )

    latest = data.iloc[-1]

    close = safe_float(
        latest["Close"]
    )

    high = safe_float(
        latest["High"]
    )

    low = safe_float(
        latest["Low"]
    )

    volume = safe_float(
        latest["Volume"]
    )

    ma5 = safe_float(
        latest["MA5"]
    )

    ma20 = safe_float(
        latest["MA20"]
    )

    vol20 = safe_float(
        latest["VOL20"]
    )

    high20 = safe_float(
        latest["HIGH20"]
    )

    if high > low:

        close_position = (
            (close - low) /
            (high - low)
        )

    else:

        close_position = 0.5

    if vol20 > 0:

        volume_ratio = (
            volume / vol20
        )

    else:

        volume_ratio = 0

    if len(data) >= 2:

        prev_close = safe_float(
            data.iloc[-2]["Close"]
        )

        daily_change = (
            (close - prev_close) /
            prev_close * 100
            if prev_close > 0
            else 0
        )

    else:

        daily_change = 0

    high20_distance = 0

    if high20 > 0:

        high20_distance = (
            (close / high20 - 1)
            * 100
        )

    return {
        "close": close,
        "ma5": ma5,
        "ma20": ma20,
        "volume": volume,
        "volume_ratio": volume_ratio,
        "close_position": close_position,
        "daily_change": daily_change,
        "high20": high20,
        "high20_distance": high20_distance
    }


# ============================================================
# 오전 시초 후보 점수
# ============================================================

def morning_score(
    quote,
    metrics
):

    score = 0

    change = quote["change"]
    volume_ratio = metrics["volume_ratio"]
    ma5 = metrics["ma5"]
    ma20 = metrics["ma20"]
    close_position = metrics["close_position"]

    # 갭 상승
    if change >= 0.5:
        score += 1

    if change >= 1.5:
        score += 1

    if change >= 3.0:
        score += 1

    # 과도한 추격 방지
    if change > MORNING_MAX_GAP:
        score -= 2

    # 이동평균
    if ma5 > ma20:
        score += 2

    # 거래량
    if volume_ratio >= 1.3:
        score += 1

    if volume_ratio >= 2.0:
        score += 1

    # 캔들 상단
    if close_position >= 0.65:
        score += 1

    if close_position >= 0.85:
        score += 1

    return score


# ============================================================
# 장중 후보 점수
# ============================================================

def intraday_score(
    quote,
    metrics
):

    score = 0

    change = quote["change"]

    if change >= 1:
        score += 1

    if change >= 2:
        score += 1

    if change >= 4:
        score += 1

    if metrics["ma5"] > metrics["ma20"]:
        score += 2

    if metrics["volume_ratio"] >= 1.3:
        score += 1

    if metrics["volume_ratio"] >= 2:
        score += 1

    if metrics["close_position"] >= 0.7:
        score += 1

    if (
        metrics["high20"] > 0
        and metrics["close"]
        >= metrics["high20"] * 0.98
    ):
        score += 2

    return score


# ============================================================
# 종가 후보 점수
# ============================================================

def closing_score(
    quote,
    metrics
):

    score = 0

    change = quote["change"]

    # 당일 상승
    if change > 0:
        score += 1

    if change >= 1:
        score += 1

    if change >= 3:
        score += 1

    # 추세
    if metrics["ma5"] > metrics["ma20"]:
        score += 2

    if metrics["close"] > metrics["ma20"]:
        score += 1

    # 거래량
    if metrics["volume_ratio"] >= 1.3:
        score += 1

    if metrics["volume_ratio"] >= 2.0:
        score += 1

    if metrics["volume_ratio"] >= 3.0:
        score += 1

    # 종가가 당일 고가에 가까운지
    if metrics["close_position"] >= 0.70:
        score += 1

    if metrics["close_position"] >= 0.90:
        score += 1

    # 20일 고점 돌파 근접
    if (
        metrics["high20"] > 0
        and metrics["close"]
        >= metrics["high20"] * 0.98
    ):
        score += 2

    if (
        metrics["high20"] > 0
        and metrics["close"]
        > metrics["high20"]
    ):
        score += 1

    return score


# ============================================================
# 포지션 생성
# ============================================================

def create_position(
    ticker,
    name,
    entry,
    mode
):

    stop_loss = round(
        entry * (1 - SL_PERCENT),
        -1
    )

    target_1 = round(
        entry * (1 + TP1_PERCENT),
        -1
    )

    target_2 = round(
        entry * (1 + TP2_PERCENT),
        -1
    )

    return {
        "name": name,
        "entry": int(entry),
        "target_1": int(target_1),
        "target_2": int(target_2),
        "stop_loss": int(stop_loss),
        "tp1_hit": False,
        "status": "ACTIVE",
        "mode": mode,
        "created_at": get_now().isoformat()
    }


# ============================================================
# 기존 포지션 모니터링
# ============================================================

def monitor_positions(
    quotes
):

    positions = load_positions()

    if not positions:

        print("추적 중인 포지션 없음")
        return positions

    print(
        f"기존 포지션 {len(positions)}개 모니터링"
    )

    updated = {}

    for ticker, pos in positions.items():

        if not isinstance(pos, dict):
            continue

        status = pos.get(
            "status",
            "ACTIVE"
        )

        if status != "ACTIVE":
            continue

        quote = quotes.get(
            ticker
        )

        if not quote:

            updated[ticker] = pos
            continue

        current = safe_float(
            quote.get("price")
        )

        if current <= 0:

            updated[ticker] = pos
            continue

        name = pos.get(
            "name",
            ticker
        )

        entry = safe_float(
            pos.get("entry")
        )

        sl = safe_float(
            pos.get("stop_loss")
        )

        tp1 = safe_float(
            pos.get("target_1")
        )

        tp2 = safe_float(
            pos.get("target_2")
        )

        # ------------------------------------------------
        # 손절
        # ------------------------------------------------

        if current <= sl:

            send_telegram(
                f"🔴 <b>[주식 손절]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{name}</b> "
                f"<code>({ticker})</code>\n"
                f"💰 현재가: "
                f"<code>{int(current):,}원</code>\n"
                f"❌ 손절가: "
                f"<code>{int(sl):,}원</code>\n"
                f"📉 진입가: "
                f"<code>{int(entry):,}원</code>"
            )

            pos["status"] = "STOP"
            continue

        # ------------------------------------------------
        # TP2
        # ------------------------------------------------

        if current >= tp2:

            send_telegram(
                f"🎯 <b>[주식 TP2 달성]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{name}</b> "
                f"<code>({ticker})</code>\n"
                f"💰 현재가: "
                f"<code>{int(current):,}원</code>\n"
                f"🎯 TP2: "
                f"<code>{int(tp2):,}원</code>\n"
                f"🔥 목표가 2차 도달"
            )

            pos["status"] = "TP2"
            continue

        # ------------------------------------------------
        # TP1
        # ------------------------------------------------

        if (
            current >= tp1
            and not pos.get(
                "tp1_hit",
                False
            )
        ):

            send_telegram(
                f"🎯 <b>[주식 TP1 달성]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{name}</b> "
                f"<code>({ticker})</code>\n"
                f"💰 현재가: "
                f"<code>{int(current):,}원</code>\n"
                f"🎯 TP1: "
                f"<code>{int(tp1):,}원</code>\n"
                f"✨ 1차 목표가 도달\n"
                f"🛡️ 추적 상태 유지"
            )

            pos["tp1_hit"] = True

            # TP1 달성 후 SL을 진입가로 올림
            if entry > 0:

                pos["stop_loss"] = int(
                    round(entry, -1)
                )

        updated[ticker] = pos

    save_positions(updated)

    return updated


# ============================================================
# 오전 09:00~09:30
# ============================================================

def scan_morning(
    universe,
    quotes,
    start_date
):

    print(
        "===================================="
    )
    print(
        " MORNING OPEN SCAN"
    )
    print(
        "===================================="
    )

    candidates = []

    for _, row in universe.iterrows():

        ticker = row["Code"]
        name = row["Name"]

        quote = quotes.get(
            ticker
        )

        if not quote:
            continue

        price = quote["price"]
        change = quote["change"]
        amount = quote["amount"]

        if price <= 0:
            continue

        if (
            change < MORNING_MIN_GAP
            or change > MORNING_MAX_GAP
        ):
            continue

        if amount > 0 and amount < MIN_TURNOVER:
            continue

        df = get_history(
            ticker,
            start_date
        )

        if df is None:
            continue

        metrics = calculate_metrics(
            df
        )

        score = morning_score(
            quote,
            metrics
        )

        if score < 6:
            continue

        candidates.append(
            {
                "ticker": ticker,
                "name": name,
                "price": price,
                "change": change,
                "amount": amount,
                "score": score,
                "volume_ratio":
                    metrics["volume_ratio"]
            }
        )

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["change"],
            x["volume_ratio"]
        ),
        reverse=True
    )

    return candidates[:TOP_SIGNAL_COUNT]


# ============================================================
# 장중
# ============================================================

def scan_intraday(
    universe,
    quotes,
    start_date
):

    print(
        "===================================="
    )
    print(
        " INTRADAY RISING SCAN"
    )
    print(
        "===================================="
    )

    candidates = []

    for _, row in universe.iterrows():

        ticker = row["Code"]
        name = row["Name"]

        quote = quotes.get(
            ticker
        )

        if not quote:
            continue

        price = quote["price"]
        change = quote["change"]
        amount = quote["amount"]

        if price <= 0:
            continue

        if change < INTRADAY_MIN_CHANGE:
            continue

        if amount > 0 and amount < MIN_TURNOVER:
            continue

        df = get_history(
            ticker,
            start_date
        )

        if df is None:
            continue

        metrics = calculate_metrics(
            df
        )

        score = intraday_score(
            quote,
            metrics
        )

        if score < 7:
            continue

        candidates.append(
            {
                "ticker": ticker,
                "name": name,
                "price": price,
                "change": change,
                "amount": amount,
                "score": score,
                "volume_ratio":
                    metrics["volume_ratio"]
            }
        )

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["change"],
            x["volume_ratio"]
        ),
        reverse=True
    )

    return candidates[:TOP_SIGNAL_COUNT]


# ============================================================
# 종가 15:10~15:20
# ============================================================

def scan_closing(
    universe,
    quotes,
    start_date
):

    print(
        "===================================="
    )
    print(
        " CLOSING BET SCAN"
    )
    print(
        "===================================="
    )

    candidates = []

    for _, row in universe.iterrows():

        ticker = row["Code"]
        name = row["Name"]

        quote = quotes.get(
            ticker
        )

        if not quote:
            continue

        price = quote["price"]
        change = quote["change"]
        amount = quote["amount"]

        if price <= 0:
            continue

        if change < CLOSE_MIN_CHANGE:
            continue

        if amount > 0 and amount < MIN_TURNOVER:
            continue

        df = get_history(
            ticker,
            start_date
        )

        if df is None:
            continue

        metrics = calculate_metrics(
            df
        )

        score = closing_score(
            quote,
            metrics
        )

        if score < 8:
            continue

        candidates.append(
            {
                "ticker": ticker,
                "name": name,
                "price": price,
                "change": change,
                "amount": amount,
                "score": score,
                "volume_ratio":
                    metrics["volume_ratio"],
                "close_position":
                    metrics["close_position"],
                "high20_distance":
                    metrics["high20_distance"]
            }
        )

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["volume_ratio"],
            x["change"]
        ),
        reverse=True
    )

    return candidates[:TOP_SIGNAL_COUNT]


# ============================================================
# 알림 + 포지션 저장
# ============================================================

def send_candidates(
    candidates,
    title,
    mode,
    save_position=False
):

    if not candidates:

        print(
            f"{title}: 후보 없음"
        )

        return

    positions = load_positions()

    messages = []

    for item in candidates:

        ticker = item["ticker"]
        name = item["name"]

        entry = safe_float(
            item["price"]
        )

        stop_loss = round(
            entry * (1 - SL_PERCENT),
            -1
        )

        target_1 = round(
            entry * (1 + TP1_PERCENT),
            -1
        )

        target_2 = round(
            entry * (1 + TP2_PERCENT),
            -1
        )

        amount_text = (
            f"{item['amount'] / 100000000:,.1f}억원"
            if item["amount"] > 0
            else "-"
        )

        chart_link = NAVER_CHART_URL.format(
            ticker
        )

        message = (
            f"📌 <b>{name}</b> "
            f"<code>({ticker})</code>\n"
            f"💰 현재가: "
            f"<code>{int(entry):,}원</code>\n"
            f"📈 등락률: "
            f"<b>+{item['change']:.2f}%</b>\n"
            f"🔥 거래대금: "
            f"<code>{amount_text}</code>\n"
            f"⭐ 점수: "
            f"<code>{item['score']}</code>\n"
            f"📊 거래량비율: "
            f"<code>{item.get('volume_ratio', 0):.2f}배</code>"
        )

        if mode == "closing":

            message += (
                f"\n\n"
                f"🟢 진입 기준: "
                f"<code>{int(entry):,}원</code>\n"
                f"🛡️ 손절 SL: "
                f"<code>{int(stop_loss):,}원</code>\n"
                f"🎯 TP1: "
                f"<code>{int(target_1):,}원</code>\n"
                f"🎯 TP2: "
                f"<code>{int(target_2):,}원</code>"
            )

        message += (
            f"\n🔗 "
            f"<a href='{chart_link}'>네이버 차트</a>"
        )

        messages.append(
            message
        )

        # 종가 후보만 추적
        if save_position:

            positions[ticker] = create_position(
                ticker,
                name,
                entry,
                mode
            )

    header = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
    )

    send_telegram(
        header +
        "\n\n".join(messages)
    )

    if save_position:

        save_positions(
            positions
        )


# ============================================================
# 실행 모드
# ============================================================

def get_mode():

    manual_mode = os.environ.get(
        "MODE",
        ""
    ).strip().lower()

    force_scan = os.environ.get(
        "FORCE_SCAN",
        ""
    ).lower()

    if manual_mode in {
        "morning",
        "intraday",
        "close",
        "monitor"
    }:

        if force_scan in {
            "",
            "true",
            "1",
            "yes"
        }:

            return manual_mode

    now = get_now()

    h = now.hour
    m = now.minute

    # 오전
    if h == 9 and m <= 30:
        return "morning"

    # 장중
    if (
        (h == 9 and m > 30)
        or (10 <= h < 15)
        or (h == 15 and m < 10)
    ):
        return "intraday"

    # 종가
    if h == 15 and 10 <= m <= 20:
        return "close"

    return "monitor"


# ============================================================
# MAIN
# ============================================================

def run():

    now = get_now()

    print(
        "===================================="
    )
    print(
        " KOREA STOCK HUNTER V6"
    )
    print(
        f" KST: {now.isoformat()}"
    )
    print(
        "===================================="
    )

    mode = get_mode()

    print(
        f"MODE: {mode}"
    )

    start_date = get_start_date()

    # --------------------------------------------------------
    # 1. 종목 universe
    # --------------------------------------------------------

    universe = get_universe()

    if universe.empty:

        print(
            "DATA SOURCE ERROR"
        )

        send_telegram(
            "⚠️ <b>[국장 봇 오류]</b>\n"
            "KRX 종목 목록을 가져오지 못했습니다."
        )

        return

    codes = (
        universe["Code"]
        .astype(str)
        .tolist()
    )

    # --------------------------------------------------------
    # 2. 실시간 조회
    # --------------------------------------------------------

    print(
        f"실시간 시세 조회: {len(codes)}개"
    )

    quotes = get_realtime_quotes(
        codes
    )

    print(
        f"실시간 데이터: {len(quotes)}개"
    )

    # --------------------------------------------------------
    # 3. 기존 포지션 모니터
    # --------------------------------------------------------

    monitor_positions(
        quotes
    )

    # --------------------------------------------------------
    # 4. 스크리닝
    # --------------------------------------------------------

    if mode == "morning":

        candidates = scan_morning(
            universe,
            quotes,
            start_date
        )

        send_candidates(
            candidates,
            "🚀 <b>[시초가 공략] 상승 후보</b>",
            "morning",
            False
        )

    elif mode == "intraday":

        candidates = scan_intraday(
            universe,
            quotes,
            start_date
        )

        send_candidates(
            candidates,
            "📈 <b>[장중 상승] 주도주 후보</b>",
            "intraday",
            False
        )

    elif mode == "close":

        candidates = scan_closing(
            universe,
            quotes,
            start_date
        )

        send_candidates(
            candidates,
            "🚨 <b>[15:20 종가베팅] 다음날 상승 후보</b>",
            "closing",
            True
        )

        send_telegram(
            "🏁 <b>[국장 자동화]</b>\n"
            "종가 후보 검색 및 포지션 저장 완료."
        )

    else:

        print(
            "현재 시각은 스크리닝 시간이 아니므로 "
            "포지션 모니터링만 실행합니다."
        )

    print(
        "===================================="
    )
    print(
        " RUN COMPLETE"
    )
    print(
        "===================================="
    )


if __name__ == "__main__":

    run()
