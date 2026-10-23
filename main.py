import os
import json
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import FinanceDataReader as fdr

TZ = ZoneInfo("Asia/Seoul")
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = (
    os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or ""
).strip()
MAX_POS = 8
MAX_DETAIL = 160
MIN_PRICE = 1000
MAX_PRICE = 50000
MIN_TURNOVER = 300_000_000
MAX_HOLD_DAYS = 5  # 장 마감 기준 최대 보유 기간 (시간 초과 판정용)
STATE = "stock_intraday_state.json"
NAVER = "https://polling.finance.naver.com/api/realtime/domestic/stock/"
S = requests.Session()
S.headers.update(
    {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
)


def now():
  return datetime.now(TZ)


def won(x):
  return f"{x:,.0f}원"


def change(a, b):
  return (b / a - 1) * 100 if a else 0


def log(x):
  print(f"[{now():%H:%M:%S}] {x}", flush=True)


def send(msg):
  if not TOKEN or not CHAT:
    raise RuntimeError("TELEGRAM_TOKEN / CHAT_ID Secrets 확인 필요")
  r = S.post(
      f"https://api.telegram.org/bot{TOKEN}/sendMessage",
      data={
          "chat_id": CHAT,
          "text": msg,
          "parse_mode": "HTML",
          "disable_web_page_preview": True,
      },
      timeout=15,
  )
  r.raise_for_status()
  if not r.json().get("ok"):
    raise RuntimeError("Telegram 전송 실패")


def load():
  try:
    with open(STATE, encoding="utf-8") as f:
      s = json.load(f)
  except (OSError, ValueError):
    s = {}
  s.setdefault("positions", {})
  s.setdefault("sent_today", {})
  s.setdefault("history", [])
  return s


def save(s):
  with open(STATE + ".tmp", "w", encoding="utf-8") as f:
    json.dump(s, f, ensure_ascii=False, indent=2)
  os.replace(STATE + ".tmp", STATE)


def quote(code):
  try:
    r = S.get(NAVER + code, timeout=8)
    r.raise_for_status()
    rows = r.json().get("datas", [])
    if not rows:
      return None
    x = rows[0]

    def n(k):
      return float(str(x.get(k, "0")).replace(",", "").replace("+", ""))

    p, op, vol = (
        n("closePrice"),
        n("openPrice"),
        n("accumulatedTradingVolume"),
    )
    if p <= 0 or op <= 0:
      return None
    return {
        "price": p,
        "open": op,
        "volume": vol,
        "gain": change(op, p),
        "name": x.get("stockName", code),
    }
  except Exception as e:
    log(f"시세오류 {code}: {type(e).__name__}")
    return None


def universe():
  df = fdr.StockListing("KRX")
  cc = next((x for x in ("Code", "Symbol") if x in df.columns), None)
  nc = "Name" if "Name" in df.columns else None
  if not cc or not nc:
    raise RuntimeError("KRX 종목 목록 형식 오류")
  df["code"] = (
      df[cc]
      .astype(str)
      .str.extract(r"(\d{1,6})", expand=False)
      .fillna("")
      .str.zfill(6)
  )
  df["name"] = df[nc].astype(str)
  df = df[df.code.str.fullmatch(r"\d{6}")]
  df = df[
      ~df.name.str.contains(
          r"ETF|ETN|스팩|리츠|인버스|레버리지|선물",
          case=False,
          na=False,
      )
  ]
  df = df.drop_duplicates("code")
  log(f"전체 종목 목록: {len(df)}개")
  return df[["code", "name"]].to_dict("records")


def yahoo_bars(code, interval, period):
  for suffix in (".KS", ".KQ"):
    try:
      url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
      r = S.get(
          url,
          params={"range": period, "interval": interval, "events": "history"},
          timeout=10,
      )
      if r.status_code != 200:
        continue
      j = r.json()["chart"]
      if j.get("error") or not j.get("result"):
        continue
      z = j["result"][0]
      q = z["indicators"]["quote"][0]
      df = pd.DataFrame(
          {
              "Open": q.get("open", []),
              "High": q.get("high", []),
              "Low": q.get("low", []),
              "Close": q.get("close", []),
              "Volume": q.get("volume", []),
          },
          index=pd.to_datetime(z["timestamp"], unit="s", utc=True)
          .tz_convert(TZ),
      )
      df = df.dropna(subset=["Close"])
      if len(df) >= 25:
        return df
    except Exception:
      continue
  return None


def daily(code):
  try:
    df = fdr.DataReader(
        code, (now().date() - timedelta(days=150)).isoformat()
    )
    if df is None or len(df) < 50:
      return None
    c = pd.to_numeric(df.Close, errors="coerce").dropna()
    if len(c) < 50:
      return None
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    h = pd.to_numeric(df.High, errors="coerce")
    l = pd.to_numeric(df.Low, errors="coerce")
    prev = pd.to_numeric(df.Close, errors="coerce").shift()
    tr = pd.concat(
        [h - l, (h - prev).abs(), (l - prev).abs()], axis=1
    ).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    return {
        "df": df,
        "close": c,
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "prev_high": float(h.tail(20).iloc[:-1].max()),
        "atr": atr,
    }
  except Exception as e:
    log(f"일봉오류 {code}: {type(e).__name__}")
    return None


def rsi(c):
  d = c.diff()
  up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
  dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
  return float(
      (100 - 100 / (1 + up / dn.replace(0, float("nan")))).iloc[-1]
  )


def analyze(code, name, q):
  p = q["price"]
  if not MIN_PRICE <= p <= MAX_PRICE:
    return None, "가격범위"
  if p * q["volume"] < MIN_TURNOVER:
    return None, "거래대금"
  if q["gain"] < 0.4:
    return None, "시가상승률"

  d = daily(code)
  if d is None:
    return None, "일봉데이터없음"
  c = d["close"]
  rv = rsi(c)
  if d["ema20"] <= d["ema50"] or p < d["ema20"]:
    return None, "일봉추세"
  if rv >= 78:
    return None, "과열"
  if p > d["prev_high"] * 1.02:
    return None, "추격매수위험"

  m15 = yahoo_bars(code, "15m", "5d")
  m5 = yahoo_bars(code, "5m", "5d")
  if m15 is None or m5 is None:
    return None, "분봉데이터없음"

  c15 = m15.Close
  c5 = m5.Close
  if len(c15) < 25 or len(c5) < 25:
    return None, "분봉부족"
  e15 = c15.ewm(span=20, adjust=False).mean()
  e5 = c5.ewm(span=20, adjust=False).mean()
  v15 = m15.Volume.fillna(0)
  v5 = m5.Volume.fillna(0)
  avg15 = float(v15.iloc[-21:-1].mean())
  avg5 = float(v5.iloc[-21:-1].mean())
  if avg15 <= 0 or avg5 <= 0:
    return None, "분봉거래량"
  if c15.iloc[-1] <= e15.iloc[-1] or c5.iloc[-1] <= e5.iloc[-1]:
    return None, "단기추세"
  if v15.iloc[-1] < avg15 * 1.2 or v5.iloc[-1] < avg5 * 1.2:
    return None, "분봉거래량증가부족"

  risk = max(p * 0.012, min(p * 0.03, d["atr"] * 0.7))
  return {
      "code": code,
      "name": name,
      "entry": p,
      "tp1": p + risk,
      "tp2": p + 2 * risk,
      "tp3": p + 3 * risk,
      "sl": p - risk,
      "initial_sl": p - risk,
      "risk": risk,
      "max_tp": 0,
      "tp1_done": False,
      "tp2_done": False,
      "created": now().isoformat(),
      "rsi": rv,
      "gain": q["gain"],
      "turnover": p * q["volume"],
  }, "통과"


def entry_msg(p):
  e = p["entry"]
  return (
      f"🚨 <b>국내 주식 상승 시그널</b>\n━━━━━━━━━━━━━━━━━━\n"
      f"종목: <b>{p['name']}</b> ({p['code']})\n"
      f"관찰 진입가: <code>{won(e)}</code>\n"
      f"TP1: <code>{won(p['tp1'])}</code> ({change(e, p['tp1']):+.2f}%)\n"
      f"TP2: <code>{won(p['tp2'])}</code> ({change(e, p['tp2']):+.2f}%)\n"
      f"TP3: <code>{won(p['tp3'])}</code> ({change(e, p['tp3']):+.2f}%)\n"
      f"손절: <code>{won(p['initial_sl'])}</code> ({change(e, p['initial_sl']):+.2f}%)\n"
      f"시가 대비 상승: {p['gain']:.2f}%\n"
      f"일봉 RSI: {p['rsi']:.1f}\n"
      f"거래대금 추정: {p['turnover']:,.0f}원\n"
      "근거: 일봉 추세 + 15분봉·5분봉 EMA 및 거래량 확인\n"
      f"차트: https://finance.naver.com/item/main.naver?code={p['code']}\n"
      "⚠️ 공개 데이터 기반 가상 시그널이며 실제 주문이 아닙니다."
  )


def report_stats(st):
  h = st.get("history", [])
  counts = {
      k: sum(x.get("outcome") == k for x in h)
      for k in ("tp3", "tp2_end", "tp1_end", "initial_sl", "timeout")
  }
  total = len(h)

  wins = counts["tp3"] + counts["tp2_end"]
  draws = counts["tp1_end"]
  losses = counts["initial_sl"] + counts["timeout"]

  decisive_total = total - draws
  rate = (100 * wins / decisive_total) if decisive_total else 0.0

  return (
      "📊 <b>목표 도달 후 추적 종료 분류 통계</b>\n"
      "━━━━━━━━━━━━━━━━━━\n"
      f"총 종료 시그널: {total}건\n\n"
      f"🎯 TP3 종료 (승): {counts['tp3']}건\n"
      f"🎯 TP2 이후 종료 (승): {counts['tp2_end']}건\n"
      f"🤝 1차 본전 종료 (무): {counts['tp1_end']}건\n"
      f"🛑 초기 손절 종료 (패): {counts['initial_sl']}건\n"
      f"⏱️ 시간 종료 (패): {counts['timeout']}건\n\n"
      f"🏆 승률 (승/패 기준): <b>{rate:.1f}%</b>\n"
      f"(승 {wins}건 / 패 {losses}건 | 무 {draws}건 제외)\n"
      f"현재 추적 중: {len(st['positions'])}건\n"
      "※ 실현 계좌 수익률이 아닌 모의 시그널 목표 도달·종료 기록입니다."
  )


def close_position(st, code, outcome, price):
  p = st["positions"].pop(code)
  e = p["entry"]
  net_pct = change(e, price)

  record = {
      "code": code,
      "name": p["name"],
      "outcome": outcome,
      "entry": e,
      "exit": price,
      "net_pct": net_pct,
      "exit_time": now(),
  }
  st.setdefault("history", []).append(record)
  st["history"] = st["history"][-1000:]

  labels = {
      "tp3": "TP3 최종 목표 도달",
      "tp2_end": "TP2 이후 본전/이익 종료",
      "tp1_end": "TP1 이후 본전 종료 (무)",
      "initial_sl": "초기 손절 (패)",
      "timeout": "시간 종료 (패)",
  }

  send(
      f"📊 <b>시그널 종료</b>\n"
      f"{p['name']} ({code})\n"
      f"결과: <b>{labels.get(outcome, outcome)}</b>\n"
      f"진입: {won(e)} → 확인가: {won(price)} ({net_pct:+.2f}%)\n"
      "━━━━━━━━━━━━━━━━━━\n"
      + report_stats(st)
  )
  save_state(st)


def monitor(st):
  closed_any = False
  for code, p in list(st["positions"].items()):
    q = quote(code)
    if not q:
      continue
    price = q["price"]

    # 시간 경과 체크 (최대 보유 기간 초과 시 timeout 처리)
    created_dt = datetime.fromisoformat(p["created"])
    if (now() - created_dt).total_seconds() >= MAX_HOLD_DAYS * 86400:
      close_position(st, code, "timeout", price)
      closed_any = True
      continue

    # 손절 체크
    if price <= p["sl"]:
      max_tp = p.get("max_tp", 0)
      if max_tp >= 2:
        outcome = "tp2_end"
      elif max_tp >= 1:
        outcome = "tp1_end"
      else:
        outcome = "initial_sl"
      close_position(st, code, outcome, p["sl"])
      closed_any = True
      continue

    # 목표가 체크
    if price >= p["tp3"]:
      for key, label, tp_val in [
          ("tp1_done", "TP1", p["tp1"]),
          ("tp2_done", "TP2", p["tp2"]),
      ]:
        if not p[key]:
          p[key] = True
          send(f"🎯 <b>{label} 도달 확인</b> | {p['name']} ({code})")
      close_position(st, code, "tp3", p["tp3"])
      closed_any = True
      continue

    if price >= p["tp2"] and not p["tp2_done"]:
      p["tp2_done"] = True
      p["max_tp"] = 2
      p["sl"] = p["tp1"]  # TP2 도달 시 손절선을 TP1 가격으로 상향
      send(
          f"🎯 <b>TP2 도달</b> | {p['name']} ({code})\n현재가:"
          f" <code>{won(price)}</code>\n가상 손절선 상향: <code>{won(p['sl'])}</code>"
      )
      save_state(st)
    elif price >= p["tp1"] and not p["tp1_done"]:
      p["tp1_done"] = True
      p["max_tp"] = 1
      p["sl"] = p["entry"]  # TP1 도달 시 손절선을 진입가(본전)로 상향
      send(
          f"🎯 <b>TP1 도달</b> | {p['name']} ({code})\n현재가:"
          f" <code>{won(price)}</code>\n가상 손절선 본전 상향:"
          f" <code>{won(p['sl'])}</code>"
      )
      save_state(st)

  return closed_any


def get_active_status_msg(st):
  if not st["positions"]:
    return "📊 <b>현재 활성 시그널 없음</b>\n모든 슬롯이 대기 중입니다."
  lines = ["📊 <b>현재 활성 시그널 현황</b>", "━━━━━━━━━━━━━━━━━━"]
  for i, (code, p) in enumerate(st["positions"].items(), 1):
    hit = p.get("max_tp", 0)
    status = "TP2 도달 (본전보호)" if hit == 2 else "TP1 도달 (본전보호)" if hit == 1 else "추적 중"
    cur_price = p["entry"]
    q = quote(code)
    if q:
      cur_price = q["price"]
    pnl_pct = change(p["entry"], cur_price)
    sign = "+" if pnl_pct >= 0 else ""
    lines.append(
        f"<b>{i}. {p['name']} ({code})</b>\n진입: <code>{won(p['entry'])}</code> |"
        f" 현재: <code>{won(cur_price)}</code>\n상태: {status} | 수익률: <b>{sign}{pnl_pct:.2f}%</b>"
    )
  return "\n".join(lines)


def main():
  if not TOKEN or not CHAT:
    raise RuntimeError("TELEGRAM_TOKEN / CHAT_ID Secrets 확인 필요")
  t = now()
  log(f"KST: {t.isoformat()}")
  if t.weekday() >= 5 or not (540 <= t.hour * 60 + t.minute <= 930):
    log("장 운영시간 외")
    return

  s = load()
  if s.get("day") != t.date().isoformat():
    s["day"] = t.date().isoformat()
    s["sent_today"] = {}
    s["full_notice_day"] = ""

  position_closed = monitor(s)

  if len(s["positions"]) >= MAX_POS:
    if position_closed:
      send(get_active_status_msg(s))
      send(report_stats(s))
    save_state(s)
    log(f"활성 시그널 풀슬롯: {len(s['positions'])}")
    return

  stocks = universe()
  stats = {
      "quote_ok": 0,
      "first_pass": 0,
      "detail": 0,
      "reject": {},
      "sent": 0,
  }
  candidates = []
  for i, item in enumerate(stocks, 1):
    code = item["code"]
    if code in s["positions"] or code in s["sent_today"]:
      continue
    q = quote(code)
    if q:
      stats["quote_ok"] += 1
      turnover = q["price"] * q["volume"]
      if (
          MIN_PRICE <= q["price"] <= MAX_PRICE
          and turnover >= MIN_TURNOVER
          and q["gain"] >= 0.25
      ):
        candidates.append((turnover, item, q))
        stats["first_pass"] += 1
    if i % 100 == 0:
      log(f"전체 스캔 {i}/{len(stocks)}")
    time.sleep(0.04)

  candidates.sort(key=lambda x: x[0], reverse=True)
  log(
      f"1차 결과: 전체={len(stocks)}, 시세성공={stats['quote_ok']},"
      f" 후보={stats['first_pass']}"
  )

  new_sent = 0
  for _, item, q in candidates[:MAX_DETAIL]:
    if len(s["positions"]) >= MAX_POS:
      break
    code = item["code"]
    stats["detail"] += 1
    try:
      p, reason = analyze(code, item["name"], q)
      if not p:
        stats["reject"][reason] = stats["reject"].get(reason, 0) + 1
        continue
      s["positions"][code] = p
      s["sent_today"][code] = t.isoformat()
      send(entry_msg(p))
      new_sent += 1
      stats["sent"] += 1
      log(f"신규 시그널: {p['name']} ({code})")
      time.sleep(0.3)
    except Exception as e:
      log(f"분석오류 {code}: {type(e).__name__}: {e}")

  if new_sent > 0 or position_closed:
    send(get_active_status_msg(s))
    send(report_stats(s))

  if len(s["positions"]) >= MAX_POS and s.get("full_notice_day"] != s["day"]:
    send(f"⏸️ 시그널 슬롯 {MAX_POS}개 모두 사용 중\n기존 시그널을 추적합니다.")
    s["full_notice_day"] = s["day"]

  s["last_run"] = now().isoformat()
  save_state(s)
  log(f"활성 시그널: {len(s['positions'])}")


def save_state(s):
  with open(STATE + ".tmp", "w", encoding="utf-8") as f:
    json.dump(s, f, ensure_ascii=False, indent=2)
  os.replace(STATE + ".tmp", STATE)


if __name__ == "__main__":
  main()
