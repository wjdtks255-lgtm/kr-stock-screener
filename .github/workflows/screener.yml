# -*- coding: utf-8 -*-
"""
Korea Stock Rise Hunter - No KIS Account Authentication
Data source: Naver Finance public read-only endpoints.
Telegram: TELEGRAM_TOKEN + CHAT_ID only.

This bot:
1) Finds Korean stock candidates for morning and pre-close sessions.
2) Sends Telegram alerts with entry/SL/TP1/TP2.
3) Stores active positions in active_positions.json.
4) Checks active positions every scheduled run and sends TP1/TP2/SL alerts.
5) Does NOT place real orders.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

BASE = "https://m.stock.naver.com"
POLLING = "https://polling.finance.naver.com/api/realtime/domestic/stock"
STATE_FILE = "bot_state.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://m.stock.naver.com/",
}

TIMEOUT = 10
MAX_CANDIDATES = 80
MIN_PRICE = 1000
MIN_TURNOVER = 300_000_000  # 3억원
MAX_RESULTS = 5

session = requests.Session()
session.headers.update(HEADERS)


def now_kst():
    return datetime.now(timezone(timedelta(hours=9)))


def today_key():
    return now_kst().strftime("%Y-%m-%d")


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = session.post(url, data=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def get_json(url, params=None):
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"GET 실패: {url} / {e}")
        return None


def num(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except Exception:
        return 0.0


def money_korean(v):
    """'5조 7,942억' / '3,200억' / '12억' -> won."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    if not s:
        return 0
    total = 0
    try:
        if "조" in s:
            a, rest = s.split("조", 1)
            total += int(float(a.strip())) * 1_000_000_000_000
            s = rest
        if "억" in s:
            a, rest = s.split("억", 1)
            total += int(float(a.strip())) * 100_000_000
            s = rest
        if "만" in s:
            a, rest = s.split("만", 1)
            total += int(float(a.strip())) * 10_000
            s = rest
        if total == 0:
            return int(float(s))
        return total
    except Exception:
        return 0


def recursive_stock_items(obj):
    """Find dictionaries containing a stock code/name anywhere in a JSON payload."""
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
            name = x.get("name") or x.get("stockName") or x.get("itemName")
            if code and name and str(code).isdigit() and len(str(code)) == 6:
                key = str(code)
                if key not in seen:
                    seen.add(key)
                    found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


def normalize_rank_item(x):
    code = str(
        x.get("itemCode")
        or x.get("code")
        or x.get("symbolCode")
        or x.get("stockCode")
        or ""
    ).zfill(6)

    name = x.get("name") or x.get("stockName") or x.get("itemName") or code

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


def get_rankings(sort_type, category, pages=2, page_size=100):
    """Naver mobile ranking endpoint. Falls back to PC HTML if needed."""
    result = []

    for page in range(1, pages + 1):
        url = f"{BASE}/front-api/stock/domestic/stockList"
        params = {
            "sortType": sort_type,
            "category": category,
            "page": page,
            "pageSize": page_size,
        }
        data = get_json(url, params)
        if data:
            items = recursive_stock_items(data)
            for x in items:
                result.append(normalize_rank_item(x))
        time.sleep(0.15)

    # Fallback: public PC ranking pages
    if not result:
        import pandas as pd

        pc_map = {
            "quantTop": "https://finance.naver.com/sise/sise_quant.naver",
            "up": "https://finance.naver.com/sise/sise_rise.naver",
        }
        url = pc_map.get(sort_type)
        if url:
            for sosok in (0, 1):
                try:
                    html = session.get(
                        url,
                        params={"sosok": sosok, "page": 1},
                        timeout=TIMEOUT,
                    ).content
                    tables = pd.read_html(html)
                    for table in tables:
                        for _, row in table.iterrows():
                            code = str(row.iloc[1]).strip() if len(row) > 1 else ""
                            if code.isdigit() and len(code) == 6:
                                result.append({
                                    "code": code,
                                    "name": str(row.iloc[0]),
                                    "price": num(row.iloc[2]) if len(row) > 2 else 0,
                                    "change_pct": 0,
                                    "volume": 0,
                                    "turnover": 0,
                                })
                except Exception as e:
                    print(f"PC ranking fallback error: {e}")

    # unique
    out = {}
    for x in result:
        if x["code"].isdigit() and len(x["code"]) == 6:
            out[x["code"]] = x
    return list(out.values())


def get_realtime(code):
    data = get_json(f"{POLLING}/{code}")
    if not data or not data.get("datas"):
        return None
    d = data["datas"][0]

    return {
        "code": code,
        "name": d.get("stockName") or code,
        "price": num(d.get("closePriceRaw") or d.get("closePrice")),
        "change": num(
            d.get("compareToPreviousClosePriceRaw")
            or d.get("compareToPreviousClosePrice")
        ),
        "change_pct": num(
            d.get("fluctuationsRatioRaw") or d.get("fluctuationsRatio")
        ),
        "open": num(d.get("openPriceRaw") or d.get("openPrice")),
        "high": num(d.get("highPriceRaw") or d.get("highPrice")),
        "low": num(d.get("lowPriceRaw") or d.get("lowPrice")),
        "volume": num(
            d.get("accumulatedTradingVolumeRaw")
            or d.get("accumulatedTradingVolume")
        ),
        "turnover": money_korean(
            d.get("accumulatedTradingValueRaw")
            or d.get("accumulatedTradingValue")
        ),
        "market_status": d.get("marketStatus", ""),
        "trade_time": d.get("localTradedAt", ""),
    }


def get_history(code, page_size=35):
    url = f"{BASE}/api/stock/{code}/price"
    data = get_json(url, {"pageSize": page_size, "page": 1})
    if not data:
        return []

    # Usually data is a list, but handle nested shapes.
    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("price", "prices", "rows", "result", "data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        if not rows:
            # search recursively for date-like rows
            def walk(x):
                if isinstance(x, list):
                    for y in x:
                        if isinstance(y, dict) and (
                            y.get("localTradedAt") or y.get("date")
                        ):
                            rows.append(y)
                        else:
                            walk(y)
                elif isinstance(x, dict):
                    for y in x.values():
                        walk(y)
            walk(data)

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        date = r.get("localTradedAt") or r.get("date") or r.get("localDate")
        close = num(r.get("closePrice") or r.get("close"))
        high = num(r.get("highPrice") or r.get("high"))
        low = num(r.get("lowPrice") or r.get("low"))
        open_ = num(r.get("openPrice") or r.get("open"))
        vol = num(r.get("accumulatedTradingVolume") or r.get("volume"))
        if date and close > 0:
            out.append({
                "date": str(date)[:10],
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": vol,
            })
    return out


def pct(a, b):
    if not b:
        return 0.0
    return (a / b - 1.0) * 100.0


def calc_score(rt, hist, mode):
    if not rt or rt["price"] <= 0:
        return 0, {}

    p = rt["price"]
    chg = rt["change_pct"]
    turnover = rt["turnover"]
    high = rt["high"]
    low = rt["low"]

    if len(hist) < 6:
        return 0, {}

    closes = [x["close"] for x in hist if x["close"] > 0]
    if len(closes) < 6:
        return 0, {}

    # Hist is newest -> oldest in normal Naver response.
    latest_close = closes[0]
    prev_close = closes[1] if len(closes) > 1 else 0
    ma5 = sum(closes[:5]) / min(5, len(closes))
    ma20 = sum(closes[:20]) / min(20, len(closes))
    ret5 = pct(latest_close, closes[5]) if len(closes) > 5 else 0
    prior20_high = max([x["high"] for x in hist[1:21] if x["high"] > 0] or [0])

    # Current price relative to today's range.
    hold = 0.5
    if high > low > 0:
        hold = (p - low) / (high - low)

    # Volume vs 20-day average.
    vols = [x["volume"] for x in hist[1:21] if x["volume"] > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0
    vol_ratio = rt["volume"] / avg_vol if avg_vol else 0

    score = 0

    # Trend
    if ma5 > ma20:
        score += 15
    if len(closes) >= 11:
        ma10 = sum(closes[:10]) / 10
        if ma10 > ma20:
            score += 10

    # Daily strength
    if chg >= 5:
        score += 15
    elif chg >= 3:
        score += 12
    elif chg >= 1:
        score += 7
    elif chg < -2:
        score -= 15

    # 5-day momentum
    if 1.5 <= ret5 <= 15:
        score += 10
    elif ret5 > 15:
        score += 3

    # Volume acceleration
    if vol_ratio >= 3:
        score += 15
    elif vol_ratio >= 2:
        score += 10
    elif vol_ratio >= 1.5:
        score += 5

    # Breakout
    if prior20_high > 0:
        if p >= prior20_high:
            score += 15
        elif p >= prior20_high * 0.98:
            score += 10

    # Holding near high
    if hold >= 0.85:
        score += 15
    elif hold >= 0.70:
        score += 8

    # Turnover
    if turnover >= 5_000_000_000:
        score += 10
    elif turnover >= 1_000_000_000:
        score += 7
    elif turnover >= 500_000_000:
        score += 4

    # Avoid chasing a failed gap-up
    if mode == "morning" and chg >= 3 and hold < 0.60:
        score -= 20
    if mode == "close" and chg >= 7 and hold < 0.60:
        score -= 10

    score = max(0, min(100, int(score)))

    metrics = {
        "change_pct": chg,
        "ret5": ret5,
        "vol_ratio": vol_ratio,
        "hold": hold,
        "turnover": turnover,
        "ma5": ma5,
        "ma20": ma20,
        "prior20_high": prior20_high,
    }
    return score, metrics


def make_levels(price):
    # Conservative fixed levels; not a profit guarantee.
    entry = int(round(price / 10) * 10)
    sl = int(round(price * 0.95 / 10) * 10)
    tp1 = int(round(price * 1.03 / 10) * 10)
    tp2 = int(round(price * 1.06 / 10) * 10)
    return entry, sl, tp1, tp2


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "positions": {},
            "sent_signals": {},
            "last_run": "",
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("positions", {})
            data.setdefault("sent_signals", {})
            data.setdefault("last_run", "")
            return data
    except Exception:
        return {"positions": {}, "sent_signals": {}, "last_run": ""}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def signal_key(mode):
    return f"{today_key()}:{mode}"


def cleanup_old_signals(state):
    cutoff = (now_kst() - timedelta(days=5)).strftime("%Y-%m-%d")
    state["sent_signals"] = {
        k: v for k, v in state["sent_signals"].items()
        if k[:10] >= cutoff
    }


def format_signal(item, mode, score, metrics, entry, sl, tp1, tp2):
    if mode == "close":
        title = "🚨 <b>[종가 상승 후보]</b>"
        note = "⏰ 15:30 장 마감 전 매수 판단용"
    else:
        title = "🌅 <b>[장초 상승 후보]</b>"
        note = "⏰ 장초 상승 모멘텀 후보"

    url = f"https://finance.naver.com/item/main.naver?code={item['code']}"
    return (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{item['name']}</b> <code>({item['code']})</code>\n"
        f"💰 현재가: <code>{entry:,}원</code>\n"
        f"📈 등락률: <code>{metrics['change_pct']:+.2f}%</code>\n"
        f"💵 거래대금: <code>{metrics['turnover']/100_000_000:,.0f}억원</code>\n"
        f"📊 상승점수: <code>{score}/100</code>\n"
        f"📦 거래량/20일평균: <code>{metrics['vol_ratio']:.1f}배</code>\n"
        f"📈 5일 수익률: <code>{metrics['ret5']:+.2f}%</code>\n"
        f"💰 진입 기준: <code>{entry:,}원</code>\n"
        f"🛡 SL: <code>{sl:,}원</code>\n"
        f"🎯 TP1: <code>{tp1:,}원</code>\n"
        f"🎯 TP2: <code>{tp2:,}원</code>\n"
        f"🔗 <a href='{url}'>네이버 차트</a>\n"
        f"{note}\n"
        f"⚠️ 조건 기반 후보이며 상승을 보장하지 않습니다."
    )


def screen(mode):
    print(f"=== {mode} screening ===")

    # Two independent ranking pools: trading volume and rising stocks.
    pool = {}
    for category in ("KOSPI", "KOSDAQ"):
        for sort_type in ("quantTop", "up"):
            rows = get_rankings(sort_type, category, pages=2, page_size=100)
            for x in rows:
                if x["price"] >= MIN_PRICE:
                    pool[x["code"]] = x

    if not pool:
        send_telegram(
            f"⚠️ <b>[{mode.upper()} 스크리닝 오류]</b>\n"
            "시장 종목 목록을 가져오지 못했습니다.\n"
            "잠시 후 다시 실행하세요."
        )
        return

    # Limit expensive quote/history calls.
    rough = sorted(
        pool.values(),
        key=lambda x: (x.get("turnover", 0), x.get("change_pct", 0)),
        reverse=True,
    )[:MAX_CANDIDATES]

    candidates = []

    for item in rough:
        rt = get_realtime(item["code"])
        if not rt or rt["price"] < MIN_PRICE:
            continue

        # For a close candidate, do not select a falling stock.
        if mode == "close" and rt["change_pct"] < 0:
            continue

        # Basic liquidity filter.
        if rt["turnover"] and rt["turnover"] < MIN_TURNOVER:
            continue

        hist = get_history(item["code"], 35)
        score, metrics = calc_score(rt, hist, mode)

        threshold = 72
        if score >= threshold:
            merged = dict(item)
            merged.update(rt)
            candidates.append((score, merged, metrics))

        time.sleep(0.08)

    candidates.sort(
        key=lambda z: (
            z[0],
            z[1].get("turnover", 0),
            z[1].get("change_pct", 0),
        ),
        reverse=True,
    )
    candidates = candidates[:MAX_RESULTS]

    state = load_state()
    cleanup_old_signals(state)

    key = signal_key(mode)

    # Avoid sending the same mode more than once per day when scheduled.
    # Manual runs can force a scan by setting FORCE_SCAN=1.
    if state["sent_signals"].get(key) and os.getenv("FORCE_SCAN", "0") != "1":
        print(f"Already sent {key}")
        save_state(state)
        return

    if not candidates:
        send_telegram(
            f"🔎 <b>[{mode.upper()} 스크리닝 완료]</b>\n"
            f"오늘 현재 조건을 만족하는 상승 후보가 없습니다.\n"
            f"조건을 억지로 완화해서 종목을 보내지는 않습니다."
        )
        state["sent_signals"][key] = True
        save_state(state)
        return

    blocks = []
    for score, item, metrics in candidates:
        entry, sl, tp1, tp2 = make_levels(item["price"])

        # One active position per ticker.
        state["positions"][item["code"]] = {
            "code": item["code"],
            "name": item["name"],
            "mode": mode,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp1_hit": False,
            "status": "ACTIVE",
            "created_at": now_kst().isoformat(),
            "score": score,
        }

        blocks.append(
            format_signal(item, mode, score, metrics, entry, sl, tp1, tp2)
        )

    header = (
        "📡 <b>[한국 주식 상승 헌터]</b>\n"
        f"🕒 {now_kst().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    send_telegram(header + "\n\n".join(blocks))
    state["sent_signals"][key] = True
    save_state(state)


def monitor_positions():
    state = load_state()
    positions = state.get("positions", {})
    if not positions:
        return

    changed = False

    for code, pos in list(positions.items()):
        if pos.get("status") != "ACTIVE":
            continue

        rt = get_realtime(code)
        if not rt or rt["price"] <= 0:
            continue

        price = rt["price"]
        entry = num(pos.get("entry"))
        sl = num(pos.get("sl"))
        tp1 = num(pos.get("tp1"))
        tp2 = num(pos.get("tp2"))

        # TP2 has priority if the observed price reaches it.
        if price >= tp2:
            send_telegram(
                f"🔥 <b>[TP2 도달]</b>\n"
                f"📌 <b>{pos['name']}</b> <code>({code})</code>\n"
                f"💰 진입가: <code>{entry:,.0f}원</code>\n"
                f"🎯 TP2: <code>{tp2:,.0f}원</code>\n"
                f"📈 현재가: <code>{price:,.0f}원</code>\n"
                f"📊 수익률: <code>{pct(price, entry):+.2f}%</code>\n"
                f"✅ 포지션 감시 종료"
            )
            pos["status"] = "TP2"
            changed = True
            continue

        if price >= tp1 and not pos.get("tp1_hit", False):
            send_telegram(
                f"🎯 <b>[TP1 도달]</b>\n"
                f"📌 <b>{pos['name']}</b> <code>({code})</code>\n"
                f"💰 진입가: <code>{entry:,.0f}원</code>\n"
                f"🎯 TP1: <code>{tp1:,.0f}원</code>\n"
                f"📈 현재가: <code>{price:,.0f}원</code>\n"
                f"📊 수익률: <code>{pct(price, entry):+.2f}%</code>\n"
                f"➡️ TP2: <code>{tp2:,.0f}원</code> 계속 감시"
            )
            pos["tp1_hit"] = True
            changed = True

        if price <= sl:
            send_telegram(
                f"🛡️ <b>[SL 손절가 도달]</b>\n"
                f"📌 <b>{pos['name']}</b> <code>({code})</code>\n"
                f"💰 진입가: <code>{entry:,.0f}원</code>\n"
                f"🛡 SL: <code>{sl:,.0f}원</code>\n"
                f"📉 현재가: <code>{price:,.0f}원</code>\n"
                f"📊 수익률: <code>{pct(price, entry):+.2f}%</code>\n"
                f"⚠️ 포지션 감시 종료"
            )
            pos["status"] = "SL"
            changed = True

    # Keep only recent completed positions to prevent unlimited state growth.
    cutoff = now_kst() - timedelta(days=14)
    for code, pos in list(positions.items()):
        if pos.get("status") in ("TP2", "SL"):
            try:
                created = datetime.fromisoformat(pos.get("created_at", ""))
                if created < cutoff:
                    del positions[code]
                    changed = True
            except Exception:
                pass

    if changed:
        state["positions"] = positions
        save_state(state)


def get_mode():
    forced = os.getenv("MODE", "").strip().lower()
    if forced in ("morning", "close", "monitor"):
        return forced

    # Automatic mode based on KST.
    t = now_kst()
    hm = t.hour * 60 + t.minute

    # 09:00~09:30: morning scan
    if 9 * 60 <= hm <= 9 * 60 + 30:
        return "morning"

    # 15:00~15:20: pre-close scan
    if 15 * 60 <= hm <= 15 * 60 + 20:
        return "close"

    return "monitor"


def run():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN 또는 CHAT_ID가 없습니다.")

    mode = get_mode()
    print(f"KST={now_kst().isoformat()} mode={mode}")

    # Always monitor first.
    monitor_positions()

    if mode in ("morning", "close"):
        screen(mode)

    state = load_state()
    state["last_run"] = now_kst().isoformat()
    save_state(state)

    print("=== DONE ===")


if __name__ == "__main__":
    run()
