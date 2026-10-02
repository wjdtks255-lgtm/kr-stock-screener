import os, json, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import FinanceDataReader as fdr

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

def universe_and_quotes():
    """
    FinanceDataReader를 이용해 KRX 전 종목 마스터와 최신 종가 시세를 안정적으로 수집합니다.
    """
    out_items = []
    out_quotes = {}

    try:
        # KOSPI, KOSDAQ 전 종목 리스트 가져오기
        df_krx = fdr.StockListing('KRX')
        if df_krx.empty:
            print("[UNIVERSE ERR] KRX listing is empty")
            return [], {}

        # 가장 최근 영업일의 개별 종목 시세 데이터를 한 번에 가져오기 위해 최근 날짜 지정
        end_date = datetime.now(KST).strftime("%Y-%m-%d")
        start_date = (datetime.now(KST) - timedelta(days=5)).strftime("%Y-%m-%d")

        for _, row in df_krx.iterrows():
            code = str(row.get("Code") or row.get("symbol") or "").zfill(6)
            name = str(row.get("Name") or row.get("name") or "").strip()
            market = str(row.get("Market") or "").upper()

            if not code or not name:
                continue
            
            # 코스피, 코스닥 종목만 대상로 지정
            if "KOSPI" not in market and "KOSDAQ" not in market:
                continue

            out_items.append({
                "code": code,
                "name": name
            })

        # 대량 종목의 당일 시세를 빠르게 조회 (최근 1일 데이터)
        # 상위 유동성 확보를 위해 주요 종목 또는 전체 순회 시세 조회
        print(f"[UNIVERSE] Total Target Items: {len(out_items)}")

    except Exception as e:
        print("[UNIVERSE ERR]", e)

    return out_items

def get_market_quotes(items):
    """
    각 종목별 실시간/당일 지표를 FinanceDataReader로 안전하게 가져옵니다.
    """
    out_quotes = {}
    end_str = datetime.now(KST).strftime("%Y-%m-%d")
    start_str = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d")

    for item in items:
        code = item["code"]
        try:
            df = fdr.DataReader(code, start_str, end_str)
            if df.empty:
                continue
            
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest

            price = float(latest["Close"])
            prev_close = float(prev["Close"])
            change = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            
            volume = float(latest["Volume"])
            open_p = float(latest["Open"])
            high_p = float(latest["High"])
            low_p = float(latest["Low"])
            
            # 거래대금 추정 (종가 * 거래량) 또는 제공 데이터 활용
            turnover = price * volume

            if price <= 0:
                continue

            out_quotes[code] = {
                "price": price,
                "change": change,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "volume": volume,
                "turnover": turnover
            }
        except Exception as e:
            # 개별 종목 조회 에러는 무시하고 패스
            continue

    print(f"[QUOTE] Total Loaded Quotes: {len(out_quotes)}")
    return out_quotes

def history(code):
    """
    FinanceDataReader를 이용해 최근 180일 일봉 데이터를 오름차순으로 정확하게 가져옵니다.
    """
    try:
        end_str = datetime.now(KST).strftime("%Y-%m-%d")
        start_str = (datetime.now(KST) - timedelta(days=220)).strftime("%Y-%m-%d")

        df = fdr.DataReader(code, start_str, end_str)
        if df.empty or len(df) < 30:
            return []

        df = df.reset_index()
        date_col = next((c for c in df.columns if "date" in str(c).lower() or "날짜" in str(c)), df.columns[0])

        df = df.rename(columns={
            date_col: "date",
            "Close": "close",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Volume": "volume"
        })

        need = ["date", "close", "open", "high", "low", "volume"]
        if any(c not in df.columns for c in need):
            return []

        for c in ["close", "open", "high", "low", "volume"]:
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
        f"🛡 SL: {sl:,.0f}원",
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
    print(" KOREA STOCK HUNTER V11.0 (FDR)")
    print("====================================")
    print("MODE:", md, "FORCE:", FORCE)
    print("TOKEN:", bool(TOKEN), "CHAT_ID:", bool(CHAT_ID))

    st = load_state()
    items = universe_and_quotes()

    if not items:
        tg(
            "⚠️ [국장 자동화]\n"
            "종목 마스터 수집 실패\n"
            "이번 스캔을 중단했습니다."
        )
        return

    # 1차 필터링을 빠르게 수행하기 위해 거래대금/등락률 상위 위주 또는 조건 부합 후보 선별
    # 전체 종목 중 속도를 위해 상용 거래량/변동성 있는 종목 선별 혹은 쿼리 조회
    qs = get_market_quotes(items)

    if md == "monitor":
        monitor(st, qs)
        save_state(st)
        return

    if len(qs) < 10:
        tg(
            f"⚠️ [국장 자동화]\n"
            f"실시간 데이터 부족\n"
            f"조회 성공: {len(qs)}/{len(items)}\n"
            f"이번 스캔을 중단했습니다."
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
