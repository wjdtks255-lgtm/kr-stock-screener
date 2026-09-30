# -*- coding: utf-8 -*-

"""
Korea Stock Hunter V2

목적
1. 09:00~09:30 장초 상승 후보 탐색
2. 15:10~15:20 종가 매수 후보 탐색
3. Telegram 알림
4. ENTRY / SL / TP1 / TP2 계산
5. 장초 반복 검색
6. 종가 반복 검색
7. 동일 종목 중복 알림 방지
8. 후보가 없더라도 다음 스캔을 계속 수행
9. bot_state.json에 상태 저장

주의
- 실제 주문을 하지 않습니다.
- 투자 수익을 보장하지 않습니다.
- Naver 공개 데이터를 기반으로 하는 후보 탐색 시스템입니다.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

import requests


# =========================================================
# 기본 설정
# =========================================================

BASE = "https://m.stock.naver.com"
POLLING = "https://polling.finance.naver.com/api/realtime/domestic/stock"

STATE_FILE = "bot_state.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TIMEOUT = 10

MIN_PRICE = 1000
MIN_TURNOVER = 300_000_000

MAX_CANDIDATES = 100
MAX_RESULTS = 5

MORNING_THRESHOLD = 68
CLOSE_THRESHOLD = 68

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://m.stock.naver.com/",
})


# =========================================================
# 시간
# =========================================================

def now_kst():
    return datetime.now(
        timezone(timedelta(hours=9))
    )


def today_key():
    return now_kst().strftime("%Y-%m-%d")


# =========================================================
# Telegram
# =========================================================

def send_telegram(text):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN 또는 CHAT_ID 없음")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:

        response = session.post(
            url,
            data=payload,
            timeout=15,
        )

        response.raise_for_status()

        return True

    except Exception as e:

        print(f"Telegram error: {e}")

        return False


# =========================================================
# HTTP
# =========================================================

def get_json(url, params=None):

    try:

        response = session.get(
            url,
            params=params,
            timeout=TIMEOUT,
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        print(
            f"GET 실패: {url} "
            f"/ {e}"
        )

        return None


# =========================================================
# 숫자 변환
# =========================================================

def num(value):

    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("%", "")
    )

    try:
        return float(text)

    except Exception:
        return 0.0


def money_korean(value):

    if value is None:
        return 0

    if isinstance(value, (int, float)):
        return int(value)

    text = (
        str(value)
        .replace(",", "")
        .strip()
    )

    if not text:
        return 0

    total = 0

    try:

        if "조" in text:

            a, text = text.split(
                "조",
                1
            )

            total += (
                int(float(a.strip()))
                * 1_000_000_000_000
            )

        if "억" in text:

            a, text = text.split(
                "억",
                1
            )

            total += (
                int(float(a.strip()))
                * 100_000_000
            )

        if "만" in text:

            a, text = text.split(
                "만",
                1
            )

            total += (
                int(float(a.strip()))
                * 10_000
            )

        if total == 0:

            return int(float(text))

        return total

    except Exception:

        return 0


# =========================================================
# JSON에서 종목 데이터 찾기
# =========================================================

def recursive_stock_items(obj):

    found = []

    seen = set()

    def walk(x):

        if isinstance(x, dict):

            code = (
                x.get("itemCode")
                or x.get("code")
                or x.get("symbolCode")
                or x.get("stockCode")
            )

            name = (
                x.get("name")
                or x.get("stockName")
                or x.get("itemName")
            )

            if (
                code
                and name
                and str(code).isdigit()
                and len(str(code)) == 6
            ):

                key = str(code)

                if key not in seen:

                    seen.add(key)

                    found.append(x)

            for value in x.values():
                walk(value)

        elif isinstance(x, list):

            for value in x:
                walk(value)

    walk(obj)

    return found


# =========================================================
# Ranking 데이터 정규화
# =========================================================

def normalize_rank_item(x):

    code = str(
        x.get("itemCode")
        or x.get("code")
        or x.get("symbolCode")
        or x.get("stockCode")
        or ""
    ).zfill(6)

    name = (
        x.get("name")
        or x.get("stockName")
        or x.get("itemName")
        or code
    )

    price = num(
        x.get("currentPrice")
        or x.get("closePrice")
        or x.get("tradePrice")
        or x.get("price")
    )

    change_pct = num(
        x.get("fluctuationsRatio")
        or x.get("changeRate")
        or x.get("changeRateValue")
        or x.get("changePercent")
    )

    volume = num(
        x.get("accumulatedTradingVolume")
        or x.get("tradingVolume")
        or x.get("volume")
        or x.get("quant")
    )

    turnover = money_korean(
        x.get("accumulatedTradingValue")
        or x.get("tradingValue")
        or x.get("amount")
    )

    return {
        "code": code,
        "name": str(name),
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "turnover": turnover,
    }


# =========================================================
# 네이버 순위 데이터
# =========================================================

def get_rankings(
    sort_type,
    category,
    pages=2,
    page_size=100,
):

    result = []

    for page in range(1, pages + 1):

        url = (
            f"{BASE}/front-api/"
            f"stock/domestic/stockList"
        )

        params = {
            "sortType": sort_type,
            "category": category,
            "page": page,
            "pageSize": page_size,
        }

        data = get_json(
            url,
            params,
        )

        if data:

            items = recursive_stock_items(data)

            for item in items:

                normalized = normalize_rank_item(item)

                if normalized["code"].isdigit():

                    result.append(normalized)

        time.sleep(0.15)

    unique = {}

    for item in result:

        code = item["code"]

        if (
            code.isdigit()
            and len(code) == 6
        ):

            unique[code] = item

    return list(unique.values())


# =========================================================
# 실시간 가격
# =========================================================

def get_realtime(code):

    data = get_json(
        f"{POLLING}/{code}"
    )

    if not data:
        return None

    datas = data.get("datas")

    if not datas:
        return None

    d = datas[0]

    return {
        "code": code,

        "name": (
            d.get("stockName")
            or code
        ),

        "price": num(
            d.get("closePriceRaw")
            or d.get("closePrice")
        ),

        "change": num(
            d.get("compareToPreviousClosePriceRaw")
            or d.get("compareToPreviousClosePrice")
        ),

        "change_pct": num(
            d.get("fluctuationsRatioRaw")
            or d.get("fluctuationsRatio")
        ),

        "open": num(
            d.get("openPriceRaw")
            or d.get("openPrice")
        ),

        "high": num(
            d.get("highPriceRaw")
            or d.get("highPrice")
        ),

        "low": num(
            d.get("lowPriceRaw")
            or d.get("lowPrice")
        ),

        "volume": num(
            d.get("accumulatedTradingVolumeRaw")
            or d.get("accumulatedTradingVolume")
        ),

        "turnover": money_korean(
            d.get("accumulatedTradingValueRaw")
            or d.get("accumulatedTradingValue")
        ),

        "market_status": d.get(
            "marketStatus",
            ""
        ),

        "trade_time": d.get(
            "localTradedAt",
            ""
        ),
    }


# =========================================================
# 일봉
# =========================================================

def get_history(
    code,
    page_size=60,
):

    url = (
        f"{BASE}/api/stock/"
        f"{code}/price"
    )

    data = get_json(
        url,
        {
            "pageSize": page_size,
            "page": 1,
        },
    )

    if not data:
        return []

    rows = []

    if isinstance(data, list):

        rows = data

    elif isinstance(data, dict):

        for key in (
            "price",
            "prices",
            "rows",
            "result",
            "data",
        ):

            if isinstance(
                data.get(key),
                list
            ):

                rows = data[key]

                break

    result = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        date = (
            row.get("localTradedAt")
            or row.get("date")
            or row.get("localDate")
        )

        close = num(
            row.get("closePrice")
            or row.get("close")
        )

        high = num(
            row.get("highPrice")
            or row.get("high")
        )

        low = num(
            row.get("lowPrice")
            or row.get("low")
        )

        open_price = num(
            row.get("openPrice")
            or row.get("open")
        )

        volume = num(
            row.get(
                "accumulatedTradingVolume"
            )
            or row.get("volume")
        )

        if date and close > 0:

            result.append({
                "date": str(date)[:10],
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            })

    return result


# =========================================================
# 상태
# =========================================================

def default_state():

    return {
        "positions": {},
        "sent_signals": {},
        "morning_seen": {},
        "close_seen": {},
        "snapshots": {},
        "last_run": "",
    }


def load_state():

    if not os.path.exists(
        STATE_FILE
    ):

        return default_state()

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            state = json.load(f)

        default = default_state()

        for key, value in default.items():

            state.setdefault(
                key,
                value
            )

        return state

    except Exception:

        return default_state()


def save_state(state):

    temp = STATE_FILE + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp,
        STATE_FILE
    )


# =========================================================
# 수익률
# =========================================================

def pct(a, b):

    if not b:
        return 0.0

    return (
        (a / b) - 1
    ) * 100


# =========================================================
# ATR 비슷한 일봉 변동성 계산
# =========================================================

def average_range(hist):

    if len(hist) < 6:
        return 0

    ranges = []

    for row in hist[:20]:

        high = row["high"]
        low = row["low"]

        if high > 0 and low > 0:

            ranges.append(
                high - low
            )

    if not ranges:
        return 0

    return sum(ranges) / len(ranges)


# =========================================================
# ENTRY / SL / TP
# =========================================================

def make_levels(
    price,
    hist,
):

    if price <= 0:

        return (
            0,
            0,
            0,
            0
        )

    avg_range = average_range(hist)

    if avg_range <= 0:

        risk = price * 0.025

    else:

        risk = max(
            price * 0.015,
            avg_range * 0.8
        )

    sl = price - risk

    tp1 = price + risk * 1.2

    tp2 = price + risk * 2.0

    entry = round(
        price / 10
    ) * 10

    sl = round(
        sl / 10
    ) * 10

    tp1 = round(
        tp1 / 10
    ) * 10

    tp2 = round(
        tp2 / 10
    ) * 10

    return (
        int(entry),
        int(sl),
        int(tp1),
        int(tp2),
    )


# =========================================================
# 종가 전략
# =========================================================

def close_score(
    rt,
    hist,
):

    if not rt:
        return 0, {}

    if len(hist) < 20:
        return 0, {}

    price = rt["price"]

    if price <= 0:
        return 0, {}

    closes = [
        x["close"]
        for x in hist
        if x["close"] > 0
    ]

    if len(closes) < 20:
        return 0, {}

    ma5 = sum(
        closes[:5]
    ) / 5

    ma20 = sum(
        closes[:20]
    ) / 20

    ret5 = pct(
        closes[0],
        closes[5]
    )

    ret20 = pct(
        closes[0],
        closes[20]
    ) if len(closes) > 20 else 0

    previous_high = max(
        x["high"]
        for x in hist[1:21]
        if x["high"] > 0
    )

    today_high = rt["high"]
    today_low = rt["low"]

    hold = 0.5

    if (
        today_high > 0
        and today_low > 0
        and today_high > today_low
    ):

        hold = (
            price - today_low
        ) / (
            today_high - today_low
        )

    volumes = [
        x["volume"]
        for x in hist[1:21]
        if x["volume"] > 0
    ]

    avg_volume = (
        sum(volumes)
        / len(volumes)
        if volumes
        else 0
    )

    volume_ratio = (
        rt["volume"]
        / avg_volume
        if avg_volume > 0
        else 0
    )

    score = 0

    # -------------------------
    # 추세
    # -------------------------

    if ma5 > ma20:
        score += 15

    if price > ma5:
        score += 8

    # -------------------------
    # 당일 상승
    # -------------------------

    change = rt["change_pct"]

    if 1 <= change <= 6:
        score += 10

    elif 6 < change <= 10:
        score += 5

    elif change > 10:
        score -= 5

    elif change < -5:
        score -= 10

    # -------------------------
    # 5일 모멘텀
    # -------------------------

    if 0 < ret5 <= 12:
        score += 10

    elif ret5 > 12:
        score += 4

    # -------------------------
    # 20일 모멘텀
    # -------------------------

    if ret20 > 0:
        score += 5

    # -------------------------
    # 거래량
    # -------------------------

    if volume_ratio >= 3:
        score += 15

    elif volume_ratio >= 2:
        score += 10

    elif volume_ratio >= 1.5:
        score += 6

    # -------------------------
    # 전고점
    # -------------------------

    if previous_high > 0:

        if price >= previous_high:
            score += 15

        elif price >= previous_high * 0.98:
            score += 8

    # -------------------------
    # 종가 강도
    # -------------------------

    if hold >= 0.90:
        score += 15

    elif hold >= 0.80:
        score += 10

    elif hold >= 0.70:
        score += 5

    # -------------------------
    # 거래대금
    # -------------------------

    turnover = rt["turnover"]

    if turnover >= 10_000_000_000:
        score += 10

    elif turnover >= 5_000_000_000:
        score += 8

    elif turnover >= 1_000_000_000:
        score += 5

    elif turnover < MIN_TURNOVER:
        score -= 10

    # -------------------------
    # 급등 추격 방지
    # -------------------------

    if change >= 12:
        score -= 12

    if hold < 0.55:
        score -= 12

    score = max(
        0,
        min(
            100,
            int(score)
        )
    )

    metrics = {
        "change_pct": change,
        "ret5": ret5,
        "ret20": ret20,
        "volume_ratio": volume_ratio,
        "hold": hold,
        "turnover": turnover,
        "ma5": ma5,
        "ma20": ma20,
        "previous_high": previous_high,
    }

    return score, metrics


# =========================================================
# 장초 전략
# =========================================================

def morning_score(
    rt,
    hist,
    previous_snapshot=None,
):

    if not rt:
        return 0, {}

    if len(hist) < 20:
        return 0, {}

    price = rt["price"]

    if price <= 0:
        return 0, {}

    change = rt["change_pct"]

    score = 0

    # =====================================================
    # 1. 상승률
    # =====================================================

    if 1.0 <= change <= 5.0:
        score += 15

    elif 0.5 <= change < 1.0:
        score += 8

    elif 5.0 < change <= 8.0:
        score += 8

    elif change > 10:
        score -= 10

    elif change < 0:
        score -= 10

    # =====================================================
    # 2. 거래대금
    # =====================================================

    turnover = rt["turnover"]

    if turnover >= 5_000_000_000:
        score += 15

    elif turnover >= 2_000_000_000:
        score += 10

    elif turnover >= 1_000_000_000:
        score += 6

    elif turnover < MIN_TURNOVER:
        score -= 8

    # =====================================================
    # 3. 장중 고점 유지
    # =====================================================

    high = rt["high"]
    low = rt["low"]

    hold = 0.5

    if (
        high > 0
        and low > 0
        and high > low
    ):

        hold = (
            price - low
        ) / (
            high - low
        )

    if hold >= 0.85:
        score += 15

    elif hold >= 0.70:
        score += 8

    elif hold < 0.45:
        score -= 12

    # =====================================================
    # 4. 일봉 추세
    # =====================================================

    closes = [
        x["close"]
        for x in hist
        if x["close"] > 0
    ]

    ma5 = sum(
        closes[:5]
    ) / 5

    ma20 = sum(
        closes[:20]
    ) / 20

    if ma5 > ma20:
        score += 10

    if price > ma5:
        score += 8

    # =====================================================
    # 5. 5일 모멘텀
    # =====================================================

    ret5 = pct(
        closes[0],
        closes[5]
    )

    if 0 < ret5 < 12:
        score += 8

    elif ret5 >= 12:
        score += 3

    # =====================================================
    # 6. 전고점 접근
    # =====================================================

    previous_high = max(
        x["high"]
        for x in hist[1:21]
        if x["high"] > 0
    )

    if previous_high > 0:

        if price >= previous_high:
            score += 15

        elif price >= previous_high * 0.985:
            score += 8

    # =====================================================
    # 7. 이전 실행보다 가격이 올라가는지
    # =====================================================

    momentum = 0

    if previous_snapshot:

        previous_price = num(
            previous_snapshot.get(
                "price"
            )
        )

        previous_volume = num(
            previous_snapshot.get(
                "volume"
            )
        )

        if (
            previous_price > 0
            and price > previous_price
        ):

            momentum += 5

        if (
            previous_volume > 0
            and rt["volume"]
            > previous_volume
        ):

            momentum += 5

    score += momentum

    # =====================================================
    # 8. 과도한 추격 방지
    # =====================================================

    if change >= 9:
        score -= 10

    if hold < 0.50:
        score -= 8

    score = max(
        0,
        min(
            100,
            int(score)
        )
    )

    metrics = {
        "change_pct": change,
        "turnover": turnover,
        "hold": hold,
        "ma5": ma5,
        "ma20": ma20,
        "ret5": ret5,
        "previous_high": previous_high,
        "momentum": momentum,
    }

    return score, metrics


# =========================================================
# 후보 풀
# =========================================================

def build_pool():

    pool = {}

    for category in (
        "KOSPI",
        "KOSDAQ",
    ):

        for sort_type in (
            "quantTop",
            "up",
        ):

            rows = get_rankings(
                sort_type,
                category,
                pages=2,
                page_size=100,
            )

            for item in rows:

                if item["price"] < MIN_PRICE:
                    continue

                pool[
                    item["code"]
                ] = item

    print(
        f"전체 후보 풀: {len(pool)}개"
    )

    return pool


# =========================================================
# 종가 후보 탐색
# =========================================================

def screen_close():

    print()
    print(
        "===================================="
    )
    print(
        " CLOSE BUY HUNTER"
    )
    print(
        " 15:20 종가 매수 후보"
    )
    print(
        "===================================="
    )

    pool = build_pool()

    if not pool:

        send_telegram(
            "⚠️ <b>[종가 헌터 데이터 오류]</b>\n"
            "시장 종목 데이터를 가져오지 못했습니다."
        )

        return

    rough = sorted(
        pool.values(),
        key=lambda x: (
            x.get("turnover", 0),
            x.get("change_pct", 0),
        ),
        reverse=True,
    )

    rough = rough[
        :MAX_CANDIDATES
    ]

    candidates = []

    checked = 0
    history_fail = 0
    score_fail = 0

    for item in rough:

        rt = get_realtime(
            item["code"]
        )

        if not rt:
            continue

        if rt["price"] < MIN_PRICE:
            continue

        if (
            rt["turnover"] > 0
            and rt["turnover"]
            < MIN_TURNOVER
        ):
            continue

        hist = get_history(
            item["code"]
        )

        if len(hist) < 20:

            history_fail += 1

            continue

        checked += 1

        score, metrics = close_score(
            rt,
            hist,
        )

        print(
            f"{item['name']} "
            f"{item['code']} "
            f"score={score} "
            f"change={rt['change_pct']:+.2f}% "
            f"turnover="
            f"{rt['turnover']/100_000_000:.0f}억"
        )

        if score >= CLOSE_THRESHOLD:

            merged = dict(item)

            merged.update(rt)

            candidates.append(
                (
                    score,
                    merged,
                    metrics,
                    hist,
                )
            )

        else:

            score_fail += 1

        time.sleep(0.08)

    candidates.sort(
        key=lambda x: (
            x[0],
            x[1]["turnover"],
            x[1]["change_pct"],
        ),
        reverse=True,
    )

    candidates = candidates[
        :MAX_RESULTS
    ]

    print(
        f"검사={checked} "
        f"후보={len(candidates)} "
        f"history_fail={history_fail} "
        f"score_fail={score_fail}"
    )

    if not candidates:

        print(
            "종가 후보 없음"
        )

        return

    state = load_state()

    today = today_key()

    blocks = []

    new_count = 0

    for (
        score,
        item,
        metrics,
        hist,
    ) in candidates:

        code = item["code"]

        signal_key = (
            f"{today}:close:{code}"
        )

        if state[
            "sent_signals"
        ].get(signal_key):

            continue

        entry, sl, tp1, tp2 = make_levels(
            item["price"],
            hist,
        )

        state["sent_signals"][
            signal_key
        ] = {
            "sent_at": now_kst().isoformat(),
            "score": score,
        }

        state["positions"][
            code
        ] = {
            "code": code,
            "name": item["name"],
            "mode": "close",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "score": score,
            "status": "ACTIVE",
            "created_at": now_kst().isoformat(),
            "tp1_hit": False,
        }

        blocks.append(
            format_signal(
                item,
                "close",
                score,
                metrics,
                entry,
                sl,
                tp1,
                tp2,
            )
        )

        new_count += 1

    if blocks:

        header = (
            "📡 <b>[한국 주식 상승 헌터]</b>\n"
            "🔵 <b>[종가 매수 후보]</b>\n"
            f"🕒 {now_kst().strftime('%H:%M:%S')}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⏰ 15:20 종가 매수 판단용\n\n"
        )

        send_telegram(
            header
            + "\n\n".join(blocks)
        )

        save_state(state)

        print(
            f"종가 신호 {new_count}개 전송"
        )


# =========================================================
# 장초 후보 탐색
# =========================================================

def screen_morning():

    print()
    print(
        "===================================="
    )
    print(
        " MORNING RISE HUNTER"
    )
    print(
        " 09:00~09:30 장초 상승 후보"
    )
    print(
        "===================================="
    )

    pool = build_pool()

    if not pool:

        send_telegram(
            "⚠️ <b>[장초 헌터 데이터 오류]</b>\n"
            "시장 종목 데이터를 가져오지 못했습니다."
        )

        return

    state = load_state()

    rough = sorted(
        pool.values(),
        key=lambda x: (
            x.get("turnover", 0),
            x.get("change_pct", 0),
        ),
        reverse=True,
    )

    rough = rough[
        :MAX_CANDIDATES
    ]

    candidates = []

    for item in rough:

        rt = get_realtime(
            item["code"]
        )

        if not rt:
            continue

        if rt["price"] < MIN_PRICE:
            continue

        if (
            rt["turnover"] > 0
            and rt["turnover"]
            < MIN_TURNOVER
        ):
            continue

        hist = get_history(
            item["code"]
        )

        if len(hist) < 20:
            continue

        previous_snapshot = (
            state["snapshots"].get(
                item["code"]
            )
        )

        score, metrics = morning_score(
            rt,
            hist,
            previous_snapshot,
        )

        # 현재 가격을 다음 실행의 비교 기준으로 저장
        state["snapshots"][
            item["code"]
        ] = {
            "price": rt["price"],
            "volume": rt["volume"],
            "change_pct": rt["change_pct"],
            "time": now_kst().isoformat(),
        }

        if score >= MORNING_THRESHOLD:

            candidates.append(
                (
                    score,
                    rt,
                    metrics,
                    hist,
                )
            )

        time.sleep(0.08)

    candidates.sort(
        key=lambda x: (
            x[0],
            x[1]["turnover"],
            x[1]["change_pct"],
        ),
        reverse=True,
    )

    candidates = candidates[
        :MAX_RESULTS
    ]

    if not candidates:

        print(
            "장초 후보 없음"
        )

        save_state(state)

        return

    today = today_key()

    blocks = []

    for (
        score,
        rt,
        metrics,
        hist,
    ) in candidates:

        code = rt["code"]

        signal_key = (
            f"{today}:morning:{code}"
        )

        # 같은 날 같은 종목은 한 번만 알림
        if state[
            "sent_signals"
        ].get(signal_key):

            continue

        entry, sl, tp1, tp2 = make_levels(
            rt["price"],
            hist,
        )

        state[
            "sent_signals"
        ][signal_key] = {
            "sent_at": now_kst().isoformat(),
            "score": score,
        }

        state[
            "positions"
        ][code] = {
            "code": code,
            "name": rt["name"],
            "mode": "morning",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "score": score,
            "status": "ACTIVE",
            "created_at": now_kst().isoformat(),
            "tp1_hit": False,
        }

        blocks.append(
            format_signal(
                rt,
                "morning",
                score,
                metrics,
                entry,
                sl,
                tp1,
                tp2,
            )
        )

    if blocks:

        header = (
            "📡 <b>[한국 주식 상승 헌터]</b>\n"
            "🟢 <b>[장초 상승 후보]</b>\n"
            f"🕒 {now_kst().strftime('%H:%M:%S')}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⏰ 장초 상승 모멘텀 후보\n\n"
        )

        send_telegram(
            header
            + "\n\n".join(blocks)
        )

    save_state(state)


# =========================================================
# Signal 메시지
# =========================================================

def format_signal(
    item,
    mode,
    score,
    metrics,
    entry,
    sl,
    tp1,
    tp2,
):

    if mode == "close":

        title = (
            "🔵 <b>[종가 매수 후보]</b>"
        )

        note = (
            "⏰ 15:20 종가 매수 판단용"
        )

    else:

        title = (
            "🟢 <b>[장초 상승 후보]</b>"
        )

        note = (
            "⏰ 장초 상승 모멘텀 후보"
        )

    turnover = (
        metrics.get(
            "turnover",
            0
        )
        / 100_000_000
    )

    volume_ratio = metrics.get(
        "volume_ratio",
        0
    )

    if volume_ratio:
        volume_text = (
            f"{volume_ratio:.1f}배"
        )
    else:
        volume_text = "실시간 증가 확인"

    url = (
        "https://finance.naver.com/"
        f"item/main.naver?code="
        f"{item['code']}"
    )

    return (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{item['name']}</b> "
        f"<code>({item['code']})</code>\n"
        f"💰 현재가: "
        f"<code>{entry:,}원</code>\n"
        f"📈 등락률: "
        f"<code>{metrics.get('change_pct', 0):+.2f}%</code>\n"
        f"💵 거래대금: "
        f"<code>{turnover:,.0f}억원</code>\n"
        f"📊 신호점수: "
        f"<code>{score}/100</code>\n"
        f"📦 거래량: "
        f"<code>{volume_text}</code>\n"
        f"📈 5일 수익률: "
        f"<code>{metrics.get('ret5', 0):+.2f}%</code>\n"
        f"💰 ENTRY: "
        f"<code>{entry:,}원</code>\n"
        f"🛡 SL: "
        f"<code>{sl:,}원</code>\n"
        f"🎯 TP1: "
        f"<code>{tp1:,}원</code>\n"
        f"🎯 TP2: "
        f"<code>{tp2:,}원</code>\n"
        f"🔗 <a href='{url}'>네이버 차트</a>\n"
        f"{note}\n"
        "⚠️ 조건 기반 후보이며 "
        "수익을 보장하지 않습니다."
    )


# =========================================================
# 보유 후보 감시
# =========================================================

def monitor_positions():

    state = load_state()

    positions = state.get(
        "positions",
        {}
    )

    if not positions:
        return

    changed = False

    for code, pos in list(
        positions.items()
    ):

        if pos.get(
            "status"
        ) != "ACTIVE":

            continue

        rt = get_realtime(code)

        if not rt:
            continue

        price = rt["price"]

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

        # TP2
        if price >= tp2:

            send_telegram(
                "🔥 <b>[TP2 도달]</b>\n"
                f"📌 <b>{pos['name']}</b>\n"
                f"💰 ENTRY: "
                f"<code>{entry:,.0f}</code>\n"
                f"🎯 TP2: "
                f"<code>{tp2:,.0f}</code>\n"
                f"📈 현재가: "
                f"<code>{price:,.0f}</code>\n"
                f"📊 수익률: "
                f"<code>{pct(price, entry):+.2f}%</code>\n"
                "✅ 감시 종료"
            )

            pos["status"] = "TP2"

            changed = True

            continue

        # TP1
        if (
            price >= tp1
            and not pos.get(
                "tp1_hit",
                False
            )
        ):

            send_telegram(
                "🎯 <b>[TP1 도달]</b>\n"
                f"📌 <b>{pos['name']}</b>\n"
                f"💰 ENTRY: "
                f"<code>{entry:,.0f}</code>\n"
                f"🎯 TP1: "
                f"<code>{tp1:,.0f}</code>\n"
                f"📈 현재가: "
                f"<code>{price:,.0f}</code>\n"
                f"📊 수익률: "
                f"<code>{pct(price, entry):+.2f}%</code>\n"
                f"➡️ TP2: "
                f"<code>{tp2:,.0f}</code>"
            )

            pos["tp1_hit"] = True

            changed = True

        # SL
        if price <= sl:

            send_telegram(
                "🛡️ <b>[SL 도달]</b>\n"
                f"📌 <b>{pos['name']}</b>\n"
                f"💰 ENTRY: "
                f"<code>{entry:,.0f}</code>\n"
                f"🛡 SL: "
                f"<code>{sl:,.0f}</code>\n"
                f"📉 현재가: "
                f"<code>{price:,.0f}</code>\n"
                f"📊 수익률: "
                f"<code>{pct(price, entry):+.2f}%</code>\n"
                "⚠️ 감시 종료"
            )

            pos["status"] = "SL"

            changed = True

    # 오래된 종료 포지션 정리
    cutoff = (
        now_kst()
        - timedelta(days=14)
    )

    for code, pos in list(
        positions.items()
    ):

        if pos.get(
            "status"
        ) not in (
            "TP2",
            "SL",
        ):
            continue

        try:

            created = datetime.fromisoformat(
                pos.get(
                    "created_at",
                    ""
                )
            )

            if created < cutoff:

                del positions[code]

                changed = True

        except Exception:
            pass

    if changed:

        state["positions"] = positions

        save_state(state)


# =========================================================
# 오래된 신호 정리
# =========================================================

def cleanup_state(state):

    cutoff = (
        now_kst()
        - timedelta(days=7)
    ).strftime(
        "%Y-%m-%d"
    )

    new_sent = {}

    for key, value in state[
        "sent_signals"
    ].items():

        if key[:10] >= cutoff:

            new_sent[key] = value

    state[
        "sent_signals"
    ] = new_sent

    # Snapshot은 현재 날짜 기준으로만 유지
    current_date = today_key()

    new_snapshots = {}

    for code, value in state[
        "snapshots"
    ].items():

        if value.get(
            "time",
            ""
        )[:10] == current_date:

            new_snapshots[
                code
            ] = value

    state[
        "snapshots"
    ] = new_snapshots


# =========================================================
# 실행 모드
# =========================================================

def get_mode():

    forced = os.getenv(
        "MODE",
        ""
    ).strip().lower()

    if forced in (
        "morning",
        "close",
        "monitor",
    ):

        return forced

    t = now_kst()

    hm = (
        t.hour * 60
        + t.minute
    )

    # 장초
    if (
        9 * 60
        <= hm
        <= 9 * 60 + 30
    ):

        return "morning"

    # 종가
    if (
        15 * 60 + 5
        <= hm
        <= 15 * 60 + 25
    ):

        return "close"

    return "monitor"


# =========================================================
# 메인
# =========================================================

def run():

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

        raise RuntimeError(
            "TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다."
        )

    state = load_state()

    cleanup_state(state)

    mode = get_mode()

    print(
        "===================================="
    )

    print(
        " KOREA STOCK HUNTER V2"
    )

    print(
        f" KST: {now_kst().isoformat()}"
    )

    print(
        f" MODE: {mode}"
    )

    print(
        "===================================="
    )

    # 기존 후보 감시
    monitor_positions()

    if mode == "morning":

        screen_morning()

    elif mode == "close":

        screen_close()

    state = load_state()

    state[
        "last_run"
    ] = now_kst().isoformat()

    save_state(state)

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
    run()
