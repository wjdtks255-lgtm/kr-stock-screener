import os
import re
import json
import html
import time
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta

# ============================================================
# KOREA STOCK HUNTER V3
#
# 1) MORNING
#    09:05 ~ 09:30
#    장초 상승 후보
#
# 2) CLOSE
#    15:10 ~ 15:20
#    종가 매수 후보 / 다음 거래일 상승 후보
#
# Data:
#    Naver Finance PC ranking pages
#    Naver realtime polling
#    Naver daily price API
# ============================================================

KST = timezone(timedelta(hours=9))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

STATE_FILE = "bot_state.json"

MIN_PRICE = 1000
MIN_TURNOVER = 200_000_000

MAX_POOL = 100
MAX_RESULTS = 3

MIN_SCORE = 42

REQUEST_TIMEOUT = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

PC_BASE = "https://finance.naver.com"

REALTIME_URL = (
    "https://polling.finance.naver.com/"
    "api/realtime/domestic/stock"
)

HISTORY_URL = (
    "https://m.stock.naver.com/api/stock/{code}/price"
)


# ============================================================
# TIME
# ============================================================

def now_kst():
    return datetime.now(KST)


def today_key():
    return now_kst().strftime("%Y-%m-%d")


def hm():
    return now_kst().strftime("%H:%M")


def auto_mode():
    now = now_kst()
    t = now.hour * 60 + now.minute

    # 09:05 ~ 09:30
    if 545 <= t <= 570:
        return "morning"

    # 15:10 ~ 15:20
    if 910 <= t <= 920:
        return "close"

    return "monitor"


def is_final_scan(mode):
    now = now_kst()
    t = now.hour * 60 + now.minute

    if mode == "morning":
        return t >= 570

    if mode == "close":
        return t >= 920

    return False


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM 설정 없음")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(
            url,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )

        if not r.ok:
            print("Telegram 실패:", r.status_code, r.text[:500])
            return False

        print("Telegram SENT")
        return True

    except Exception as e:
        print("Telegram ERROR:", e)
        return False


# ============================================================
# HTTP
# ============================================================

def get(url, params=None, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=timeout
        )

        r.raise_for_status()
        return r

    except Exception as e:
        print(f"GET 실패: {url}")
        print(" ", e)
        return None


def get_json(url, params=None):
    r = get(url, params)
    if not r:
        return None

    try:
        return r.json()
    except Exception as e:
        print("JSON 변환 실패:", e)
        return None


# ============================================================
# NUMBER
# ============================================================

def num(v, default=0.0):
    if v is None:
        return default

    if isinstance(v, (int, float)):
        return float(v)

    s = str(v).strip()

    if not s:
        return default

    s = (
        s.replace(",", "")
        .replace("%", "")
        .replace("+", "")
        .replace("원", "")
        .replace("억", "")
        .replace("만", "")
    )

    try:
        return float(s)
    except Exception:
        return default


def money(v):
    v = num(v)

    if v >= 100_000_000:
        return f"{v / 100_000_000:.1f}억"

    if v >= 10_000:
        return f"{v / 10_000:.0f}만"

    return f"{v:,.0f}"


def pct(v):
    return f"{v:+.2f}%"


# ============================================================
# NAME / CODE
# ============================================================

def clean_code(v):
    s = str(v).strip()

    m = re.search(r"\d{6}", s)

    if m:
        return m.group(0)

    return ""


# ============================================================
# NAVER PC RANKING
#
# 이것으로 기존 404 front-api를 완전히 대체한다.
# ============================================================

def get_pc_ranking(kind, market):
    """
    kind:
        quant = 거래량 상위
        rise  = 상승 상위

    market:
        KOSPI = sosok 0
        KOSDAQ = sosok 1
    """

    if kind == "quant":
        url = f"{PC_BASE}/sise/sise_quant.naver"

    elif kind == "rise":
        url = f"{PC_BASE}/sise/sise_rise.naver"

    else:
        return []

    params = {
        "sosok": market,
    }

    r = get(url, params)

    if not r:
        return []

    try:
        tables = pd.read_html(r.text)

    except Exception as e:
        print("HTML TABLE 실패:", e)
        return []

    results = []

    for table in tables:

        if table is None or table.empty:
            continue

        # MultiIndex column 정리
        if isinstance(table.columns, pd.MultiIndex):
            table.columns = [
                " ".join(
                    [
                        str(x)
                        for x in col
                        if str(x) != "nan"
                    ]
                ).strip()
                for col in table.columns
            ]

        table.columns = [
            str(x).strip()
            for x in table.columns
        ]

        # 종목명 column 찾기
        name_col = None

        for col in table.columns:
            if "종목명" in col:
                name_col = col
                break

        if name_col is None:
            continue

        for _, row in table.iterrows():

            name = str(row.get(name_col, "")).strip()

            if not name or name == "nan":
                continue

            # 종목명에서 code 추출
            code = ""

            # pandas HTML에서는 종목명이 링크로 존재하므로
            # href를 다시 찾기 어렵기 때문에 URL 기반 table을
            # 한 번 더 처리한다.
            try:
                raw_name = str(row.to_dict())
                code_match = re.search(
                    r"\b\d{6}\b",
                    raw_name
                )

                if code_match:
                    code = code_match.group(0)

            except Exception:
                pass

            # 기본 table만으로 code를 못 얻는 경우
            # 실제 HTML에서 종목 링크 검색
            if not code:
                try:
                    html_text = r.text

                    pattern = (
                        r'href="/item/main.naver\?code='
                        r'(\d{6})"[^>]*>'
                        r'\s*'
                        + re.escape(name)
                    )

                    m = re.search(
                        pattern,
                        html_text
                    )

                    if m:
                        code = m.group(1)

                except Exception:
                    pass

            if not code:
                continue

            item = {
                "code": code,
                "name": name,
                "market": (
                    "KOSPI"
                    if market == "0"
                    else "KOSDAQ"
                ),
            }

            # 거래량
            for col in table.columns:
                c = str(col)

                if "거래량" in c:
                    item["rank_volume"] = num(
                        row.get(col)
                    )

                if "거래대금" in c:
                    item["rank_turnover"] = num(
                        row.get(col)
                    )

                if "현재가" in c:
                    item["rank_price"] = num(
                        row.get(col)
                    )

                if "등락률" in c:
                    item["rank_change"] = num(
                        row.get(col)
                    )

            results.append(item)

    return results


# ============================================================
# PC RANKING FALLBACK
#
# 종목 링크에서 code를 확실하게 가져오는 방식
# ============================================================

def get_pc_ranking_links(kind, market):
    if kind == "quant":
        path = "/sise/sise_quant.naver"

    elif kind == "rise":
        path = "/sise/sise_rise.naver"

    else:
        return []

    params = {
        "sosok": market,
    }

    r = get(
        PC_BASE + path,
        params
    )

    if not r:
        return []

    results = []

    pattern = re.compile(
        r'href="/item/main\.naver\?code=(\d{6})"'
        r'[^>]*>(.*?)</a>',
        re.S
    )

    seen = set()

    for m in pattern.finditer(r.text):

        code = m.group(1)

        if code in seen:
            continue

        name = re.sub(
            r"<.*?>",
            "",
            m.group(2)
        )

        name = (
            name
            .replace("&nbsp;", " ")
            .strip()
        )

        if not name:
            continue

        seen.add(code)

        results.append({
            "code": code,
            "name": name,
            "market": (
                "KOSPI"
                if market == "0"
                else "KOSDAQ"
            ),
        })

    return results


def get_candidate_pool():
    all_items = {}

    for market in ["0", "1"]:

        for kind in ["quant", "rise"]:

            print(
                f"Ranking: "
                f"{'KOSPI' if market == '0' else 'KOSDAQ'} "
                f"{kind}"
            )

            items = get_pc_ranking_links(
                kind,
                market
            )

            print(
                f"  → {len(items)}개"
            )

            for item in items:

                code = item["code"]

                if code not in all_items:
                    all_items[code] = item

    pool = list(all_items.values())

    print(
        f"전체 후보 풀: {len(pool)}개"
    )

    return pool


# ============================================================
# REALTIME
# ============================================================

def get_realtime(code):

    # 기존 국내 stock endpoint
    url = (
        f"{REALTIME_URL}/{code}"
    )

    data = get_json(url)

    if not data:
        # fallback: query 방식
        url2 = REALTIME_URL

        params = {
            "query": f"SERVICE_ITEM:{code}"
        }

        data = get_json(
            url2,
            params
        )

    if not data:
        return None

    try:
        datas = data.get("datas", [])

        if not datas:
            return None

        d = datas[0]

        result = {
            "price": num(
                d.get("closePrice")
            ),
            "prev_close": num(
                d.get(
                    "compareToPreviousClosePrice"
                )
            ),
            "change_pct": num(
                d.get("fluctuationsRatio")
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
                d.get("accumulatedTradingVolume")
            ),
            "turnover": num(
                d.get("accumulatedTradingValue")
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

        return result

    except Exception as e:
        print(
            f"Realtime parse 실패 {code}:",
            e
        )
        return None


# ============================================================
# DAILY HISTORY
# ============================================================

def get_history(code, page_size=35):

    url = HISTORY_URL.format(
        code=code
    )

    params = {
        "pageSize": page_size,
        "page": 1,
    }

    data = get_json(
        url,
        params
    )

    if not data:
        return []

    if isinstance(data, dict):

        for key in [
            "price",
            "prices",
            "result",
            "data",
        ]:

            if isinstance(
                data.get(key),
                list
            ):
                data = data[key]
                break

    if not isinstance(data, list):
        return []

    rows = []

    for d in data:

        try:

            rows.append({
                "date": str(
                    d.get(
                        "localTradedAt",
                        ""
                    )
                )[:10],

                "close": num(
                    d.get("closePrice")
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

            })

        except Exception:
            continue

    rows = [
        x for x in rows
        if x["close"] > 0
    ]

    rows.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    return rows


# ============================================================
# SCORE
# ============================================================

def calculate_score(rt, hist, mode):

    price = rt["price"]
    open_price = rt["open"]
    high = rt["high"]
    low = rt["low"]
    change = rt["change_pct"]
    turnover = rt["turnover"]

    score = 0
    reasons = []

    hold = 0.5

    if high > low:
        hold = (
            (price - low)
            / (high - low)
        )

    # --------------------------------------------------------
    # REALTIME
    # --------------------------------------------------------

    if price >= open_price and open_price > 0:
        score += 8
        reasons.append("시가 위")

    if hold >= 0.85:
        score += 10
        reasons.append("고가 부근")

    elif hold >= 0.70:
        score += 6
        reasons.append("고가권 유지")

    # 거래대금
    if turnover >= 5_000_000_000:
        score += 12
        reasons.append("거래대금 50억+")

    elif turnover >= 2_000_000_000:
        score += 9
        reasons.append("거래대금 20억+")

    elif turnover >= 500_000_000:
        score += 6
        reasons.append("거래대금 5억+")

    elif turnover >= 200_000_000:
        score += 3

    # --------------------------------------------------------
    # DAILY HISTORY
    # --------------------------------------------------------

    ma5 = 0
    ma20 = 0
    ret5 = 0
    vol_ratio = 1
    near_high = False

    if len(hist) >= 5:

        closes = [
            x["close"]
            for x in hist
        ]

        ma5 = sum(
            closes[:5]
        ) / 5

        ret5 = (
            price / closes[4] - 1
        ) * 100

        if ret5 >= 3:
            score += 8
            reasons.append("5일 상승")

        elif ret5 >= 1:
            score += 5
            reasons.append("5일 양호")

        elif ret5 < -5:
            score -= 5

    if len(hist) >= 20:

        closes20 = [
            x["close"]
            for x in hist[:20]
        ]

        ma20 = (
            sum(closes20)
            / len(closes20)
        )

        if price > ma20:
            score += 8
            reasons.append("20일선 위")

        if ma5 > ma20:
            score += 10
            reasons.append("5일선>20일선")

        previous = [
            x["high"]
            for x in hist[1:21]
        ]

        if previous:

            highest = max(previous)

            if price >= highest:
                score += 12
                reasons.append(
                    "20일 고점 돌파"
                )
                near_high = True

            elif price >= highest * 0.98:
                score += 7
                reasons.append(
                    "20일 고점 근접"
                )
                near_high = True

    if len(hist) >= 10:

        avg_volume = sum(
            x["volume"]
            for x in hist[1:21]
        ) / min(
            20,
            len(hist) - 1
        )

        if avg_volume > 0:

            vol_ratio = (
                rt["volume"]
                / avg_volume
            )

            if vol_ratio >= 2.0:
                score += 10
                reasons.append(
                    "평균거래량 2배+"
                )

            elif vol_ratio >= 1.5:
                score += 7
                reasons.append(
                    "거래량 증가"
                )

    # --------------------------------------------------------
    # MODE
    # --------------------------------------------------------

    if mode == "morning":

        if change >= 5:
            score += 8
            reasons.append(
                "장초 강한 상승"
            )

        elif change >= 2:
            score += 12
            reasons.append(
                "장초 상승"
            )

        elif change >= 0.5:
            score += 7
            reasons.append(
                "장초 양봉"
            )

        elif change < 0:
            score -= 8

        if change > 12:
            score -= 8
            reasons.append(
                "단기 과열 주의"
            )

    elif mode == "close":

        if change >= 5:
            score += 8
            reasons.append(
                "종가 상승 유지"
            )

        elif change >= 2:
            score += 10
            reasons.append(
                "종가 상승"
            )

        elif change >= 0.5:
            score += 6

        elif change <= 0:
            score -= 8

        if hold >= 0.85:
            score += 5
            reasons.append(
                "종가 고가권"
            )

        if change > 10:
            score -= 8
            reasons.append(
                "단기 과열 주의"
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
        "near_high": near_high,
        "reasons": reasons,
    }


# ============================================================
# LEVELS
# ============================================================

def make_levels(price):

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

        if "positions" not in state:
            state["positions"] = {}

        if "sent_signals" not in state:
            state["sent_signals"] = {}

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


# ============================================================
# CLEAN OLD SIGNAL KEYS
# ============================================================

def cleanup_state(state):

    today = today_key()

    old_keys = []

    for key in state[
        "sent_signals"
    ].keys():

        if not key.startswith(today):
            old_keys.append(key)

    for key in old_keys:
        del state[
            "sent_signals"
        ][key]


# ============================================================
# SCREEN
# ============================================================

def screen(mode, force=False):

    print("")
    print("=" * 36)

    if mode == "morning":

        print(
            " MORNING RISE HUNTER"
        )

        print(
            " 09:05~09:30 장초 상승 후보"
        )

    elif mode == "close":

        print(
            " CLOSE RISE HUNTER"
        )

        print(
            " 15:10~15:20 종가 / 다음날 후보"
        )

    print("=" * 36)

    state = load_state()
    cleanup_state(state)

    session_key = (
        f"{today_key()}:{mode}"
    )

    # 이미 보냈으면 중복 전송 방지
    if (
        not force
        and state["sent_signals"].get(
            session_key
        )
    ):

        print(
            "이미 해당 세션의 후보를 "
            "전송했습니다."
        )

        save_state(state)
        return

    pool = get_candidate_pool()

    if not pool:

        print("")
        print(
            "DATA SOURCE ERROR"
        )
        print(
            "후보 종목을 가져오지 못했습니다."
        )

        if is_final_scan(mode):

            send_telegram(
                f"⚠️ <b>{'장초' if mode == 'morning' else '종가'} "
                f"최종 스캔</b>\n\n"
                f"데이터 수집 실패로 후보를 계산하지 못했습니다.\n"
                f"시간: {hm()}"
            )

        return

    # pool 중복 제거
    unique = {}

    for item in pool:

        code = item["code"]

        if code not in unique:
            unique[code] = item

    pool = list(
        unique.values()
    )

    # 너무 많은 요청 방지
    pool = pool[:MAX_POOL]

    print(
        f"실제 검사 종목: {len(pool)}개"
    )

    candidates = []

    realtime_ok = 0
    history_ok = 0

    for idx, item in enumerate(
        pool,
        start=1
    ):

        code = item["code"]

        rt = get_realtime(code)

        if not rt:
            continue

        realtime_ok += 1

        price = rt["price"]

        if price < MIN_PRICE:
            continue

        # 장중 현재 날짜 데이터인지 확인
        trade_time = rt.get(
            "trade_time",
            ""
        )

        if trade_time:

            if not trade_time.startswith(
                today_key()
            ):
                continue

        # 장초
        if mode == "morning":

            if rt["change_pct"] < 0:
                continue

            if rt["turnover"] < MIN_TURNOVER:
                continue

        # 종가
        elif mode == "close":

            if rt["change_pct"] <= 0:
                continue

            if rt["turnover"] < MIN_TURNOVER:
                continue

        hist = get_history(code)

        if hist:
            history_ok += 1

        result = calculate_score(
            rt,
            hist,
            mode
        )

        score = result["score"]

        if score < MIN_SCORE:
            continue

        candidate = {
            "code": code,
            "name": item["name"],
            "market": item["market"],
            "price": price,
            "change_pct": rt[
                "change_pct"
            ],
            "turnover": rt[
                "turnover"
            ],
            "open": rt["open"],
            "high": rt["high"],
            "low": rt["low"],
            "score": score,
            "hold": result["hold"],
            "ret5": result["ret5"],
            "vol_ratio": result[
                "vol_ratio"
            ],
            "near_high": result[
                "near_high"
            ],
            "reasons": result[
                "reasons"
            ],
        }

        candidates.append(
            candidate
        )

        print(
            f"[{idx}/{len(pool)}] "
            f"{item['name']} "
            f"{score}점"
        )

        # 너무 빠른 요청 방지
        time.sleep(0.08)

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

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["turnover"],
            x["change_pct"]
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
        start=1
    ):

        print(
            f"{i}. "
            f"{c['name']} "
            f"({c['code']}) "
            f"{c['score']}점 "
            f"{pct(c['change_pct'])}"
        )

    # --------------------------------------------------------
    # 후보 없음
    # --------------------------------------------------------

    if not candidates:

        print(
            "후보 없음"
        )

        # 최종 스캔일 때만 Telegram
        if is_final_scan(mode):

            title = (
                "장초 최종 스캔"
                if mode == "morning"
                else "종가 최종 스캔"
            )

            send_telegram(
                f"🔎 <b>{title}</b>\n\n"
                f"현재 조건을 만족하는 후보가 없습니다.\n"
                f"검사 종목: {len(pool)}개\n"
                f"시간: {hm()}"
            )

        save_state(state)
        return

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    title = (
        "🚀 장초 상승 후보"
        if mode == "morning"
        else "🎯 종가 / 다음날 후보"
    )

    message = (
        f"<b>{title}</b>\n"
        f"시간: {hm()}\n"
        f"검사: {len(pool)}개\n\n"
    )

    for i, c in enumerate(
        candidates,
        start=1
    ):

        levels = make_levels(
            c["price"]
        )

        reasons = ", ".join(
            c["reasons"][:6]
        )

        safe_name = html.escape(
            c["name"]
        )

        message += (
            f"<b>{i}. "
            f"{safe_name}</b> "
            f"({c['code']})\n"
            f"시장: {c['market']}\n"
            f"점수: <b>{c['score']}</b>\n"
            f"현재가: "
            f"{c['price']:,.0f}원 "
            f"({pct(c['change_pct'])})\n"
            f"거래대금: "
            f"{money(c['turnover'])}\n"
            f"5일 수익률: "
            f"{pct(c['ret5'])}\n"
            f"현재 위치: "
            f"{c['hold'] * 100:.0f}%\n"
            f"근거: {reasons}\n"
            f"────────────────\n"
            f"ENTRY: {levels['entry']:,.0f}\n"
            f"SL: {levels['sl']:,.0f}\n"
            f"TP1: {levels['tp1']:,.0f}\n"
            f"TP2: {levels['tp2']:,.0f}\n\n"
        )

    message += (
        "⚠️ 자동 스캐너 후보이며 "
        "수익을 보장하지 않습니다."
    )

    sent = send_telegram(
        message
    )

    if sent:

        state["sent_signals"][
            session_key
        ] = {
            "time": now_kst().isoformat(),
            "codes": [
                c["code"]
                for c in candidates
            ],
            "scores": [
                c["score"]
                for c in candidates
            ],
        }

        # 포지션 기록
        for c in candidates:

            state["positions"][
                c["code"]
            ] = {
                "name": c["name"],
                "mode": mode,
                "entry": c["price"],
                "sl": c["price"] * 0.965,
                "tp1": c["price"] * 1.03,
                "tp2": c["price"] * 1.06,
                "created": now_kst().isoformat(),
                "tp1_sent": False,
                "tp2_sent": False,
                "sl_sent": False,
            }

    save_state(state)


# ============================================================
# POSITION MONITOR
# ============================================================

def monitor_positions():

    state = load_state()

    positions = state.get(
        "positions",
        {}
    )

    if not positions:
        return

    changed = False

    for code in list(
        positions.keys()
    ):

        p = positions[code]

        rt = get_realtime(code)

        if not rt:
            continue

        price = rt["price"]

        name = p.get(
            "name",
            code
        )

        # STOP LOSS
        if (
            price <= p["sl"]
            and not p.get(
                "sl_sent",
                False
            )
        ):

            send_telegram(
                f"🛑 <b>손절 경고</b>\n\n"
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

        time.sleep(0.05)

    if changed:
        save_state(state)


# ============================================================
# MAIN
# ============================================================

def main():

    requested_mode = (
        os.getenv("MODE", "")
        .strip()
        .lower()
    )

    force = (
        os.getenv(
            "FORCE_SCAN",
            ""
        )
        .strip()
        .lower()
        in [
            "1",
            "true",
            "yes",
            "y",
        ]
    )

    if requested_mode in [
        "morning",
        "close",
        "monitor",
    ]:

        mode = requested_mode

    else:

        mode = auto_mode()

    print("=" * 36)
    print(" KOREA STOCK HUNTER V3")
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
            force=force
        )

    elif mode == "close":

        screen(
            "close",
            force=force
        )

    else:

        monitor_positions()

    print("=" * 36)
    print(" DONE")
    print("=" * 36)


if __name__ == "__main__":
    main()
