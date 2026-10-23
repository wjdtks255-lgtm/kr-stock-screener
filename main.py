#!/usr/bin/env python3
"""Korea intraday stock signal monitor using KIS real-time trade WebSocket.

This is a signals-and-simulated-tracking program only. It does not place orders.
It requires KIS Open API credentials and a persistent process during market hours.
This implementation conservatively scans at most 40 symbols per session; check
the broker's current subscription policy before changing that code-side cap.
"""

import asyncio
import html
import json
import logging
import os
import signal as os_signal
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests
import websockets

# ----------------------------- Configuration -----------------------------
VERSION = "1.3.0"
KST = timezone(timedelta(hours=9))

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "").strip()
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "").strip()
KIS_ENV = os.getenv("KIS_ENV", "real").strip().lower()  # real | demo
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()

STATE_FILE = Path(os.getenv("STOCK_SIGNAL_STATE", "stock_intraday_state.json"))
MAX_SLOTS = min(8, max(1, int(os.getenv("STOCK_MAX_POSITIONS", "8"))))
MAX_SYMBOLS = min(40, max(1, int(os.getenv("KIS_MAX_SYMBOLS", "40"))))
SLOT_BUDGET_KRW = float(os.getenv("STOCK_SLOT_BUDGET_KRW", "0"))
MIN_STOCK_PRICE_KRW = float(os.getenv("STOCK_MIN_PRICE_KRW", "1000"))
MAX_STOCK_PRICE_KRW = float(os.getenv("STOCK_MAX_PRICE_KRW", "50000"))
SYMBOLS_PER_PRICE_BAND = int(os.getenv("STOCK_SYMBOLS_PER_PRICE_BAND", "8"))
MIN_OPEN_GAIN_PCT = float(os.getenv("STOCK_MIN_OPEN_GAIN_PCT", "0.40"))
MIN_VOLUME_SURGE = float(os.getenv("STOCK_MIN_VOLUME_SURGE", "1.50"))
MAX_ENTRY_GAP_PCT = float(os.getenv("STOCK_MAX_ENTRY_GAP_PCT", "0.35"))
MAX_HOLDING_DAYS = int(os.getenv("STOCK_MAX_HOLDING_DAYS", "7"))
ROUND_TRIP_COST_PCT = float(os.getenv("STOCK_ROUND_TRIP_COST_PCT", "0"))
NEWS_POLL_SECONDS = max(60, int(os.getenv("STOCK_NEWS_POLL_SECONDS", "300")))
NEWS_ITEMS_PER_ALERT = max(1, min(5, int(os.getenv("STOCK_NEWS_ITEMS_PER_ALERT", "3"))))

BAR_SECONDS = 60
HISTORY_BARS = 80
BREAKOUT_LOOKBACK = 20
MIN_BARS_FOR_SIGNAL = 30
MAX_STOP_PCT = 0.045
MIN_STOP_PCT = 0.006
STOP_ATR_MULTIPLE = 1.5
TP_R = (1.5, 2.8, 4.2)
MARKET_OPEN = (9, 0)
MARKET_CLOSE = (15, 30)
NEW_SIGNALS_CUTOFF = (15, 15)

if KIS_ENV == "real":
    APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
    WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"
elif KIS_ENV == "demo":
    APPROVAL_URL = "https://openapivts.koreainvestment.com:29443/oauth2/Approval"
    WS_URL = "ws://ops.koreainvestment.com:31000/tryitout"
else:
    raise ValueError("KIS_ENV는 real 또는 demo여야 합니다.")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="[%(asctime)s KST] %(levelname)s %(message)s",
)
log = logging.getLogger("kis-intraday-signals")

RUNNING = True


def now():
    return datetime.now(KST)


def today_text():
    return now().strftime("%Y-%m-%d")


def in_market_hours():
    n = now()
    current = (n.hour, n.minute)
    return n.weekday() < 5 and MARKET_OPEN <= current < MARKET_CLOSE


def should_scan_new():
    n = now()
    return in_market_hours() and (n.hour, n.minute) < NEW_SIGNALS_CUTOFF


def log_time():
    return now().strftime("%Y-%m-%d %H:%M:%S KST")


def pct_from_entry(price, entry):
    entry = safe_number(entry, 0)
    return (safe_number(price, 0) / entry - 1) * 100 if entry > 0 else 0.0


def free_slot_number(positions):
    used = {int(p.get("slot_no", 0)) for p in positions.values() if p.get("slot_no")}
    return next((n for n in range(1, MAX_SLOTS + 1) if n not in used), None)


def default_state():
    return {
        "version": VERSION,
        "positions": {},
        "history": [],
        "last_signal_date": {},
        "last_market_notice": None,
        "last_alerted": {},
        "minute_bars": {},
        "news_seen": {},
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"상태 파일을 읽지 못했습니다: {STATE_FILE}: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("상태 파일 형식이 올바르지 않습니다.")
    defaults = default_state()
    for key, value in defaults.items():
        state.setdefault(key, value)
    if not isinstance(state["positions"], dict) or not isinstance(state["history"], list):
        raise RuntimeError("상태 파일의 포지션/이력 구조가 잘못됐습니다.")
    # Migrate older state files and keep slot numbers unique and stable.
    used = set()
    for symbol, position in state["positions"].items():
        slot = position.get("slot_no")
        if not isinstance(slot, int) or slot < 1 or slot > MAX_SLOTS or slot in used:
            slot = next((n for n in range(1, MAX_SLOTS + 1) if n not in used), None)
        if slot is not None:
            position["slot_no"] = slot
            used.add(slot)
        position.setdefault("last_price", position.get("entry", 0))
        position.setdefault("highest_tp", 0)
    state["version"] = VERSION
    return state


def save_state(state):
    state["version"] = VERSION
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, STATE_FILE)
    finally:
        if temp.exists():
            temp.unlink()


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN과 TELEGRAM_CHAT_ID(또는 CHAT_ID)를 설정하세요.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        response.raise_for_status()
        return bool(response.json().get("ok"))
    except Exception:
        log.exception("텔레그램 전송 실패")
        return False


def market_regime_previous_close():
    """Use completed prior-session index bars as context, never as live quotes."""
    result = {}
    for name, symbol in (("KOSPI", "KS11"), ("KOSDAQ", "KQ11")):
        start = (now().date() - timedelta(days=100)).isoformat()
        raw = fdr.DataReader(symbol, start)
        if raw is None or raw.empty or "Close" not in raw:
            raise RuntimeError(f"{name} 지수 데이터를 읽지 못했습니다.")
        df = raw.copy()
        df.index = pd.to_datetime(df.index).tz_localize(None) if getattr(pd.to_datetime(df.index), "tz", None) else pd.to_datetime(df.index)
        df = df.sort_index()
        # Exclude today's potentially incomplete daily bar; this is only a
        # previous-session regime context and not the intraday signal source.
        if df.index[-1].date() >= now().date():
            df = df.iloc[:-1]
        if len(df) < 25:
            raise RuntimeError(f"{name} 지수의 직전 확정 일봉이 부족합니다.")
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()
        latest_day = close.index[-1].date()
        if (now().date() - latest_day).days > 7:
            raise RuntimeError(f"{name} 지수 데이터가 오래됐습니다: {latest_day}")
        ma20 = close.rolling(20).mean().iloc[-1]
        day_change = (float(close.iloc[-1]) / float(close.iloc[-2]) - 1) * 100
        result[name] = {
            "date": latest_day.isoformat(),
            "close": float(close.iloc[-1]),
            "ma20": float(ma20),
            "day_change_pct": day_change,
            "below_ma20": bool(close.iloc[-1] < ma20),
        }
    bear = (
        result["KOSPI"]["below_ma20"] and result["KOSDAQ"]["below_ma20"]
    ) or result["KOSPI"]["day_change_pct"] <= -2.0 or result["KOSDAQ"]["day_change_pct"] <= -2.0
    return bear, result


def build_universe(active_symbols):
    """Select liquid candidates evenly across share-price bands, plus active slots."""
    active_symbols = sorted(set(active_symbols))
    if len(active_symbols) > MAX_SLOTS:
        raise RuntimeError(f"저장된 활성 포지션 {len(active_symbols)}개가 최대 슬롯 {MAX_SLOTS}개를 넘습니다. 기존 상태를 확인하세요.")
    if len(active_symbols) > MAX_SYMBOLS:
        raise RuntimeError("활성 포지션 수가 실시간 구독 상한보다 많습니다. KIS_MAX_SYMBOLS 설정을 확인하세요.")
    # Keep candidate subscriptions warm even when all slots are full. Signal
    # generation remains blocked while full, but a freed slot can resume without
    # needing to restart the WebSocket connection.
    custom = os.getenv("STOCK_SYMBOLS", "").strip()
    if custom:
        ranked = [x.strip().zfill(6) for x in custom.split(",") if x.strip()]
        ranked = list(dict.fromkeys(ranked))
    else:
        listing = fdr.StockListing("KRX")
        if listing is None or listing.empty or "Code" not in listing or "Name" not in listing:
            raise RuntimeError("KRX 종목 목록을 가져올 수 없습니다.")
        listing = listing.dropna(subset=["Code", "Name"]).copy()
        listing["Code"] = listing["Code"].astype(str).str.zfill(6)
        price_col = next((c for c in ("Close", "close", "Price", "price") if c in listing.columns), None)
        if price_col is None:
            raise RuntimeError("가격대별 감시를 위해 KRX 목록의 최근가 필드가 필요합니다.")
        listing["_price"] = pd.to_numeric(listing[price_col], errors="coerce")
        max_price = min(MAX_STOCK_PRICE_KRW, SLOT_BUDGET_KRW) if SLOT_BUDGET_KRW > 0 else MAX_STOCK_PRICE_KRW
        listing = listing[(listing["_price"] >= MIN_STOCK_PRICE_KRW) & (listing["_price"] <= max_price)]
        # Avoid common preferred shares and special-purpose securities where
        # recognizable; the user can still explicitly provide a custom list.
        listing = listing[~listing["Name"].astype(str).str.contains(r"우선주|우B|우C|스팩|리츠", regex=True)]
        amount_col = next((c for c in ("Amount", "amount", "TradingValue", "거래대금") if c in listing.columns), None)
        if amount_col:
            listing["_liquidity"] = pd.to_numeric(listing[amount_col], errors="coerce").fillna(0)
        else:
            volume_col = next((c for c in ("Volume", "volume", "거래량") if c in listing.columns), None)
            listing["_liquidity"] = pd.to_numeric(listing[volume_col], errors="coerce").fillna(0) if volume_col else 0

        # Fixed bands produce variety in nominal share price instead of
        # allowing a single market-cap rank to dominate the whole universe.
        edges = [MIN_STOCK_PRICE_KRW] + [x for x in (5000, 10000, 20000, 30000, 50000, 100000) if MIN_STOCK_PRICE_KRW < x < max_price] + [max_price]
        edges = sorted(set(edges))
        ranked = []
        for low, high in zip(edges, edges[1:]):
            band = listing[(listing["_price"] >= low) & (listing["_price"] < high)]
            if high == max_price:
                band = listing[(listing["_price"] >= low) & (listing["_price"] <= high)]
            band = band.sort_values("_liquidity", ascending=False).head(SYMBOLS_PER_PRICE_BAND)
            ranked.extend(band["Code"].tolist())

    # Keep every open signal subscribed first, then fill remaining slots with
    # the configured/listed candidate universe. This cannot monitor >40 names.
    ordered = list(dict.fromkeys(active_symbols + ranked))
    if len(active_symbols) > MAX_SYMBOLS:
        raise RuntimeError(f"활성 포지션 {len(active_symbols)}개가 KIS 구독 상한 {MAX_SYMBOLS}개를 넘었습니다.")
    return ordered[:MAX_SYMBOLS]


def get_approval_key():
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET 환경변수를 설정해야 합니다.")
    response = requests.post(
        APPROVAL_URL,
        json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "secretkey": KIS_APP_SECRET,
        },
        headers={"content-type": "application/json; charset=utf-8"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    key = data.get("approval_key")
    if not key:
        raise RuntimeError(f"KIS WebSocket 접속키 발급 응답에 approval_key가 없습니다: {data}")
    return key


def subscription_message(approval_key, symbol, tr_type="1"):
    return {
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": tr_type,
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": "H0STCNT0", "tr_key": symbol}},
    }


def parse_trade_frame(raw):
    """Parse plain H0STCNT0 payload; ignore JSON system/heartbeat messages."""
    if not isinstance(raw, str) or not raw or raw[0] not in ("0", "1"):
        return None
    parts = raw.split("|", 3)
    if len(parts) != 4 or parts[1] != "H0STCNT0":
        return None
    values = parts[3].split("^")
    if len(values) < 34:
        return None
    try:
        symbol = values[0].zfill(6)
        trade_time = values[1]
        price = abs(float(values[2]))
        open_price = abs(float(values[7]))
        high_price = abs(float(values[8]))
        low_price = abs(float(values[9]))
        tick_volume = max(0.0, abs(float(values[12])))
        cumulative_volume = max(0.0, abs(float(values[13])))
        cumulative_value = max(0.0, abs(float(values[14])))
        business_date = values[33]
        if not symbol or price <= 0 or len(trade_time) < 6:
            return None
        return {
            "symbol": symbol,
            "time": trade_time[:6],
            "date": business_date,
            "price": price,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "tick_volume": tick_volume,
            "cum_volume": cumulative_volume,
            "cum_value": cumulative_value,
        }
    except (ValueError, IndexError):
        return None


def safe_number(value, default=0.0):
    try:
        x = float(value)
        return x if np.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def fetch_company_news(symbol, name):
    """Fetch related headlines from Google News RSS; do not infer sentiment."""
    query = f'"{name}" {symbol} 주식'
    response = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=8,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for item in root.findall("./channel/item")[:NEWS_ITEMS_PER_ALERT]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        source_node = item.find("source")
        source = (source_node.text or "Google News").strip() if source_node is not None else "Google News"
        if title and link:
            items.append({"id": link, "title": title, "link": link, "date": pubdate, "source": source})
    return items


def classify_news_title(title):
    """Conservative keyword label only; this is not a verified sentiment model."""
    text = title.lower()
    negative = ("실적 하향", "적자전환", "영업손실", "계약 해지", "공급계약 해지", "유상증자", "횡령", "배임", "상장폐지", "거래정지", "소송", "리콜", "감사의견 거절", "불성실공시")
    positive = ("공급계약 체결", "수주", "흑자전환", "최대 실적", "실적 개선", "자사주 매입", "배당 확대", "승인 획득", "신약 허가", "사업 선정")
    if any(word in text for word in negative):
        return "부정 키워드 감지(원문 확인 필요)"
    if any(word in text for word in positive):
        return "긍정 키워드 감지(원문 확인 필요)"
    return "중립/자동 분류 불가"


async def poll_open_position_news(state):
    active = list(state["positions"].keys())
    if not active:
        return
    names = [SYMBOL_NAMES.get(code, code) for code in active]
    results = await asyncio.gather(
        *(asyncio.to_thread(fetch_company_news, code, name) for code, name in zip(active, names)),
        return_exceptions=True,
    )
    changed = False
    for code, name, result in zip(active, names, results):
        if isinstance(result, Exception):
            log.warning("뉴스 조회 실패 %s: %s", code, result)
            continue
        items = result
        position = state["positions"].get(code)
        if position is None:
            continue
        previous_ids = state["news_seen"].get(code)
        if previous_ids is None:
            # First poll sends a compact current-news snapshot, then only new
            # RSS entries trigger subsequent alerts.
            new_items = items[:2]
        else:
            previous_set = set(previous_ids)
            new_items = [item for item in items if item["id"] not in previous_set]
        state["news_seen"][code] = [item["id"] for item in items][:50]
        position["latest_news"] = items[:NEWS_ITEMS_PER_ALERT]
        changed = True
        if new_items:
            entry = safe_number(position.get("entry"), 0)
            price = safe_number(position.get("last_price"), entry)
            pnl = pct_from_entry(price, entry)
            lines = [
                f"📰 <b>[슬롯 {position.get('slot_no', '?')}/{MAX_SLOTS} 뉴스·공시 감시] {html.escape(name)} ({code})</b>",
                f"현재가 {price:,.0f}원 | 최초 진입가 대비 {pnl:+.2f}% | 사용 슬롯 {len(state['positions'])}/{MAX_SLOTS}",
                "아래 분류는 제목 키워드 참고용이며 원문과 공시 내용을 확인해야 합니다.",
            ]
            for item in new_items[:NEWS_ITEMS_PER_ALERT]:
                title = html.escape(item["title"])
                source = html.escape(item["source"])
                link = html.escape(item["link"], quote=True)
                date = html.escape(item["date"])
                label = html.escape(classify_news_title(item["title"]))
                lines.append(f"• <a href=\"{link}\">{title}</a>\n  분류: {label}\n  출처: {source} ({date})")
            lines.append(f"확인 시각: {log_time()}")
            if not await asyncio.to_thread(send_telegram, "\n".join(lines)):
                log.error("뉴스 알림 전송 실패: %s", code)
    if changed:
        save_state(state)


class SignalEngine:
    def __init__(self, state, universe, market):
        self.state = state
        self.universe = set(universe)
        self.market_bear = market[0]
        self.market_values = market[1]
        saved_bars = state.setdefault("minute_bars", {})
        self.bars = {}
        today_key = now().strftime("%Y%m%d")
        for symbol in universe:
            restored = deque(maxlen=HISTORY_BARS)
            for saved in saved_bars.get(symbol, []):
                key = saved.get("key", [])
                if len(key) == 2 and key[0] == today_key:
                    restored.append(saved)
            self.bars[symbol] = restored
        self.current_bar = {}
        self.session = {}
        self.last_tick_time = {}

    def technical_flags(self, symbol, completed, session):
        bars = list(self.bars[symbol])
        closes = [bar["close"] for bar in bars]
        ema8 = self._ema(closes[-30:], 8) if len(closes) >= 8 else None
        ema21 = self._ema(closes[-50:], 21) if len(closes) >= 21 else None
        prior_volumes = [bar["volume"] for bar in bars[-21:-1]]
        average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
        volume_ratio = completed["volume"] / average_volume if average_volume > 0 else 0
        vwap = safe_number(session.get("vwap"), 0)
        previous_close = bars[-2]["close"] if len(bars) >= 2 else completed["open"]
        change_pct = (completed["close"] / previous_close - 1) * 100 if previous_close > 0 else 0
        return {
            "ema_bearish": bool(ema8 is not None and ema21 is not None and ema8 <= ema21),
            "below_vwap": bool(vwap > 0 and completed["close"] < vwap),
            "volume_surge": bool(volume_ratio >= MIN_VOLUME_SURGE),
            "volume_drop": bool(0 < volume_ratio <= 0.50),
            "sudden_drop": bool(change_pct <= -1.0),
            "volume_ratio": volume_ratio,
            "change_pct": change_pct,
        }

    def monitor_technical_change(self, symbol, completed, session):
        position = self.state["positions"].get(symbol)
        if not position:
            return
        flags = self.technical_flags(symbol, completed, session)
        previous = position.get("technical_flags")
        position["technical_flags"] = {key: flags[key] for key in ("ema_bearish", "below_vwap", "volume_surge", "volume_drop", "sudden_drop")}
        if not isinstance(previous, dict):
            save_state(self.state)
            return
        labels = {
            "ema_bearish": "8EMA/21EMA 약세 전환",
            "below_vwap": "VWAP 아래로 이탈",
            "volume_surge": "1분 거래량 급증 구간",
            "volume_drop": "거래량 급감 구간",
            "sudden_drop": "1분봉 급락(-1% 이상)",
        }
        changes = []
        for key, label in labels.items():
            if previous.get(key) != flags[key]:
                status = "발생" if flags[key] else "해소/회복"
                changes.append(f"• {label}: {status}")
        if changes:
            entry = safe_number(position.get("entry"), 0)
            pnl = pct_from_entry(completed["close"], entry)
            message = (
                f"⚠️ <b>[슬롯 {position.get('slot_no', '?')}/{MAX_SLOTS} 기술 변화] {self.symbol_name(symbol)} ({symbol})</b>\n"
                f"현재가 {completed['close']:,.0f}원 | 최초 진입가 대비 {pnl:+.2f}% | 사용 슬롯 {len(self.state['positions'])}/{MAX_SLOTS}\n"
                + "\n".join(changes)
                + f"\n거래량 비율: {flags['volume_ratio']:.2f}배 | 1분 변화: {flags['change_pct']:+.2f}%"
                + "\n관찰 알림이며 자동 매수·매도 주문은 실행하지 않습니다."
            )
            if not send_telegram(message):
                log.error("기술 변화 알림 전송 실패: %s", symbol)
        save_state(self.state)

    def _session_state(self, tick):
        symbol = tick["symbol"]
        date_key = tick["date"] or today_text().replace("-", "")
        current = self.session.get(symbol)
        if current is None or current["date"] != date_key:
            current = {
                "date": date_key,
                "open": tick["open"] or tick["price"],
                "cum_value": tick["cum_value"],
                "cum_volume": tick["cum_volume"],
                "vwap": tick["price"],
                "high": tick["high"],
            }
            self.session[symbol] = current
        else:
            # Cumulative API totals are preferred; derive a vwap fallback from
            # recent tick data only if cumulative fields are absent.
            current["cum_value"] = max(current["cum_value"], tick["cum_value"])
            current["cum_volume"] = max(current["cum_volume"], tick["cum_volume"])
            if current["cum_volume"] > 0 and current["cum_value"] > 0:
                current["vwap"] = current["cum_value"] / current["cum_volume"]
            current["high"] = max(current["high"], tick["high"], tick["price"])
        return current

    def _minute_key(self, tick):
        return (tick["date"], tick["time"][:4])

    def on_tick(self, tick):
        symbol = tick["symbol"]
        if symbol not in self.universe:
            return
        minute_key = self._minute_key(tick)
        session = self._session_state(tick)
        bar = self.current_bar.get(symbol)
        if bar is None:
            self.current_bar[symbol] = {
                "key": minute_key,
                "open": tick["price"],
                "high": tick["price"],
                "low": tick["price"],
                "close": tick["price"],
                "volume": tick["tick_volume"],
            }
        elif bar["key"] == minute_key:
            bar["high"] = max(bar["high"], tick["price"])
            bar["low"] = min(bar["low"], tick["price"])
            bar["close"] = tick["price"]
            bar["volume"] += tick["tick_volume"]
        else:
            completed = dict(bar)
            self.bars[symbol].append(completed)
            self.state["minute_bars"][symbol] = list(self.bars[symbol])
            save_state(self.state)
            self.current_bar[symbol] = {
                "key": minute_key,
                "open": tick["price"],
                "high": tick["price"],
                "low": tick["price"],
                "close": tick["price"],
                "volume": tick["tick_volume"],
            }
            self.evaluate_signal(symbol, completed, session)
            self.monitor_technical_change(symbol, completed, session)

        self.last_tick_time[symbol] = time.time()
        self.monitor_position(symbol, tick["price"], tick)

    @staticmethod
    def _ema(values, span):
        if len(values) < span:
            return None
        alpha = 2.0 / (span + 1)
        result = float(values[0])
        for value in values[1:]:
            result = alpha * float(value) + (1.0 - alpha) * result
        return result

    def evaluate_signal(self, symbol, completed, session):
        if self.market_bear or not should_scan_new():
            return
        if len(self.state["positions"]) >= MAX_SLOTS or symbol in self.state["positions"]:
            return
        if self.state["last_signal_date"].get(symbol) == today_text():
            return
        bars = list(self.bars[symbol])
        if len(bars) < MIN_BARS_FOR_SIGNAL:
            return
        prior = bars[-(BREAKOUT_LOOKBACK + 1):-1]
        if len(prior) < BREAKOUT_LOOKBACK:
            return
        closes = [b["close"] for b in bars]
        ema8 = self._ema(closes[-30:], 8)
        ema21 = self._ema(closes[-50:], 21)
        if ema8 is None or ema21 is None or ema8 <= ema21:
            return
        prior_high = max(b["high"] for b in prior)
        prior_volume = sum(b["volume"] for b in prior) / len(prior)
        volume_ratio = completed["volume"] / prior_volume if prior_volume > 0 else 0.0
        reference_price = completed["close"]
        live_price = safe_number(self.current_bar[symbol]["close"], reference_price)
        if reference_price <= 0 or live_price <= 0:
            return
        if live_price < MIN_STOCK_PRICE_KRW or live_price > MAX_STOCK_PRICE_KRW:
            return
        if SLOT_BUDGET_KRW > 0 and live_price > SLOT_BUDGET_KRW:
            return
        entry_gap_pct = abs(live_price / reference_price - 1) * 100
        open_price = safe_number(session.get("open"), 0)
        day_gain_pct = (live_price / open_price - 1) * 100 if open_price > 0 else 0
        vwap = safe_number(session.get("vwap"), 0)
        close_location = (completed["close"] - completed["low"]) / max(completed["high"] - completed["low"], 1e-9)

        if live_price <= prior_high:
            return
        if volume_ratio < MIN_VOLUME_SURGE:
            return
        if day_gain_pct < MIN_OPEN_GAIN_PCT:
            return
        if vwap <= 0 or live_price < vwap:
            return
        if close_location < 0.65 or entry_gap_pct > MAX_ENTRY_GAP_PCT:
            return

        entry = live_price
        recent_ranges = [max(b["high"] - b["low"], 0) for b in bars[-15:]]
        atr1m = sum(recent_ranges) / len(recent_ranges) if recent_ranges else entry * MIN_STOP_PCT
        stop_pct = min(MAX_STOP_PCT, max(MIN_STOP_PCT, STOP_ATR_MULTIPLE * atr1m / entry))
        risk = entry * stop_pct
        slot_no = free_slot_number(self.state["positions"])
        if slot_no is None:
            return
        position = {
            "symbol": symbol,
            "slot_no": slot_no,
            "entry": entry,
            "sl": entry - risk,
            "tp1": entry + risk * TP_R[0],
            "tp2": entry + risk * TP_R[1],
            "tp3": entry + risk * TP_R[2],
            "risk_pct": stop_pct * 100,
            "slot_budget_krw": SLOT_BUDGET_KRW,
            "estimated_max_shares": int(SLOT_BUDGET_KRW / entry) if SLOT_BUDGET_KRW > 0 else None,
            "technical_flags": {"ema_bearish": False, "below_vwap": False, "volume_surge": True, "volume_drop": False, "sudden_drop": False},
            "volume_ratio": volume_ratio,
            "day_gain_pct": day_gain_pct,
            "vwap": vwap,
            "opened_at": log_time(),
            "opened_date": today_text(),
            "signal_bar": f"{completed['key'][0]} {completed['key'][1]}",
            "highest_tp": 0,
            "last_price": entry,
        }
        message = self.signal_message(position)
        # Only register the virtual slot after Telegram accepted the signal.
        if not send_telegram(message):
            log.error("신호 전송 실패: %s; 슬롯에 등록하지 않습니다.", symbol)
            return
        self.state["positions"][symbol] = position
        self.state["last_signal_date"][symbol] = today_text()
        save_state(self.state)
        log.info("장중 신호 발송 %s entry=%s volume=%.2fx", symbol, entry, volume_ratio)
        if len(self.state["positions"]) >= MAX_SLOTS and self.state.get("last_slot_full_notice") != today_text():
            if send_telegram(f"⛔ <b>슬롯 전체 사용 {len(self.state['positions'])}/{MAX_SLOTS}</b>\n신규 종목 탐색을 중단하고 기존 슬롯의 가격·뉴스·거래량 변화를 계속 감시합니다.\n{log_time()}"):
                self.state["last_slot_full_notice"] = today_text()
                save_state(self.state)

    def signal_message(self, p):
        name = self.symbol_name(p["symbol"])
        lines = [
            "🚨 <b>[장중 변동성 종목 포착 리포트]</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            f"📌 포트폴리오 슬롯: [ {p['slot_no']} / {MAX_SLOTS} ]",
            f"🪙 종목명: <b>{name} ({p['symbol']})</b>",
            f"💰 추정 진입가: {p['entry']:,.0f}원 (기준 0.00%)",
        ]
        if p.get("slot_budget_krw", 0) > 0:
            lines.append(f"슬롯 예산 {p['slot_budget_krw']:,.0f}원 기준 약 {p['estimated_max_shares']}주 (거래비용 별도)")
        lines.extend([
            "",
            "🔥 <b>[상승 조건 — 관측된 사실]</b>",
            "• 기술적 조건: 1분봉 20분 고점 돌파 + 8EMA/21EMA 정배열",
            f"• 거래량: 직전 20분 평균 대비 {p['volume_ratio']:.2f}배",
            f"• 가격 위치: 당일 시가 대비 {p['day_gain_pct']:+.2f}% | VWAP 상회",
            f"• 신호봉: {p['signal_bar']} KST, 종가가 봉 고가 영역",
            "• 뉴스/공시: 활성 슬롯 종목만 RSS 제목을 감시하며 분류는 키워드 참고용",
            "",
            f"🛡 손절가 (SL): {p['sl']:,.0f}원 (-{p['risk_pct']:.1f}%)",
            f"🎯 1차 목표가 (TP1): {p['tp1']:,.0f}원 (+{TP_R[0] * p['risk_pct']:.1f}%)",
            f"🎯 2차 목표가 (TP2): {p['tp2']:,.0f}원 (+{TP_R[1] * p['risk_pct']:.1f}%)",
            f"🎯 3차 목표가 (TP3): {p['tp3']:,.0f}원 (+{TP_R[2] * p['risk_pct']:.1f}%)",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "💡 가상 추적 기준이며 주문은 실행하지 않습니다. TP1/TP2 도달 시 추적 손절 기준을 올립니다.",
            "⚠️ 검증된 70% 승률이 아니며 승률 보장이 아닙니다. 실제 체결·비용은 다를 수 있습니다.",
            f"📊 차트: https://www.tradingview.com/chart/?symbol=KRX%3A{p['symbol']}",
            f"📍 슬롯 현황: {len(self.state['positions']) + 1}/{MAX_SLOTS} 사용 예정",
            f"발송: {log_time()}",
        ])
        return "\n".join(lines)

    @staticmethod
    def symbol_name(symbol):
        return html.escape(SYMBOL_NAMES.get(symbol, symbol))

    def monitor_position(self, symbol, price, tick):
        position = self.state["positions"].get(symbol)
        if not position or price <= 0:
            return
        position["last_price"] = price
        try:
            age_days = (now().date() - datetime.strptime(position["opened_date"], "%Y-%m-%d").date()).days
        except (KeyError, ValueError):
            age_days = 0
        if age_days >= MAX_HOLDING_DAYS:
            self.close_position(symbol, price, f"최대 보유기간 {MAX_HOLDING_DAYS}일 종료", tick)
            return
        # Close the virtual signal at the observed tick, not an assumed exact
        # stop fill. This allows gap/slippage effects to appear in gross P&L.
        if price <= float(position["sl"]):
            self.close_position(symbol, price, "손절/상향 스탑 도달", tick)
            return
        tp_level = int(position.get("highest_tp", 0))
        if price >= float(position["tp1"]) and tp_level < 1:
            position["highest_tp"] = 1
            position["sl"] = float(position["entry"])
            self.tp_notice(symbol, 1, price, position)
            tp_level = 1
        if price >= float(position["tp2"]) and tp_level < 2:
            position["highest_tp"] = 2
            position["sl"] = float(position["tp1"])
            self.tp_notice(symbol, 2, price, position)
        if price >= float(position["tp3"]):
            self.close_position(symbol, price, "TP3 도달", tick)

    def tp_notice(self, symbol, level, price, position):
        pnl = pct_from_entry(price, position["entry"])
        message = (
            f"🎯 <b>[슬롯 {position.get('slot_no', '?')}/{MAX_SLOTS}] TP{level} 목표가 도달</b>\n"
            f"{self.symbol_name(symbol)} ({symbol})\n"
            f"최초 진입가: {position['entry']:,.0f}원 | 도달가: {price:,.0f}원\n"
            f"진입가 대비: <b>{pnl:+.2f}%</b> | 가상 추적 손절 기준: {position['sl']:,.0f}원\n"
            f"슬롯 현황: {len(self.state['positions'])}/{MAX_SLOTS} 사용 중\n"
            "실제 부분청산은 수행하지 않습니다."
        )
        if not send_telegram(message):
            log.error("TP%d 안내 전송 실패: %s", level, symbol)
        save_state(self.state)

    def close_position(self, symbol, exit_price, reason, tick):
        position = self.state["positions"].pop(symbol, None)
        if not position:
            return
        entry = float(position["entry"])
        gross_pct = (exit_price / entry - 1) * 100
        net_pct = gross_pct - ROUND_TRIP_COST_PCT
        result = "WIN" if net_pct > 0 else "LOSS" if net_pct < 0 else "BREAKEVEN"
        record = {
            **position,
            "exit": float(exit_price),
            "exit_at": log_time(),
            "exit_reason": reason,
            "gross_pct": gross_pct,
            "cost_assumption_pct": ROUND_TRIP_COST_PCT,
            "net_pct": net_pct,
            "result": result,
        }
        self.state["history"].append(record)
        self.state["history"] = self.state["history"][-2000:]
        save_state(self.state)
        result_ko = {"WIN": "비용 가정 후 플러스", "LOSS": "비용 가정 후 마이너스", "BREAKEVEN": "본전"}[result]
        slot_no = position.get("slot_no", "?")
        message = (
            "🏁 <b>장중 시그널 가상 추적 종료</b>\n"
            f"슬롯 {slot_no}번 | {self.symbol_name(symbol)} ({symbol}) | {reason}\n"
            f"최초 진입가 {entry:,.0f}원 → 관측 종료가 {exit_price:,.0f}원\n"
            f"진입가 대비: <b>{gross_pct:+.2f}%</b> | 비용 가정 차감 후 {net_pct:+.2f}% ({result_ko})\n"
            f"슬롯 해제 완료 | 사용 슬롯 {len(self.state['positions'])}/{MAX_SLOTS} | 잔여 {MAX_SLOTS-len(self.state['positions'])}개\n"
            f"왕복 비용 가정: {ROUND_TRIP_COST_PCT:.3f}% | 종료: {log_time()}\n"
            "※ 가격 도달 기반 가상 추적이며 실제 체결을 의미하지 않습니다."
        )
        if not send_telegram(message):
            log.error("종료 알림 전송 실패: %s", symbol)
        if len(self.state["positions"]) < MAX_SLOTS:
            send_telegram(
                f"🔄 <b>신규 종목 탐색 재개</b>\n슬롯 {slot_no}번 해제 완료\n"
                f"사용 슬롯 {len(self.state['positions'])}/{MAX_SLOTS} | 잔여 {MAX_SLOTS-len(self.state['positions'])}개\n{log_time()}"
            )
        self.send_performance()

    def send_performance(self):
        rows = self.state["history"]
        if not rows:
            return
        wins = sum(row["result"] == "WIN" for row in rows)
        losses = sum(row["result"] == "LOSS" for row in rows)
        be = sum(row["result"] == "BREAKEVEN" for row in rows)
        n = len(rows)
        winrate = 100 * wins / n if n else 0
        avg_net = sum(float(row["net_pct"]) for row in rows) / n if n else 0
        send_telegram(
            "📊 <b>장중 시그널 가상 추적 성과</b>\n"
            f"종료 {n}건 | 플러스 {wins} | 마이너스 {losses} | 본전 {be}\n"
            f"플러스 비율 {winrate:.1f}% | 평균 손익률 {avg_net:+.2f}%\n"
            f"기준 왕복비용 {ROUND_TRIP_COST_PCT:.3f}%\n"
            "※ 미청산 포지션 제외, 기준가 체결을 가정한 모의 기록입니다."
        )

    def status(self):
        positions = self.state["positions"]
        lines = [f"📍 <b>장중 감시 슬롯 {len(positions)}/{MAX_SLOTS}</b>"]
        for symbol, p in positions.items():
            price = float(p.get("last_price", p["entry"]))
            pnl = (price / float(p["entry"]) - 1) * 100
            lines.append(
                f"슬롯 {p.get('slot_no', '?')}: {self.symbol_name(symbol)} ({symbol}) {price:,.0f}원 ({pnl:+.2f}%) | TP{p.get('highest_tp', 0)} 단계"
                f"\n  SL {p['sl']:,.0f} | TP1 {p['tp1']:,.0f} | TP2 {p['tp2']:,.0f} | TP3 {p['tp3']:,.0f}"
            )
        lines.append(f"Universe {len(self.universe)}종목 / 실시간 WebSocket | {log_time()}")
        return "\n".join(lines)


SYMBOL_NAMES = {}


def market_notice_text(market):
    bear, data = market
    state = "방어 필터 작동 — 신규 신호 차단" if bear else "장중 신호 탐지 가능"
    return (
        "📉 <b>장 시작 시장 컨텍스트</b> (직전 확정 일봉 기준)\n"
        f"코스피 {data['KOSPI']['date']} 종가 {data['KOSPI']['close']:,.2f} / 20일선 {data['KOSPI']['ma20']:,.2f}\n"
        f"코스닥 {data['KOSDAQ']['date']} 종가 {data['KOSDAQ']['close']:,.2f} / 20일선 {data['KOSDAQ']['ma20']:,.2f}\n"
        f"상태: <b>{state}</b>\n"
        "※ 종목별 실시간 체결은 KIS WebSocket에서 수신합니다."
    )


async def handle_session(state, universe, market):
    approval_key = get_approval_key()
    engine = SignalEngine(state, universe, market)
    async with websockets.connect(
        WS_URL,
        ping_interval=30,
        ping_timeout=30,
        close_timeout=5,
        max_size=None,
    ) as ws:
        for symbol in universe:
            await ws.send(json.dumps(subscription_message(approval_key, symbol)))
            await asyncio.sleep(0.12)
        log.info("KIS 실시간 구독 시작: %d개 종목", len(universe))
        if state.get("last_market_notice") != today_text():
            if send_telegram(market_notice_text(market)):
                state["last_market_notice"] = today_text()
                save_state(state)
        last_status = time.monotonic()
        last_news_poll = time.monotonic() - NEWS_POLL_SECONDS
        news_task = None
        while RUNNING and in_market_hours():
            if time.monotonic() - last_news_poll >= NEWS_POLL_SECONDS and (news_task is None or news_task.done()):
                news_task = asyncio.create_task(poll_open_position_news(state))
                last_news_poll = time.monotonic()
            if news_task is not None and news_task.done() and not news_task.cancelled():
                error = news_task.exception()
                if error:
                    log.error("뉴스 모니터 작업 실패: %s", error)
                news_task = None
            if time.monotonic() - last_status >= 300:
                if state["positions"] and not send_telegram(engine.status()):
                    log.warning("정기 슬롯 현황 전송 실패")
                last_status = time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed as exc:
                raise RuntimeError(f"KIS WebSocket 연결 종료: {exc}") from exc

            # KIS system messages are JSON, while H0STCNT0 ticks are
            # pipe/caret-delimited strings. Echo application-level ping/pong.
            if isinstance(raw, str) and raw.startswith("{"):
                try:
                    message = json.loads(raw)
                    tr_id = message.get("header", {}).get("tr_id")
                    body = message.get("body", {})
                    if tr_id == "PINGPONG":
                        await ws.pong(raw.encode("utf-8"))
                    elif body and body.get("rt_cd") not in (None, "0"):
                        log.error("KIS 구독 오류: %s", body)
                except json.JSONDecodeError:
                    log.warning("알 수 없는 KIS 시스템 메시지")
                continue

            tick = parse_trade_frame(raw)
            if tick:
                engine.on_tick(tick)
            elif isinstance(raw, str) and raw:
                log.debug("비체결 WebSocket 메시지: %s", raw[:200])

    # Keep virtual positions in state overnight; next session resubscribes them.
    save_state(state)


async def main_loop():
    global RUNNING, SYMBOL_NAMES
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET 환경변수를 설정하세요.")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN과 TELEGRAM_CHAT_ID/CHAT_ID 환경변수를 설정하세요.")

    state = load_state()
    while RUNNING:
        if not in_market_hours():
            await asyncio.sleep(20)
            continue
        try:
            market = market_regime_previous_close()
            active = set(state["positions"].keys())
            universe = build_universe(active)
            listing = fdr.StockListing("KRX")
            if listing is not None and not listing.empty and "Code" in listing and "Name" in listing:
                SYMBOL_NAMES = {
                    str(row["Code"]).zfill(6): str(row["Name"])
                    for _, row in listing.dropna(subset=["Code", "Name"]).iterrows()
                }
            if len(universe) > 40:
                raise RuntimeError("KIS WebSocket 40종목 구독 한도를 초과했습니다.")
            log.info(
                "감시 시작 env=%s universe=%d slots=%d bear=%s",
                KIS_ENV, len(universe), len(state["positions"]), market[0]
            )
            await handle_session(state, universe, market)
        except Exception:
            log.exception("장중 연결/감시 오류; 10초 후 재연결합니다.")
            await asyncio.sleep(10)
        # Reconnect after market closes on the next trading day. On weekends,
        # a 20-second sleep above avoids a busy loop.
        await asyncio.sleep(2)


def request_shutdown(signum, frame):
    global RUNNING
    RUNNING = False
    log.warning("종료 요청 수신 signal=%s", signum)


if __name__ == "__main__":
    os_signal.signal(os_signal.SIGINT, request_shutdown)
    os_signal.signal(os_signal.SIGTERM, request_shutdown)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("프로그램이 종료되었습니다.")
        raise
