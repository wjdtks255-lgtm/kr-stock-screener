import os
import json
import math
import time
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import FinanceDataReader as fdr


# ============================================================
# KOREA STOCK HUNTER V5
#
# 목적
# 1) 장초 상승 후보 탐색
# 2) 15:10~15:20 다음날 상승 후보 탐색
#
# 데이터
# - FinanceDataReader KOSPI/KOSDAQ listing
# - FinanceDataReader 국내 주가 데이터
#
# ============================================================


KST = timezone(timedelta(hours=9))

STATE_FILE = "bot_state.json"

MIN_PRICE = 1000
MIN_TURNOVER = 200_000_000

MAX_STOCKS_PER_MARKET = 80
MAX_RESULTS = 5

MIN_SCORE_MORNING = 55
MIN_SCORE_CLOSE = 55

REQUEST_SLEEP = 0.08

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

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return DEFAULT_STATE.copy()

        state.setdefault("positions", {})
        state.setdefault("sent_signals", {})
        state.setdefault("last_run", "")

        return state

    except Exception as e:
        print("STATE LOAD ERROR:", e)
        return DEFAULT_STATE.copy()


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception as e:
        print("STATE SAVE ERROR:", e)


# ============================================================
# TIME / MODE
# ============================================================

def now_kst():
    return datetime.now(KST)


def today():
    return now_kst().strftime("%Y-%m-%d")


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
    if MODE_ENV in ("morning", "close", "monitor"):
        return MODE_ENV

    return auto_mode()


# ============================================================
# SAFE NUMBER
# ============================================================

def num(value, default=0.0):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()

        x = float(value)

        if math.isnan(x) or math.isinf(x):
            return default

        return x

    except Exception:
        return default


# ============================================================
# STOCK LIST
# ============================================================

def normalize_listing(df, market):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # 컬럼명 표준화
    rename_map = {}

    for col in df.columns:
        c = str(col).strip().lower()

        if c == "symbol":
            rename_map[col] = "Symbol"

        elif c == "name":
            rename_map[col] = "Name"

        elif c in ("market", "exchange"):
            rename_map[col] = "Market"

    df = df.rename(columns=rename_map)

    if "Symbol" not in df.columns:
        return pd.DataFrame()

    if "Name" not in df.columns:
        df["Name"] = ""

    df["Symbol"] = (
        df["Symbol"]
        .astype(str)
        .str.replace(".0", "", regex=False)
        .str.zfill(6)
    )

    df["Name"] = df["Name"].astype(str)

    df["Market"] = market

    # 숫자 코드만
    df = df[df["Symbol"].str.fullmatch(r"\d{6}")]

    # ETF / ETN / 스팩 등 일부 제외
    bad_words = [
        "ETF",
        "ETN",
        "스팩",
        "SPAC",
        "리츠",
        "REIT"
    ]

    pattern = "|".join(bad_words)

    df = df[
        ~df["Name"]
        .str.upper()
        .str.contains(pattern, na=False)
    ]

    return df[["Symbol", "Name", "Market"]].drop_duplicates(
        subset=["Symbol"]
    )


def get_stock_listings():

    print()
    print("====================================")
    print(" STOCK LIST DOWNLOAD")
    print("====================================")

    markets = []

    for market in ["KOSPI", "KOSDAQ"]:

        try:
            print(f"{market} listing 다운로드...")

            df = fdr.StockListing(market)

            normalized = normalize_listing(df, market)

            print(
                f"{market}: "
                f"{len(normalized)}개"
            )

            if not normalized.empty:
                markets.append(normalized)

        except Exception as e:
            print(
                f"{market} listing ERROR:",
                repr(e)
            )

    if not markets:
        return pd.DataFrame(
            columns=["Symbol", "Name", "Market"]
        )

    result = pd.concat(
        markets,
        ignore_index=True
    )

    result = result.drop_duplicates(
        subset=["Symbol"]
    )

    print(
        f"전체 상장 종목: {len(result)}개"
    )

    return result


# ============================================================
# PRICE DATA
# ============================================================

def get_price_data(code):

    try:
        df = fdr.DataReader(
            code,
            (now_kst() - timedelta(days=120)).strftime("%Y-%m-%d")
        )

        if df is None or df.empty:
            return None

        df = df.copy()

        # 컬럼 정리
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
# CANDIDATE PRE-FILTER
# ============================================================

def basic_metrics(df):

    if df is None or len(df) < 25:
        return None

    last = df.iloc[-1]

    close = num(last["Close"])
    open_price = num(last["Open"])
    high = num(last["High"])
    low = num(last["Low"])
    volume = num(last["Volume"])

    if close <= 0:
        return None

    turnover = close * volume

    if close < MIN_PRICE:
        return None

    if turnover < MIN_TURNOVER:
        return None

    prev_close = num(df["Close"].iloc[-2])

    if prev_close <= 0:
        change_pct = 0
    else:
        change_pct = (
            (close / prev_close) - 1
        ) * 100

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

def calculate_score(df, mode):

    metrics = basic_metrics(df)

    if metrics is None:
        return None

    close = metrics["close"]
    open_price = metrics["open"]
    high = metrics["high"]
    low = metrics["low"]
    volume = metrics["volume"]
    turnover = metrics["turnover"]
    change_pct = metrics["change_pct"]

    score = 0

    # --------------------------------------------------------
    # 1. 양봉
    # --------------------------------------------------------

    if close > open_price:
        score += 10

    # --------------------------------------------------------
    # 2. 당일 고점 부근
    # --------------------------------------------------------

    if high > low:

        position = (
            (close - low) /
            (high - low)
        )

        if position >= 0.85:
            score += 15

        elif position >= 0.70:
            score += 10

        elif position >= 0.55:
            score += 5

    # --------------------------------------------------------
    # 3. 상승률
    # --------------------------------------------------------

    if 1.0 <= change_pct <= 6.0:
        score += 15

    elif 0.3 <= change_pct < 1.0:
        score += 8

    elif 6.0 < change_pct <= 10:
        score += 7

    elif change_pct < 0:
        score -= 15

    elif change_pct > 12:
        score -= 10

    # --------------------------------------------------------
    # 4. 이동평균
    # --------------------------------------------------------

    close_series = df["Close"]

    ma5 = num(
        close_series.tail(5).mean()
    )

    ma20 = num(
        close_series.tail(20).mean()
    )

    if close > ma20:
        score += 10

    if ma5 > ma20:
        score += 10

    # --------------------------------------------------------
    # 5. 최근 5일 상승
    # --------------------------------------------------------

    if len(df) >= 6:

        close_5 = num(
            close_series.iloc[-6]
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
    # 6. 20일 고점 돌파 / 근접
    # --------------------------------------------------------

    if len(df) >= 21:

        previous_high = num(
            close_series.iloc[-21:-1].max()
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
    # 7. 거래량 증가
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

    # --------------------------------------------------------
    # MODE별 보정
    # --------------------------------------------------------

    if mode == "morning":

        # 장초에서는 과도한 급등 종목 제한
        if 1 <= change_pct <= 7:
            score += 5

        if change_pct > 12:
            score -= 15

    elif mode == "close":

        # 종가 후보는 종가 위치를 중요하게
        if high > low:

            position = (
                (close - low) /
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

def scan_market(listing_df, mode):

    results = []

    if listing_df.empty:
        return results

    # 거래량/거래대금 기준으로 먼저 가격 데이터 조회
    # 전체 종목을 전부 조회하지 않고 순차적으로 검사
    total = len(listing_df)

    print(
        f"{mode.upper()} scan 시작: "
        f"{total}개"
    )

    for idx, row in listing_df.iterrows():

        code = str(row["Symbol"])
        name = str(row["Name"])
        market = str(row["Market"])

        df = get_price_data(code)

        if df is None:
            continue

        metrics = basic_metrics(df)

        if metrics is None:
            continue

        result = calculate_score(
            df,
            mode
        )

        if result is None:
            continue

        score = result["score"]

        minimum = (
            MIN_SCORE_MORNING
            if mode == "morning"
            else MIN_SCORE_CLOSE
        )

        if score < minimum:
            continue

        result.update({
            "code": code,
            "name": name,
            "market": market
        })

        results.append(result)

        if len(results) >= MAX_STOCKS_PER_MARKET:
            break

        if len(results) % 10 == 0:
            print(
                f"현재 후보: {len(results)}"
            )

        time.sleep(REQUEST_SLEEP)

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
# FASTER SCAN
# ============================================================

def scan_all(listings, mode):

    if listings.empty:
        return []

    # --------------------------------------------------------
    # 전체 종목을 무작정 조회하면 GitHub Actions에서 너무 오래
    # 걸릴 수 있으므로 시장별로 제한
    # --------------------------------------------------------

    kospi = listings[
        listings["Market"] == "KOSPI"
    ].copy()

    kosdaq = listings[
        listings["Market"] == "KOSDAQ"
    ].copy()

    print()
    print(
        f"KOSPI 목록: {len(kospi)}"
    )

    print(
        f"KOSDAQ 목록: {len(kosdaq)}"
    )

    # --------------------------------------------------------
    # 시가총액/상장순서가 아니라 랜덤이 아닌 앞부분만 자르지
    # 않도록 이름순 정렬 후 충분히 검사
    # --------------------------------------------------------

    kospi = kospi.sort_values("Symbol")
    kosdaq = kosdaq.sort_values("Symbol")

    # 너무 긴 GitHub Actions 실행 방지
    # 우선 각 시장 250개씩 검사
    kospi = kospi.head(250)
    kosdaq = kosdaq.head(250)

    print()
    print(
        f"검사 대상: KOSPI {len(kospi)} / "
        f"KOSDAQ {len(kosdaq)}"
    )

    results = []

    for market_df in [kospi, kosdaq]:

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

def telegram_send(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "Telegram 환경변수가 없습니다."
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

        r = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if r.ok:
            print("Telegram SENT")
            return True

        print(
            "Telegram ERROR:",
            r.status_code,
            r.text[:300]
        )

    except Exception as e:

        print(
            "Telegram ERROR:",
            repr(e)
        )

    return False


# ============================================================
# MESSAGE
# ============================================================

def build_signal_message(
    results,
    mode
):

    if mode == "morning":

        title = (
            "🌅 <b>KOREA STOCK HUNTER</b>\n"
            "장초 상승 후보"
        )

        description = (
            "09:05~09:35 기준 "
            "상승 모멘텀 후보입니다."
        )

    else:

        title = (
            "🌙 <b>KOREA STOCK HUNTER</b>\n"
            "다음날 상승 후보"
        )

        description = (
            "15:10~15:20 기준 "
            "다음 거래일 모멘텀 후보입니다."
        )

    lines = [
        title,
        description,
        "",
        f"⏰ {now_kst().strftime('%Y-%m-%d %H:%M:%S')}",
        ""
    ]

    if not results:

        lines.append(
            "❌ 조건을 만족하는 후보가 없습니다."
        )

        return "\n".join(lines)

    for i, r in enumerate(results, 1):

        price = r["close"]
        change = r["change_pct"]
        score = r["score"]

        entry = price

        sl = price * 0.965

        tp1 = price * 1.03

        tp2 = price * 1.06

        lines.extend([
            (
                f"<b>{i}. {r['name']}</b> "
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
                f"ENTRY {entry:,.0f}"
            ),
            (
                f"SL {sl:,.0f}"
            ),
            (
                f"TP1 {tp1:,.0f}"
            ),
            (
                f"TP2 {tp2:,.0f}"
            ),
            ""
        ])

    lines.extend([
        "⚠️ 참고용 후보 탐색 결과입니다.",
        "자동 매매 신호가 아닙니다."
    ])

    return "\n".join(lines)


# ============================================================
# POSITION MONITOR
# ============================================================

def monitor_positions(state):

    positions = state.get(
        "positions",
        {}
    )

    if not positions:
        print("활성 포지션 없음")
        return

    print(
        f"활성 포지션: {len(positions)}"
    )

    changed = False

    for code, pos in list(
        positions.items()
    ):

        try:

            df = get_price_data(code)

            if df is None:
                continue

            price = num(
                df["Close"].iloc[-1]
            )

            entry = num(
                pos.get("entry")
            )

            tp1 = num(
                pos.get("tp1")
            )

            tp2 = num(
                pos.get("tp2")
            )

            sl = num(
                pos.get("sl")
            )

            name = pos.get(
                "name",
                code
            )

            print(
                name,
                price,
                entry,
                sl,
                tp1,
                tp2
            )

            if sl > 0 and price <= sl:

                telegram_send(
                    f"🛑 <b>STOP LOSS</b>\n"
                    f"{name} ({code})\n"
                    f"현재가: {price:,.0f}\n"
                    f"SL: {sl:,.0f}"
                )

                del positions[code]
                changed = True
                continue

            if tp2 > 0 and price >= tp2:

                telegram_send(
                    f"🎯 <b>TP2 도달</b>\n"
                    f"{name} ({code})\n"
                    f"현재가: {price:,.0f}\n"
                    f"TP2: {tp2:,.0f}"
                )

                del positions[code]
                changed = True
                continue

            if tp1 > 0 and price >= tp1:

                if not pos.get(
                    "tp1_sent",
                    False
                ):

                    telegram_send(
                        f"🎯 <b>TP1 도달</b>\n"
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
# SAVE NEW POSITIONS
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

        positions[code] = {
            "name": r["name"],
            "market": r["market"],
            "entry": r["close"],
            "sl": r["close"] * 0.965,
            "tp1": r["close"] * 1.03,
            "tp2": r["close"] * 1.06,
            "tp1_sent": False,
            "created_at": now_kst().isoformat()
        }


# ============================================================
# MAIN SCAN
# ============================================================

def run_scan(mode):

    print()
    print("====================================")

    if mode == "morning":

        print(
            " MORNING RISE HUNTER"
        )

        print(
            " 09:05~09:35 장초 상승 후보"
        )

    elif mode == "close":

        print(
            " CLOSE NEXT-DAY HUNTER"
        )

        print(
            " 15:10~15:20 다음날 상승 후보"
        )

    print("====================================")

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
                f"change={r['change_pct']:+.2f}% "
                f"price={r['close']:,.0f}"
            )

    message = build_signal_message(
        results,
        mode
    )

    telegram_send(message)

    state = load_state()

    if results:
        register_positions(
            state,
            results
        )

    state["last_run"] = now_kst().isoformat()

    save_state(state)


# ============================================================
# MAIN
# ============================================================

def main():

    mode = get_mode()

    print("====================================")
    print(" KOREA STOCK HUNTER V5")
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
    print("====================================")

    state = load_state()

    if mode == "monitor":

        monitor_positions(
            state
        )

        state["last_run"] = (
            now_kst().isoformat()
        )

        save_state(state)

    elif mode in (
        "morning",
        "close"
    ):

        run_scan(mode)

    else:

        print(
            "UNKNOWN MODE:",
            mode
        )

    print("====================================")
    print(" DONE")
    print("====================================")


if __name__ == "__main__":
    main()
