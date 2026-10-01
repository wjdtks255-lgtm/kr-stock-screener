import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import FinanceDataReader as fdr
import pytz


# ============================================================
# KOREA STOCK HUNTER V6.1
# ============================================================
#
# 09:00 ~ 09:30  : 시초 상승 후보
# 09:31 ~ 15:05  : 장중 상승 후보
# 15:10 ~ 15:20  : 종가 후보
# 항상            : 기존 포지션 TP / SL 추적
#
# 실시간 데이터
# -> Naver 국내주식 종목별 realtime endpoint
#
# 과거 데이터
# -> FinanceDataReader
# ============================================================


# ============================================================
# ENV
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

MODE_ENV = os.environ.get("MODE", "").strip().lower()
FORCE_SCAN_ENV = os.environ.get("FORCE_SCAN", "").strip().lower()

STATE_FILE = "active_positions.json"

KST = pytz.timezone("Asia/Seoul")


# ============================================================
# SETTINGS
# ============================================================

UNIVERSE_SIZE = 350

HISTORY_DAYS = 90

MIN_TURNOVER = 500_000_000

TOP_SIGNAL_COUNT = 3

# 시초
MORNING_MIN_CHANGE = 0.5
MORNING_MAX_CHANGE = 7.0

# 장중
INTRADAY_MIN_CHANGE = 1.0

# 종가
CLOSING_MIN_CHANGE = 0.0

# 포지션
SL_PERCENT = 0.05
TP1_PERCENT = 0.03
TP2_PERCENT = 0.06


# ============================================================
# NAVER
# ============================================================

NAVER_REALTIME_BASE = (
    "https://polling.finance.naver.com"
    "/api/realtime/domestic/stock/"
)

NAVER_CHART_URL = (
    "https://finance.naver.com/item/main.naver?code={}"
)


HEADERS = {
    "User-Agent":
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36",

    "Accept": "application/json,text/plain,*/*",

    "Accept-Language":
        "ko-KR,ko;q=0.9,en-US;q=0.8"
}


SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# BASIC
# ============================================================

def now_kst():

    return datetime.now(KST)


def start_date():

    return (
        now_kst() -
        timedelta(days=HISTORY_DAYS)
    ).strftime("%Y-%m-%d")


def to_float(value, default=0.0):

    try:

        if value is None:
            return default

        if isinstance(value, str):

            value = (
                value
                .replace(",", "")
                .replace("%", "")
                .strip()
            )

        return float(value)

    except Exception:

        return default


def to_int(value, default=0):

    try:

        return int(
            round(
                to_float(
                    value,
                    default
                )
            )
        )

    except Exception:

        return default


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "TELEGRAM ENV ERROR"
        )

        print(message)

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

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
                "Telegram error:",
                response.status_code,
                response.text[:300]
            )

            return False

        return True

    except Exception as e:

        print(
            f"Telegram send failed: {e}"
        )

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

        print(
            f"STATE LOAD ERROR: {e}"
        )

    return {}


def save_positions(data):

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=4
            )

    except Exception as e:

        print(
            f"STATE SAVE ERROR: {e}"
        )


# ============================================================
# NAVER REALTIME
# ============================================================

def get_one_realtime(code, retry=2):

    url = (
        NAVER_REALTIME_BASE +
        str(code).zfill(6)
    )

    for attempt in range(
        retry + 1
    ):

        try:

            response = SESSION.get(
                url,
                timeout=7
            )

            if response.status_code != 200:

                print(
                    f"[NAVER] {code} "
                    f"HTTP {response.status_code}"
                )

                time.sleep(
                    0.5
                )

                continue

            data = response.json()

            datas = data.get(
                "datas",
                []
            )

            if not datas:

                time.sleep(
                    0.5
                )

                continue

            d = datas[0]

            price = to_float(
                d.get("closePrice")
            )

            change = to_float(
                d.get(
                    "fluctuationsRatio"
                )
            )

            # 일부 응답에서는 부호 없는 값이 올 수 있으므로
            # compareToPreviousClosePrice도 확인
            change_value = to_float(
                d.get(
                    "compareToPreviousClosePrice"
                )
            )

            if change == 0 and change_value != 0:

                prev = to_float(
                    d.get(
                        "previousClosePrice"
                    )
                )

                if prev > 0:

                    change = (
                        change_value /
                        prev *
                        100
                    )

            volume = to_float(
                d.get(
                    "accumulatedTradingVolume"
                )
            )

            amount = to_float(
                d.get(
                    "accumulatedTradingValue"
                )
            )

            open_price = to_float(
                d.get(
                    "openPrice"
                )
            )

            high_price = to_float(
                d.get(
                    "highPrice"
                )
            )

            low_price = to_float(
                d.get(
                    "lowPrice"
                )
            )

            prev_close = to_float(
                d.get(
                    "previousClosePrice"
                )
            )

            market_status = d.get(
                "marketStatus",
                ""
            )

            return {
                "price": price,
                "change": change,
                "volume": volume,
                "amount": amount,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "prev_close": prev_close,
                "market_status": market_status
            }

        except Exception as e:

            if attempt >= retry:

                print(
                    f"[NAVER FAIL] {code}: {e}"
                )

            time.sleep(
                0.5
            )

    return None


def get_realtime_quotes(codes):

    result = {}

    total = len(codes)

    print(
        f"실시간 시세 조회 시작: {total}개"
    )

    for index, code in enumerate(
        codes,
        start=1
    ):

        quote = get_one_realtime(
            code
        )

        if quote:

            result[code] = quote

        # 지나치게 빠른 연속 요청 방지
        time.sleep(0.08)

        if index % 50 == 0:

            print(
                f"실시간 조회 진행: "
                f"{index}/{total} "
                f"성공 {len(result)}"
            )

    print(
        f"실시간 데이터: "
        f"{len(result)}/{total}개"
    )

    return result


# ============================================================
# KRX UNIVERSE
# ============================================================

def get_universe():

    print(
        "KRX 종목 목록 다운로드..."
    )

    try:

        df = fdr.StockListing(
            "KRX"
        )

        if df is None or df.empty:

            return pd.DataFrame()

        if (
            "Code" not in df.columns
            or
            "Name" not in df.columns
        ):

            print(
                "KRX Code / Name 컬럼 없음"
            )

            return pd.DataFrame()

        df = df.copy()

        if "Amount" in df.columns:

            df["Amount"] = pd.to_numeric(
                df["Amount"],
                errors="coerce"
            ).fillna(0)

            df = df.sort_values(
                "Amount",
                ascending=False
            )

        df = df.head(
            UNIVERSE_SIZE
        )

        df["Code"] = (
            df["Code"]
            .astype(str)
            .str.extract(
                r"(\d{6})"
            )[0]
        )

        df = df.dropna(
            subset=[
                "Code",
                "Name"
            ]
        )

        df = df.drop_duplicates(
            subset=["Code"]
        )

        print(
            f"KRX universe: "
            f"{len(df)}개"
        )

        return df

    except Exception as e:

        print(
            f"KRX 목록 오류: {e}"
        )

        return pd.DataFrame()


# ============================================================
# HISTORY
# ============================================================

def get_history(
    code,
    begin
):

    try:

        df = fdr.DataReader(
            code,
            begin
        )

        if df is None or df.empty:

            return None

        required = [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

        for column in required:

            if column not in df.columns:

                return None

            df[column] = pd.to_numeric(
                df[column],
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
# METRICS
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

    close = to_float(
        latest["Close"]
    )

    high = to_float(
        latest["High"]
    )

    low = to_float(
        latest["Low"]
    )

    volume = to_float(
        latest["Volume"]
    )

    ma5 = to_float(
        latest["MA5"]
    )

    ma20 = to_float(
        latest["MA20"]
    )

    vol20 = to_float(
        latest["VOL20"]
    )

    high20 = to_float(
        latest["HIGH20"]
    )

    if high > low:

        close_position = (
            (close - low) /
            (high - low)
        )

    else:

        close_position = 0.5

    volume_ratio = (
        volume / vol20
        if vol20 > 0
        else 0
    )

    return {
        "close": close,
        "high": high,
        "low": low,
        "ma5": ma5,
        "ma20": ma20,
        "volume": volume,
        "volume_ratio": volume_ratio,
        "close_position": close_position,
        "high20": high20
    }


# ============================================================
# MORNING SCORE
# ============================================================

def morning_score(
    quote,
    metrics
):

    score = 0

    change = quote["change"]

    if (
        MORNING_MIN_CHANGE
        <= change
        <= MORNING_MAX_CHANGE
    ):

        score += 2

    if change >= 1.5:

        score += 1

    if change >= 3:

        score += 1

    if (
        metrics["ma5"]
        > metrics["ma20"]
    ):

        score += 2

    if (
        metrics["volume_ratio"]
        >= 1.3
    ):

        score += 1

    if (
        metrics["volume_ratio"]
        >= 2
    ):

        score += 1

    if (
        metrics["close_position"]
        >= 0.7
    ):

        score += 1

    return score


# ============================================================
# INTRADAY SCORE
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

    if (
        metrics["ma5"]
        > metrics["ma20"]
    ):

        score += 2

    if (
        metrics["volume_ratio"]
        >= 1.3
    ):

        score += 1

    if (
        metrics["volume_ratio"]
        >= 2
    ):

        score += 1

    if (
        metrics["close_position"]
        >= 0.7
    ):

        score += 1

    if (
        metrics["high20"] > 0
        and
        metrics["close"]
        >= metrics["high20"] * 0.98
    ):

        score += 2

    return score


# ============================================================
# CLOSING SCORE
# ============================================================

def closing_score(
    quote,
    metrics
):

    score = 0

    change = quote["change"]

    if change > 0:

        score += 1

    if change >= 1:

        score += 1

    if change >= 3:

        score += 1

    if (
        metrics["ma5"]
        > metrics["ma20"]
    ):

        score += 2

    if (
        metrics["close"]
        > metrics["ma20"]
    ):

        score += 1

    if (
        metrics["volume_ratio"]
        >= 1.3
    ):

        score += 1

    if (
        metrics["volume_ratio"]
        >= 2
    ):

        score += 1

    if (
        metrics["volume_ratio"]
        >= 3
    ):

        score += 1

    if (
        metrics["close_position"]
        >= 0.7
    ):

        score += 1

    if (
        metrics["close_position"]
        >= 0.9
    ):

        score += 1

    if (
        metrics["high20"] > 0
        and
        metrics["close"]
        >= metrics["high20"] * 0.98
    ):

        score += 2

    if (
        metrics["high20"] > 0
        and
        metrics["close"]
        > metrics["high20"]
    ):

        score += 1

    return score


# ============================================================
# MORNING SCAN
# ============================================================

def scan_morning(
    universe,
    quotes,
    begin
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

        code = row["Code"]
        name = row["Name"]

        quote = quotes.get(
            code
        )

        if not quote:
            continue

        price = quote["price"]
        change = quote["change"]
        amount = quote["amount"]

        if price <= 0:
            continue

        if (
            change < MORNING_MIN_CHANGE
            or
            change > MORNING_MAX_CHANGE
        ):

            continue

        if (
            amount > 0
            and
            amount < MIN_TURNOVER
        ):

            continue

        history = get_history(
            code,
            begin
        )

        if history is None:
            continue

        metrics = calculate_metrics(
            history
        )

        score = morning_score(
            quote,
            metrics
        )

        if score < 6:
            continue

        candidates.append({
            "ticker": code,
            "name": name,
            "price": price,
            "change": change,
            "amount": amount,
            "score": score,
            "volume_ratio":
                metrics["volume_ratio"]
        })

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["change"],
            x["volume_ratio"]
        ),
        reverse=True
    )

    print(
        f"Morning candidates: "
        f"{len(candidates)}"
    )

    return candidates[
        :TOP_SIGNAL_COUNT
    ]


# ============================================================
# INTRADAY SCAN
# ============================================================

def scan_intraday(
    universe,
    quotes,
    begin
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

        code = row["Code"]
        name = row["Name"]

        quote = quotes.get(
            code
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

        if (
            amount > 0
            and
            amount < MIN_TURNOVER
        ):

            continue

        history = get_history(
            code,
            begin
        )

        if history is None:
            continue

        metrics = calculate_metrics(
            history
        )

        score = intraday_score(
            quote,
            metrics
        )

        if score < 7:
            continue

        candidates.append({
            "ticker": code,
            "name": name,
            "price": price,
            "change": change,
            "amount": amount,
            "score": score,
            "volume_ratio":
                metrics["volume_ratio"]
        })

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["change"],
            x["volume_ratio"]
        ),
        reverse=True
    )

    print(
        f"Intraday candidates: "
        f"{len(candidates)}"
    )

    return candidates[
        :TOP_SIGNAL_COUNT
    ]


# ============================================================
# CLOSING SCAN
# ============================================================

def scan_closing(
    universe,
    quotes,
    begin
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

        code = row["Code"]
        name = row["Name"]

        quote = quotes.get(
            code
        )

        if not quote:
            continue

        price = quote["price"]
        change = quote["change"]
        amount = quote["amount"]

        if price <= 0:
            continue

        if change < CLOSING_MIN_CHANGE:
            continue

        if (
            amount > 0
            and
            amount < MIN_TURNOVER
        ):

            continue

        history = get_history(
            code,
            begin
        )

        if history is None:
            continue

        metrics = calculate_metrics(
            history
        )

        # 실시간 가격으로 현재 종가 위치를 보정
        realtime_high = quote["high"]
        realtime_low = quote["low"]

        if realtime_high > realtime_low:

            realtime_position = (
                (price - realtime_low) /
                (
                    realtime_high -
                    realtime_low
                )
            )

            metrics["close_position"] = (
                realtime_position
            )

        metrics["close"] = price

        score = closing_score(
            quote,
            metrics
        )

        if score < 8:
            continue

        candidates.append({
            "ticker": code,
            "name": name,
            "price": price,
            "change": change,
            "amount": amount,
            "score": score,
            "volume_ratio":
                metrics["volume_ratio"],
            "close_position":
                metrics["close_position"]
        })

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["volume_ratio"],
            x["change"]
        ),
        reverse=True
    )

    print(
        f"Closing candidates: "
        f"{len(candidates)}"
    )

    return candidates[
        :TOP_SIGNAL_COUNT
    ]


# ============================================================
# POSITION
# ============================================================

def create_position(
    ticker,
    name,
    entry,
    mode
):

    stop_loss = round(
        entry * (
            1 - SL_PERCENT
        ),
        -1
    )

    target_1 = round(
        entry * (
            1 + TP1_PERCENT
        ),
        -1
    )

    target_2 = round(
        entry * (
            1 + TP2_PERCENT
        ),
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
        "created_at":
            now_kst().isoformat()
    }


# ============================================================
# POSITION MONITOR
# ============================================================

def monitor_positions(
    quotes
):

    positions = load_positions()

    if not positions:

        print(
            "기존 포지션 없음"
        )

        return

    print(
        f"기존 포지션 "
        f"{len(positions)}개 모니터링"
    )

    updated = {}

    for ticker, pos in positions.items():

        if not isinstance(
            pos,
            dict
        ):

            continue

        if pos.get(
            "status",
            "ACTIVE"
        ) != "ACTIVE":

            continue

        quote = quotes.get(
            ticker
        )

        if not quote:

            updated[ticker] = pos

            continue

        current = quote["price"]

        if current <= 0:

            updated[ticker] = pos

            continue

        name = pos.get(
            "name",
            ticker
        )

        entry = to_float(
            pos.get("entry")
        )

        sl = to_float(
            pos.get("stop_loss")
        )

        tp1 = to_float(
            pos.get("target_1")
        )

        tp2 = to_float(
            pos.get("target_2")
        )

        # ==========================================
        # STOP
        # ==========================================

        if current <= sl:

            send_telegram(
                f"🔴 <b>[주식 STOP LOSS]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{name}</b> "
                f"<code>({ticker})</code>\n"
                f"💰 현재가: "
                f"<code>{int(current):,}원</code>\n"
                f"❌ SL: "
                f"<code>{int(sl):,}원</code>\n"
                f"📌 진입가: "
                f"<code>{int(entry):,}원</code>"
            )

            pos["status"] = "STOP"

            continue

        # ==========================================
        # TP2
        # ==========================================

        if current >= tp2:

            send_telegram(
                f"🎯 <b>[주식 TP2 달성]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{name}</b> "
                f"<code>({ticker})</code>\n"
                f"💰 현재가: "
                f"<code>{int(current):,}원</code>\n"
                f"🎯 TP2: "
                f"<code>{int(tp2):,}원</code>"
            )

            pos["status"] = "TP2"

            continue

        # ==========================================
        # TP1
        # ==========================================

        if (
            current >= tp1
            and
            not pos.get(
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
                f"🛡️ SL → 진입가 이동"
            )

            pos["tp1_hit"] = True

            if entry > 0:

                pos["stop_loss"] = int(
                    round(
                        entry,
                        -1
                    )
                )

        updated[ticker] = pos

    save_positions(
        updated
    )


# ============================================================
# SEND CANDIDATES
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

        entry = to_float(
            item["price"]
        )

        chart = NAVER_CHART_URL.format(
            ticker
        )

        amount = item["amount"]

        if amount >= 100_000_000:

            amount_text = (
                f"{amount / 100_000_000:,.1f}"
                f"억원"
            )

        elif amount > 0:

            amount_text = (
                f"{amount / 10_000:,.0f}"
                f"만원"
            )

        else:

            amount_text = "-"

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

            stop_loss = round(
                entry * (
                    1 - SL_PERCENT
                ),
                -1
            )

            target_1 = round(
                entry * (
                    1 + TP1_PERCENT
                ),
                -1
            )

            target_2 = round(
                entry * (
                    1 + TP2_PERCENT
                ),
                -1
            )

            message += (
                f"\n\n"
                f"🟢 <b>진입 기준</b>: "
                f"<code>{int(entry):,}원</code>\n"
                f"🛡️ <b>SL</b>: "
                f"<code>{int(stop_loss):,}원</code>\n"
                f"🎯 <b>TP1</b>: "
                f"<code>{int(target_1):,}원</code>\n"
                f"🎯 <b>TP2</b>: "
                f"<code>{int(target_2):,}원</code>"
            )

        message += (
            f"\n🔗 "
            f"<a href='{chart}'>네이버 차트</a>"
        )

        messages.append(
            message
        )

        # ==========================================
        # 종가 후보만 포지션 등록
        # ==========================================

        if save_position:

            # 같은 날 이미 등록된 종목은
            # 새 포지션으로 덮어씀
            positions[ticker] = create_position(
                ticker,
                name,
                entry,
                mode
            )

    header = (
        title +
        "\n━━━━━━━━━━━━━━━━━━━\n\n"
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
# MODE
# ============================================================

def get_mode():

    # ------------------------------------------
    # 수동 실행
    # ------------------------------------------

    if (
        MODE_ENV
        in {
            "morning",
            "intraday",
            "close",
            "monitor"
        }
        and
        FORCE_SCAN_ENV
        in {
            "",
            "true",
            "1",
            "yes"
        }
    ):

        return MODE_ENV

    # ------------------------------------------
    # 한국시간
    # ------------------------------------------

    now = now_kst()

    hour = now.hour
    minute = now.minute

    # ------------------------------------------
    # 09:00 ~ 09:30
    # ------------------------------------------

    if (
        hour == 9
        and
        0 <= minute <= 30
    ):

        return "morning"

    # ------------------------------------------
    # 09:31 ~ 15:05
    # ------------------------------------------

    if (
        (
            hour == 9
            and
            minute >= 31
        )
        or
        (
            10 <= hour <= 14
        )
        or
        (
            hour == 15
            and
            minute <= 5
        )
    ):

        return "intraday"

    # ------------------------------------------
    # 15:10 ~ 15:20
    # ------------------------------------------

    if (
        hour == 15
        and
        10 <= minute <= 20
    ):

        return "close"

    # ------------------------------------------
    # 나머지
    # ------------------------------------------

    return "monitor"


# ============================================================
# MAIN
# ============================================================

def run():

    now = now_kst()

    print(
        "===================================="
    )

    print(
        " KOREA STOCK HUNTER V6.1"
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

    begin = start_date()

    # ==========================================
    # KRX
    # ==========================================

    universe = get_universe()

    if universe.empty:

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

    # ==========================================
    # REALTIME
    # ==========================================

    quotes = get_realtime_quotes(
        codes
    )

    # 실시간 데이터가 너무 적으면
    # 스크리닝을 하지 않고 종료
    if len(quotes) < 30:

        print(
            "⚠️ 실시간 데이터가 "
            f"{len(quotes)}개뿐입니다."
        )

        send_telegram(
            "⚠️ <b>[국장 데이터 경고]</b>\n"
            f"KRX 종목: {len(codes)}개\n"
            f"실시간 데이터: {len(quotes)}개\n\n"
            "실시간 데이터가 충분하지 않아 "
            "이번 스크리닝을 건너뜁니다."
        )

        # 기존 포지션은 가능한 범위에서 추적
        monitor_positions(
            quotes
        )

        return

    # ==========================================
    # POSITION MONITOR
    # ==========================================

    monitor_positions(
        quotes
    )

    # ==========================================
    # SCAN
    # ==========================================

    if mode == "morning":

        candidates = scan_morning(
            universe,
            quotes,
            begin
        )

        send_candidates(
            candidates,
            "🌅 <b>[시초 상승 후보]</b>",
            "morning",
            False
        )

    elif mode == "intraday":

        candidates = scan_intraday(
            universe,
            quotes,
            begin
        )

        send_candidates(
            candidates,
            "📈 <b>[장중 상승 후보]</b>",
            "intraday",
            False
        )

    elif mode == "close":

        candidates = scan_closing(
            universe,
            quotes,
            begin
        )

        send_candidates(
            candidates,
            "🚨 <b>[15:20 종가 후보]</b>",
            "closing",
            True
        )

        send_telegram(
            "🏁 <b>[국장 자동화]</b>\n"
            "종가 후보 검색 완료.\n"
            f"추적 포지션: "
            f"<code>{len(load_positions())}개</code>"
        )

    else:

        print(
            "스크리닝 시간이 아니므로 "
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
