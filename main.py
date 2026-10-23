#!/usr/bin/env python3
import asyncio, html, json, logging, os, signal, time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests
import websockets

VERSION = "1.3.0"
KST = timezone(timedelta(hours=9))
APP_KEY = os.getenv("KIS_APP_KEY", "").strip()
APP_SECRET = os.getenv("KIS_APP_SECRET", "").strip()
ENV = os.getenv("KIS_ENV", "real").lower()
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE_FILE = Path("stock_intraday_state.json")
MAX_SLOTS = min(8, max(1, int(os.getenv("STOCK_MAX_POSITIONS", "8"))))
MAX_SYMBOLS = min(40, max(1, int(os.getenv("KIS_MAX_SYMBOLS", "40"))))
MIN_PRICE = float(os.getenv("STOCK_MIN_PRICE_KRW", "1000"))
MAX_PRICE = float(os.getenv("STOCK_MAX_PRICE_KRW", "50000"))
MIN_GAIN = float(os.getenv("STOCK_MIN_OPEN_GAIN_PCT", "0.40"))
MIN_VOL = float(os.getenv("STOCK_MIN_VOLUME_SURGE", "1.50"))
MAX_GAP = float(os.getenv("STOCK_MAX_ENTRY_GAP_PCT", "0.35"))
NEWS_SECONDS = max(60, int(os.getenv("STOCK_NEWS_POLL_SECONDS", "300")))
BAR_COUNT = 80
STOP_MIN = 0.006
STOP_MAX = 0.045
TP_R = (1.5, 2.8, 4.2)
OPEN_TIME = (9, 0)
CLOSE_TIME = (15, 30)
SIGNAL_CUTOFF = (15, 15)

if ENV == "real":
    APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
    WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"
elif ENV == "demo":
    APPROVAL_URL = "https://openapivts.koreainvestment.com:29443/oauth2/Approval"
    WS_URL = "ws://ops.koreainvestment.com:31000/tryitout"
else:
    raise ValueError("KIS_ENV는 real 또는 demo여야 합니다.")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("stock-bot")
RUNNING = True
SYMBOL_NAMES = {}

def now():
    return datetime.now(KST)

def stamp():
    return now().strftime("%Y-%m-%d %H:%M:%S KST")

def today():
    return now().strftime("%Y-%m-%d")

def market_open():
    n = now()
    return n.weekday() < 5 and OPEN_TIME <= (n.hour, n.minute) < CLOSE_TIME

def can_scan():
    n = now()
    return market_open() and (n.hour, n.minute) < SIGNAL_CUTOFF

def num(v, default=0.0):
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except (TypeError, ValueError):
        return default

def pct(price, entry):
    return (num(price) / num(entry) - 1) * 100 if num(entry) > 0 else 0.0

def send(text):
    if not TOKEN or not CHAT_ID:
        log.error("TELEGRAM_TOKEN 및 TELEGRAM_CHAT_ID/CHAT_ID를 설정하세요.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15
        )
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except Exception:
        log.exception("Telegram 전송 실패")
        return False

def default_state():
    return {
        "version": VERSION, "positions": {}, "history": [],
        "last_signal_date": {}, "minute_bars": {}, "news_seen": {},
        "last_market_notice": None, "last_slot_full_notice": None
    }

def load_state():
    if not STATE_FILE.exists():
        return default_state()
    with STATE_FILE.open(encoding="utf-8") as f:
        s = json.load(f)
    d = default_state()
    for k, v in d.items():
        s.setdefault(k, v)
    used = set()
    for code, p in s["positions"].items():
        slot = p.get("slot_no")
        if not isinstance(slot, int) or not 1 <= slot <= MAX_SLOTS or slot in used:
            slot = next((i for i in range(1, MAX_SLOTS + 1) if i not in used), None)
        if slot is not None:
            p["slot_no"] = slot
            used.add(slot)
        p.setdefault("last_price", p.get("entry", 0))
        p.setdefault("highest_tp", 0)
    s["version"] = VERSION
    return s

def save_state(s):
    s["version"] = VERSION
    temp = STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE_FILE)

def free_slot(s):
    used = {p.get("slot_no") for p in s["positions"].values()}
    return next((i for i in range(1, MAX_SLOTS + 1) if i not in used), None)

def get_approval_key():
    if not APP_KEY or not APP_SECRET:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET가 필요합니다.")
    r = requests.post(APPROVAL_URL, json={
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "secretkey": APP_SECRET
    }, headers={"content-type": "application/json; charset=utf-8"}, timeout=15)
    r.raise_for_status()
    key = r.json().get("approval_key")
    if not key:
        raise RuntimeError(f"KIS approval_key 발급 실패: {r.text[:300]}")
    return key

def sub_message(key, code, tr_type="1"):
    return {
        "header": {"approval_key": key, "custtype": "P", "tr_type": tr_type,
                   "content-type": "utf-8"},
        "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}}
    }

def parse_tick(raw):
    if not isinstance(raw, str) or not raw or raw[0] not in ("0", "1"):
        return None
    parts = raw.split("|", 3)
    if len(parts) != 4 or parts[1] != "H0STCNT0":
        return None
    v = parts[3].split("^")
    if len(v) < 34:
        return None
    try:
        code = v[0].zfill(6)
        t = v[1]
        price = abs(float(v[2]))
        if not code or len(t) < 6 or price <= 0:
            return None
        return {
            "symbol": code, "time": t[:6], "date": v[33],
            "price": price, "open": abs(float(v[7])),
            "high": abs(float(v[8])), "low": abs(float(v[9])),
            "tick_volume": abs(float(v[12])),
            "cum_volume": abs(float(v[13])),
            "cum_value": abs(float(v[14]))
        }
    except (ValueError, IndexError):
        return None

def market_filter():
    result = {}
    for name, code in (("KOSPI", "KS11"), ("KOSDAQ", "KQ11")):
        start = (now().date() - timedelta(days=100)).isoformat()
        df = fdr.DataReader(code, start)
        if df is None or df.empty or "Close" not in df:
            raise RuntimeError(f"{name} 지수 조회 실패")
        df = df.sort_index()
        if pd.Timestamp(df.index[-1]).date() >= now().date():
            df = df.iloc[:-1]
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if len(close) < 25:
            raise RuntimeError(f"{name} 확정 일봉 부족")
        ma = close.rolling(20).mean().iloc[-1]
        change = (close.iloc[-1] / close.iloc[-2] - 1) * 100
        result[name] = {
            "date": str(close.index[-1].date()),
            "close": float(close.iloc[-1]), "ma20": float(ma),
            "change": float(change), "below": bool(close.iloc[-1] < ma)
        }
    bear = (result["KOSPI"]["below"] and result["KOSDAQ"]["below"]) or \
           result["KOSPI"]["change"] <= -2 or result["KOSDAQ"]["change"] <= -2
    return bear, result

def build_universe(active):
    custom = os.getenv("STOCK_SYMBOLS", "").strip()
    if custom:
        candidates = list(dict.fromkeys(x.strip().zfill(6) for x in custom.split(",") if x.strip()))
    else:
        df = fdr.StockListing("KRX")
        if df is None or df.empty or not {"Code", "Name"}.issubset(df.columns):
            raise RuntimeError("KRX 종목 목록을 조회하지 못했습니다.")
        df = df.dropna(subset=["Code", "Name"]).copy()
        df["Code"] = df["Code"].astype(str).str.zfill(6)
        col = next((c for c in ("Close", "Price", "price") if c in df.columns), None)
        if not col:
            raise RuntimeError("KRX 목록에서 가격 필드를 찾지 못했습니다.")
        df["_price"] = pd.to_numeric(df[col], errors="coerce")
        df = df[(df["_price"] >= MIN_PRICE) & (df["_price"] <= MAX_PRICE)]
        df = df[~df["Name"].astype(str).str.contains("우선주|우B|우C|스팩|리츠", regex=True)]
        liq = next((c for c in ("Amount", "TradingValue", "거래대금", "Volume") if c in df.columns), None)
        df["_liq"] = pd.to_numeric(df[liq], errors="coerce").fillna(0) if liq else 0
        df = df.sort_values("_liq", ascending=False)
        candidates = df["Code"].head(MAX_SYMBOLS).tolist()
    universe = list(dict.fromkeys(list(active) + candidates))
    if len(active) > MAX_SLOTS:
        raise RuntimeError("활성 슬롯 수가 최대값을 초과했습니다.")
    return universe[:MAX_SYMBOLS]

def fetch_news(code, name):
    r = requests.get("https://news.google.com/rss/search",
        params={"q": f'"{name}" {code} 주식', "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.findall("./channel/item")[:3]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        date = (item.findtext("pubDate") or "").strip()
        src = item.find("source")
        source = (src.text or "Google News") if src is not None else "Google News"
        if title and link:
            items.append({"id": link, "title": title, "link": link, "date": date, "source": source})
    return items

def news_label(title):
    negative = ("실적 하향", "적자전환", "영업손실", "계약 해지", "유상증자",
                "횡령", "배임", "상장폐지", "거래정지", "소송", "리콜", "불성실공시")
    positive = ("공급계약 체결", "수주", "흑자전환", "최대 실적", "실적 개선",
                "자사주 매입", "배당 확대", "승인 획득", "신약 허가")
    if any(x in title for x in negative):
        return "부정 키워드 감지 · 원문 확인 필요"
    if any(x in title for x in positive):
        return "긍정 키워드 감지 · 원문 확인 필요"
    return "자동 분류 불가 · 내용 확인 필요"

class Engine:
    def __init__(self, state, universe, bear, market):
        self.s = state
        self.universe = set(universe)
        self.bear = bear
        self.market = market
        self.bars = {}
        self.current = {}
        saved = state.get("minute_bars", {})
        day = now().strftime("%Y%m%d")
        for code in universe:
            q = deque(maxlen=BAR_COUNT)
            for b in saved.get(code, []):
                if b.get("key", [""])[0] == day:
                    q.append(b)
            self.bars[code] = q

    @staticmethod
    def ema(values, span):
        if len(values) < span:
            return None
        a = 2 / (span + 1)
        e = float(values[0])
        for x in values[1:]:
            e = a * float(x) + (1 - a) * e
        return e

    def session_data(self, tick):
        code = tick["symbol"]
        date = tick["date"] or now().strftime("%Y%m%d")
        d = getattr(self, "session", {})
        self.session = d
        cur = d.get(code)
        if cur is None or cur["date"] != date:
            cur = {"date": date, "open": tick["open"] or tick["price"],
                   "cum_volume": tick["cum_volume"], "cum_value": tick["cum_value"],
                   "vwap": tick["price"], "high": tick["high"]}
            d[code] = cur
        else:
            cur["cum_volume"] = max(cur["cum_volume"], tick["cum_volume"])
            cur["cum_value"] = max(cur["cum_value"], tick["cum_value"])
            if cur["cum_volume"] > 0 and cur["cum_value"] > 0:
                cur["vwap"] = cur["cum_value"] / cur["cum_volume"]
            cur["high"] = max(cur["high"], tick["high"], tick["price"])
        return cur

    def on_tick(self, tick):
        code = tick["symbol"]
        if code not in self.universe:
            return
        session = self.session_data(tick)
        key = (tick["date"], tick["time"][:4])
        bar = self.current.get(code)
        if bar is None:
            self.current[code] = {"key": key, "open": tick["price"], "high": tick["price"],
                                  "low": tick["price"], "close": tick["price"],
                                  "volume": tick["tick_volume"]}
        elif bar["key"] == key:
            bar["high"] = max(bar["high"], tick["price"])
            bar["low"] = min(bar["low"], tick["price"])
            bar["close"] = tick["price"]
            bar["volume"] += tick["tick_volume"]
        else:
            done = dict(bar)
            self.bars[code].append(done)
            self.s["minute_bars"][code] = list(self.bars[code])
            self.current[code] = {"key": key, "open": tick["price"], "high": tick["price"],
                                  "low": tick["price"], "close": tick["price"],
                                  "volume": tick["tick_volume"]}
            save_state(self.s)
            self.evaluate(code, done, session)
            self.technical_watch(code, done, session)
        self.monitor_price(code, tick["price"])

    def evaluate(self, code, bar, session):
        if self.bear or not can_scan() or code in self.s["positions"]:
            return
        if len(self.s["positions"]) >= MAX_SLOTS:
            return
        if self.s["last_signal_date"].get(code) == today():
            return
        bars = list(self.bars[code])
        if len(bars) < 30:
            return
        prior = bars[-21:-1]
        if len(prior) < 20:
            return
        closes = [b["close"] for b in bars]
        e8 = self.ema(closes[-30:], 8)
        e21 = self.ema(closes[-50:], 21)
        if e8 is None or e21 is None or e8 <= e21:
            return
        avg_vol = sum(b["volume"] for b in prior) / len(prior)
        ratio = bar["volume"] / avg_vol if avg_vol > 0 else 0
        price = num(self.current[code]["close"], bar["close"])
        if not MIN_PRICE <= price <= MAX_PRICE:
            return
        prior_high = max(b["high"] for b in prior)
        day_gain = (price / session["open"] - 1) * 100 if session["open"] else 0
        vwap = num(session.get("vwap"))
        location = (bar["close"] - bar["low"]) / max(bar["high"] - bar["low"], 1e-9)
        gap = abs(price / bar["close"] - 1) * 100 if bar["close"] else 100
        if price <= prior_high or ratio < MIN_VOL or day_gain < MIN_GAIN:
            return
        if not vwap or price < vwap or location < .65 or gap > MAX_GAP:
            return
        risk_pct = min(STOP_MAX, max(STOP_MIN,
            1.5 * np.mean([b["high"] - b["low"] for b in bars[-15:]]) / price))
        risk = price * risk_pct
        slot = free_slot(self.s)
        if slot is None:
            return
        p = {
            "symbol": code, "slot_no": slot, "entry": price, "last_price": price,
            "sl": price - risk, "tp1": price + risk * TP_R[0],
            "tp2": price + risk * TP_R[1], "tp3": price + risk * TP_R[2],
            "risk_pct": risk_pct * 100, "volume_ratio": ratio,
            "day_gain_pct": day_gain, "vwap": vwap, "highest_tp": 0,
            "opened_at": stamp(), "technical_flags": None
        }
        msg = self.entry_message(p)
        if not send(msg):
            return
        self.s["positions"][code] = p
        self.s["last_signal_date"][code] = today()
        save_state(self.s)
        log.info("신규 시그널 %s %s", code, price)
        if len(self.s["positions"]) == MAX_SLOTS and self.s.get("last_slot_full_notice") != today():
            if send(f"⛔ <b>슬롯 전체 사용 8/8</b>\n신규 시그널 탐색 중단. 기존 슬롯 감시는 계속합니다.\n{stamp()}"):
                self.s["last_slot_full_notice"] = today()
                save_state(self.s)

    def name(self, code):
        return html.escape(SYMBOL_NAMES.get(code, code))

    def entry_message(self, p):
        code = p["symbol"]
        entry = p["entry"]
        slot_count = len(self.s["positions"]) + 1
        return (
            "🚀 <b>장중 상승 시그널</b>\n"
            f"📍 슬롯 {p['slot_no']}/{MAX_SLOTS} | 사용 예정 {slot_count}/{MAX_SLOTS}\n"
            f"종목: <b>{self.name(code)} ({code})</b>\n"
            f"진입 기준가: {entry:,.0f}원 (0.00%)\n\n"
            "🎯 <b>목표가</b>\n"
            f"TP1: {p['tp1']:,.0f}원 ({pct(p['tp1'], entry):+.2f}%)\n"
            f"TP2: {p['tp2']:,.0f}원 ({pct(p['tp2'], entry):+.2f}%)\n"
            f"TP3: {p['tp3']:,.0f}원 ({pct(p['tp3'], entry):+.2f}%)\n"
            f"손절가: {p['sl']:,.0f}원 ({pct(p['sl'], entry):+.2f}%)\n\n"
            "📈 <b>상승 근거</b>\n"
            "• 1분봉 단기 EMA 정배열\n"
            "• 최근 20개 1분봉 고점 돌파\n"
            f"• 거래량: 평균 대비 {p['volume_ratio']:.2f}배\n"
            f"• 당일 시가 대비 {p['day_gain_pct']:+.2f}% 및 VWAP 상회\n\n"
            "📰 관련 호재: 확인된 내용이 없으면 별도 기재하지 않습니다.\n"
            f"📊 차트: https://www.tradingview.com/chart/?symbol=KRX%3A{code}\n"
            f"🕒 {stamp()}\n"
            "⚠️ 참고용 가상 시그널입니다. 실제 매수 주문이나 체결을 의미하지 않으며 수익을 보장하지 않습니다."
        )

    def monitor_price(self, code, price):
        p = self.s["positions"].get(code)
        if not p or price <= 0:
            return
        p["last_price"] = price
        entry = p["entry"]
        slot = p["slot_no"]
        if price <= p["sl"]:
            self.close(code, price, "손절가 도달")
            return
        if price >= p["tp1"] and p["highest_tp"] < 1:
            p["highest_tp"] = 1
            p["sl"] = entry
            self.target_alert(code, 1, price, p)
        if price >= p["tp2"] and p["highest_tp"] < 2:
            p["highest_tp"] = 2
            p["sl"] = p["tp1"]
            self.target_alert(code, 2, price, p)
        if price >= p["tp3"]:
            self.close(code, price, "TP3 도달")
            return
        save_state(self.s)

    def target_alert(self, code, level, price, p):
        entry = p["entry"]
        send(
            f"🎯 <b>[슬롯 {p['slot_no']}/{MAX_SLOTS}] TP{level} 도달</b>\n"
            f"{self.name(code)} ({code})\n"
            f"최초 진입가: {entry:,.0f}원\n도달 관측가: {price:,.0f}원\n"
            f"진입가 대비: <b>{pct(price, entry):+.2f}%</b>\n"
            f"다음 목표: {('TP2' if level == 1 else 'TP3')} | 추적 손절 기준: {p['sl']:,.0f}원\n"
            f"슬롯 현황: {len(self.s['positions'])}/{MAX_SLOTS}\n"
            "※ 실제 부분청산은 실행하지 않습니다."
        )
        save_state(self.s)

    def close(self, code, price, reason):
        p = self.s["positions"].pop(code, None)
        if not p:
            return
        entry = p["entry"]
        change = pct(price, entry)
        self.s["history"].append({
            **p, "exit": price, "exit_reason": reason, "exit_at": stamp(),
            "change_pct": change
        })
        self.s["history"] = self.s["history"][-2000:]
        save_state(self.s)
        send(
            f"🏁 <b>시그널 종료</b>\n슬롯 {p['slot_no']}번 · {self.name(code)} ({code})\n"
            f"종료 사유: {reason}\n최초 진입가: {entry:,.0f}원\n"
            f"종료 관측가: {price:,.0f}원\n진입가 대비: <b>{change:+.2f}%</b>\n"
            f"슬롯 해제 완료: 사용 {len(self.s['positions'])}/{MAX_SLOTS}, "
            f"잔여 {MAX_SLOTS-len(self.s['positions'])}개\n"
            "※ 실제 체결이 아닌 가격 도달 기반 가상 추적입니다."
        )
        send(f"🔄 <b>신규 탐색 재개</b>\n슬롯 {p['slot_no']}번 해제\n"
             f"사용 {len(self.s['positions'])}/{MAX_SLOTS} | {stamp()}")
        self.performance()

    def performance(self):
        rows = self.s["history"]
        if not rows:
            return
        wins = sum(x["change_pct"] > 0 for x in rows)
        losses = sum(x["change_pct"] < 0 for x in rows)
        be = len(rows) - wins - losses
        send(
            "📊 <b>가상 시그널 추적 통계</b>\n"
            f"종료 {len(rows)}건 | 플러스 {wins} | 마이너스 {losses} | 본전 {be}\n"
            f"가격 변동 기준 플러스 비율: {wins/len(rows)*100:.1f}%\n"
            "※ 실제 매매 수익률이나 체결 기반 승률이 아닙니다."
        )

    def technical_watch(self, code, bar, session):
        p = self.s["positions"].get(code)
        if not p:
            return
        bars = list(self.bars[code])
        closes = [b["close"] for b in bars]
        e8 = self.ema(closes[-30:], 8)
        e21 = self.ema(closes[-50:], 21)
        prior_vol = [b["volume"] for b in bars[-21:-1]]
        avg = sum(prior_vol) / len(prior_vol) if prior_vol else 0
        ratio = bar["volume"] / avg if avg else 0
        change = (bar["close"] / bars[-2]["close"] - 1) * 100 if len(bars) >= 2 and bars[-2]["close"] else 0
        flags = {
            "ema_bear": bool(e8 is not None and e21 is not None and e8 <= e21),
            "below_vwap": bool(session.get("vwap", 0) and bar["close"] < session["vwap"]),
            "vol_surge": ratio >= 2,
            "vol_drop": 0 < ratio <= .5,
            "drop": change <= -1
        }
        old = p.get("technical_flags")
        p["technical_flags"] = flags
        save_state(self.s)
        if old is None:
            return
        labels = {
            "ema_bear": "8EMA/21EMA 약세 전환",
            "below_vwap": "VWAP 하향 이탈",
            "vol_surge": "거래량 급증",
            "vol_drop": "거래량 급감",
            "drop": "1분봉 급락"
        }
        changes = [f"• {labels[k]}: {'발생' if v else '해소/회복'}"
                   for k, v in flags.items() if old.get(k) != v]
        if changes:
            send(
                f"⚠️ <b>[슬롯 {p['slot_no']}/{MAX_SLOTS}] 기술 변화</b>\n"
                f"{self.name(code)} ({code})\n현재가: {bar['close']:,.0f}원\n"
                f"진입가 대비: {pct(bar['close'], p['entry']):+.2f}%\n"
                + "\n".join(changes) +
                f"\n거래량 비율: {ratio:.2f}배 | 1분 변화: {change:+.2f}%\n"
                "관찰 경고이며 자동 매수·매도 주문은 실행하지 않습니다."
            )

async def news_monitor(state):
    active = list(state["positions"].keys())
    if not active:
        return
    tasks = []
    for code in active:
        name = SYMBOL_NAMES.get(code, code)
        tasks.append(asyncio.to_thread(fetch_news, code, name))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for code, result in zip(active, results):
        if isinstance(result, Exception):
            log.warning("뉴스 조회 실패 %s: %s", code, result)
            continue
        p = state["positions"].get(code)
        if not p:
            continue
        old = set(state["news_seen"].get(code, []))
        new = [x for x in result if x["id"] not in old]
        state["news_seen"][code] = [x["id"] for x in result][:50]
        if new:
            lines = [
                f"📰 <b>[슬롯 {p['slot_no']}/{MAX_SLOTS}] 뉴스 변화 감지</b>",
                f"{html.escape(SYMBOL_NAMES.get(code, code))} ({code})",
                f"현재가: {p.get('last_price', p['entry']):,.0f}원 | "
                f"진입가 대비 {pct(p.get('last_price', p['entry']), p['entry']):+.2f}%",
                "※ 제목 키워드 분류는 참고용이며 실제 호재·악재를 확정하지 않습니다."
            ]
            for item in new[:3]:
                title = html.escape(item["title"])
                link = html.escape(item["link"], quote=True)
                source = html.escape(item["source"])
                label = html.escape(news_label(item["title"]))
                lines.append(f'• <a href="{link}">{title}</a>\n  분류: {label}\n  출처: {source}')
            lines.append(stamp())
            send("\n".join(lines))
    save_state(state)

def status_text(state, universe):
    lines = [f"📊 <b>실시간 슬롯 현황 {len(state['positions'])}/{MAX_SLOTS}</b>"]
    for code, p in sorted(state["positions"].items(), key=lambda x: x[1].get("slot_no", 99)):
        price = num(p.get("last_price"), p["entry"])
        lines.append(
            f"\n슬롯 {p['slot_no']}: {html.escape(SYMBOL_NAMES.get(code, code))} ({code})\n"
            f"현재가 {price:,.0f}원 ({pct(price, p['entry']):+.2f}%)\n"
            f"SL {p['sl']:,.0f} | TP1 {p['tp1']:,.0f} | TP2 {p['tp2']:,.0f} | TP3 {p['tp3']:,.0f}"
        )
    lines.append(f"\n감시 종목 {len(universe)}개 | {stamp()}")
    return "\n".join(lines)

async def handle_session(state, universe, bear, market):
    key = get_approval_key()
    engine = Engine(state, universe, bear, market)
    async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=30,
                                  close_timeout=5, max_size=None) as ws:
        for code in universe:
            await ws.send(json.dumps(sub_message(key, code)))
            await asyncio.sleep(.12)
        log.info("실시간 구독 시작: %d개 종목", len(universe))
        if state.get("last_market_notice") != today():
            k, q = market["KOSPI"], market["KOSDAQ"]
            msg = (
                "📉 <b>장 시작 시장 컨텍스트</b>\n"
                f"KOSPI: {k['date']} 종가 {k['close']:,.2f} / 20일선 {k['ma20']:,.2f}\n"
                f"KOSDAQ: {q['date']} 종가 {q['close']:,.2f} / 20일선 {q['ma20']:,.2f}\n"
                f"신규 진입 필터: {'차단' if bear else '통과'}\n{stamp()}"
            )
            if send(msg):
                state["last_market_notice"] = today()
                save_state(state)
        last_news = 0
        last_status = time.monotonic()
        while RUNNING and market_open():
            if time.monotonic() - last_news >= NEWS_SECONDS:
                await news_monitor(state)
                last_news = time.monotonic()
            if time.monotonic() - last_status >= 300:
                if state["positions"]:
                    send(status_text(state, universe))
                last_status = time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed as e:
                raise RuntimeError(f"WebSocket 연결 종료: {e}") from e
            if isinstance(raw, str) and raw.startswith("{"):
                try:
                    obj = json.loads(raw)
                    if obj.get("header", {}).get("tr_id") == "PINGPONG":
                        await ws.pong(raw.encode())
                    elif obj.get("body", {}).get("rt_cd") not in (None, "0"):
                        log.error("KIS 구독 오류: %s", obj.get("body"))
                except json.JSONDecodeError:
                    pass
                continue
            tick = parse_tick(raw)
            if tick:
                engine.on_tick(tick)
    save_state(state)

async def main():
    global SYMBOL_NAMES, RUNNING
    if not APP_KEY or not APP_SECRET or not TOKEN or not CHAT_ID:
        raise RuntimeError("KIS_APP_KEY, KIS_APP_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID를 설정하세요.")
    state = load_state()
    while RUNNING:
        if not market_open():
            await asyncio.sleep(20)
            continue
        try:
            bear, market = market_filter()
            universe = build_universe(state["positions"].keys())
            listing = fdr.StockListing("KRX")
            if listing is not None and not listing.empty and {"Code", "Name"}.issubset(listing.columns):
                SYMBOL_NAMES = {
                    str(r["Code"]).zfill(6): str(r["Name"])
                    for _, r in listing.dropna(subset=["Code", "Name"]).iterrows()
                }
            if len(universe) > 40:
                raise RuntimeError("실시간 구독 종목 수가 40개를 초과했습니다.")
            log.info("시작: 종목=%d 슬롯=%d 약세필터=%s", len(universe), len(state["positions"]), bear)
            await handle_session(state, universe, bear, market)
        except Exception:
            log.exception("감시 오류. 10초 후 재연결합니다.")
            await asyncio.sleep(10)
        await asyncio.sleep(2)

def shutdown(sig, frame):
    global RUNNING
    RUNNING = False
    log.warning("종료 요청: %s", sig)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
