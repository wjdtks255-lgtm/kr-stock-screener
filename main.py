import os
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import pandas as pd


# ============================================================
# KOREA STOCK RISE HUNTER BOT
#
# MODE
#   close   = 15:10 종가 매수 후보
#   morning = 09:05~09:30 장초 상승 후보
#
# ============================================================


# ============================================================
# SETTINGS
# ============================================================

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "").strip()
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "").strip()

KIS_ENV = os.getenv("KIS_ENV", "real").strip().lower()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()


REAL_BASE = "https://openapi.koreainvestment.com:9443"
DEMO_BASE = "https://openapivts.koreainvestment.com:29443"

BASE_URL = DEMO_BASE if KIS_ENV == "demo" else REAL_BASE


STATE_FILE = Path("bot_state.json")
LOG_FILE = Path("bot_log.json")


REQUEST_SLEEP = 0.07

MIN_PRICE = 1000
MIN_TURNOVER = 300_000_000

MAX_CANDIDATES = 60

CLOSE_SCORE = 72
MORNING_SCORE = 72


session = requests.Session()

_access_token = None


# ============================================================
# TIME
# ============================================================

def now_kst():

    return datetime.now(
        timezone.utc
    ).astimezone(
        timezone(timedelta(hours=9))
    )


# ============================================================
# BASIC
# ============================================================

def to_float(value, default=0.0):

    try:

        return float(
            str(value).replace(",", "")
        )

    except Exception:

        return default


def pct(value):

    return f"{to_float(value):+.2f}%"


def fmt_money(value):

    value = to_float(value)

    if value >= 100_000_000:

        return f"{value / 100_000_000:.1f}억"

    if value >= 10_000:

        return f"{value / 10_000:.1f}만"

    return f"{value:,.0f}"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print("Telegram 설정이 없습니다.")

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

        response = session.post(
            url,
            data=payload,
            timeout=15
        )

        if not response.ok:

            print(
                "Telegram 오류:",
                response.status_code,
                response.text[:500]
            )

            return False

        return True

    except Exception as e:

        print(
            "Telegram 전송 오류:",
            e
        )

        return False


# ============================================================
# JSON
# ============================================================

def load_json(path, default):

    try:

        if path.exists():

            return json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

    except Exception as e:

        print(
            "JSON 읽기 오류:",
            e
        )

    return default


def save_json(path, data):

    path.write_text(

        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),

        encoding="utf-8"
    )


# ============================================================
# KIS AUTH
# ============================================================

def get_access_token():

    global _access_token

    if _access_token:

        return _access_token

    if not KIS_APP_KEY:

        raise RuntimeError(
            "KIS_APP_KEY가 없습니다."
        )

    if not KIS_APP_SECRET:

        raise RuntimeError(
            "KIS_APP_SECRET가 없습니다."
        )

    response = session.post(

        f"{BASE_URL}/oauth2/tokenP",

        headers={
            "content-type":
                "application/json"
        },

        json={

            "grant_type":
                "client_credentials",

            "appkey":
                KIS_APP_KEY,

            "appsecret":
                KIS_APP_SECRET,
        },

        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if "access_token" not in data:

        raise RuntimeError(
            f"KIS 인증 실패: {data}"
        )

    _access_token = data[
        "access_token"
    ]

    return _access_token


# ============================================================
# KIS GET
# ============================================================

def kis_get(
    path,
    tr_id,
    params
):

    global _access_token

    token = get_access_token()

    headers = {

        "content-type":
            "application/json",

        "authorization":
            f"Bearer {token}",

        "appkey":
            KIS_APP_KEY,

        "appsecret":
            KIS_APP_SECRET,

        "tr_id":
            tr_id,

        "custtype":
            "P",
    }

    time.sleep(
        REQUEST_SLEEP
    )

    response = session.get(

        f"{BASE_URL}{path}",

        headers=headers,

        params=params,

        timeout=15
    )

    if response.status_code == 401:

        _access_token = None

        token = get_access_token()

        headers["authorization"] = (
            f"Bearer {token}"
        )

        response = session.get(

            f"{BASE_URL}{path}",

            headers=headers,

            params=params,

            timeout=15
        )

    response.raise_for_status()

    data = response.json()

    if data.get("rt_cd") != "0":

        raise RuntimeError(

            f"KIS API 오류 [{tr_id}] "
            f"{data.get('msg_cd')} "
            f"{data.get('msg1')}"
        )

    return data


# ============================================================
# 상승률 순위
# ============================================================

def get_fluctuation_rank(
    market
):

    data = kis_get(

        "/uapi/domestic-stock/v1/"
        "ranking/fluctuation",

        "FHPST01700000",

        {

            "fid_cond_mrkt_div_code":
                "J",

            "fid_input_iscd":
                market,

            "fid_rank_sort_cls_code":
                "0",

            "fid_input_cnt_1":
                "30",

            "fid_prc_cls_code":
                "0",

            "fid_rsfl_rate1":
                "-30",

            "fid_rsfl_rate2":
                "30",

            "fid_div_cls_code":
                "0",

            "fid_trgt_cls_code":
                "0",

            "fid_trgt_exls_cls_code":
                "0000000000",
        }
    )

    return data.get(
        "output",
        []
    )


# ============================================================
# 거래량 순위
# ============================================================

def get_volume_rank(
    market
):

    data = kis_get(

        "/uapi/domestic-stock/v1/"
        "quotations/volume-rank",

        "FHPST01710000",

        {

            "fid_cond_mrkt_div_code":
                "J",

            "fid_input_iscd":
                market,

            "fid_div_cls_code":
                "0",

            "fid_blng_cls_code":
                "0",

            "fid_trgt_cls_code":
                "0",

            "fid_trgt_exls_cls_code":
                "0000000000",

            "fid_input_price_1":
                "",

            "fid_input_price_2":
                "",

            "fid_input_vol_1":
                "",

            "fid_input_vol_2":
                "",

            "fid_input_date_1":
                "",
        }
    )

    return data.get(
        "output",
        []
    )


# ============================================================
# 현재가
# ============================================================

def get_current_price(
    code
):

    data = kis_get(

        "/uapi/domestic-stock/v1/"
        "quotations/inquire-price",

        "FHKST01010100",

        {

            "FID_COND_MRKT_DIV_CODE":
                "J",

            "FID_INPUT_ISCD":
                code,
        }
    )

    return data.get(
        "output",
        {}
    )


# ============================================================
# 일봉
# ============================================================

def get_daily_price(
    code
):

    data = kis_get(

        "/uapi/domestic-stock/v1/"
        "quotations/inquire-daily-price",

        "FHKST01010400",

        {

            "FID_COND_MRKT_DIV_CODE":
                "J",

            "FID_INPUT_ISCD":
                code,

            "FID_PERIOD_DIV_CODE":
                "D",

            "FID_ORG_ADJ_PRC":
                "1",
        }
    )

    return data.get(
        "output",
        []
    )


# ============================================================
# HISTORY
# ============================================================

def make_history(
    rows
):

    result = []

    for row in rows:

        result.append({

            "date":
                str(
                    row.get(
                        "stck_bsop_date",
                        ""
                    )
                ),

            "open":
                to_float(
                    row.get(
                        "stck_oprc"
                    )
                ),

            "high":
                to_float(
                    row.get(
                        "stck_hgpr"
                    )
                ),

            "low":
                to_float(
                    row.get(
                        "stck_lwpr"
                    )
                ),

            "close":
                to_float(
                    row.get(
                        "stck_clpr"
                    )
                ),

            "volume":
                to_float(
                    row.get(
                        "acml_vol"
                    )
                ),
        })

    df = pd.DataFrame(
        result
    )

    if df.empty:

        return df

    return (
        df
        .sort_values("date")
        .reset_index(drop=True)
    )


# ============================================================
# 기술적 데이터
# ============================================================

def get_features(
    hist
):

    if hist is None:

        return None

    if len(hist) < 20:

        return None

    df = hist.copy()

    df["ma5"] = (
        df["close"]
        .rolling(5)
        .mean()
    )

    df["ma10"] = (
        df["close"]
        .rolling(10)
        .mean()
    )

    df["ma20"] = (
        df["close"]
        .rolling(20)
        .mean()
    )

    df["vol20"] = (
        df["volume"]
        .rolling(20)
        .mean()
    )

    last = df.iloc[-1]

    prev = df.iloc[-2]

    price = last["close"]

    if price <= 0:

        return None

    ret1 = (

        (
            price
            / prev["close"]
        )
        - 1
    ) * 100

    ret5 = (

        (
            price
            / df.iloc[-6]["close"]
        )
        - 1
    ) * 100

    previous_20_high = (

        df["high"]
        .iloc[-21:-1]
        .max()
    )

    if previous_20_high <= 0:

        return None

    day_range = (
        last["high"]
        - last["low"]
    )

    if day_range > 0:

        close_position = (

            price
            - last["low"]

        ) / day_range

    else:

        close_position = 0.5

    volume_ratio = (

        last["volume"]
        / last["vol20"]

        if last["vol20"] > 0

        else 0
    )

    return {

        "price":
            price,

        "ret1":
            ret1,

        "ret5":
            ret5,

        "ma5":
            last["ma5"],

        "ma10":
            last["ma10"],

        "ma20":
            last["ma20"],

        "volume_ratio":
            volume_ratio,

        "turnover":
            price
            * last["volume"],

        "previous_20_high":
            previous_20_high,

        "close_position":
            close_position,
    }


# ============================================================
# 15:10 종가 매수 점수
# ============================================================

def score_close(
    features,
    current
):

    price = to_float(
        current.get(
            "stck_prpr"
        )
    )

    previous_close = to_float(
        current.get(
            "stck_sdpr"
        )
    )

    open_price = to_float(
        current.get(
            "stck_oprc"
        )
    )

    high_price = to_float(
        current.get(
            "stck_hgpr"
        )
    )

    low_price = to_float(
        current.get(
            "stck_lwpr"
        )
    )

    change = to_float(
        current.get(
            "prdy_ctrt"
        )
    )

    volume = to_float(
        current.get(
            "acml_vol"
        )
    )

    turnover = to_float(
        current.get(
            "acml_tr_pbmn"
        )
    )

    if price <= 0:

        return None

    if previous_close <= 0:

        previous_close = (
            features["price"]
        )

    if turnover <= 0:

        turnover = (
            price * volume
        )

    score = 0

    reasons = []

    # ----------------------------------------
    # 1. 중기 추세
    # ----------------------------------------

    if (
        features["ma5"]
        >
        features["ma20"]
    ):

        score += 15

        reasons.append(
            "5일선>20일선"
        )

    if (
        features["ma10"]
        >
        features["ma20"]
    ):

        score += 10

        reasons.append(
            "10일선>20일선"
        )

    # ----------------------------------------
    # 2. 당일 상승
    # ----------------------------------------

    if change >= 5:

        score += 15

        reasons.append(
            f"당일 {change:+.1f}%"
        )

    elif change >= 3:

        score += 12

        reasons.append(
            f"당일 {change:+.1f}%"
        )

    elif change >= 1:

        score += 7

        reasons.append(
            f"당일 {change:+.1f}%"
        )

    elif change < -2:

        score -= 15

        reasons.append(
            "당일 약세"
        )

    # ----------------------------------------
    # 3. 최근 5일 추세
    # ----------------------------------------

    if (
        1.5
        <= features["ret5"]
        <= 15
    ):

        score += 10

        reasons.append(
            f"5일 {features['ret5']:+.1f}%"
        )

    elif features["ret5"] > 15:

        score += 3

        reasons.append(
            "단기급등 주의"
        )

    # ----------------------------------------
    # 4. 거래량
    # ----------------------------------------

    vr = features[
        "volume_ratio"
    ]

    if vr >= 3:

        score += 15

        reasons.append(
            f"거래량 {vr:.1f}배"
        )

    elif vr >= 2:

        score += 10

        reasons.append(
            f"거래량 {vr:.1f}배"
        )

    elif vr >= 1.5:

        score += 5

        reasons.append(
            f"거래량 {vr:.1f}배"
        )

    # ----------------------------------------
    # 5. 20일 고점 돌파
    # ----------------------------------------

    high20 = features[
        "previous_20_high"
    ]

    if price >= high20:

        score += 15

        reasons.append(
            "20일 고점 돌파"
        )

    elif (
        price
        >= high20 * 0.98
    ):

        score += 10

        reasons.append(
            "20일 고점 근접"
        )

    # ----------------------------------------
    # 6. 현재 고점 유지
    # ----------------------------------------

    day_range = (
        high_price
        - low_price
    )

    if day_range > 0:

        high_hold = (

            price
            - low_price

        ) / day_range

    else:

        high_hold = 0.5

    if high_hold >= 0.85:

        score += 15

        reasons.append(
            "고점권 유지"
        )

    elif high_hold >= 0.70:

        score += 8

        reasons.append(
            "고점 부근"
        )

    # ----------------------------------------
    # 7. 거래대금
    # ----------------------------------------

    if turnover >= 5_000_000_000:

        score += 10

        reasons.append(
            f"거래대금 {fmt_money(turnover)}"
        )

    elif turnover >= 1_000_000_000:

        score += 7

        reasons.append(
            f"거래대금 {fmt_money(turnover)}"
        )

    elif turnover >= 500_000_000:

        score += 4

    # ----------------------------------------
    # 8. 갭상승 후 무너짐
    # ----------------------------------------

    if (
        open_price > 0
        and previous_close > 0
    ):

        gap = (

            (
                open_price
                / previous_close
            ) - 1

        ) * 100

    else:

        gap = 0

    if (
        gap >= 3
        and change < 0
    ):

        score -= 20

        reasons.append(
            "갭상승 후 밀림"
        )

    # ----------------------------------------
    # 9. 급등 후 고점 이탈
    # ----------------------------------------

    if (
        change >= 7
        and high_hold < 0.60
    ):

        score -= 10

        reasons.append(
            "급등 후 고점 이탈"
        )

    score = max(
        0,
        min(
            100,
            score
        )
    )

    return {

        "score":
            score,

        "reasons":
            reasons,

        "price":
            price,

        "change":
            change,

        "turnover":
            turnover,

        "high_hold":
            high_hold,

        "gap":
            gap,
    }


# ============================================================
# 장초 점수
# ============================================================

def score_morning(
    features,
    current
):

    price = to_float(
        current.get(
            "stck_prpr"
        )
    )

    previous_close = to_float(
        current.get(
            "stck_sdpr"
        )
    )

    open_price = to_float(
        current.get(
            "stck_oprc"
        )
    )

    high_price = to_float(
        current.get(
            "stck_hgpr"
        )
    )

    low_price = to_float(
        current.get(
            "stck_lwpr"
        )
    )

    change = to_float(
        current.get(
            "prdy_ctrt"
        )
    )

    turnover = to_float(
        current.get(
            "acml_tr_pbmn"
        )
    )

    if (
        price <= 0
        or previous_close <= 0
    ):

        return None

    gap = (

        (
            open_price
            / previous_close
        ) - 1

    ) * 100

    day_range = (
        high_price
        - low_price
    )

    high_hold = (

        (
            price
            - low_price
        ) / day_range

        if day_range > 0

        else 0.5
    )

    score = 0

    reasons = []

    if change >= 7:

        score += 25

        reasons.append(
            f"현재 {change:+.1f}%"
        )

    elif change >= 4:

        score += 20

        reasons.append(
            f"현재 {change:+.1f}%"
        )

    elif change >= 2:

        score += 12

        reasons.append(
            f"현재 {change:+.1f}%"
        )

    elif change >= 1:

        score += 6

        reasons.append(
            f"현재 {change:+.1f}%"
        )

    if gap >= 3:

        score += 10

        reasons.append(
            f"시초갭 {gap:+.1f}%"
        )

    elif gap >= 1:

        score += 5

        reasons.append(
            f"시초갭 {gap:+.1f}%"
        )

    if high_hold >= 0.85:

        score += 20

        reasons.append(
            "고점 유지 강함"
        )

    elif high_hold >= 0.70:

        score += 10

        reasons.append(
            "고점권 유지"
        )

    if turnover >= 5_000_000_000:

        score += 20

        reasons.append(
            f"거래대금 {fmt_money(turnover)}"
        )

    elif turnover >= 1_000_000_000:

        score += 15

        reasons.append(
            f"거래대금 {fmt_money(turnover)}"
        )

    elif turnover >= 500_000_000:

        score += 8

    if (
        features["ma5"]
        >
        features["ma20"]
    ):

        score += 10

        reasons.append(
            "중기 상승추세"
        )

    if (
        gap >= 3
        and change < 0
    ):

        score -= 20

        reasons.append(
            "갭상승 후 밀림"
        )

    return {

        "score":
            max(
                0,
                min(
                    100,
                    score
                )
            ),

        "reasons":
            reasons,

        "price":
            price,

        "change":
            change,

        "gap":
            gap,

        "turnover":
            turnover,

        "high_hold":
            high_hold,
    }


# ============================================================
# 후보 수집
# ============================================================

def get_candidate_pool():

    pool = {}

    for market in (
        "0000",
        "0001",
        "1001"
    ):

        # 상승률
        try:

            rows = (
                get_fluctuation_rank(
                    market
                )
            )

            for row in rows:

                code = (

                    row.get(
                        "stck_shrn_iscd"
                    )

                    or

                    row.get(
                        "mksc_shrn_iscd"
                    )
                )

                name = row.get(
                    "hts_kor_isnm",
                    ""
                )

                if code:

                    pool.setdefault(

                        code,

                        {
                            "code":
                                code,

                            "name":
                                name,
                        }
                    )

        except Exception as e:

            print(
                "상승률 조회 오류:",
                e
            )

        # 거래량
        try:

            rows = (
                get_volume_rank(
                    market
                )
            )

            for row in rows:

                code = (

                    row.get(
                        "stck_shrn_iscd"
                    )

                    or

                    row.get(
                        "mksc_shrn_iscd"
                    )
                )

                name = row.get(
                    "hts_kor_isnm",
                    ""
                )

                if code:

                    pool.setdefault(

                        code,

                        {
                            "code":
                                code,

                            "name":
                                name,
                        }
                    )

        except Exception as e:

            print(
                "거래량 조회 오류:",
                e
            )

    return list(
        pool.values()
    )


# ============================================================
# 후보 일봉 분석
# ============================================================

def enrich_candidates(
    candidates
):

    result = []

    for item in candidates[
        :MAX_CANDIDATES
    ]:

        try:

            rows = (
                get_daily_price(
                    item["code"]
                )
            )

            hist = make_history(
                rows
            )

            features = (
                get_features(
                    hist
                )
            )

            if not features:

                continue

            if (
                features["price"]
                < MIN_PRICE
            ):

                continue

            if (
                features["turnover"]
                < MIN_TURNOVER
            ):

                continue

            item[
                "features"
            ] = features

            result.append(
                item
            )

        except Exception as e:

            print(
                "일봉 분석 오류:",
                item["code"],
                e
            )

    return result


# ============================================================
# 15:10 종가 매수 후보
# ============================================================

def close_scan():

    print(
        "===== 15:10 종가 매수 스캔 ====="
    )

    pool = (
        get_candidate_pool()
    )

    enriched = (
        enrich_candidates(
            pool
        )
    )

    results = []

    for item in enriched:

        try:

            current = (
                get_current_price(
                    item["code"]
                )
            )

            # 현재가 기준
            result = score_close(

                item[
                    "features"
                ],

                current
            )

            if not result:

                continue

            # 15:10 종가 매수 후보는
            # 상승/보합 종목 중심
            if result["change"] < 0:

                continue

            if (
                result["score"]
                < CLOSE_SCORE
            ):

                continue

            item[
                "result"
            ] = result

            results.append(
                item
            )

        except Exception as e:

            print(
                "현재가 오류:",
                item["code"],
                e
            )

    results.sort(

        key=lambda x: (

            x["result"]["score"],

            x["result"]["turnover"],

            x["result"]["change"],

        ),

        reverse=True
    )

    return (
        results[:10],
        len(pool),
        len(enriched)
    )


# ============================================================
# 장초 상승 후보
# ============================================================

def morning_scan():

    print(
        "===== 장초 상승 스캔 ====="
    )

    pool = (
        get_candidate_pool()
    )

    enriched = (
        enrich_candidates(
            pool
        )
    )

    results = []

    for item in enriched:

        try:

            current = (
                get_current_price(
                    item["code"]
                )
            )

            result = score_morning(

                item[
                    "features"
                ],

                current
            )

            if not result:

                continue

            if (
                result["change"]
                < 1
            ):

                continue

            if (
                result["score"]
                < MORNING_SCORE
            ):

                continue

            item[
                "result"
            ] = result

            results.append(
                item
            )

        except Exception as e:

            print(
                "현재가 오류:",
                item["code"],
                e
            )

    results.sort(

        key=lambda x: (

            x["result"]["score"],

            x["result"]["change"],

            x["result"]["turnover"],

        ),

        reverse=True
    )

    return (
        results[:10],
        len(pool),
        len(enriched)
    )


# ============================================================
# 중복 방지
# ============================================================

def remove_duplicates(
    mode,
    candidates
):

    state = load_json(
        STATE_FILE,
        {}
    )

    now = now_kst()

    if mode == "morning":

        cooldown = timedelta(
            minutes=30
        )

    else:

        cooldown = timedelta(
            hours=20
        )

    result = []

    for item in candidates:

        key = (
            f"{mode}:"
            f"{item['code']}"
        )

        old = state.get(
            key
        )

        allow = True

        if old:

            try:

                old_time = (
                    datetime.fromisoformat(
                        old["time"]
                    )
                )

                if (
                    now - old_time
                    < cooldown
                ):

                    if (
                        mode
                        == "morning"
                    ):

                        allow = (

                            item[
                                "result"
                            ]["score"]

                            >=

                            old.get(
                                "score",
                                0
                            ) + 8
                        )

                    else:

                        allow = False

            except Exception:

                pass

        if allow:

            result.append(
                item
            )

            state[key] = {

                "time":
                    now.isoformat(),

                "score":
                    item[
                        "result"
                    ]["score"],
            }

    save_json(
        STATE_FILE,
        state
    )

    return result


# ============================================================
# 차트
# ============================================================

def chart_url(
    code
):

    return (

        "https://finance.naver.com/"
        f"item/main.naver?code={code}"

    )


# ============================================================
# 종가 Telegram
# ============================================================

def make_close_message(
    candidates,
    pool_count,
    analyzed_count
):

    now = now_kst()

    header = (

        "🔔 <b>[15:10 종가 매수 후보]</b>\n"

        f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S')}\n"

        "⏰ 15:30 장 마감 전 매수 판단\n"

        f"📊 후보 {pool_count}개 / "
        f"분석 {analyzed_count}개\n"

        "━━━━━━━━━━━━━━━━━━"

    )

    if not candidates:

        return (

            header

            + "\n\n"

            "현재 조건을 동시에 만족하는 "
            "종가 매수 후보가 없습니다.\n\n"

            "조건: 상승추세 + 거래량 증가 + "
            "고점 유지 + 거래대금 + 가격 강도"
        )

    blocks = []

    for i, item in enumerate(
        candidates,
        1
    ):

        f = item[
            "features"
        ]

        r = item[
            "result"
        ]

        reasons = (
            " · ".join(
                r[
                    "reasons"
                ][:7]
            )
        )

        blocks.append(

            f"🔥 <b>{i}. "
            f"{item['name']}</b> "
            f"<code>({item['code']})</code>\n"

            f"⭐ 종가매수 점수 "
            f"<b>{r['score']}/100</b>\n"

            f"💰 현재가 "
            f"<b>{r['price']:,.0f}원</b>\n"

            f"📈 당일 "
            f"<b>{pct(r['change'])}</b>\n"

            f"📊 거래량 "
            f"<b>{f['volume_ratio']:.1f}배</b>\n"

            f"💵 거래대금 "
            f"<b>{fmt_money(r['turnover'])}</b>\n"

            f"📌 고점 유지 "
            f"<b>{r['high_hold'] * 100:.0f}%</b>\n"

            f"🔎 {reasons}\n"

            f"📎 <a href='{chart_url(item['code'])}'>"
            f"차트 보기</a>"
        )

    return (

        header
        + "\n\n"
        + "\n\n".join(
            blocks
        )
    )


# ============================================================
# 장초 Telegram
# ============================================================

def make_morning_message(
    candidates,
    pool_count,
    analyzed_count
):

    now = now_kst()

    header = (

        "🚨 <b>[장초 상승 포착]</b>\n"

        f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S')}\n"

        f"📊 후보 {pool_count}개 / "
        f"분석 {analyzed_count}개\n"

        "━━━━━━━━━━━━━━━━━━"

    )

    if not candidates:

        return (

            header
            + "\n\n"

            "현재 장초 상승 조건을 만족하는 "
            "종목이 없습니다."
        )

    blocks = []

    for i, item in enumerate(
        candidates,
        1
    ):

        r = item[
            "result"
        ]

        reasons = (
            " · ".join(
                r[
                    "reasons"
                ][:7]
            )
        )

        blocks.append(

            f"🚀 <b>{i}. "
            f"{item['name']}</b> "
            f"<code>({item['code']})</code>\n"

            f"⭐ 장초 점수 "
            f"<b>{r['score']}/100</b>\n"

            f"💰 현재가 "
            f"<b>{r['price']:,.0f}원</b>\n"

            f"📈 현재 "
            f"<b>{pct(r['change'])}</b>\n"

            f"🌅 시초갭 "
            f"<b>{pct(r['gap'])}</b>\n"

            f"💵 거래대금 "
            f"<b>{fmt_money(r['turnover'])}</b>\n"

            f"📊 고점 유지 "
            f"<b>{r['high_hold'] * 100:.0f}%</b>\n"

            f"🔎 {reasons}\n"

            f"📎 <a href='{chart_url(item['code'])}'>"
            f"차트 보기</a>"
        )

    return (

        header
        + "\n\n"
        + "\n\n".join(
            blocks
        )
    )


# ============================================================
# RUN
# ============================================================

def run(
    mode
):

    if not KIS_APP_KEY:

        raise RuntimeError(
            "KIS_APP_KEY가 없습니다."
        )

    if not KIS_APP_SECRET:

        raise RuntimeError(
            "KIS_APP_SECRET가 없습니다."
        )

    if mode == "close":

        candidates, pool, analyzed = (
            close_scan()
        )

        fresh = remove_duplicates(
            "close",
            candidates
        )

        message = make_close_message(
            fresh,
            pool,
            analyzed
        )

    elif mode == "morning":

        candidates, pool, analyzed = (
            morning_scan()
        )

        fresh = remove_duplicates(
            "morning",
            candidates
        )

        message = make_morning_message(
            fresh,
            pool,
            analyzed
        )

    else:

        raise ValueError(
            "MODE는 close 또는 morning이어야 합니다."
        )

    print(message)

    send_telegram(
        message
    )

    # 로그
    logs = load_json(
        LOG_FILE,
        []
    )

    logs.append({

        "time":
            now_kst().isoformat(),

        "mode":
            mode,

        "pool":
            pool,

        "analyzed":
            analyzed,

        "signals":

            [

                {

                    "code":
                        item["code"],

                    "name":
                        item["name"],

                    "score":
                        item[
                            "result"
                        ]["score"]

                }

                for item in candidates

            ]
    })

    save_json(
        LOG_FILE,
        logs[-500:]
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    mode = os.getenv(
        "MODE",
        "close"
    ).strip().lower()

    run(
        mode
    )
