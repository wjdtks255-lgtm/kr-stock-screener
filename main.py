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
      f"🚨 국내 주식 상승 시그널\n━━━━━━━━━━━━━━\n"
      f"종목: {p['name']} ({p['code']})\n"
      f"관찰 진입가: {won(e)}\n"
      f"TP1: {won(p['tp1'])} ({change(e, p['tp1']):+.2f}%)\n"
      f"TP2: {won(p['tp2'])} ({change(e, p['tp2']):+.2f}%)\n"
      f"TP3: {won(p['tp3'])} ({change(e, p['tp3']):+.2f}%)\n"
      f"손절: {won(p['initial_sl'])} ({change(e, p['initial_sl']):+.2f}%)\n"
      f"시가 대비 상승: {p['gain']:.2f}%\n"
      f"일봉 RSI: {p['rsi']:.1f}\n"
      f"거래대금 추정: {p['turnover']:,.0f}원\n"
      "근거: 일봉 추세 + 15분봉·5분봉 EMA 및 거래량 확인\n"
      f"차트: https://finance.naver.com/item/main.naver?code={p['code']}\n"
      "⚠️ 공개 데이터 기반 가상 시그널이며 실제 주문이 아닙니다."
  )


def close_signal(s, code, reason, price):
  p = s["positions"].pop(code)
  e = p["entry"]
  send(
      f"🏁 시그널 종료 | {p['name']} ({code})\n"
      f"사유: {reason}\n확인가: {won(price)}\n"
      f"진입 대비: {change(e, price):+.2f}%\n"
      f"남은 슬롯: {MAX_POS-len(s['positions'])}/{MAX_POS}"
  )


def monitor(s, code, q):
  p = s["positions"].get(code)
  if not p:
    return
  price = q["price"]
  if price <= p["sl"]:
    close_signal(s, code, "손절/가상 추적 손절", price)
    return
  if price >= p["tp3"]:
    for key, label in (("tp1_done", "TP1"), ("tp2_done", "TP2")):
      if not p[key]:
        p[key] = True
        send(f"🎯 {label} 도달 확인 | {p['name']} ({code})")
    close_signal(s, code, "TP3 최종 목표", price)
    return
  if price >= p["tp2"] and not p["tp2_done"]:
    p["tp2_done"] = True
    p["sl"] = p["tp1"]
    send(
        f"🎯 TP2 도달 | {p['name']} ({code})\n현재가: {won(price)}\n가상"
        f" 손절선: {won(p['sl'])}"
    )
  elif price >= p["tp1"] and not p["tp1_done"]:
    p["tp1_done"] = True
    p["sl"] = p["entry"]
    send(
        f"🎯 TP1 도달 | {p['name']} ({code})\n현재가: {won(price)}\n가상"
        f" 손절선: {won(p['sl'])}"
    )


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

  for code in list(s["positions"]):
    q = quote(code)
    if q:
      try:
        monitor(s, code, q)
      except Exception as e:
        log(f"추적오류 {code}: {e}")

  if len(s["positions"]) < MAX_POS:
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
        stats["sent"] += 1
        log(f"신규 시그널: {p['name']} ({code})")
        time.sleep(0.3)
      except Exception as e:
        log(f"분석오류 {code}: {type(e).__name__}: {e}")

    log(
        f"상세분석={stats['detail']}, 시그널={stats['sent']},"
        f" 탈락사유={stats['reject']}"
    )

  if len(s["positions"]) >= MAX_POS and s.get("full_notice_day") != s["day"]:
    send(f"⏸️ 시그널 슬롯 {MAX_POS}개 모두 사용 중\n기존 시그널을 추적합니다.")
    s["full_notice_day"] = s["day"]

  s["last_run"] = now().isoformat()
  save(s)
  log(f"활성 시그널: {len(s['positions'])}")


if __name__ == "__main__":
  main()
