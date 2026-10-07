from datetime import datetime, timedelta
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
import FinanceDataReader as fdr
import pandas as pd

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
MODE = os.getenv("MODE", "").strip().lower()
FORCE = os.getenv("FORCE_SCAN", "").strip().lower()

STATE_FILE = "active_positions.json"
KST = ZoneInfo("Asia/Seoul")
TIMEOUT = 10
S = requests.Session()


def tg(text):
    if not TOKEN or not CHAT_ID:
        return False
    for n in range(2):
        try:
            r = S.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={
                    "chat_id": CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": "true",
                },
                timeout=TIMEOUT,
            )
            if r.ok:
                return True
        except Exception as e:
            print("[TELEGRAM ERR]", e)
        time.sleep(1)
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except:
        s = {}
    s.setdefault("positions", {})
    s.setdefault("sent", {})
    return s


def save_state(s):
    try:
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        print("[STATE SAVE ERR]", e)


def run_mode():
    if FORCE in ("1", "true", "yes", "on") and MODE in (
        "morning",
        "intraday",
        "close",
        "monitor",
    ):
        return MODE

    m = datetime.now(KST).hour * 60 + datetime.now(KST).minute

    if 540 <= m <= 570:
        return "morning"
    if 571 <= m <= 905:
        return "intraday"
    if 910 <= m <= 920:
        return "close"
    return "monitor"


def universe_and_quotes():
    out_items = []
    # KRX 종목 목록 로드 안정성 강화 (여러 마켓 심볼 대응)
    for market_type in ["KRX", "KOSPI", "KOSDAQ"]:
        try:
            df_krx = fdr.StockListing(market_type)
            if df_krx is not None and not df_krx.empty:
                for _, row in df_krx.iterrows():
                    code = str(
                        row.get("Code")
                        or row.get("symbol")
                        or row.get("Symbol")
                        or ""
                    ).zfill(6)
                    name = str(
                        row.get("Name")
                        or row.get("name")
                        or row.get("Marcap")
                        or ""
                    ).strip()
                    market = str(
                        row.get("Market") or row.get("market") or ""
                    ).upper()

                    if not code or code == "000000" or not name:
                        continue
                    # 중복 방지
                    if any(item["code"] == code for item in out_items):
                        continue

                    out_items.append({"code": code, "name": name})
        except Exception as e:
            print(f"[UNIVERSE ERR - {market_type}]", e)

        if len(out_items) > 1000:
            break

    print(f"[UNIVERSE] Total Target Items: {len(out_items)}")
    return out_items


def fetch_single_quote(item, start_str, end_str):
    code = item["code"]
    try:
        df = fdr.DataReader(code, start_str, end_str)
        if df is None or df.empty or len(df) < 2:
            return None

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(latest["Close"])
        prev_close = float(prev["Close"])
        change = (
            ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
        )

        volume = float(latest["Volume"])
        open_p = float(latest["Open"])
        high_p = float(latest["High"])
        low_p = float(latest["Low"])
        turnover = price * volume

        if price <= 0:
            return None

        return code, {
            "price": price,
            "change": change,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "volume": volume,
            "turnover": turnover,
        }
    except:
        return None


def get_market_quotes(items):
    out_quotes = {}
    end_str = datetime.now(KST).strftime("%Y-%m-%d")
    start_str = (datetime.now(KST) - timedelta(days=10)).strftime("%Y-%m-%d")

    print("[QUOTE] Fetching market quotes with multithreading...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(fetch_single_quote, item, start_str, end_str)
            for item in items
        ]
        for future in as_completed(futures):
            res = future.result()
            if res:
                code, q_data = res
                out_quotes[code] = q_data

    print(f"[QUOTE] Total Loaded Quotes: {len(out_quotes)}")
    return out_quotes


def history(code):
    try:
        end_str = datetime.now(KST).strftime("%Y-%m-%d")
        start_str = (datetime.now(KST) - timedelta(days=220)).strftime(
            "%Y-%m-%d"
        )

        df = fdr.DataReader(code, start_str, end_str)
        if df is None or df.empty or len(df) < 30:
            return []

        df = df.reset_index()
        date_col = next(
            (
                c
                for c in df.columns
                if "date" in str(c).lower() or "날짜" in str(c)
            ),
            df.columns[0],
        )

        df = df.rename(
            columns={
                date_col: "date",
                "Close": "close",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Volume": "volume",
            }
        )

        need = ["date", "close", "open", "high", "low", "volume"]
        if any(c not in df.columns for c in need):
            return []

        for c in ["close", "open", "high", "low", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df.sort_values("date").dropna(subset=need)

        bars = []
        for _, row in df.iterrows():
            bars.append(
                {
                    "close": row["close"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "volume": row["volume"],
                }
            )
        return bars
    except:
        return []


def avg(a):
    return sum(a) / len(a) if a else 0


def analyze(q, bars, md):
    if len(bars) < 30:
        return None

    closes = [x["close"] for x in bars]
    vols = [x["volume"] for x in bars]

    p = q["price"]
    ma5 = avg(closes[-5:])
    ma20 = avg(closes[-20:])
    ma60 = avg(closes[-60:]) if len(closes) >= 60 else avg(closes)

    avgv = avg(vols[-20:])
    vr = q["volume"] / avgv if avgv else 0

    hi = max(closes[-20:])
    lo = min(closes[-20:])
    pos = (p - lo) / (hi - lo) if hi > lo else 0.5

    score = 0
    why = []

    if p > ma20:
        score += 2
        why.append("20일선 위")
    if ma5 > ma20:
        score += 2
        why.append("단기 정배열")
    if p > ma60:
        score += 1
        why.append("60일선 위")

    if vr >= 3:
        score += 3
        why.append(f"거래량 {vr:.1f}배")
    elif vr >= 2:
        score += 2
        why.append(f"거래량 {vr:.1f}배")
    elif vr >= 1.3:
        score += 1
        why.append(f"거래량 {vr:.1f}배")

    if q["change"] >= 7:
        score += 2
        why.append(f"상승률 +{q['change']:.1f}%")
    elif q["change"] >= 3:
        score += 1
        why.append(f"상승률 +{q['change']:.1f}%")

    if pos >= 0.8:
        score += 2
        why.append("20일 고점권")
    elif pos >= 0.65:
        score += 1
        why.append("상단 가격대")

    if p >= hi:
        score += 3
        why.append("20일 신고가")
    elif p >= hi * 0.98:
        score += 1
        why.append("20일 고점 근접")

    if q["turnover"] >= 50e8:
        score += 2
        why.append("거래대금 50억+")
    elif q["turnover"] >= 10e8:
        score += 1
        why.append("거래대금 10억+")

    need = {"morning": 7, "intraday": 8, "close": 9}.get(md, 8)
    if score < need:
        return None

    return score, why, vr


def news(name):
    try:
        u = "https://news.google.com/rss/search"
        r = S.get(
            u,
            params={
                "q": f'"{name}" 주식',
                "hl": "ko",
                "gl": "KR",
                "ceid": "KR:ko",
            },
            timeout=5,
        )
        if r.status_code != 200:
            return []
        import re

        return [
            re.sub(r"<.*?>", "", x).strip()
            for x in re.findall(
                r"<item>.*?<title>(.*?)</title>.*?</item>", r.text, r.S | r.I
            )[:2]
        ]
    except:
        return []


def build(item, q, a, md):
    score, why, vr = a
    p = q["price"]
    sl = p * 0.95
    tp1 = p * 1.03
    tp2 = p * 1.06

    lines = [
        f"🚨 [{md.upper()} 상승 후보]",
        "━━━━━━━━━━━━━━━━━━",
        f"📌 {item['name']} ({item['code']})",
        f"💰 현재가: {p:,.0f}원",
        f"📈 등락률: {q['change']:+.2f}%",
        f"💵 거래대금: {q['turnover']/1e8:,.0f}억원",
        f"🔥 거래량: {vr:.2f}배",
        f"⭐ 점수: {score}",
        "",
        f"📊 상승 근거: {' · '.join(why[:5])}",
        "",
        f"🟢 진입: {p:,.0f}원",
        f"🛡 SL: {sl:,.0f}원",
        f"🎯 TP1: {tp1:,.0f}원",
        f"🎯 TP2: {tp2:,.0f}원",
    ]

    ns = news(item["name"])
    if ns:
        lines += ["", "📰 최근 뉴스"]
        lines += [f"• {x}" for x in ns]

    pc_url = f"https://finance.naver.com/item/main.naver?code={item['code']}"
    m_url = f"https://m.stock.naver.com/domestic/stock/{item['code']}/total"
    lines += ["", f"🔗 PC: {pc_url}", f"📱 모바일: {m_url}"]

    position = {
        "name": item["name"],
        "entry": p,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp1_hit": False,
    }
    return "\n".join(lines), position


def monitor(st, qs):
    for code, pos in list(st["positions"].items()):
        q = qs.get(code)
        if not q:
            continue
        p = q["price"]
        sl = float(pos.get("sl", 0))
        entry = float(pos.get("entry", 0))
        tp1 = float(pos.get("tp1", 0))
        tp2 = float(pos.get("tp2", 0))

        if p <= sl:
            tg(
                f"🛑 [손절 도달]\n{pos.get('name', code)}\n현재가: {p:,.0f}원\nSL: {sl:,.0f}원"
            )
            del st["positions"][code]
        elif not pos.get("tp1_hit") and p >= tp1:
            tg(
                f"🎯 [TP1 도달]\n{pos.get('name', code)}\n현재가: {p:,.0f}원\nTP1: {tp1:,.0f}원\n🔒 SL → 진입가"
            )
            pos["tp1_hit"] = True
            pos["sl"] = entry
        elif p >= tp2:
            tg(
                f"🏁 [TP2 도달]\n{pos.get('name', code)}\n현재가: {p:,.0f}원\nTP2: {tp2:,.0f}원\n추적 종료"
            )
            del st["positions"][code]


def main():
    md = run_mode()
    print("====================================")
    print(" KOREA STOCK HUNTER V11.3 (Optimized)")
    print("====================================")
    print("MODE:", md, "FORCE:", FORCE)

    st = load_state()
    items = universe_and_quotes()

    if not items:
        tg("⚠️ [국장 자동화]\n종목 마스터 수집 실패")
        return

    qs = get_market_quotes(items)

    if md == "monitor":
        monitor(st, qs)
        save_state(st)
        return

    if len(qs) < 10:
        tg(
            f"⚠️ [국장 자동화]\n실시간 데이터 부족\n조회 성공: {len(qs)}/{len(items)}"
        )
        return

    pool = []
    for item in items:
        q = qs.get(item["code"])
        if not q:
            continue
        if q["turnover"] >= 3e8 or q["change"] >= 1.5:
            pool.append(item)

    pool.sort(
        key=lambda x: (qs[x["code"]]["turnover"], qs[x["code"]]["change"]),
        reverse=True,
    )
    pool = pool[:70]
    print("[SCAN] prefilter:", len(pool))

    found = []
    for item in pool:
        a = analyze(qs[item["code"]], history(item["code"]), md)
        if a:
            found.append((a[0], item, qs[item["code"]], a))

    found.sort(key=lambda x: x[0], reverse=True)
    print("[SCAN] final:", len(found))

    for _, item, q, a in found[:3]:
        key = f"{md}:{item['code']}"
        last = st["sent"].get(key)
        if last:
            try:
                if (
                    datetime.now(KST) - datetime.fromisoformat(last)
                    < timedelta(hours=4)
                ):
                    continue
            except:
                pass

        msg, pos = build(item, q, a, md)
        if tg(msg):
            st["sent"][key] = datetime.now(KST).isoformat()
            # 종가뿐만 아니라 모니터링이 필요한 모든 포지션에 등록되도록 개선
            st["positions"][item["code"]] = pos

    save_state(st)


if __name__ == "__main__":
    main()
