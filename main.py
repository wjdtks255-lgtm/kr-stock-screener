import os
import json
import math
import time
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import FinanceDataReader as fdr


# ============================================================
# KOREA STOCK HUNTER V5.2 (Reason Added)
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
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return DEFAULT_STATE.copy()
        state.setdefault("positions", {})
        state.setdefault("sent_signals", {})
        state.setdefault("last_run", "")
        return state
    except Exception as e:
        print("STATE LOAD ERROR:", repr(e))
        return DEFAULT_STATE.copy()


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("STATE SAVE ERROR:", repr(e))


# ============================================================
# TIME
# ============================================================

def now_kst():
    return datetime.now(KST)


def auto_mode():
    t = now_kst().strftime("%H:%M")
    if "09:05" <= t <= "09:35":
        return "morning"
    if "15:10" <= t <= "15:20":
        return "close"
    return "monitor"


def get_mode():
    if MODE_ENV in ("morning", "close", "monitor"):
        return MODE_ENV
    return auto_mode()


# ============================================================
# NUMBER
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
# STOCK LISTING NORMALIZATION
# ============================================================

def normalize_listing(df):
    if df is None or df.empty:
        return pd.DataFrame(columns=["Symbol", "Name", "Market"])

    df = df.copy()
    code_col = next((col for col in ["Code", "Symbol", "code", "symbol"] if col in df.columns), None)
    name_col = next((col for col in ["Name", "name", "종목명"] if col in df.columns), None)
    market_col = next((col for col in ["Market", "market", "시장구분"] if col in df.columns), None)

    if code_col is None:
        return pd.DataFrame(columns=["Symbol", "Name", "Market"])

    result = pd.DataFrame()
    result["Symbol"] = df[code_col].astype(str).str.replace(".0", "", regex=False).str.strip().str.zfill(6)
    result["Name"] = df[name_col].astype(str).str.strip() if name_col else ""
    result["Market"] = df[market_col].astype(str).str.strip() if market_col else "KRX"

    result = result[result["Symbol"].str.fullmatch(r"\d{6}")]
    bad_pattern = r"ETF|ETN|스팩|SPAC|리츠|REIT"
    result = result[~result["Name"].str.upper().str.contains(bad_pattern, na=False)]

    result = result.drop_duplicates(subset=["Symbol"]).reset_index(drop=True)
    return result[["Symbol", "Name", "Market"]]


def get_stock_listings():
    print()
    print("====================================")
    print(" STOCK LIST DOWNLOAD (KRX)")
    print("====================================")

    try:
        df = fdr.StockListing("KRX")
        normalized = normalize_listing(df)
        print(f"다운로드 성공: 총 {len(normalized)}개 종목")
        return normalized
    except Exception as e:
        print("KRX listing ERROR:", repr(e))
        return pd.DataFrame(columns=["Symbol", "Name", "Market"])


def get_price_data(code):
    try:
        start_date = (now_kst() - timedelta(days=120)).strftime("%Y-%m-%d")
        df = fdr.DataReader(code, start_date)
        if df is None or df.empty:
            return None
        df = df.copy()
        df.columns = [str(c).strip() for c in df.columns]
        required = ["Open", "High", "Low", "Close", "Volume"]
        for col in required:
            if col not in df.columns:
                return None
        df = df.dropna(subset=required)
        if len(df) < 25:
            return None
        return df
    except Exception:
        return None


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
    if close < MIN_PRICE or turnover < MIN_TURNOVER:
        return None

    prev_close = num(df["Close"].iloc[-2])
    change_pct = ((close / prev_close - 1) * 100) if prev_close > 0 else 0

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
# SCORE & REASON GENERATOR
# ============================================================

def calculate_score(df, mode):
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
    reasons = []

    if close > open_price:
        score += 10
        reasons.append("시가 대비 양봉 마감")

    if high > low:
        position = (close - low) / (high - low)
        if position >= 0.85:
            score += 15
            reasons.append("고가놀이 패턴 (상단 마감)")
        elif position >= 0.70:
            score += 10
            reasons.append("장중 강세 유지")
        elif position >= 0.55:
            score += 5

    if 1.0 <= change_pct <= 6.0:
        score += 15
        reasons.append(f"적정 상승률({change_pct:+.2f}%)")
    elif 0.3 <= change_pct < 1.0:
        score += 8
    elif 6.0 < change_pct <= 10.0:
        score += 7
        reasons.append(f"급등세 포착({change_pct:+.2f}%)")

    closes = df["Close"]
    ma5 = num(closes.tail(5).mean())
    ma20 = num(closes.tail(20).mean())

    if close > ma20:
        score += 10
        reasons.append("20일선 위 안착")
    if ma5 > ma20:
        score += 10
        reasons.append("단기 이평선 정배열")

    if len(df) >= 21:
        previous_high = num(closes.iloc[-21:-1].max())
        if previous_high > 0 and close >= previous_high:
            score += 15
            reasons.append("전고점 돌파 시도")

    if len(df) >= 21:
        avg_volume = num(df["Volume"].iloc[-21:-1].mean())
        if avg_volume > 0:
            volume_ratio = volume / avg_volume
            if volume_ratio >= 2.0:
                score += 15
                reasons.append(f"거래량 폭증 (평균 대비 {volume_ratio:.1f}배)")
            elif volume_ratio >= 1.5:
                score += 10
                reasons.append(f"거래량 증가 (평균 대비 {volume_ratio:.1f}배)")
            elif volume_ratio >= 1.2:
                score += 5

    reason_str = " | ".join(reasons) if reasons else "모멘텀 조건 충족"

    return {
        "score": score,
        "close": close,
        "change_pct": change_pct,
        "turnover": turnover,
        "reason": reason_str
    }


# ============================================================
# SCAN
# ============================================================

def scan_market(listing_df, mode):
    results = []
    total = len(listing_df)
    print(f"{mode.upper()} 검사 시작: {total}개")

    for index, row in listing_df.iterrows():
        code = str(row["Symbol"])
        name = str(row["Name"])
        market = str(row["Market"])

        df = get_price_data(code)
        if df is None:
            continue

        result = calculate_score(df, mode)
        if result is None:
            continue

        minimum_score = MIN_SCORE_MORNING if mode == "morning" else MIN_SCORE_CLOSE
        if result["score"] < minimum_score:
            continue

        result.update({"code": code, "name": name, "market": market})
        results.append(result)

        if len(results) >= 30:
            break
        time.sleep(REQUEST_SLEEP)

    results.sort(key=lambda x: (x["score"], x["change_pct"], x["turnover"]), reverse=True)
    return results


def scan_all(listings, mode):
    if listings.empty:
        return []

    kospi = listings[listings["Market"].astype(str).str.upper().str.contains("KOSPI", na=False)].copy()
    kosdaq = listings[listings["Market"].astype(str).str.upper().str.contains("KOSDAQ", na=False)].copy()

    if kospi.empty and kosdaq.empty:
        kospi = listings.head(300)
        kosdaq = listings.iloc[300:600]
    else:
        kospi = kospi.head(300)
        kosdaq = kosdaq.head(300)

    results = []
    for market_df in [kospi, kosdaq]:
        market_results = scan_market(market_df, mode)
        results.extend(market_results)

    results.sort(key=lambda x: (x["score"], x["change_pct"], x["turnover"]), reverse=True)
    return results[:MAX_RESULTS]


# ============================================================
# TELEGRAM MESSAGE WITH REASON
# ============================================================

def telegram_send(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.ok:
            return True
    except Exception as e:
        print("Telegram ERROR:", repr(e))
    return False


def build_signal_message(results, mode):
    if mode == "morning":
        title = "🌅 <b>KOREA STOCK HUNTER V5.2</b>\n장초 상승 후보 (이유 포함)"
        desc = "09:05~09:35 기준\n상승 모멘텀 조건을 만족한 종목"
    else:
        title = "🌙 <b>KOREA STOCK HUNTER V5.2</b>\n다음날 상승 후보 (이유 포함)"
        desc = "15:10~15:20 기준\n다음 거래일 모멘텀 후보"

    lines = [title, desc, "", f"⏰ {now_kst().strftime('%Y-%m-%d %H:%M:%S')}", ""]

    if not results:
        lines.append("❌ 조건을 만족하는 후보가 없습니다.")
        return "\n".join(lines)

    for i, r in enumerate(results, 1):
        price = r["close"]
        change = r["change_pct"]
        score = r["score"]
        reason = r.get("reason", "조건 충족")
        entry = price
        sl = price * 0.965
        tp1 = price * 1.03
        tp2 = price * 1.06

        lines.extend([
            f"<b>{i}. {r['name']}</b> ({r['code']})",
            f"시장: {r['market']} | Score: <b>{score}</b>",
            f"현재가: {price:,.0f}원 ({change:+.2f}%)",
            f"💡 <b>포착 사유:</b> {reason}",
            f"ENTRY: {entry:,.0f} | SL: {sl:,.0f}",
            f"TP1: {tp1:,.0f} | TP2: {tp2:,.0f}",
            ""
        ])

    lines.extend(["⚠ 기술적 지표 및 수급 조건 기반 분석입니다."])
    return "\n".join(lines)


# ============================================================
# POSITION REGISTER & MONITOR
# ============================================================

def register_positions(state, results):
    positions = state.setdefault("positions", {})
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
            "created_at": now_kst().isoformat()
        }


def monitor_positions(state):
    positions = state.get("positions", {})
    if not positions:
        return

    for code, pos in list(positions.items()):
        try:
            df = get_price_data(code)
            if df is None:
                continue

            price = num(df["Close"].iloc[-1])
            sl = num(pos.get("sl"))
            tp1 = num(pos.get("tp1"))
            tp2 = num(pos.get("tp2"))
            name = pos.get("name", code)

            if sl > 0 and price <= sl:
                telegram_send(f"🛑 <b>STOP LOSS</b>\n{name} ({code})\n현재가: {price:,.0f}")
                del positions[code]
                continue
            if tp2 > 0 and price >= tp2:
                telegram_send(f"🎯 <b>TP2 도달</b>\n{name} ({code})\n현재가: {price:,.0f}")
                del positions[code]
                continue
            if tp1 > 0 and price >= tp1 and not pos.get("tp1_sent", False):
                telegram_send(f"🎯 <b>TP1 도달</b>\n{name} ({code})\n현재가: {price:,.0f}")
                pos["tp1_sent"] = True
        except Exception:
            pass


def run_scan(mode):
    listings = get_stock_listings()
    if listings.empty:
        telegram_send("⚠️ 종목 데이터 소스 오류 발생")
        return

    results = scan_all(listings, mode)
    message = build_signal_message(results, mode)
    telegram_send(message)

    state = load_state()
    if results:
        register_positions(state, results)
    state["last_run"] = now_kst().isoformat()
    save_state(state)


def main():
    mode = get_mode()
    state = load_state()

    if mode == "monitor":
        monitor_positions(state)
        state["last_run"] = now_kst().isoformat()
        save_state(state)
    elif mode in ("morning", "close"):
        run_scan(mode)


if __name__ == "__main__":
    main()
