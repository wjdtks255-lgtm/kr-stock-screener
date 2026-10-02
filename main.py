import os
import json
import time
import re
import requests
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from urllib.parse import quote

import pandas as pd
import numpy as np
import pytz
import FinanceDataReader as fdr


# ============================================================
# KOREA STOCK HUNTER V6.2 (Fallback Enhanced)
# ============================================================
# 목적
# 1) 장초 상승 후보
# 2) 장중 상승 후보
# 3) 15:10~15:20 종가 매수 후보
# 4) 개별 종목 Telegram 알림
# 5) 기술적 상승 근거 표시
# 6) 최근 뉴스/소식 표시
# 7) 종가 후보 TP/SL 추적
# ============================================================


# ============================================================
# ENV
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

MODE_ENV = os.environ.get("MODE", "").strip().lower()
FORCE_SCAN_ENV = os.environ.get("FORCE_SCAN", "").strip().lower()

STATE_FILE = "active_positions.json"


# ============================================================
# SETTINGS
# ============================================================

KST = pytz.timezone("Asia/Seoul")

UNIVERSE_SIZE = 350
HISTORY_DAYS = 90

MIN_TURNOVER = 500_000_000

TOP_SIGNAL_COUNT = 3

SL_PERCENT = 0.05
TP1_PERCENT = 0.03
TP2_PERCENT = 0.06

NEWS_DAYS = 7
NEWS_TIMEOUT = 8

REQUEST_TIMEOUT = 7

SIGNAL_COOLDOWN_HOURS = 4

# 실시간 데이터 최소 확보 개수
MIN_REALTIME_DATA = 30


# ============================================================
# HEADERS
# ============================================================

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://finance.naver.com/",
}


NEWS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
    )
}


# ============================================================
# BASIC
# ============================================================

def now_kst():
    return datetime.now(KST)


def fmt_money(value):
    if value is None:
        return "-"

    try:
        value = float(value)

        if value >= 1_000_000_000_000:
            return f"{value / 1_000_000_000_000:.2f}조원"

        if value >= 100_000_000:
            return f"{value / 100_000_000:.1f}억원"

        if value >= 10_000:
            return f"{value / 10_000:.1f}만원"

        return f"{int(value):,}원"

    except Exception:
        return "-"


def fmt_price(value):
    try:
        return f"{float(value):,.0f}원"
    except Exception:
        return "-"


def fmt_pct(value):
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def clean_text(text):
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[TELEGRAM] TOKEN 또는 CHAT_ID 없음")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.ok:
            print("[TELEGRAM] 전송 성공")
            return True

        print(
            "[TELEGRAM] 전송 실패:",
            response.status_code,
            response.text[:300]
        )

    except Exception as e:
        print("[TELEGRAM] ERROR:", e)

    return False


# ============================================================
# STATE
# ============================================================

def load_state():
    default_state = {
        "positions": {},
        "sent_signals": {},
        "last_run": "",
    }

    if not os.path.exists(STATE_FILE):
        return default_state

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_state

        state.setdefault("positions", {})
        state.setdefault("sent_signals", {})
        state.setdefault("last_run", "")

        return state

    except Exception as e:
        print("[STATE] LOAD ERROR:", e)
        return default_state


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

        print("[STATE] 저장 완료")

    except Exception as e:
        print("[STATE] SAVE ERROR:", e)


# ============================================================
# DUPLICATE SIGNAL CONTROL
# ============================================================

def signal_recent(state, code, mode):
    key = f"{mode}:{code}"

    raw = state.get("sent_signals", {}).get(key)

    if not raw:
        return False

    try:
        old_time = datetime.fromisoformat(raw)
        current = now_kst()

        diff = current - old_time

        return diff < timedelta(hours=SIGNAL_COOLDOWN_HOURS)

    except Exception:
        return False


def mark_signal(state, code, mode):
    key = f"{mode}:{code}"

    state.setdefault("sent_signals", {})
    state["sent_signals"][key] = now_kst().isoformat()


# ============================================================
# REALTIME NAVER
# ============================================================

def get_one_realtime(code, retry=2):

    url = (
        "https://polling.finance.naver.com/"
        f"api/realtime/domestic/stock/{code}"
    )

    for attempt in range(retry + 1):

        try:
            response = requests.get(
                url,
                headers=NAVER_HEADERS,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                time.sleep(0.5)
                continue

            data = response.json()

            datas = data.get("datas", [])

            if not datas:
                time.sleep(0.5)
                continue

            item = datas[0]

            def num(key):
                value = item.get(key)

                try:
                    return float(value)
                except Exception:
                    return None

            return {
                "code": code,
                "price": num("closePrice"),
                "change_pct": num("fluctuationsRatio"),
                "change_price": num(
                    "compareToPreviousClosePrice"
                ),
                "previous_close": num(
                    "previousClosePrice"
                ),
                "volume": num(
                    "accumulatedTradingVolume"
                ),
                "trading_value": num(
                    "accumulatedTradingValue"
                ),
                "open": num("openPrice"),
                "high": num("highPrice"),
                "low": num("lowPrice"),
                "market_status": item.get(
                    "marketStatus"
                ),
            }

        except Exception as e:
            if attempt == retry:
                print(
                    f"[REALTIME ERROR] {code}: {e}"
                )

            time.sleep(0.5)

    return None


def get_realtime_quotes(codes):

    result = {}

    total = len(codes)

    print(
        f"실시간 시세 조회 시작: {total}개"
    )

    for idx, code in enumerate(codes, 1):

        quote_data = get_one_realtime(code)

        if quote_data and quote_data.get("price"):
            result[code] = quote_data

        if idx % 50 == 0:
            print(
                f"실시간 조회 진행: "
                f"{idx}/{total} "
                f"성공 {len(result)}"
            )

        time.sleep(0.08)

    print(
        f"실시간 데이터: "
        f"{len(result)}/{total}개"
    )

    return result


# ============================================================
# KRX UNIVERSE (FALLBACK ENHANCED)
# ============================================================

def get_universe():
    print("KRX 종목 목록 다운로드...")

    # 1차 시도: FinanceDataReader 활용
    try:
        df = fdr.StockListing("KRX")
        if df is not None and not df.empty:
            print(f"원본 KRX universe (FDR): {len(df)}개")

            if "Code" in df.columns:
                df["Code"] = (
                    df["Code"]
                    .astype(str)
                    .str.replace(".0", "", regex=False)
                    .str.zfill(6)
                )
            elif "Symbol" in df.columns:
                df["Symbol"] = (
                    df["Symbol"]
                    .astype(str)
                    .str.replace(".0", "", regex=False)
                    .str.zfill(6)
                )
                df = df.rename(columns={"Symbol": "Code"})

            if "Name" not in df.columns:
                df["Name"] = df["Code"]

            if "Amount" in df.columns:
                df["Amount"] = pd.to_numeric(
                    df["Amount"],
                    errors="coerce"
                ).fillna(0)
                df = df.sort_values(
                    "Amount",
                    ascending=False
                )

            df = df.head(UNIVERSE_SIZE)
            df = df[["Code", "Name"]].drop_duplicates(subset=["Code"])
            print(f"KRX universe 확정: {len(df)}개")
            return df

    except Exception as e:
        print(f"[UNIVERSE ERROR - FDR Failed] {repr(e)}")

    # 2차 시도: 네이버 금융 크롤링 Fallback (FDR 장애 대응)
    print("네이버 금융 대체 크롤링으로 종목 목록을 구성합니다...")
    fallback_rows = []
    try:
        for sosok in [0, 1]:  # 0: 코스피, 1: 코스닥
            page = 1
            while page <= 15:  # 상위 페이지 탐색
                url = f"https://finance.naver.com/sise/sise_market_sum.nhn?sosok={sosok}&page={page}"
                res = requests.get(url, headers=NAVER_HEADERS, timeout=7)
                if res.status_code != 200:
                    break
                
                html = res.text
                matches = re.findall(r'href="/item/main\.(?:naver|nhn)\?code=(\d{6})"[^>]*>([^<]+)</a>', html)
                
                if not matches:
                    break
                
                seen_in_page = set()
                for code, name in matches:
                    if code not in seen_in_page:
                        seen_in_page.add(code)
                        fallback_rows.append({"Code": code, "Name": name.strip()})
                
                page += 1
                time.sleep(0.05)

        if fallback_rows:
            df_fallback = pd.DataFrame(fallback_rows)
            df_fallback = df_fallback.drop_duplicates(subset=["Code"]).head(UNIVERSE_SIZE)
            print(f"네이버 금융 Fallback 유니버스 확보 성공: {len(df_fallback)}개")
            return df_fallback

    except Exception as ex:
        print(f"[FALLBACK ERROR] {repr(ex)}")

    return pd.DataFrame()


# ============================================================
# HISTORY
# ============================================================

def get_history(code):

    end = datetime.now()

    start = end - timedelta(
        days=HISTORY_DAYS
    )

    try:

        df = fdr.DataReader(
            code,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d")
        )

        if df is None or df.empty:
            return None

        df = df.copy()

        required = [
            "Close",
            "Volume",
        ]

        for col in required:
            if col not in df.columns:
                return None

        return df

    except Exception as e:

        print(
            f"[HISTORY ERROR] {code}: {e}"
        )

        return None


# ============================================================
# TECHNICAL ANALYSIS
# ============================================================

def analyze_stock(code, name, quote, history):

    if history is None:
        return None

    if len(history) < 25:
        return None

    try:

        df = history.copy()

        close = pd.to_numeric(
            df["Close"],
            errors="coerce"
        )

        volume = pd.to_numeric(
            df["Volume"],
            errors="coerce"
        )

        close = close.dropna()
        volume = volume.reindex(
            close.index
        ).fillna(0)

        if len(close) < 25:
            return None

        current_price = float(
            quote["price"]
        )

        current_change = float(
            quote.get("change_pct") or 0
        )

        current_volume = float(
            quote.get("volume") or 0
        )

        trading_value = float(
            quote.get("trading_value") or 0
        )

        if trading_value <= 0:
            trading_value = (
                current_price *
                current_volume
            )

        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        avg_volume20 = (
            volume.rolling(20).mean()
        )

        avg_volume20_value = float(
            avg_volume20.iloc[-1]
        )

        if avg_volume20_value <= 0:
            volume_ratio = 0
        else:
            volume_ratio = (
                current_volume /
                avg_volume20_value
            )

        recent_high20 = float(
            close.tail(20).max()
        )

        previous_close = float(
            close.iloc[-1]
        )

        # 최근 종가 위치
        recent_low20 = float(
            close.tail(20).min()
        )

        range20 = (
            recent_high20 -
            recent_low20
        )

        if range20 > 0:
            close_position = (
                current_price -
                recent_low20
            ) / range20
        else:
            close_position = 0.5

        # 최근 고점 대비 거리
        if recent_high20 > 0:
            high_distance_pct = (
                (
                    current_price /
                    recent_high20
                ) - 1
            ) * 100
        else:
            high_distance_pct = 0

        # 오늘 시가 대비
        open_price = float(
            quote.get("open") or current_price
        )

        if open_price > 0:
            intraday_change = (
                (
                    current_price /
                    open_price
                ) - 1
            ) * 100
        else:
            intraday_change = 0

        score = 0
        reasons = []
        warnings = []

        # ====================================================
        # TREND
        # ====================================================

        if len(ma20.dropna()) > 0:

            ma20_now = float(
                ma20.iloc[-1]
            )

            if current_price > ma20_now:
                score += 2

                reasons.append(
                    "현재가가 20일 이동평균선 위"
                )

            else:
                warnings.append(
                    "현재가가 20일선 아래"
                )

        if len(ma5.dropna()) > 0 and len(ma20.dropna()) > 0:

            ma5_now = float(
                ma5.iloc[-1]
            )

            ma20_now = float(
                ma20.iloc[-1]
            )

            if ma5_now > ma20_now:

                score += 2

                reasons.append(
                    "5일선이 20일선 위에서 상승 추세"
                )

        if len(ma60.dropna()) > 0:

            ma60_now = float(
                ma60.iloc[-1]
            )

            if current_price > ma60_now:

                score += 1

                reasons.append(
                    "중기 60일선 위"
                )

        # ====================================================
        # VOLUME
        # ====================================================

        if volume_ratio >= 3.0:

            score += 3

            reasons.append(
                f"거래량이 20일 평균 대비 "
                f"{volume_ratio:.2f}배 증가"
            )

        elif volume_ratio >= 2.0:

            score += 2

            reasons.append(
                f"거래량이 평균 대비 "
                f"{volume_ratio:.2f}배 증가"
            )

        elif volume_ratio >= 1.3:

            score += 1

            reasons.append(
                f"거래량 증가 "
                f"{volume_ratio:.2f}배"
            )

        else:

            warnings.append(
                "거래량 증가폭 제한적"
            )

        # ====================================================
        # PRICE MOMENTUM
        # ====================================================

        if current_change >= 7:

            score += 2

            reasons.append(
                f"당일 강한 상승 "
                f"{current_change:+.2f}%"
            )

        elif current_change >= 3:

            score += 1

            reasons.append(
                f"당일 상승 모멘텀 "
                f"{current_change:+.2f}%"
            )

        # ====================================================
        # CLOSE POSITION
        # ====================================================

        if close_position >= 0.80:

            score += 2

            reasons.append(
                "최근 20일 가격 범위의 "
                "고가권에서 마감"
            )

        elif close_position >= 0.65:

            score += 1

            reasons.append(
                "최근 가격 범위 상단권"
            )

        # ====================================================
        # 20D HIGH
        # ====================================================

        if current_price >= recent_high20:

            score += 3

            reasons.append(
                "최근 20일 고점 돌파"
            )

        elif high_distance_pct >= -2:

            score += 1

            reasons.append(
                "최근 20일 고점 근접"
            )

        # ====================================================
        # INTRADAY STRENGTH
        # ====================================================

        if intraday_change >= 3:

            score += 1

            reasons.append(
                f"시가 대비 상승 "
                f"{intraday_change:+.2f}%"
            )

        # ====================================================
        # OVERHEAT WARNING
        # ====================================================

        if current_change >= 15:

            warnings.append(
                "당일 +15% 이상 급등으로 "
                "추격매수 과열 주의"
            )

        elif current_change >= 10:

            warnings.append(
                "당일 +10% 이상 급등 상태"
            )

        elif current_change >= 7:

            warnings.append(
                "단기 급등에 따른 변동성 주의"
            )

        # ====================================================
        # TURNOVER
        # ====================================================

        if trading_value >= 50_000_000_000:

            score += 2

            reasons.append(
                f"거래대금 {fmt_money(trading_value)}"
            )

        elif trading_value >= 10_000_000_000:

            score += 1

            reasons.append(
                f"거래대금 {fmt_money(trading_value)}"
            )

        return {
            "code": code,
            "name": name,
            "price": current_price,
            "change_pct": current_change,
            "trading_value": trading_value,
            "volume": current_volume,
            "volume_ratio": volume_ratio,
            "score": score,
            "reasons": reasons,
            "warnings": warnings,
            "close_position": close_position,
            "recent_high20": recent_high20,
            "high_distance_pct": high_distance_pct,
            "intraday_change": intraday_change,
        }

    except Exception as e:

        print(
            f"[ANALYSIS ERROR] "
            f"{code} {name}: {e}"
        )

        return None


# ============================================================
# NEWS
# ============================================================

def get_google_news(name, code):

    query = quote(
        f'"{name}" "{code}"'
    )

    url = (
        "https://news.google.com/rss/search?"
        f"q={query}"
        "&hl=ko"
        "&gl=KR"
        "&ceid=KR:ko"
    )

    try:

        response = requests.get(
            url,
            headers=NEWS_HEADERS,
            timeout=NEWS_TIMEOUT
        )

        if response.status_code != 200:
            return []

        root = ET.fromstring(
            response.content
        )

        results = []

        for item in root.findall(".//item"):

            title = item.findtext(
                "title"
            )

            link = item.findtext(
                "link"
            )

            pub_date = item.findtext(
                "pubDate"
            )

            source = item.findtext(
                "source"
            )

            title = clean_text(title)

            if not title:
                continue

            results.append({
                "title": title,
                "link": link or "",
                "pub_date": pub_date or "",
                "source": clean_text(source),
            })

            if len(results) >= 3:
                break

        return results

    except Exception as e:

        print(
            f"[NEWS ERROR] "
            f"{name}: {e}"
        )

        return []


def get_naver_news(name, code):

    url = (
        "https://search.naver.com/"
        "search.naver"
        f"?where=news&query={quote(name)}"
    )

    try:

        response = requests.get(
            url,
            headers=NEWS_HEADERS,
            timeout=NEWS_TIMEOUT
        )

        if response.status_code != 200:
            return []

        html = response.text

        pattern = re.compile(
            r'class="news_tit"[^>]*>'
            r'\s*([^<]+)'
        )

        titles = pattern.findall(
            html
        )

        results = []

        for title in titles[:3]:

            title = clean_text(title)

            if title:
                results.append({
                    "title": title,
                    "link": "",
                    "pub_date": "",
                    "source": "Naver News",
                })

        return results

    except Exception as e:

        print(
            f"[NAVER NEWS ERROR] "
            f"{name}: {e}"
        )

        return []


def get_recent_news(name, code):

    news = get_google_news(
        name,
        code
    )

    if news:
        return news

    return get_naver_news(
        name,
        code
    )


# ============================================================
# NEWS REASON
# ============================================================

def make_news_reason(news):

    if not news:

        return [
            "최근 주요 뉴스/소식이 "
            "검색되지 않음"
        ]

    reasons = []

    for item in news[:2]:

        title = item.get(
            "title",
            ""
        )

        source = item.get(
            "source",
            ""
        )

        if len(title) > 85:
            title = title[:82] + "..."

        if source:
            reasons.append(
                f"{title} "
                f"({source})"
            )

        else:
            reasons.append(
                title
            )

    return reasons


# ============================================================
# MODE
# ============================================================

def get_mode():

    manual_modes = {
        "morning",
        "intraday",
        "close",
        "monitor",
    }

    if (
        MODE_ENV in manual_modes
        and FORCE_SCAN_ENV in {
            "",
            "true",
            "1",
            "yes",
        }
    ):

        return MODE_ENV

    now = now_kst()

    hhmm = (
        now.hour * 60 +
        now.minute
    )

    # 09:00 ~ 09:30
    if 540 <= hhmm <= 570:
        return "morning"

    # 09:31 ~ 15:05
    if 571 <= hhmm <= 905:
        return "intraday"

    # 15:10 ~ 15:20
    if 910 <= hhmm <= 920:
        return "close"

    return "monitor"


# ============================================================
# SCORE THRESHOLD
# ============================================================

def get_threshold(mode):

    if mode == "morning":
        return 7

    if mode == "intraday":
        return 8

    if mode == "close":
        return 9

    return 999


# ============================================================
# SCAN
# ============================================================

def scan_candidates(
    universe,
    quotes,
    mode
):

    candidates = []

    threshold = get_threshold(
        mode
    )

    total = len(universe)

    print(
        f"{mode.upper()} SCAN "
        f"threshold={threshold}"
    )

    for idx, row in universe.iterrows():

        code = str(
            row["Code"]
        ).zfill(6)

        name = str(
            row["Name"]
        )

        quote = quotes.get(code)

        if not quote:
            continue

        price = quote.get(
            "price"
        )

        if not price or price <= 0:
            continue

        trading_value = float(
            quote.get(
                "trading_value"
            ) or 0
        )

        if trading_value <= 0:

            trading_value = (
                float(price) *
                float(
                    quote.get(
                        "volume"
                    ) or 0
                )
            )

            quote["trading_value"] = (
                trading_value
            )

        if trading_value < MIN_TURNOVER:
            continue

        history = get_history(
            code
        )

        if history is None:
            continue

        result = analyze_stock(
            code,
            name,
            quote,
            history
        )

        if result is None:
            continue

        if result["score"] < threshold:
            continue

        candidates.append(
            result
        )

        if idx % 30 == 0:
            print(
                f"분석 진행: "
                f"{idx}/{total}"
            )

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["volume_ratio"],
            x["trading_value"],
            x["change_pct"],
        ),
        reverse=True
    )

    return candidates[
        :TOP_SIGNAL_COUNT
    ]


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def mode_title(mode):

    if mode == "morning":
        return "09시 장초 상승 후보"

    if mode == "intraday":
        return "장중 상승 후보"

    if mode == "close":
        return "15:20 종가 후보"

    return "주식 포지션"


def build_candidate_message(
    candidate,
    mode,
    news
):

    code = candidate["code"]
    name = candidate["name"]

    price = candidate["price"]

    sl = price * (
        1 - SL_PERCENT
    )

    tp1 = price * (
        1 + TP1_PERCENT
    )

    tp2 = price * (
        1 + TP2_PERCENT
    )

    news_reasons = make_news_reason(
        news
    )

    technical = candidate[
        "reasons"
    ][:6]

    warnings = candidate[
        "warnings"
    ]

    chart_url = (
        "https://finance.naver.com/"
        f"item/main.naver?code={code}"
    )

    message = (
        f"🚨 [{mode_title(mode)}]\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 {name} ({code})\n\n"
        f"💰 현재가: "
        f"{fmt_price(price)}\n"
        f"📈 등락률: "
        f"{fmt_pct(candidate['change_pct'])}\n"
        f"🔥 거래대금: "
        f"{fmt_money(candidate['trading_value'])}\n"
        f"📊 거래량비율: "
        f"{candidate['volume_ratio']:.2f}배\n"
        f"⭐ 기술점수: "
        f"{candidate['score']}\n\n"
        f"🟢 진입 기준: "
        f"{fmt_price(price)}\n"
        f"🛡️ SL: "
        f"{fmt_price(sl)}\n"
        f"🎯 TP1: "
        f"{fmt_price(tp1)}\n"
        f"🎯 TP2: "
        f"{fmt_price(tp2)}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 상승 근거\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )

    if technical:

        for reason in technical:

            message += (
                f"• {reason}\n"
            )

    else:

        message += (
            "• 주요 기술적 근거 확인 필요\n"
        )

    message += (
        "\n📰 최근 뉴스/소식\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    for reason in news_reasons:

        message += (
            f"• {reason}\n"
        )

    if warnings:

        message += (
            "\n⚠️ 주의사항\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

        for warning in warnings[:3]:

            message += (
                f"• {warning}\n"
            )

    message += (
        "\n🔗 네이버 차트\n"
        f"{chart_url}\n\n"
        "ℹ️ 기술적 조건과 최근 뉴스/소식을 "
        "함께 표시한 참고용 신호입니다."
    )

    return message


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def create_position(
    state,
    candidate,
    mode
):

    code = candidate["code"]

    entry = float(
        candidate["price"]
    )

    sl = entry * (
        1 - SL_PERCENT
    )

    tp1 = entry * (
        1 + TP1_PERCENT
    )

    tp2 = entry * (
        1 + TP2_PERCENT
    )

    state["positions"][code] = {
        "code": code,
        "name": candidate["name"],
        "direction": "LONG",
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp1_hit": False,
        "mode": mode,
        "created_at": now_kst().isoformat(),
    }

    print(
        f"[POSITION] "
        f"{candidate['name']} "
        f"ENTRY={entry}"
    )


# ============================================================
# POSITION MONITOR
# ============================================================

def monitor_positions(
    state,
    quotes
):

    positions = state.get(
        "positions",
        {}
    )

    if not positions:

        print(
            "추적 포지션 없음"
        )

        return

    print(
        f"기존 포지션 "
        f"{len(positions)}개 모니터링"
    )

    remove_codes = []

    for code, position in list(
        positions.items()
    ):

        quote = quotes.get(code)

        if not quote:

            print(
                f"[MONITOR] "
                f"{code} 실시간 데이터 없음"
            )

            continue

        current_price = float(
            quote["price"]
        )

        entry = float(
            position["entry"]
        )

        sl = float(
            position["sl"]
        )

        tp1 = float(
            position["tp1"]
        )

        tp2 = float(
            position["tp2"]
        )

        name = position.get(
            "name",
            code
        )

        # ================================================
        # SL
        # ================================================

        if current_price <= sl:

            message = (
                "🛑 [주식 손절 알림]\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📌 {name} ({code})\n\n"
                f"💰 현재가: "
                f"{fmt_price(current_price)}\n"
                f"🛡️ SL: "
                f"{fmt_price(sl)}\n"
                f"🟢 진입가: "
                f"{fmt_price(entry)}\n\n"
                "포지션 추적 종료"
            )

            send_telegram(
                message
            )

            remove_codes.append(
                code
            )

            continue

        # ================================================
        # TP2
        # ================================================

        if current_price >= tp2:

            message = (
                "🎯 [주식 TP2 도달]\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📌 {name} ({code})\n\n"
                f"💰 현재가: "
                f"{fmt_price(current_price)}\n"
                f"🎯 TP2: "
                f"{fmt_price(tp2)}\n"
                f"🟢 진입가: "
                f"{fmt_price(entry)}\n\n"
                "포지션 추적 종료"
            )

            send_telegram(
                message
            )

            remove_codes.append(
                code
            )

            continue

        # ================================================
        # TP1
        # ================================================

        if (
            current_price >= tp1
            and not position.get(
                "tp1_hit",
                False
            )
        ):

            position["tp1_hit"] = True

            position["sl"] = entry

            message = (
                "🎯 [주식 TP1 도달]\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📌 {name} ({code})\n\n"
                f"💰 현재가: "
                f"{fmt_price(current_price)}\n"
                f"🎯 TP1: "
                f"{fmt_price(tp1)}\n\n"
                f"🛡️ SL 이동: "
                f"{fmt_price(entry)}\n"
                "\n"
                "→ 손익분기점 보호 모드"
            )

            send_telegram(
                message
            )

    for code in remove_codes:

        if code in positions:
            del positions[code]


# ============================================================
# RUN SCAN
# ============================================================

def run_scan(
    state,
    mode,
    universe,
    quotes
):

    candidates = scan_candidates(
        universe,
        quotes,
        mode
    )

    print(
        f"{mode.upper()} candidates: "
        f"{len(candidates)}"
    )

    if not candidates:

        message = (
            f"🔎 [{mode_title(mode)}]\n\n"
            "현재 조건을 만족하는 "
            "후보 종목이 없습니다.\n\n"
            "기술적 조건과 거래대금 기준을 "
            "통과한 종목만 알림합니다."
        )

        send_telegram(
            message
        )

        return

    sent_count = 0

    for candidate in candidates:

        code = candidate["code"]

        if signal_recent(
            state,
            code,
            mode
        ):

            print(
                f"[DUPLICATE] "
                f"{candidate['name']} "
                f"{code}"
            )

            continue

        print(
            "\n===================================="
        )

        print(
            f"NEWS SEARCH: "
            f"{candidate['name']} "
            f"({code})"
        )

        news = get_recent_news(
            candidate["name"],
            code
        )

        print(
            f"NEWS FOUND: "
            f"{len(news)}"
        )

        message = build_candidate_message(
            candidate,
            mode,
            news
        )

        if send_telegram(
            message
        ):

            sent_count += 1

            mark_signal(
                state,
                code,
                mode
            )

            if mode == "close":

                create_position(
                    state,
                    candidate,
                    mode
                )

        time.sleep(1)

    print(
        f"개별 Telegram 알림: "
        f"{sent_count}개"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "===================================="
    )

    print(
        " KOREA STOCK HUNTER V6.2"
    )

    print(
        f" KST: {now_kst().isoformat()}"
    )

    print(
        "===================================="
    )

    mode = get_mode()

    print(
        f"MODE: {mode}"
    )

    print(
        f"FORCE_SCAN: {FORCE_SCAN_ENV or '-'}"
    )

    state = load_state()

    state["last_run"] = (
        now_kst().isoformat()
    )

    # ================================================
    # UNIVERSE
    # ================================================

    universe = get_universe()

    if universe.empty:

        send_telegram(
            "⚠️ [국장 자동화]\n\n"
            "KRX 종목 목록을 가져오지 못했습니다."
        )

        save_state(state)

        return

    codes = (
        universe["Code"]
        .astype(str)
        .str.zfill(6)
        .tolist()
    )

    # ================================================
    # REALTIME
    # ================================================

    quotes = get_realtime_quotes(
        codes
    )

    # ================================================
    # 기존 포지션 모니터링
    # ================================================

    monitor_positions(
        state,
        quotes
    )

    # ================================================
    # 실시간 데이터 부족
    # ================================================

    if len(quotes) < MIN_REALTIME_DATA:

        warning = (
            "⚠️ [국장 자동화]\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "실시간 데이터 부족\n\n"
            f"조회 성공: "
            f"{len(quotes)}/"
            f"{len(codes)}\n\n"
            "후보 검색을 중단했습니다.\n"
            "기존 포지션 TP/SL 추적만 수행했습니다."
        )

        send_telegram(
            warning
        )

        save_state(state)

        return

    # ================================================
    # SCAN
    # ================================================

    if mode in {
        "morning",
        "intraday",
        "close",
    }:

        run_scan(
            state,
            mode,
            universe,
            quotes
        )

    else:

        print(
            "현재 시각은 스크리닝 시간이 아니므로 "
            "포지션 모니터링만 실행합니다."
        )

    # ================================================
    # SAVE
    # ================================================

    save_state(state)

    print(
        "===================================="
    )

    print(
        "RUN COMPLETE"
    )

    print(
        f"추적 포지션: "
        f"{len(state.get('positions', {}))}개"
    )

    print(
        "===================================="
    )


if __name__ == "__main__":
    main()
