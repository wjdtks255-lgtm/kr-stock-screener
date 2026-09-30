import os
import json
import math
import time
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import FinanceDataReader as fdr


# ============================================================
# KOREA STOCK HUNTER V5.1
#
# 1. MORNING
#    장초 상승 후보
#
# 2. CLOSE
#    15:10~15:20 다음날 상승 후보
#
# ============================================================


KST = timezone(timedelta(hours=9))

STATE_FILE = "bot_state.json"

MIN_PRICE = 1000
MIN_TURNOVER = 200_000_000

MAX_RESULTS = 5

MIN_SCORE_MORNING = 55
MIN_SCORE_CLOSE = 55

REQUEST_SLEEP = 0.05

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

MODE_ENV = os.getenv("MODE", "").strip().lower()
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() == "true"


# ============================================================
# STATE
# ============================================================

DEFAULT_STATE = {
    "positions": {},
    "sent_signals": {},
    "last_run": ""
}


def load_state():

    try:

        if not os.path.exists(STATE_FILE):
            return DEFAULT_STATE.copy()

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        if not isinstance(state, dict):
            return DEFAULT_STATE.copy()

        state.setdefault("positions", {})
        state.setdefault("sent_signals", {})
        state.setdefault("last_run", "")

        return state

    except Exception as e:

        print(
            "STATE LOAD ERROR:",
            repr(e)
        )

        return DEFAULT_STATE.copy()


def save_state(state):

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "STATE SAVE ERROR:",
            repr(e)
        )


# ============================================================
# TIME
# ============================================================

def now_kst():

    return datetime.now(KST)


def auto_mode():

    t = now_kst().strftime("%H:%M")

    # 장초
    if "09:05" <= t <= "09:35":
        return "morning"

    # 장마감 직전
    if "15:10" <= t <= "15:20":
        return "close"

    return "monitor"


def get_mode():

    if MODE_ENV in (
        "morning",
        "close",
        "monitor"
    ):

        return MODE_ENV

    return auto_mode()


# ============================================================
# NUMBER
# ============================================================

def num(
    value,
    default=0.0
):

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

        x = float(value)

        if math.isnan(x):
            return default

        if math.isinf(x):
            return default

        return x

    except Exception:

        return default


# ============================================================
# STOCK LISTING
#
# FinanceDataReader 최신 KRX 목록:
#
# Code
# Name
# Market
#
# ============================================================

def normalize_listing(
    df,
    market
):

    if df is None or df.empty:

        return pd.DataFrame(
            columns=[
                "Symbol",
                "Name",
                "Market"
            ]
        )

    df = df.copy()

    print(
        f"{market} 원본 컬럼:",
        list(df.columns)
    )

    # --------------------------------------------------------
    # Code -> Symbol
    # 최신 FinanceDataReader는 Code 사용
    # --------------------------------------------------------

    code_col = None

    for col in [
        "Code",
        "Symbol",
        "code",
        "symbol"
    ]:

        if col in df.columns:

            code_col = col
            break

    if code_col is None:

        print(
            f"{market}: 종목코드 컬럼을 찾지 못했습니다."
        )

        return pd.DataFrame(
            columns=[
                "Symbol",
                "Name",
                "Market"
            ]
        )

    # --------------------------------------------------------
    # Name
    # --------------------------------------------------------

    name_col = None

    for col in [
        "Name",
        "name",
        "종목명"
    ]:

        if col in df.columns:

            name_col = col
            break

    # --------------------------------------------------------
    # Market
    # --------------------------------------------------------

    market_col = None

    for col in [
        "Market",
        "market",
        "시장구분"
    ]:

        if col in df.columns:

            market_col = col
            break

    # --------------------------------------------------------
    # 표준화
    # --------------------------------------------------------

    result = pd.DataFrame()

    result["Symbol"] = (
        df[code_col]
        .astype(str)
        .str.replace(
            ".0",
            "",
            regex=False
        )
        .str.strip()
        .str.zfill(6)
    )

    if name_col is not None:

        result["Name"] = (
            df[name_col]
            .astype(str)
            .str.strip()
        )

    else:

        result["Name"] = ""

    if market_col is not None:

        result["Market"] = (
            df[market_col]
            .astype(str)
            .str.strip()
        )

    else:

        result["Market"] = market

    # --------------------------------------------------------
    # 6자리 종목코드만
    # --------------------------------------------------------

    result = result[
        result["Symbol"]
        .str.fullmatch(
            r"\d{6}"
        )
    ]

    # --------------------------------------------------------
    # ETF / ETN / SPAC / REIT 제외
    # --------------------------------------------------------

    bad_pattern = (
        r"ETF|ETN|스팩|SPAC|리츠|REIT"
    )

    result = result[
        ~result["Name"]
        .str.upper()
        .str.contains(
            bad_pattern,
            na=False
        )
    ]

    result = (
        result
        .drop_duplicates(
            subset=["Symbol"]
        )
        .reset_index(drop=True)
    )

    return result[
        [
            "Symbol",
            "Name",
            "Market"
        ]
    ]


# ============================================================
# GET LISTINGS
# ============================================================

def get_stock_listings():

    print()
    print("====================================")
    print(" STOCK LIST DOWNLOAD")
    print("====================================")

    markets = []

    for market in [
        "KOSPI",
        "KOSDAQ"
    ]:

        try:

            print(
                f"{market} listing 다운로드..."
            )

            df = fdr.StockListing(
                market
            )

            normalized = normalize_listing(
                df,
                market
            )

            print(
                f"{market}: "
                f"{len(normalized)}개"
            )

            if not normalized.empty:

                markets.append(
                    normalized
                )

        except Exception as e:

            print(
                f"{market} listing ERROR:",
                repr(e)
            )

    if not markets:

        return pd.DataFrame(
            columns=[
                "Symbol",
                "Name",
                "Market"
            ]
        )

    result = pd.concat(
        markets,
        ignore_index=True
    )

    result = (
        result
        .drop_duplicates(
            subset=["Symbol"]
        )
        .reset_index(drop=True)
    )

    print()
    print(
        f"전체 상장 종목: "
        f"{len(result)}개"
    )

    return result


# ============================================================
# PRICE DATA
# ============================================================

def get_price_data(code):

    try:

        start_date = (
            now_kst()
            - timedelta(days=120)
        ).strftime("%Y-%m-%d")

        df = fdr.DataReader(
            code,
            start_date
        )

        if df is None or df.empty:
            return None

        df = df.copy()

        # 컬럼명 정리
        df.columns = [
            str(c).strip()
            for c in df.columns
        ]

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

        df = df.dropna(
            subset=required
        )

        if len(df) < 25:
            return None

        return df

    except Exception:

        return None


# ============================================================
# BASIC METRICS
# ============================================================

def basic_metrics(df):

    if df is None:
        return None

    if len(df) < 25:
        return None

    last = df.iloc[-1]

    close = num(
        last["Close"]
    )

    open_price = num(
        last["Open"]
    )

    high = num(
        last["High"]
    )

    low = num(
        last["Low"]
    )

    volume = num(
        last["Volume"]
    )

    if close <= 0:
        return None

    turnover = (
        close * volume
    )

    if close < MIN_PRICE:
        return None

    if turnover < MIN_TURNOVER:
        return None

    prev_close = num(
        df["Close"].iloc[-2]
    )

    if prev_close > 0:

        change_pct = (
            close / prev_close - 1
        ) * 100

    else:

        change_pct = 0

    return {
        "close": close,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "turnover": turnover,
        "change_pct": change_pct
    }


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    df,
    mode
):

    m = basic_metrics(df)

    if m is None:
        return None

    close = m["close"]
    open_price = m["open"]
    high = m["high"]
    low = m["low"]
    volume = m["volume"]
    turnover = m["turnover"]
    change_pct = m["change_pct"]

    score = 0

    # --------------------------------------------------------
    # 양봉
    # --------------------------------------------------------

    if close > open_price:

        score += 10

    # --------------------------------------------------------
    # 고가 부근
    # --------------------------------------------------------

    if high > low:

        position = (
            (close - low)
            /
            (high - low)
        )

        if position >= 0.85:

            score += 15

        elif position >= 0.70:

            score += 10

        elif position >= 0.55:

            score += 5

    # --------------------------------------------------------
    # 상승률
    # --------------------------------------------------------

    if 1.0 <= change_pct <= 6.0:

        score += 15

    elif 0.3 <= change_pct < 1.0:

        score += 8

    elif 6.0 < change_pct <= 10.0:

        score += 7

    elif change_pct < 0:

        score -= 15

    elif change_pct > 12:

        score -= 10

    # --------------------------------------------------------
    # MA
    # --------------------------------------------------------

    closes = df["Close"]

    ma5 = num(
        closes.tail(5).mean()
    )

    ma20 = num(
        closes.tail(20).mean()
    )

    if close > ma20:

        score += 10

    if ma5 > ma20:

        score += 10

    # --------------------------------------------------------
    # 5일 수익률
    # --------------------------------------------------------

    if len(df) >= 6:

        close_5 = num(
            closes.iloc[-6]
        )

        if close_5 > 0:

            ret5 = (
                close / close_5 - 1
            ) * 100

            if 0 < ret5 <= 12:

                score += 10

            elif ret5 > 12:

                score += 3

            elif ret5 < -8:

                score -= 5

    # --------------------------------------------------------
    # 20일 고점
    # --------------------------------------------------------

    if len(df) >= 21:

        previous_high = num(
            closes.iloc[-21:-1].max()
        )

        if previous_high > 0:

            breakout = (
                close / previous_high - 1
            ) * 100

            if breakout >= 0:

                score += 15

            elif breakout >= -2:

                score += 8

    # --------------------------------------------------------
    # 거래량
    # --------------------------------------------------------

    if len(df) >= 21:

        avg_volume = num(
            df["Volume"]
            .iloc[-21:-1]
            .mean()
        )

        if avg_volume > 0:

            volume_ratio = (
                volume / avg_volume
            )

            if volume_ratio >= 2.0:

                score += 15

            elif volume_ratio >= 1.5:

                score += 10

            elif volume_ratio >= 1.2:

                score += 5

    # ========================================================
    # MORNING
    # ========================================================

    if mode == "morning":

        if 1 <= change_pct <= 7:

            score += 5

        if change_pct > 12:

            score -= 15

    # ========================================================
    # CLOSE
    # ========================================================

    elif mode == "close":

        if high > low:

            position = (
                (close - low)
                /
                (high - low)
            )

            if position >= 0.90:

                score += 10

        if 1 <= change_pct <= 8:

            score += 5

        if change_pct <= 0:

            score -= 15

        if change_pct > 12:

            score -= 10

    return {
        "score": score,
        "close": close,
        "change_pct": change_pct,
        "turnover": turnover
    }


# ============================================================
# SCAN
# ============================================================

def scan_market(
    listing_df,
    mode
):

    results = []

    total = len(
        listing_df
    )

    print(
        f"{mode.upper()} "
        f"검사 시작: {total}개"
    )

    for index, row in listing_df.iterrows():

        code = str(
            row["Symbol"]
        )

        name = str(
            row["Name"]
        )

        market = str(
            row["Market"]
        )

        df = get_price_data(
            code
        )

        if df is None:

            continue

        result = calculate_score(
            df,
            mode
        )

        if result is None:

            continue

        minimum_score = (
            MIN_SCORE_MORNING
            if mode == "morning"
            else MIN_SCORE_CLOSE
        )

        if result["score"] < minimum_score:

            continue

        result.update({

            "code": code,

            "name": name,

            "market": market

        })

        results.append(
            result
        )

        if len(results) >= 30:

            break

        if (
            len(results) % 5 == 0
        ):

            print(
                f"현재 후보 "
                f"{len(results)}개"
            )

        time.sleep(
            REQUEST_SLEEP
        )

    results.sort(
        key=lambda x: (
            x["score"],
            x["change_pct"],
            x["turnover"]
        ),
        reverse=True
    )

    return results


# ============================================================
# SCAN ALL
# ============================================================

def scan_all(
    listings,
    mode
):

    if listings.empty:

        return []

    kospi = listings[
        listings["Market"]
        .astype(str)
        .str.upper()
        .str.contains(
            "KOSPI",
            na=False
        )
    ].copy()

    kosdaq = listings[
        listings["Market"]
        .astype(str)
        .str.upper()
        .str.contains(
            "KOSDAQ",
            na=False
        )
    ].copy()

    print()
    print(
        f"KOSPI 목록: "
        f"{len(kospi)}"
    )

    print(
        f"KOSDAQ 목록: "
        f"{len(kosdaq)}"
    )

    # --------------------------------------------------------
    # FinanceDataReader KRX 목록은 시총순 데이터가 제공되므로
    # 여기서는 시장별 앞쪽 종목부터 검사
    # --------------------------------------------------------

    kospi = kospi.head(300)

    kosdaq = kosdaq.head(300)

    print()
    print(
        "실제 가격 데이터 검사:"
    )

    print(
        f"KOSPI {len(kospi)}개"
    )

    print(
        f"KOSDAQ {len(kosdaq)}개"
    )

    results = []

    for market_df in [
        kospi,
        kosdaq
    ]:

        market_results = scan_market(
            market_df,
            mode
        )

        results.extend(
            market_results
        )

    results.sort(
        key=lambda x: (
            x["score"],
            x["change_pct"],
            x["turnover"]
        ),
        reverse=True
    )

    return results[:MAX_RESULTS]


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(
    message
):

    if not TELEGRAM_TOKEN:

        print(
            "Telegram TOKEN 없음"
        )

        return False

    if not CHAT_ID:

        print(
            "Telegram CHAT_ID 없음"
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {

        "chat_id": CHAT_ID,

        "text": message,

        "parse_mode": "HTML"

    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.ok:

            print(
                "Telegram SENT"
            )

            return True

        print(
            "Telegram ERROR:",
            response.status_code,
            response.text[:300]
        )

    except Exception as e:

        print(
            "Telegram ERROR:",
            repr(e)
        )

    return False


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_signal_message(
    results,
    mode
):

    if mode == "morning":

        title = (
            "🌅 <b>KOREA STOCK HUNTER V5.1</b>\n"
            "장초 상승 후보"
        )

        desc = (
            "09:05~09:35 기준\n"
            "상승 모멘텀 조건을 만족한 종목"
        )

    else:

        title = (
            "🌙 <b>KOREA STOCK HUNTER V5.1</b>\n"
            "다음날 상승 후보"
        )

        desc = (
            "15:10~15:20 기준\n"
            "다음 거래일 모멘텀 후보"
        )

    lines = [

        title,

        desc,

        "",

        (
            "⏰ "
            +
            now_kst().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        ),

        ""

    ]

    if not results:

        lines.append(
            "❌ 조건을 만족하는 후보가 없습니다."
        )

        return "\n".join(lines)

    for i, r in enumerate(
        results,
        1
    ):

        price = r["close"]

        change = r["change_pct"]

        score = r["score"]

        entry = price

        sl = price * 0.965

        tp1 = price * 1.03

        tp2 = price * 1.06

        lines.extend([

            (
                f"<b>{i}. "
                f"{r['name']}</b> "
                f"({r['code']})"
            ),

            (
                f"시장: {r['market']}"
            ),

            (
                f"현재가: "
                f"{price:,.0f}원 "
                f"({change:+.2f}%)"
            ),

            (
                f"Score: <b>{score}</b>"
            ),

            (
                f"ENTRY: {entry:,.0f}"
            ),

            (
                f"SL: {sl:,.0f}"
            ),

            (
                f"TP1: {tp1:,.0f}"
            ),

            (
                f"TP2: {tp2:,.0f}"
            ),

            ""

        ])

    lines.extend([

        "⚠️ 참고용 후보 탐색 결과입니다.",

        "자동 매매 신호가 아닙니다."

    ])

    return "\n".join(lines)


# ============================================================
# POSITION REGISTER
# ============================================================

def register_positions(
    state,
    results
):

    positions = state.setdefault(
        "positions",
        {}
    )

    for r in results:

        code = r["code"]

        price = r["close"]

        positions[code] = {

            "name": r["name"],

            "market": r["market"],

            "entry": price,

            "sl": price * 0.965,

            "tp1": price * 1.03,

            "tp2": price * 1.06,

            "tp1_sent": False,

            "created_at":
                now_kst().isoformat()

        }


# ============================================================
# MONITOR
# ============================================================

def monitor_positions(
    state
):

    positions = state.get(
        "positions",
        {}
    )

    if not positions:

        print(
            "활성 포지션 없음"
        )

        return

    print(
        f"활성 포지션: "
        f"{len(positions)}개"
    )

    changed = False

    for code, pos in list(
        positions.items()
    ):

        try:

            df = get_price_data(
                code
            )

            if df is None:

                continue

            price = num(
                df["Close"].iloc[-1]
            )

            entry = num(
                pos.get("entry")
            )

            sl = num(
                pos.get("sl")
            )

            tp1 = num(
                pos.get("tp1")
            )

            tp2 = num(
                pos.get("tp2")
            )

            name = pos.get(
                "name",
                code
            )

            print(
                f"{name} "
                f"{price:,.0f}"
            )

            if (
                sl > 0
                and
                price <= sl
            ):

                telegram_send(

                    "🛑 <b>STOP LOSS</b>\n"
                    f"{name} ({code})\n"
                    f"현재가: {price:,.0f}\n"
                    f"SL: {sl:,.0f}"

                )

                del positions[code]

                changed = True

                continue

            if (
                tp2 > 0
                and
                price >= tp2
            ):

                telegram_send(

                    "🎯 <b>TP2 도달</b>\n"
                    f"{name} ({code})\n"
                    f"현재가: {price:,.0f}\n"
                    f"TP2: {tp2:,.0f}"

                )

                del positions[code]

                changed = True

                continue

            if (
                tp1 > 0
                and
                price >= tp1
                and
                not pos.get(
                    "tp1_sent",
                    False
                )
            ):

                telegram_send(

                    "🎯 <b>TP1 도달</b>\n"
                    f"{name} ({code})\n"
                    f"현재가: {price:,.0f}\n"
                    f"TP1: {tp1:,.0f}"

                )

                pos["tp1_sent"] = True

                changed = True

        except Exception as e:

            print(
                "MONITOR ERROR:",
                code,
                repr(e)
            )

    if changed:

        state["positions"] = positions


# ============================================================
# RUN SCAN
# ============================================================

def run_scan(
    mode
):

    print()
    print("====================================")

    if mode == "morning":

        print(
            " MORNING RISE HUNTER"
        )

        print(
            " 09:05~09:35 장초 상승 후보"
        )

    else:

        print(
            " CLOSE NEXT-DAY HUNTER"
        )

        print(
            " 15:10~15:20 다음날 상승 후보"
        )

    print(
        "===================================="
    )

    listings = get_stock_listings()

    if listings.empty:

        print()
        print(
            "DATA SOURCE ERROR"
        )

        print(
            "KOSPI/KOSDAQ 종목 목록을 "
            "가져오지 못했습니다."
        )

        telegram_send(

            "⚠️ <b>KOREA STOCK HUNTER</b>\n\n"
            "종목 데이터 소스 오류\n"
            "KOSPI/KOSDAQ 목록을 가져오지 못했습니다."

        )

        return

    results = scan_all(
        listings,
        mode
    )

    print()
    print("====================================")
    print(" FINAL CANDIDATES")
    print("====================================")

    if not results:

        print(
            "조건을 만족하는 종목 없음"
        )

    else:

        for i, r in enumerate(
            results,
            1
        ):

            print(

                f"{i}. "
                f"{r['name']} "
                f"({r['code']}) "
                f"score={r['score']} "
                f"change="
                f"{r['change_pct']:+.2f}% "
                f"price="
                f"{r['close']:,.0f}"

            )

    message = build_signal_message(
        results,
        mode
    )

    telegram_send(
        message
    )

    state = load_state()

    if results:

        register_positions(
            state,
            results
        )

    state["last_run"] = (
        now_kst().isoformat()
    )

    save_state(
        state
    )


# ============================================================
# MAIN
# ============================================================

def main():

    mode = get_mode()

    print(
        "===================================="
    )

    print(
        " KOREA STOCK HUNTER V5.1"
    )

    print(
        " KST:",
        now_kst().isoformat()
    )

    print(
        " MODE:",
        mode
    )

    print(
        " FORCE:",
        FORCE_SCAN
    )

    print(
        "===================================="
    )

    state = load_state()

    if mode == "monitor":

        monitor_positions(
            state
        )

        state["last_run"] = (
            now_kst().isoformat()
        )

        save_state(
            state
        )

    elif mode in (
        "morning",
        "close"
    ):

        run_scan(
            mode
        )

    else:

        print(
            "UNKNOWN MODE:",
            mode
        )

    print(
        "===================================="
    )

    print(
        " DONE"
    )

    print(
        "===================================="
    )


if __name__ == "__main__":

    main()
