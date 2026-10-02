import os, json, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from pykrx import stock

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
MODE = os.getenv("MODE", "").strip().lower()
FORCE = os.getenv("FORCE_SCAN", "").strip().lower()

STATE_FILE = "active_positions.json"
KST = ZoneInfo("Asia/Seoul")
TIMEOUT = 10
S = requests.Session()

def tg(text):
    if not TOKEN or not CHAT_ID: return False
    try:
        r = S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": "true"
            },
            timeout=TIMEOUT
        )
        print("[TELEGRAM]", r.status_code)
        return r.ok
    except Exception as e:
        print("[TELEGRAM ERR]", e)
        return False

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f: s = json.load(f)
    except: s = {}
    s.setdefault("positions", {})
    s.setdefault("sent", {})
    return s

def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[STATE SAVE ERR]", e)

def run_mode():
    if FORCE in ("1", "true", "yes", "on") and MODE in ("morning", "intraday", "close", "monitor"):
        return MODE

    m = datetime.now(KST).hour * 60 + datetime.now(KST).minute

    if 540 <= m <= 570: return "morning"
    if 571 <= m <= 905: return "intraday"
    if 910 <= m <= 920: return "close"
    return "monitor"

def get_latest_trading_date():
    # 오늘 또는 직전 영업일 찾기 (주말/공휴일 대응)
    d = datetime.now(KST)
    for _ in range(5):
        s_date = d.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv_by_ticker(s_date, market="KOSPI")
            if not df.empty:
                return s_date
        except:
            pass
        d -= timedelta(days=1)
    return datetime.now(KST).strftime("%Y%m%d")

def universe_and_quotes():
    """
    pykrx를 이용해 KOSPI/KOSDAQ 전 종목의 당일 시세(가격, 거래량, 거래대금, 등락률)를 
    한 번에 가져와서 유니버스와 실시간 쿼리 데이터(quotes)를 동시 구축합니다.
    """
    out_items = []
    out_quotes = {}
    
    date_str = get_latest_trading_date()
    print("[PYKRX] Target Date:", date_str)

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df.empty:
                continue
            
            for code, row in df.iterrows():
                code_str = str(code).zfill(6)
                name = stock.get_market_ticker_name(code_str)
                
                price = float(row.get("종가가격") if "종가가격" in row else row.get("종가", 0))
                change = float(row.get("등락률", 0))
                open_p = float(row.get("시가", 0))
                high_p = float(row.get("고가", 0))
                low_p = float(row.get("저가", 0))
                volume = float(row.get("거래량", 0))
                turnover = float(row.get("거래대금", 0))

                if price <= 0:
                    continue

                out_items.append({
                    "code": code_str,
                    "name": name
                })

                out_quotes[code_str] = {
                    "price": price,
                    "change": change,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "volume": volume,
                    "turnover": turnover
                }
        except Exception as e:
            print(f"[UNIVERSE ERR] {market}:", e)

    print(f"[UNIVERSE] Total Items: {len(out_items)}")
    print(f"[QUOTE] Total Quotes: {len(out_quotes)}")
    return out_items, out_quotes

def history(code):
    """
    pykrx를 이용해 특정 종목의 최근 180일 일봉 데이터를 오름차순으로 정확하게 가져옵니다.
    """
    try:
        end = datetime.now(KST).strftime("%Y%m%d")
        start = (datetime.now(KST) - timedelta(days=220)).strftime("%Y%m%d")

        df = stock.get_market_ohlcv_by_date(start, end, code)
        if df.empty or len(df) < 30:
            return []

        df = df.reset_index()
        # 날짜 컬럼명 대응 (날짜 또는 날짜/시간)
        date_col = next((c for c in df.columns if "날짜" in c or "date" in str(c).lower()), df.columns[0])
        
        df = df.rename(columns={
            date_col: "date",
            "종가": "close",
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "거래량": "volume"
        })

        need = ["close", "open", "high", "low", "volume"]
        for c in need:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df.sort_values("date").dropna(subset=need)

        bars = []
        for _, row in df.iterrows():
            bars.append({
                "close": row["close"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "volume": row["volume"]
            })

        return bars

    except Exception as e:
        print("[HIST ERR]", code, e)
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

    pos = (p - lo) / (hi - lo) if hi > lo else .5

    score = 0
    why = []

    checks = [
        (p > ma20, 2, "20일선 위"),
        (ma5 > ma20, 2, "단기 정배열"),
        (p > ma60, 1, "60일선 위")
    ]

    for ok, pts, msg in checks:
        if ok:
            score += pts
            why.append(msg)

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

    if pos >= .8:
        score += 2
        why.append("20일 고점권")
    elif pos >= .65:
        score += 1
        why.append("상단 가격대")

    if p >= hi:
        score += 3
        why.append("20일 신고가")
    elif p >= hi * .98:
        score += 1
        why.append("20일 고점 근접")

    if q["turnover"] >= 50e8:
        score += 2
        why.append("거래대금 50억+")
    elif q["turnover"] >= 10e8:
        score += 1
        why.append("거래대금 10억+")

    need = {
        "morning": 7,
        "intraday": 8,
        "close": 9
    }.get(md, 8)

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
                "ceid": "KR:ko"
            },
            timeout=5
        )

        if r.status_code != 200:
            return []

        import re
        return [
            re.sub(r"<.*?>", "", x).strip()
            for x in re.findall(r"<item>.*?<title>(.*?)</title>.*?</item>", r.text, re.S | re.I)[:2]
        ]
    except:
        return []

def build(item, q, a, md):
    score, why, vr = a

    p = q["price"]
    sl = p * .95
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
        f"🛡️️ SL: {sl:,.0f}원",
        f"🎯 TP1: {tp1:,.0f}원",
        f"🎯 TP2: {tp2:,.0f}원"
    ]

    ns = news(item["name"])
    if ns:
        lines += ["", "📰 최근 뉴스"]
        lines += [f"• {x}" for x in ns]

    lines += [
        "",
        f"https://finance.naver.com/item/main.naver?code={item['code']}"
    ]

    position = {
        "name": item["name"],
        "entry": p,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp1_hit": False
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
                f"🛑 [손절 도달]\n"
                f"{pos.get('name', code)}\n"
                f"현재가: {p:,.0f}원\n"
                f"SL: {sl:,.0f}원"
            )
            del st["positions"][code]

        elif not pos.get("tp1_hit") and p >= tp1:
            tg(
                f"🎯 [TP1 도달]\n"
                f"{pos.get('name', code)}\n"
                f"현재가: {p:,.0f}원\n"
                f"TP1: {tp1:,.0f}원\n"
                f"🔒 SL → 진입가"
            )
            pos["tp1_hit"] = True
            pos["sl"] = entry

        elif p >= tp2:
            tg(
                f"🏁 [TP2 도달]\n"
                f"{pos.get('name', code)}\n"
                f"현재가: {p:,.0f}원\n"
                f"TP2: {tp2:,.0f}원\n"
                f"추적 종료"
            )
            del st["positions"][code]

def main():
    md = run_mode()

    print("====================================")
    print(" KOREA STOCK HUNTER V10.0 (PyKrx)")
    print("====================================")
    print("MODE:", md, "FORCE:", FORCE)
    print("TOKEN:", bool(TOKEN), "CHAT_ID:", bool(CHAT_ID))

    st = load_state()
    items, qs = universe_and_quotes()

    if not items or not qs:
        tg(
            "⚠️ [국장 자동화]\n"
            "KRX 데이터 수집 실패\n"
            "이번 스캔을 중단했습니다."
        )
        return

    if md == "monitor":
        monitor(st, qs)
        save_state(st)
        return

    pool = []
    for item in items:
        q = qs.get(item["code"])
        if not q:
            continue

        if q["turnover"] >= 3e8 or q["change"] >= 1.5:
            pool.append(item)

    pool.sort(
        key=lambda x: (
            qs[x["code"]]["turnover"],
            qs[x["code"]]["change"]
        ),
        reverse=True
    )

    pool = pool[:70]
    print("[SCAN] prefilter:", len(pool))

    found = []
    for item in pool:
        a = analyze(
            qs[item["code"]],
            history(item["code"]),
            md
        )

        if a:
            found.append((
                a[0],
                item,
                qs[item["code"]],
                a
            ))

    found.sort(key=lambda x: x[0], reverse=True)
    print("[SCAN] final:", len(found))

    for _, item, q, a in found[:3]:
        key = f"{md}:{item['code']}"
        last = st["sent"].get(key)

        if last:
            try:
                if datetime.now(KST) - datetime.fromisoformat(last) < timedelta(hours=4):
                    continue
            except:
                pass

        msg, pos = build(item, q, a, md)

        if tg(msg):
            st["sent"][key] = datetime.now(KST).isoformat()
            if md == "close":
                st["positions"][item["code"]] = pos

    save_state(st)

if __name__ == "__main__":
    main()
