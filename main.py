import os, json, re, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
MODE = os.getenv("MODE", "").strip().lower()
FORCE = os.getenv("FORCE_SCAN", "").strip().lower()

STATE_FILE = "active_positions.json"
KST = ZoneInfo("Asia/Seoul")
TIMEOUT = 8
S = requests.Session()

S.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.stock.naver.com/"
})

def n(v, d=0.):
    try: return float(str(v).replace(",", "").replace("%", "").strip())
    except: return d

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
        print("[TELEGRAM]", e)
        return False

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f: s = json.load(f)
    except: s = {}
    s.setdefault("positions", {})
    s.setdefault("sent", {})
    return s

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def run_mode():
    if FORCE in ("1", "true", "yes", "on") and MODE in ("morning", "intraday", "close", "monitor"):
        return MODE

    m = datetime.now(KST).hour * 60 + datetime.now(KST).minute

    if 540 <= m <= 570: return "morning"
    if 571 <= m <= 905: return "intraday"
    if 910 <= m <= 920: return "close"
    return "monitor"

def universe():
    out = []
    seen = set()

    # 네이버 모바일 실시간 거래상위/시총상위 API를 활용하여 우회 수집 (차단 없음)
    api_urls = [
        "https://m.stock.naver.com/api/json/sise/siseList.json?menu=market_sum&sosok=0", # 코스피 시총
        "https://m.stock.naver.com/api/json/sise/siseList.json?menu=market_sum&sosok=1", # 코스닥 시총
        "https://m.stock.naver.com/api/json/sise/siseList.json?menu=rise&sosok=0",     # 코스피 상승
        "https://m.stock.naver.com/api/json/sise/siseList.json?menu=rise&sosok=1",     # 코스닥 상승
        "https://m.stock.naver.com/api/json/sise/siseList.json?menu=quant&sosok=0",    # 코스피 거래량
        "https://m.stock.naver.com/api/json/sise/siseList.json?menu=quant&sosok=1"     # 코스닥 거래량
    ]

    for u in api_urls:
        try:
            r = S.get(u, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            j = r.json()
            items = j.get("result", {}).get("itemList", [])
            for item in items:
                code = str(item.get("cd", "")).strip()
                name = str(item.get("nm", "")).strip()
                if re.fullmatch(r"\d{6}", code) and name and code not in seen:
                    seen.add(code)
                    out.append({"code": code, "name": name})
        except Exception as e:
            print("[UNIVERSE ERR]", e)

    print("[UNIVERSE]", len(out))
    return out

def quotes(items):
    out = {}

    for i in range(0, len(items), 40):
        batch = items[i:i+40]
        q_str = "|".join(x["code"] for x in batch)

        try:
            u = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{q_str}"
            r = S.get(u, timeout=TIMEOUT)

            if r.status_code != 200:
                continue

            j = r.json()

            for area in j.get("areas", []):
                for d in area.get("datas", []):
                    code = str(d.get("cd") or "").zfill(6)
                    price = n(d.get("nv"))

                    if not code or not price:
                        continue

                    out[code] = {
                        "price": price,
                        "change": n(d.get("cr")),
                        "open": n(d.get("ov")),
                        "high": n(d.get("hv")),
                        "low": n(d.get("lv")),
                        "volume": n(d.get("aq")),
                        "turnover": n(d.get("aa"))
                    }

        except Exception as e:
            print("[QUOTE ERR]", i, e)

    print("[QUOTE]", len(out), "/", len(items))
    return out

def history(code):
    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=180)

        u = f"https://api.stock.naver.com/chart/domestic/item/{code}"
        r = S.get(
            u,
            params={
                "periodType": "dayCandle",
                "startDateTime": start.strftime("%Y%m%d"),
                "endDateTime": end.strftime("%Y%m%d")
            },
            timeout=TIMEOUT
        )

        if r.status_code != 200:
            return []

        j = r.json()
        d = pd.DataFrame(j.get("priceInfo") or [])

        if d.empty:
            return []

        d = d.rename(columns={
            "closePrice": "close",
            "openPrice": "open",
            "highPrice": "high",
            "lowPrice": "low",
            "accumulatedTradingVolume": "volume"
        })

        need = ["close", "open", "high", "low", "volume"]
        if len(d) < 30 or any(c not in d for c in need):
            return []

        for c in need:
            d[c] = pd.to_numeric(d[c], errors="coerce")

        bars = []
        for _, row in d.dropna(subset=need).iterrows():
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
            timeout=TIMEOUT
        )

        if r.status_code != 200:
            return []

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
        f"🛡️ SL: {sl:,.0f}원",
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
        sl = n(pos.get("sl"))
        entry = n(pos.get("entry"))
        tp1 = n(pos.get("tp1"))
        tp2 = n(pos.get("tp2"))

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
    print(" KOREA STOCK HUNTER V8.3")
    print("====================================")
    print("MODE:", md, "FORCE:", FORCE)
    print("TOKEN:", bool(TOKEN), "CHAT_ID:", bool(CHAT_ID))

    st = load_state()
    items = universe()

    if not items:
        tg(
            "⚠️ [국장 자동화]\n"
            "종목 목록을 가져오지 못했습니다.\n"
            "이번 스캔을 중단했습니다."
        )
        return

    qs = quotes(items)

    if md == "monitor":
        monitor(st, qs)
        save_state(st)
        return

    if len(qs) < 20:
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
