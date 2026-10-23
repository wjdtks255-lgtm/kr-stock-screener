#!/usr/bin/env python3
"""Korea Daily Stock Signal Monitor — informational, simulated tracking only.

Signals are based on completed daily candles. Entry prices and exits are
reference prices, not guaranteed fills. No brokerage orders are placed.
"""

import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests

# ----------------------------- Configuration -----------------------------
VERSION = "33.1.0"
KST = timezone(timedelta(hours=9))
STATE_FILE = Path(os.getenv("STOCK_SIGNAL_STATE", "stock_signal_state.json"))
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()

MAX_POSITIONS = int(os.getenv("STOCK_MAX_POSITIONS", "8"))
MAX_SCAN = int(os.getenv("STOCK_MAX_SCAN", "150"))
MIN_DAILY_TURNOVER_KRW = float(os.getenv("STOCK_MIN_TURNOVER_KRW", "5000000000"))
MAX_HOLDING_SESSIONS = int(os.getenv("STOCK_MAX_HOLDING_SESSIONS", "20"))
# Set to a broker/tax-specific round-trip cost estimate. Default 0 means
# reported performance is explicitly gross and excludes all costs.
ROUND_TRIP_COST_PCT = float(os.getenv("STOCK_ROUND_TRIP_COST_PCT", "0"))

# Signal uses a completed daily candle only. The script is intended to run
# after the Korean cash market close; before 16:10 KST today's candle is dropped.
SIGNAL_AFTER_HOUR = 16
SIGNAL_AFTER_MINUTE = 10

TP_R = (1.5, 2.5, 4.0)
INITIAL_STOP_ATR = 1.5
MIN_STOP_PCT = 0.01
MAX_STOP_PCT = 0.05

HEADERS = {"User-Agent": "Mozilla/5.0 KoreaDailySignal/33.0"}
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="[%(asctime)s KST] %(levelname)s %(message)s",
)
log = logging.getLogger("korea-stock-signal")


def now():
    return datetime.now(KST)


def now_text():
    return now().strftime("%Y-%m-%d %H:%M:%S KST")


def default_state():
    return {
        "version": VERSION,
        "positions": {},
        "history": [],
        "last_signal_date": {},
        "last_market_report_date": None,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        # Do not silently erase historical performance or active positions.
        raise RuntimeError(f"상태 파일을 읽을 수 없습니다: {STATE_FILE}: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("상태 파일 형식이 올바르지 않습니다.")
    defaults = default_state()
    for key, value in defaults.items():
        state.setdefault(key, value)
    if not isinstance(state["positions"], dict):
        raise RuntimeError("상태 파일의 positions 형식이 올바르지 않습니다.")
    if not isinstance(state["history"], list):
        raise RuntimeError("상태 파일의 history 형식이 올바르지 않습니다.")
    state["version"] = VERSION
    return state


def save_state(state):
    state["version"] = VERSION
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    finally:
        if tmp.exists():
            tmp.unlink()


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        log.error("TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID/CHAT_ID가 설정되지 않았습니다.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            log.error("Telegram API 응답 오류: %s", result)
            return False
        return True
    except Exception:
        log.exception("Telegram 전송 실패")
        return False


def escape(value):
    return html.escape(str(value), quote=False)


def normalize_ohlcv(raw):
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(col).strip().lower() for col in df.columns]
    required = ["open", "high", "low", "close", "volume"]
    if any(col not in df.columns for col in required):
        return pd.DataFrame()
    df = df[required].apply(pd.to_numeric, errors="coerce").dropna()
    if df.empty:
        return df
    # Normalize dates to naive local dates for consistent daily-bar comparisons.
    dates = pd.to_datetime(df.index, errors="coerce")
    try:
        if getattr(dates, "tz", None) is not None:
            dates = dates.tz_convert("Asia/Seoul").tz_localize(None)
    except (TypeError, AttributeError):
        pass
    df.index = pd.DatetimeIndex(dates).normalize()
    df = df[~df.index.isna()].sort_index()
    return df[~df.index.duplicated(keep="last")]


def get_daily_bars(symbol, calendar_days=260):
    start = (now().date() - timedelta(days=calendar_days)).isoformat()
    raw = fdr.DataReader(str(symbol), start)
    df = normalize_ohlcv(raw)
    if df.empty:
        return df
    today = now().date()
    after_close = (now().hour, now().minute) >= (SIGNAL_AFTER_HOUR, SIGNAL_AFTER_MINUTE)
    # During the session and shortly after close, today's daily bar may still
    # be incomplete/unstable. Never use it for a new signal or exit decision.
    if not after_close and df.index[-1].date() == today:
        df = df.iloc[:-1]
    return df


def add_indicators(df):
    if df.empty:
        return df
    out = df.copy()
    close = out["close"]
    previous_close = close.shift(1)
    true_range = pd.concat([
        out["high"] - out["low"],
        (out["high"] - previous_close).abs(),
        (out["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    out["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    out["ema50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()
    out["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100
    out.loc[(avg_loss == 0) & (avg_gain == 0), "rsi14"] = 50
    out["turnover"] = out["close"] * out["volume"]
    out["volume_ma20_prev"] = out["volume"].shift(1).rolling(20, min_periods=20).mean()
    out["turnover_ma20_prev"] = out["turnover"].shift(1).rolling(20, min_periods=20).mean()
    out["prior20_high"] = out["high"].shift(1).rolling(20, min_periods=20).max()
    out["prior5_low"] = out["low"].shift(1).rolling(5, min_periods=5).min()
    out["close_position"] = (
        (out["close"] - out["low"]) / (out["high"] - out["low"]).replace(0, np.nan)
    )
    return out.replace([np.inf, -np.inf], np.nan)


def market_regime():
    """Return market context or raise; missing index data blocks new entries."""
    results = {}
    for label, code in (("KOSPI", "KS11"), ("KOSDAQ", "KQ11")):
        df = get_daily_bars(code, calendar_days=90)
        if len(df) < 25:
            raise RuntimeError(f"{label} 지수 데이터가 부족합니다.")
        if df.index[-1].date() != now().date():
            raise RuntimeError(f"{label} 최신 일봉이 오늘 날짜가 아닙니다. 데이터 지연 또는 휴장으로 보고 신규 신호를 중단합니다.")
        close = df["close"]
        ma20 = close.rolling(20, min_periods=20).mean().iloc[-1]
        previous = close.iloc[-2]
        change_pct = (close.iloc[-1] / previous - 1) * 100 if previous else 0
        if not np.isfinite(ma20):
            raise RuntimeError(f"{label} 20일 이동평균 계산에 실패했습니다.")
        results[label] = {
            "close": float(close.iloc[-1]),
            "ma20": float(ma20),
            "change_pct": float(change_pct),
            "below_ma20": bool(close.iloc[-1] < ma20),
        }
    # Conservative regime filter: pause if both indices are below MA20, or
    # either index suffers a 2%+ daily decline. Data failure is not neutral.
    bear = (
        (results["KOSPI"]["below_ma20"] and results["KOSDAQ"]["below_ma20"])
        or results["KOSPI"]["change_pct"] <= -2.0
        or results["KOSDAQ"]["change_pct"] <= -2.0
    )
    return bear, results


def list_candidates():
    listing = fdr.StockListing("KRX")
    if listing is None or listing.empty or "Code" not in listing or "Name" not in listing:
        raise RuntimeError("KRX 종목 목록을 읽지 못했습니다.")
    listing = listing.dropna(subset=["Code", "Name"]).copy()
    listing["Code"] = listing["Code"].astype(str).str.zfill(6)
    # Use a stated, stable selection basis rather than relying on provider row order.
    if "Marcap" in listing.columns:
        listing["_rank"] = pd.to_numeric(listing["Marcap"], errors="coerce").fillna(0)
        listing = listing.sort_values("_rank", ascending=False)
    elif "MarketCap" in listing.columns:
        listing["_rank"] = pd.to_numeric(listing["MarketCap"], errors="coerce").fillna(0)
        listing = listing.sort_values("_rank", ascending=False)
    else:
        listing = listing.sort_values("Code")
        log.warning("시가총액 열이 없어 종목코드 순서로 스캔합니다.")
    # Exclude preferred shares and obvious non-common share codes where possible.
    listing = listing[~listing["Code"].str.endswith("5")]
    return listing.head(MAX_SCAN)


def make_signal(df):
    """Return an objective daily-chart setup or None."""
    if len(df) < 60:
        return None
    data = add_indicators(df)
    row = data.iloc[-1]
    prev = data.iloc[-2]
    required = [
        row["ema20"], row["ema50"], row["atr14"], row["rsi14"],
        row["volume_ma20_prev"], row["turnover_ma20_prev"],
        row["prior20_high"], row["prior5_low"], row["close_position"],
    ]
    if not all(np.isfinite(value) for value in required):
        return None
    close = float(row["close"])
    if close <= 0 or float(row["turnover"]) < MIN_DAILY_TURNOVER_KRW:
        return None
    if float(row["turnover_ma20_prev"]) < MIN_DAILY_TURNOVER_KRW:
        return None

    trend_ok = close > row["ema20"] > row["ema50"]
    rsi_ok = 50 <= row["rsi14"] <= 70
    strong_close = row["close_position"] >= 0.65
    volume_ratio = (
        float(row["volume"]) / float(row["volume_ma20_prev"])
        if row["volume_ma20_prev"] > 0 else 0
    )
    if not trend_ok or not rsi_ok or not strong_close:
        return None

    # Pullback: at least one of the prior five sessions tested the 20-day EMA
    # area, and the latest completed candle closes above the previous high.
    recent = data.iloc[-6:-1]
    recent_touch = bool((recent["low"] <= recent["ema20"] + 0.5 * recent["atr14"]).any())
    pullback = recent_touch and close > float(prev["high"]) and volume_ratio >= 0.8

    # Breakout: close above the previous 20-session high with volume expansion.
    breakout = close > float(row["prior20_high"]) and volume_ratio >= 1.5
    if not (pullback or breakout):
        return None

    atr_value = float(row["atr14"])
    stop_pct = min(MAX_STOP_PCT, max(MIN_STOP_PCT, INITIAL_STOP_ATR * atr_value / close))
    risk = close * stop_pct
    sl = close - risk
    tps = [close + risk * r for r in TP_R]
    strategy = "20일선 눌림 후 반등" if pullback else "20일 고점 돌파"
    date_text = data.index[-1].strftime("%Y-%m-%d")
    return {
        "date": date_text,
        "entry_ref": close,
        "sl": sl,
        "tp1": tps[0],
        "tp2": tps[1],
        "tp3": tps[2],
        "risk_pct": stop_pct * 100,
        "strategy": strategy,
        "rsi": float(row["rsi14"]),
        "atr": atr_value,
        "volume_ratio": volume_ratio,
        "turnover": float(row["turnover"]),
    }


def format_price(price):
    if price >= 1000:
        return f"{price:,.0f}"
    if price >= 100:
        return f"{price:,.1f}"
    if price >= 1:
        return f"{price:,.2f}"
    return f"{price:.4f}"


def signal_message(name, code, signal, market):
    market_state = "신규진입 필터 통과" if not market[0] else "방어모드 — 신규진입 중지"
    return (
        "📊 <b>한국 주식 일봉 시그널 (모의 기준)</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"종목: <b>{escape(name)} ({escape(code)})</b>\n"
        f"전략: <b>{escape(signal['strategy'])}</b>\n"
        f"기준봉: {signal['date']} 일봉 종가 확정 기준\n"
        f"기준가격: <code>{format_price(signal['entry_ref'])}</code>원\n"
        f"손절 기준: <code>{format_price(signal['sl'])}</code>원 (-{signal['risk_pct']:.2f}%)\n"
        f"TP1: <code>{format_price(signal['tp1'])}</code>원 (+{TP_R[0]:g}R)\n"
        f"TP2: <code>{format_price(signal['tp2'])}</code>원 (+{TP_R[1]:g}R)\n"
        f"TP3: <code>{format_price(signal['tp3'])}</code>원 (+{TP_R[2]:g}R)\n"
        f"RSI(14): {signal['rsi']:.1f} | 거래량/20일 평균: {signal['volume_ratio']:.2f}배\n"
        f"시장 상태: {market_state}\n"
        f"발송: {now_text()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ 종가 기준 모의 신호이며 실제 체결·수익을 보장하지 않습니다.\n"
        "다음 거래일 가격이 기준가에서 크게 이탈하면 신호를 추격하지 마세요."
    )


def close_position(state, code, position, exit_price, exit_date, reason):
    entry = float(position["entry_ref"])
    gross_pct = (exit_price / entry - 1) * 100 if entry else 0.0
    net_pct = gross_pct - ROUND_TRIP_COST_PCT
    result = "WIN" if net_pct > 0 else "LOSS" if net_pct < 0 else "BREAKEVEN"
    record = {
        "code": code,
        "name": position.get("name", code),
        "strategy": position.get("strategy"),
        "entry_date": position.get("entry_date"),
        "exit_date": exit_date,
        "entry_ref": entry,
        "exit_ref": float(exit_price),
        "gross_pct": gross_pct,
        "assumed_round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "net_pct": net_pct,
        "result": result,
        "reason": reason,
        "highest_tp": int(position.get("highest_tp", 0)),
    }
    state["history"].append(record)
    state["history"] = state["history"][-2000:]
    state["positions"].pop(code, None)
    label = {"WIN": "비용 가정 후 플러스", "LOSS": "비용 가정 후 마이너스", "BREAKEVEN": "본전"}[result]
    msg = (
        "🏁 <b>주식 시그널 모의 추적 종료</b>\n"
        f"{escape(position.get('name', code))} ({escape(code)})\n"
        f"결과: <b>{label}</b> | 사유: {escape(reason)}\n"
        f"기준 진입 {format_price(entry)}원 → 기준 종료 {format_price(exit_price)}원\n"
        f"총수익률 {gross_pct:+.2f}% | 비용 가정 차감 후 {net_pct:+.2f}%\n"
        f"기준 비용: 왕복 {ROUND_TRIP_COST_PCT:.3f}% | 기준일 {exit_date}\n"
        "※ 실제 사용자의 체결가·비용과 다를 수 있는 모의 추적입니다."
    )
    if not send_telegram(msg):
        log.error("종료 알림 미전송: %s", code)
    log.info("포지션 종료 %s %s %s net=%+.3f%%", code, reason, result, net_pct)


def monitor_positions(state):
    history_before = len(state["history"])
    for code, position in list(state["positions"].items()):
        try:
            bars = get_daily_bars(code, calendar_days=260)
            if len(bars) < 2:
                continue
            last_processed = position.get("last_processed_date", position.get("entry_date", ""))
            new_bars = bars[bars.index.strftime("%Y-%m-%d") > last_processed]
            if new_bars.empty:
                continue

            for bar_date, bar in new_bars.iterrows():
                date_text = bar_date.strftime("%Y-%m-%d")
                entry = float(position["entry_ref"])
                stop = float(position["sl"])
                open_price = float(bar["open"])
                high = float(bar["high"])
                low = float(bar["low"])
                close = float(bar["close"])

                # Gap-through-stop: use the open, not the requested stop, as a
                # more realistic adverse reference. Intraday paths are unknown.
                if open_price <= stop:
                    close_position(state, code, position, open_price, date_text, "시가 갭 하락으로 스탑 하회")
                    break
                # Conservative OHLC convention: if the day's low touches the
                # active stop, process the stop before any target in that bar.
                if low <= stop:
                    reason = "초기 손절" if position.get("highest_tp", 0) == 0 else "상향 스탑 종료"
                    close_position(state, code, position, stop, date_text, reason)
                    break

                # TP3 is a full virtual exit; bar-path ambiguities are treated
                # conservatively by the active-stop checks above.
                if high >= float(position["tp3"]):
                    close_position(state, code, position, float(position["tp3"]), date_text, "TP3 도달")
                    break

                reached = 0
                if high >= float(position["tp2"]):
                    reached = 2
                elif high >= float(position["tp1"]):
                    reached = 1

                old_level = int(position.get("highest_tp", 0))
                if reached > old_level:
                    position["highest_tp"] = reached
                    # Virtual trailing rule: TP1 -> entry stop; TP2 -> TP1 stop.
                    position["sl"] = float(position["entry_ref"] if reached == 1 else position["tp1"])
                    position["last_processed_date"] = date_text
                    notice = (
                        f"🎯 <b>{escape(position.get('name', code))} TP{reached} 도달</b>\n"
                        f"기준봉 {date_text} | 모의 스탑을 {format_price(position['sl'])}원으로 조정했습니다.\n"
                        "부분청산을 의미하지 않으며, 가격 순서는 일봉 OHLC 기준 추정입니다."
                    )
                    if not send_telegram(notice):
                        log.error("TP 알림 미전송: %s TP%s", code, reached)
                    # If the bar also retraced through the newly raised stop,
                    # assume the target was reached first and stop second.
                    if low <= float(position["sl"]):
                        reason = "TP1 후 본전 스탑" if reached == 1 else "TP2 후 TP1 스탑"
                        close_position(state, code, position, float(position["sl"]), date_text, reason)
                        break
                else:
                    position["last_processed_date"] = date_text

                held_sessions = int(position.get("held_sessions", 0)) + 1
                position["held_sessions"] = held_sessions
                if held_sessions >= MAX_HOLDING_SESSIONS:
                    close_position(state, code, position, close, date_text, f"보유 기간 {MAX_HOLDING_SESSIONS}거래일 종료")
                    break
        except Exception:
            log.exception("포지션 감시 실패: %s", code)
    save_state(state)
    return len(state["history"]) - history_before


def performance_message(state):
    history = state["history"]
    if not history:
        return "📊 종료된 모의 시그널이 아직 없습니다."
    wins = sum(row["result"] == "WIN" for row in history)
    losses = sum(row["result"] == "LOSS" for row in history)
    breakeven = sum(row["result"] == "BREAKEVEN" for row in history)
    total = len(history)
    win_rate = wins / total * 100 if total else 0.0
    avg_net = sum(float(row["net_pct"]) for row in history) / total if total else 0.0
    cost_text = f"왕복 비용 가정 {ROUND_TRIP_COST_PCT:.3f}%" if ROUND_TRIP_COST_PCT else "비용 미포함(총수익률)"
    return (
        "📊 <b>주식 시그널 모의 성과</b>\n"
        f"종료 {total}건 | 플러스 {wins} | 마이너스 {losses} | 본전 {breakeven}\n"
        f"플러스 비율: <b>{win_rate:.1f}%</b> | 평균 손익률: {avg_net:+.2f}%\n"
        f"기준: {cost_text}\n"
        "※ 실제 체결 성과가 아닌 기준가 기반 시뮬레이션입니다. 미청산 포지션은 제외됩니다."
    )


def send_market_report(regime):
    bear, values = regime
    state_text = "방어 모드: 신규 신호 중지" if bear else "필터 통과"
    msg = (
        "📈 <b>시장 필터 (완료 일봉 기준)</b>\n"
        f"코스피: {values['KOSPI']['close']:,.2f} | 20일선 {values['KOSPI']['ma20']:,.2f} | 일간 {values['KOSPI']['change_pct']:+.2f}%\n"
        f"코스닥: {values['KOSDAQ']['close']:,.2f} | 20일선 {values['KOSDAQ']['ma20']:,.2f} | 일간 {values['KOSDAQ']['change_pct']:+.2f}%\n"
        f"판정: <b>{state_text}</b>"
    )
    send_telegram(msg)


def scan_new_signals(state, regime):
    bear, _ = regime
    if bear:
        log.warning("시장 방어 필터 발동: 신규 신호를 만들지 않습니다.")
        return
    if len(state["positions"]) >= MAX_POSITIONS:
        log.info("보유 슬롯이 가득 차 신규 스캔을 생략합니다.")
        return

    today = now().strftime("%Y-%m-%d")
    active = set(state["positions"])
    sent_count = 0
    candidates = list_candidates()
    for _, row in candidates.iterrows():
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        code = str(row["Code"]).zfill(6)
        name = str(row["Name"])
        if code in active or state["last_signal_date"].get(code) == today:
            continue
        try:
            bars = get_daily_bars(code)
            signal = make_signal(bars)
            if not signal:
                continue
            if signal["date"] != today:
                log.info("오늘 확정된 신호봉이 아니므로 제외: %s (%s)", code, signal["date"])
                continue
            # The signal reference is the completed candle close. It is not
            # represented as a guaranteed next-session fill.
            position = {
                "code": code,
                "name": name,
                "strategy": signal["strategy"],
                "entry_ref": signal["entry_ref"],
                "sl": signal["sl"],
                "tp1": signal["tp1"],
                "tp2": signal["tp2"],
                "tp3": signal["tp3"],
                "entry_date": signal["date"],
                "last_processed_date": signal["date"],
                "created_at": now_text(),
                "highest_tp": 0,
                "held_sessions": 0,
                "risk_pct": signal["risk_pct"],
            }
            message = signal_message(name, code, signal, regime)
            if not send_telegram(message):
                log.error("신호 전송 실패; 포지션으로 등록하지 않습니다: %s", code)
                continue
            state["positions"][code] = position
            state["last_signal_date"][code] = today
            active.add(code)
            sent_count += 1
            save_state(state)
            log.info("신호 발송: %s %s %s", code, name, signal["strategy"])
            time.sleep(0.35)
        except Exception:
            log.exception("종목 분석 실패: %s %s", code, name)

    if sent_count:
        send_telegram(performance_message(state))


def main():
    current = now()
    if current.weekday() >= 5:
        log.info("주말이므로 신규 일봉 스캔을 생략합니다.")
        return
    if (current.hour, current.minute) < (SIGNAL_AFTER_HOUR, SIGNAL_AFTER_MINUTE):
        log.info("확정 일봉 확보 전입니다. %02d:%02d KST 이후 실행하세요.", SIGNAL_AFTER_HOUR, SIGNAL_AFTER_MINUTE)
        return

    state = load_state()
    log.info("한국 주식 일봉 시그널 v%s 시작", VERSION)
    closed_count = monitor_positions(state)
    if closed_count:
        send_telegram(performance_message(state))

    try:
        regime = market_regime()
    except Exception:
        # Fail closed: unknown market state is not treated as a bullish/neutral regime.
        log.exception("시장 데이터 확인 실패: 신규 신호를 만들지 않습니다.")
        return

    today = current.strftime("%Y-%m-%d")
    if state.get("last_market_report_date") != today:
        send_market_report(regime)
        state["last_market_report_date"] = today
        save_state(state)

    scan_new_signals(state, regime)
    save_state(state)
    log.info("완료: 활성 %d / %d", len(state["positions"]), MAX_POSITIONS)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("실행 중 오류")
        raise
