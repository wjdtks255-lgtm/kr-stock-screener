import os
import re
import json
import html
import time
import requests
import pandas as pd

from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta


# ============================================================
# KOREA STOCK HUNTER V4
#
# MORNING
# 09:05 ~ 09:30
# 장초 상승 후보
#
# CLOSE
# 15:10 ~ 15:20
# 종가 / 다음날 후보
# ============================================================

KST = timezone(timedelta(hours=9))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

STATE_FILE = "bot_state.json"

MIN_PRICE = 1000
MIN_TURNOVER = 200_000_000

MAX_POOL = 120
MAX_RESULTS = 3
MIN_SCORE = 40

TIMEOUT = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

NAVER_PC = "https://finance.naver.com"

REALTIME_URL = (
    "https://polling.finance.naver.com/"
    "api/realtime/domestic/stock"
)

MOBILE_HISTORY_URL = (
    "https://m.stock.naver.com/api/stock/{code}/price"
)

LEGACY_HISTORY_URL = (
    "https://api.finance.naver.com/siseJson.naver"
)


# ============================================================
# TIME
# ============================================================

def now_kst():
    return datetime.now(KST)


def today():
    return now_kst().strftime("%Y%m%d")


def today_iso():
    return now_kst().strftime("%Y-%m-%d")


def hm():
    return now_kst().strftime("%H:%M")


def auto_mode():

    t = now_kst().hour * 60 + now_kst().minute

    # 09:05 ~ 09:30
    if 545 <= t <= 570:
        return "morning"

    # 15:10 ~ 15:20
    if 910 <= t <= 920:
        return "close"

    return "monitor"


def final_scan(mode):

    t = now_kst().hour * 60 + now_kst().minute

    if mode == "morning":
        return t >= 570

    if mode == "close":
        return t >= 920

    return False


# ============================================================
# HTTP SESSION
# ============================================================

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get(url, params=None):

    try:

        r = SESSION.get(
            url,
            params=params,
            timeout=TIMEOUT
        )

        r.raise_for_status()

        return r

    except Exception as e:

        print(
            f"GET 실패: {url}"
        )

        print(
            f"  {e}"
        )

        return None


def get_json(url, params=None):

    r = get(
        url,
        params
    )

    if not r:
        return None

    try:
        return r.json()

    except Exception as e:

        print(
            "JSON 변환 실패:",
            e
        )

        return None


# ============================================================
# NUMBER
# ============================================================

def num(value, default=0.0):

    if value is None:
        return default

    if isinstance(
        value,
        (int, float)
    ):
        return float(value)

    s = str(value).strip()

    if not s:
        return default

    s = (
        s.replace(",", "")
         .replace("%", "")
         .replace("+", "")
         .replace("원", "")
         .replace(" ", "")
    )

    try:
        return float(s)

    except Exception:
        return default


def money(value):

    value = num(value)

    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}억"

    if value >= 10_000:
        return f"{value / 10_000:.0f}만"

    return f"{value:,.0f}"


def pct(value):
    return f"{value:+.2f}%"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "Telegram Secret 없음"
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:

        r = SESSION.post(
            url,
            json=payload,
            timeout=TIMEOUT
        )

        if not r.ok:

            print(
                "Telegram 실패:",
                r.status_code,
                r.text[:500]
            )

            return False

        print(
            "Telegram SENT"
        )

        return True

    except Exception as e:

        print(
            "Telegram ERROR:",
            e
        )

        return False


# ============================================================
# PC NAVER STOCK LIST
# ============================================================

def get_ranking_page(kind, sosok):

    if kind == "quant":

        path = "/sise/sise_quant.naver"

    elif kind == "rise":

        path = "/sise/sise_rise.naver"

    else:

        return None

    url = NAVER_PC + path

    params = {
        "sosok": sosok,
        "page": 1,
    }

    return get(
        url,
        params
    )


def parse_stock_links(response, market):

    if not response:
        return []

    try:

        response.encoding = "euc-kr"

        soup = BeautifulSoup(
            response.text,
            "lxml"
        )

    except Exception as e:

        print(
            "HTML parse 실패:",
            e
        )

        return []

    results = []
    seen = set()

    # 실제 종목 상세 링크
    links = soup.select(
        'a[href*="/item/main.naver?code="]'
    )

    for a in links:

        href = a.get(
            "href",
            ""
        )

        match = re.search(
            r"code=(\d{6})",
            href
        )

        if not match:
            continue

        code = match.group(1)

        if code in seen:
            continue

        name = a.get_text(
            strip=True
        )

        if not name:
            continue

        seen.add(code)

        results.append({
            "code": code,
            "name": name,
            "market": market,
        })

    return results


def get_ranking(kind, sosok, market):

    response = get_ranking_page(
        kind,
        sosok
    )

    items = parse_stock_links(
        response,
        market
    )

    print(
        f"{market} {kind}: "
        f"{len(items)}개"
    )

    return items


def get_candidate_pool():

    all_items = {}

    # KOSPI
    for kind in [
        "quant",
        "rise"
    ]:

        items = get_ranking(
            kind,
            "0",
            "KOSPI"
        )

        for item in items:

            code = item["code"]

            if code not in all_items:

                all_items[code] = item

    # KOSDAQ
    for kind in [
        "quant",
        "rise"
    ]:

        items = get_ranking(
            kind,
            "1",
            "KOSDAQ"
        )

        for item in items:

            code = item["code"]

            if code not in all_items:

                all_items[code] = item

    pool = list(
        all_items.values()
    )

    print(
        f"전체 후보 풀: {len(pool)}개"
    )

    return pool


# ============================================================
# REALTIME
# ============================================================

def parse_realtime_data(data):

    if not data:
        return None

    try:

        datas = data.get(
            "datas",
            []
        )

        if not datas:
            return None

        d = datas[0]

        return {
            "price": num(
                d.get("closePrice")
            ),

            "change_pct": num(
                d.get(
                    "fluctuationsRatio"
                )
            ),

            "open": num(
                d.get("openPrice")
            ),

            "high": num(
                d.get("highPrice")
            ),

            "low": num(
                d.get("lowPrice")
            ),

            "volume": num(
                d.get(
                    "accumulatedTradingVolume"
                )
            ),

            "turnover": num(
                d.get(
                    "accumulatedTradingValue"
                )
            ),

            "trade_time": str(
                d.get(
                    "localTradedAt",
                    ""
                )
            ),

            "market_status": str(
                d.get(
                    "marketStatus",
                    ""
                )
            ),
        }

    except Exception as e:

        print(
            "Realtime parse 실패:",
            e
        )

        return None


def get_realtime(code):

    # 1차: query 방식
    data = get_json(
        REALTIME_URL,
        {
            "query":
                f"SERVICE_ITEM:{code}"
        }
    )

    result = parse_realtime_data(
        data
    )

    if result:
        return result

    # 2차: code path 방식
    data = get_json(
        f"{REALTIME_URL}/{code}"
    )

    return parse_realtime_data(
        data
    )


# ============================================================
# HISTORY - MOBILE
# ============================================================

def parse_mobile_history(data):

    if not data:
        return []

    if isinstance(
        data,
        dict
    ):

        for key in [
            "price",
            "prices",
            "result",
            "data",
        ]:

            value = data.get(
                key
            )

            if isinstance(
                value,
                list
            ):

                data = value

                break

    if not isinstance(
        data,
        list
    ):

        return []

    result = []

    for d in data:

        try:

            date = str(
                d.get(
                    "localTradedAt",
                    ""
                )
            )[:10]

            close = num(
                d.get("closePrice")
            )

            high = num(
                d.get("highPrice")
            )

            low = num(
                d.get("lowPrice")
            )

            volume = num(
                d.get(
                    "accumulatedTradingVolume"
                )
            )

            if close <= 0:
                continue

            result.append({
                "date": date,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
            })

        except Exception:
            continue

    result.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    return result


def get_history_mobile(code):

    url = (
        MOBILE_HISTORY_URL.format(
            code=code
        )
    )

    data = get_json(
        url,
        {
            "pageSize": 35,
            "page": 1,
        }
    )

    return parse_mobile_history(
        data
    )


# ============================================================
# HISTORY - LEGACY FALLBACK
# ============================================================

def get_history_legacy(code):

    params = {
        "symbol": code,
        "requestType": 1,
        "startTime": (
            datetime.now(KST)
            - timedelta(days=90)
        ).strftime("%Y%m%d"),
        "endTime": today(),
        "timeframe": "day",
    }

    r = get(
        LEGACY_HISTORY_URL,
        params
    )

    if not r:
        return []

    text = r.text.strip()

    if not text:
        return []

    # JSON-like array parser
    try:

        data = json.loads(
            text
        )

    except Exception:

        try:

            # Naver legacy response may contain
            # JavaScript-like array
            data = eval(
                text,
                {
                    "__builtins__": {}
                },
                {}
            )

        except Exception:

            return []

    result = []

    for row in data:

        if not isinstance(
            row,
            list
        ):
            continue

        if len(row) < 6:
            continue

        try:

            date = str(
                row[0]
            )[:10]

            result.append({
                "date": date,
                "open": num(row[1]),
                "high": num(row[2]),
                "low": num(row[3]),
                "close": num(row[4]),
                "volume": num(row[5]),
            })

        except Exception:
            continue

    result.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    return result


def get_history(code):

    result = get_history_mobile(
        code
    )

    if len(result) >= 5:
        return result

    print(
        f"{code}: mobile history fallback"
    )

    return get_history_legacy(
        code
    )


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    rt,
    history,
    mode
):

    score = 0
    reasons = []

    price = rt["price"]
    open_price = rt["open"]
    high = rt["high"]
    low = rt["low"]

    change = rt["change_pct"]
    turnover = rt["turnover"]

    # 현재가 위치
    hold = 0.5

    if high > low:

        hold = (
            price - low
        ) / (
            high - low
        )

    # ----------------------------------------
    # 현재가 / 시가
    # ----------------------------------------

    if (
        open_price > 0
        and price >= open_price
    ):

        score += 8

        reasons.append(
            "시가 위"
        )

    if hold >= 0.85:

        score += 10

        reasons.append(
            "고가권 유지"
        )

    elif hold >= 0.70:

        score += 6

        reasons.append(
            "고가권"
        )

    # ----------------------------------------
    # 거래대금
    # ----------------------------------------

    if turnover >= 5_000_000_000:

        score += 12

        reasons.append(
            "거래대금 50억+"
        )

    elif turnover >= 2_000_000_000:

        score += 9

        reasons.append(
            "거래대금 20억+"
        )

    elif turnover >= 500_000_000:

        score += 6

        reasons.append(
            "거래대금 5억+"
        )

    elif turnover >= 200_000_000:

        score += 3

    # ----------------------------------------
    # 일봉
    # ----------------------------------------

    ret5 = 0
    vol_ratio = 1

    if len(history) >= 5:

        closes = [
            x["close"]
            for x in history
        ]

        if closes[4] > 0:

            ret5 = (
                price / closes[4]
                - 1
            ) * 100

            if ret5 >= 3:

                score += 8

                reasons.append(
                    "5일 상승"
                )

            elif ret5 >= 1:

                score += 5

                reasons.append(
                    "5일 상승세"
                )

            elif ret5 < -5:

                score -= 5

    if len(history) >= 20:

        closes20 = [
            x["close"]
            for x in history[:20]
        ]

        ma20 = (
            sum(closes20)
            / len(closes20)
        )

        if price > ma20:

            score += 8

            reasons.append(
                "20일선 위"
            )

        ma5 = (
            sum(
                closes20[:5]
            ) / 5
        )

        if ma5 > ma20:

            score += 10

            reasons.append(
                "5일선>20일선"
            )

        previous_highs = [
            x["high"]
            for x in history[1:21]
            if x["high"] > 0
        ]

        if previous_highs:

            highest = max(
                previous_highs
            )

            if price >= highest:

                score += 12

                reasons.append(
                    "20일 고점 돌파"
                )

            elif price >= highest * 0.98:

                score += 7

                reasons.append(
                    "20일 고점 근접"
                )

    if len(history) >= 10:

        old_volumes = [
            x["volume"]
            for x in history[1:21]
            if x["volume"] > 0
        ]

        if old_volumes:

            avg_volume = (
                sum(old_volumes)
                / len(old_volumes)
            )

            if avg_volume > 0:

                vol_ratio = (
                    rt["volume"]
                    / avg_volume
                )

                if vol_ratio >= 2:

                    score += 10

                    reasons.append(
                        "거래량 2배+"
                    )

                elif vol_ratio >= 1.5:

                    score += 7

                    reasons.append(
                        "거래량 증가"
                    )

    # ----------------------------------------
    # MORNING
    # ----------------------------------------

    if mode == "morning":

        if change >= 2:

            score += 12

            reasons.append(
                "장초 상승"
            )

        elif change >= 0.5:

            score += 7

            reasons.append(
                "장초 강세"
            )

        elif change < 0:

            score -= 10

        if change > 12:

            score -= 8

            reasons.append(
                "과열 주의"
            )

    # ----------------------------------------
    # CLOSE
    # ----------------------------------------

    if mode == "close":

        if change >= 2:

            score += 10

            reasons.append(
                "종가 상승"
            )

        elif change >= 0.5:

            score += 6

        elif change <= 0:

            score -= 10

        if hold >= 0.85:

            score += 5

            reasons.append(
                "종가 고가권"
            )

        if change > 10:

            score -= 8

            reasons.append(
                "과열 주의"
            )

    score = max(
        0,
        min(
            100,
            score
        )
    )

    return {
        "score": score,
        "hold": hold,
        "ret5": ret5,
        "vol_ratio": vol_ratio,
        "reasons": reasons,
    }


# ============================================================
# LEVEL
# ============================================================

def levels(price):

    return {
        "entry": price,
        "sl": price * 0.965,
        "tp1": price * 1.03,
        "tp2": price * 1.06,
    }


# ============================================================
# STATE
# ============================================================

def load_state():

    if not os.path.exists(
        STATE_FILE
    ):

        return {
            "positions": {},
            "sent_signals": {},
            "last_run": "",
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        state.setdefault(
            "positions",
            {}
        )

        state.setdefault(
            "sent_signals",
            {}
        )

        return state

    except Exception:

        return {
            "positions": {},
            "sent_signals": {},
            "last_run": "",
        }


def save_state(state):

    state["last_run"] = (
        now_kst().isoformat()
    )

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


def cleanup_state(state):

    key_today = today_iso()

    old = []

    for key in state[
        "sent_signals"
    ]:

        if not key.startswith(
            key_today
        ):

            old.append(key)

    for key in old:

        del state[
            "sent_signals"
        ][key]


# ============================================================
# SCREEN
# ============================================================

def screen(
    mode,
    force=False
):

    print("")
    print("=" * 36)

    if mode == "morning":

        print(
            " MORNING RISE HUNTER"
        )

        print(
            " 09:05~09:30 장초 상승 후보"
        )

    else:

        print(
            " CLOSE RISE HUNTER"
        )

        print(
            " 15:10~15:20 종가 후보"
        )

    print("=" * 36)

    state = load_state()

    cleanup_state(
        state
    )

    session_key = (
        f"{today_iso()}:{mode}"
    )

    if (
        not force
        and state[
            "sent_signals"
        ].get(session_key)
    ):

        print(
            "이미 신호 전송 완료"
        )

        save_state(
            state
        )

        return

    # ========================================
    # 종목 풀
    # ========================================

    pool = get_candidate_pool()

    if not pool:

        print("")
        print(
            "DATA SOURCE ERROR"
        )

        print(
            "종목 목록을 가져오지 못했습니다."
        )

        if final_scan(mode):

            send_telegram(
                f"⚠️ <b>"
                f"{'장초' if mode == 'morning' else '종가'} "
                f"최종 스캔</b>\n\n"
                f"종목 데이터 수집 실패\n"
                f"시간: {hm()}"
            )

        return

    # 거래량/상승률 페이지에서 가져온
    # 순서 자체도 후보 선정에 활용
    pool = pool[
        :MAX_POOL
    ]

    print(
        f"검사 대상: {len(pool)}개"
    )

    candidates = []

    realtime_ok = 0
    history_ok = 0

    for idx, item in enumerate(
        pool,
        start=1
    ):

        code = item["code"]

        rt = get_realtime(
            code
        )

        if not rt:
            continue

        realtime_ok += 1

        price = rt["price"]

        if price < MIN_PRICE:
            continue

        # ====================================
        # 거래일 확인
        # ====================================

        trade_time = rt.get(
            "trade_time",
            ""
        )

        if trade_time:

            if not trade_time.startswith(
                today_iso()
            ):

                continue

        # ====================================
        # MODE FILTER
        # ====================================

        if mode == "morning":

            if rt[
                "change_pct"
            ] < 0:

                continue

        elif mode == "close":

            if rt[
                "change_pct"
            ] <= 0:

                continue

        if rt[
            "turnover"
        ] < MIN_TURNOVER:

            continue

        # ====================================
        # HISTORY
        # ====================================

        history = get_history(
            code
        )

        if history:

            history_ok += 1

        result = calculate_score(
            rt,
            history,
            mode
        )

        if (
            result["score"]
            < MIN_SCORE
        ):

            continue

        candidates.append({
            "code": code,
            "name": item["name"],
            "market": item["market"],
            "price": price,
            "change": rt[
                "change_pct"
            ],
            "turnover": rt[
                "turnover"
            ],
            "score": result[
                "score"
            ],
            "hold": result[
                "hold"
            ],
            "ret5": result[
                "ret5"
            ],
            "vol_ratio": result[
                "vol_ratio"
            ],
            "reasons": result[
                "reasons"
            ],
        })

        print(
            f"PASS "
            f"{item['name']} "
            f"{result['score']}점 "
            f"{pct(rt['change_pct'])}"
        )

        time.sleep(
            0.05
        )

    print("")
    print(
        f"Realtime OK : {realtime_ok}"
    )

    print(
        f"History OK  : {history_ok}"
    )

    print(
        f"조건 통과   : {len(candidates)}개"
    )

    # ========================================
    # RANK
    # ========================================

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["turnover"],
            x["change"]
        ),
        reverse=True
    )

    candidates = candidates[
        :MAX_RESULTS
    ]

    print("")
    print("=" * 36)
    print(" TOP CANDIDATES")
    print("=" * 36)

    for i, c in enumerate(
        candidates,
        1
    ):

        print(
            f"{i}. "
            f"{c['name']} "
            f"({c['code']}) "
            f"{c['score']}점 "
            f"{pct(c['change'])}"
        )

    # ========================================
    # NO CANDIDATE
    # ========================================

    if not candidates:

        print(
            "후보 없음"
        )

        # 마지막 검사에서만 알림
        if final_scan(mode):

            send_telegram(
                f"🔎 <b>"
                f"{'장초' if mode == 'morning' else '종가'} "
                f"최종 스캔</b>\n\n"
                f"조건을 만족하는 종목이 없습니다.\n"
                f"검사: {len(pool)}개\n"
                f"시간: {hm()}"
            )

        save_state(
            state
        )

        return

    # ========================================
    # TELEGRAM
    # ========================================

    if mode == "morning":

        title = (
            "🚀 장초 상승 후보"
        )

    else:

        title = (
            "🎯 종가 / 다음날 후보"
        )

    message = (
        f"<b>{title}</b>\n"
        f"시간: {hm()}\n"
        f"검사 종목: {len(pool)}개\n\n"
    )

    for i, c in enumerate(
        candidates,
        1
    ):

        lv = levels(
            c["price"]
        )

        reasons = ", ".join(
            c["reasons"][:6]
        )

        name = html.escape(
            c["name"]
        )

        message += (
            f"<b>{i}. {name}</b> "
            f"({c['code']})\n"
            f"{c['market']} / "
            f"<b>{c['score']}점</b>\n"
            f"현재가: "
            f"{c['price']:,.0f}원 "
            f"({pct(c['change'])})\n"
            f"거래대금: "
            f"{money(c['turnover'])}\n"
            f"5일: "
            f"{pct(c['ret5'])}\n"
            f"가격 위치: "
            f"{c['hold'] * 100:.0f}%\n"
            f"근거: {reasons}\n"
            f"────────────\n"
            f"ENTRY {lv['entry']:,.0f}\n"
            f"SL {lv['sl']:,.0f}\n"
            f"TP1 {lv['tp1']:,.0f}\n"
            f"TP2 {lv['tp2']:,.0f}\n\n"
        )

    message += (
        "⚠️ 후보 탐색 결과이며 "
        "수익을 보장하지 않습니다."
    )

    sent = send_telegram(
        message
    )

    if sent:

        state[
            "sent_signals"
        ][session_key] = {
            "time":
                now_kst().isoformat(),
            "codes": [
                c["code"]
                for c in candidates
            ],
            "scores": [
                c["score"]
                for c in candidates
            ],
        }

        for c in candidates:

            state[
                "positions"
            ][c["code"]] = {
                "name": c["name"],
                "mode": mode,
                "entry": c["price"],
                "sl":
                    c["price"] * 0.965,
                "tp1":
                    c["price"] * 1.03,
                "tp2":
                    c["price"] * 1.06,
                "created":
                    now_kst().isoformat(),
                "tp1_sent": False,
                "tp2_sent": False,
                "sl_sent": False,
            }

    save_state(
        state
    )


# ============================================================
# MONITOR
# ============================================================

def monitor():

    state = load_state()

    positions = state.get(
        "positions",
        {}
    )

    if not positions:

        print(
            "모니터링 포지션 없음"
        )

        return

    changed = False

    for code in list(
        positions.keys()
    ):

        p = positions[code]

        rt = get_realtime(
            code
        )

        if not rt:
            continue

        price = rt["price"]

        name = p.get(
            "name",
            code
        )

        # SL
        if (
            price <= p["sl"]
            and not p.get(
                "sl_sent",
                False
            )
        ):

            send_telegram(
                f"🛑 <b>SL 도달</b>\n\n"
                f"{html.escape(name)} "
                f"({code})\n"
                f"현재가: {price:,.0f}\n"
                f"SL: {p['sl']:,.0f}"
            )

            p["sl_sent"] = True
            changed = True

        # TP2
        elif (
            price >= p["tp2"]
            and not p.get(
                "tp2_sent",
                False
            )
        ):

            send_telegram(
                f"🎯 <b>TP2 도달</b>\n\n"
                f"{html.escape(name)} "
                f"({code})\n"
                f"현재가: {price:,.0f}\n"
                f"TP2: {p['tp2']:,.0f}"
            )

            p["tp2_sent"] = True
            changed = True

        # TP1
        elif (
            price >= p["tp1"]
            and not p.get(
                "tp1_sent",
                False
            )
        ):

            send_telegram(
                f"🎯 <b>TP1 도달</b>\n\n"
                f"{html.escape(name)} "
                f"({code})\n"
                f"현재가: {price:,.0f}\n"
                f"TP1: {p['tp1']:,.0f}"
            )

            p["tp1_sent"] = True
            changed = True

    if changed:

        save_state(
            state
        )


# ============================================================
# MAIN
# ============================================================

def main():

    requested = os.getenv(
        "MODE",
        ""
    ).strip().lower()

    force = (
        os.getenv(
            "FORCE_SCAN",
            ""
        ).strip().lower()
        in [
            "1",
            "true",
            "yes",
            "y"
        ]
    )

    if requested in [
        "morning",
        "close",
        "monitor"
    ]:

        mode = requested

    else:

        mode = auto_mode()

    print("=" * 36)

    print(
        " KOREA STOCK HUNTER V4"
    )

    print(
        f" KST: {now_kst().isoformat()}"
    )

    print(
        f" MODE: {mode}"
    )

    print(
        f" FORCE: {force}"
    )

    print("=" * 36)

    if mode == "morning":

        screen(
            "morning",
            force
        )

    elif mode == "close":

        screen(
            "close",
            force
        )

    else:

        monitor()

    print("=" * 36)

    print(
        " DONE"
    )

    print("=" * 36)


if __name__ == "__main__":
    main()
