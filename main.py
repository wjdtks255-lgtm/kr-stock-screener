from datetime import datetime, timedelta
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
import FinanceDataReader as fdr
import pandas as pd
import requests

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()

STATE_FILE = "active_positions.json"
KST = ZoneInfo("Asia/Seoul")
TIMEOUT = 10
S = requests.Session()

MAX_SLOTS = 8       # 최대 보유 슬롯 8개 제한
COOLDOWN_HOURS = 4  # 동일 종목 재발송 쿨다운 (시간)


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
                    "parse_mode": "HTML",
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


def universe_and_quotes():
    out_items = []
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
                        or ""
                    ).strip()

                    if not code or code == "000000" or not name:
                        continue
                    if any(item["code"] == code for item in out_items):
                        continue

                    out_items.append({"code": code, "name": name})
        except Exception as e:
            print(f"[UNIVERSE ERR - {market_type}]", e)

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
    with ThreadPoolExecutor(max_workers=12) as executor:
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
        start_str = (datetime.now(KST) - timedelta(days=200)).strftime("%Y-%m-%d")

        df = fdr.DataReader(code, start_str, end_str)
        if df is None or df.empty or len(df) < 30:
            return []

        df = df.reset_index()
        date_col = next(
            (c for c in df.columns if "date" in str(c).lower() or "날짜" in str(c)),
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


def analyze(q, bars):
    if len(bars) < 30:
        return None

    closes = [x["close"] for x in bars]
    vols = [x["volume"] for x in bars]

    p = q["price"]
    ma5 = avg(closes[-5:])
    ma20 = avg(closes[-20:])

    avgv = avg(vols[-20:])
    vr = q["volume"] / avgv if avgv else 0

    hi = max(closes[-20:])

    score = 0
    why = []

    # [고확신 상승 종목 엄격 필터링 로직]
    if p > ma20:
        score += 2
        why.append("20일선 위 안착")
    if ma5 > ma20:
        score += 2
        why.append("단기 이동평균선 정배열")
    
    # 거래량 급증 조건
    if vr >= 2.5:
        score += 3
        why.append(f"평균 거래량 {vr:.1f}배 폭증")
    elif vr >= 1.8:
        score += 2
        why.append(f"평균 거래량 {vr:.1f}배 급증")

    # 강한 상승률 조건
    if q["change"] >= 5.0:
        score += 3
        why.append(f"급등세 (+{q['change']:.1f}%)")
    elif q["change"] >= 3.0:
        score += 2
        why.append(f"상승세 (+{q['change']:.1f}%)")

    # 20일 신고가 경신 혹은 임박
    if p >= hi * 0.99:
        score += 3
        why.append("20일 신고가 돌파/임박")

    # 풍부한 거래대금 (50억 원 이상)
    if q["turnover"] >= 50e8:
        score += 2
        why.append("거래대금 50억 원 이상 유입")

    # 고확신 종목만 통과시키기 위한 기준 점수 (12점 이상)
    if score < 12:
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


def build(item, q, a):
    score, why, vr = a
    p = q["price"]
    sl = p * 0.96   # 손절가 -4%
    tp1 = p * 1.03  # 1차 익절 +3%
    tp2 = p * 1.06  # 2차 익절 +6%
    tp3 = p * 1.10  # 3차 최종 익절 +10%

    lines = [
        "🔥 <b>[고확신 주도주 퀀트 시그널]</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📌 <b>{item['name']}</b> (<code>{item['code']}</code>)",
        f"💰 현재가: <b>{p:,.0f}원</b> ({q['change']:+.2f}%)",
        f"💵 거래대금: {q['turnover']/1e8:,.0f}억원 | 🔥 거래량: {vr:.2f}배",
        f"⭐ 확신 점수: <b>{score}점</b>",
        "",
        f"📊 <b>상승 근거</b>: {' · '.join(why[:4])}",
        "",
        f"🟢 진입가: {p:,.0f}원",
        f"🛡 손절가(SL): <code>{sl:,.0f}원</code> (-4%)",
        f"🎯 TP1: <code>{tp1:,.0f}원</code> (+3%)",
        f"🎯 TP2: <code>{tp2:,.0f}원</code> (+6%)",
        f"🎯 TP3 (최종): <code>{tp3:,.0f}원</code> (+10%)",
    ]

    ns = news(item["name"])
    if ns:
        lines += ["", "📰 <b>관련 뉴스 이슈</b>"]
        lines += [f"• {x}" for x in ns]

    pc_url = f"https://finance.naver.com/item/main.naver?code={item['code']}"
    m_url = f"https://m.stock.naver.com/domestic/stock/{item['code']}/total"
    lines += ["", f"🔗 <a href='{pc_url}'>네이버 금융(PC)</a> | <a href='{m_url}'>모바일 차트</a>"]

    position = {
        "name": item["name"],
        "code": item["code"],
        "entry": p,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp1_hit": False,
        "tp2_hit": False,
    }
    return "\n".join(lines), position


def monitor(st, qs):
    # 보유 중인 포지션 감시 (SL 혹은 최종 TP3 도달 시 시그널 종료 및 슬롯 반환)
    for code, pos in list(st["positions"].items()):
        q = qs.get(code)
        if not q:
            continue
        p = q["price"]
        sl = float(pos.get("sl", 0))
        entry = float(pos.get("entry", 0))
        tp1 = float(pos.get("tp1", 0))
        tp2 = float(pos.get("tp2", 0))
        tp3 = float(pos.get("tp3", 0))

        # 1. 손절가 이탈 시 (시그널 종료 및 슬롯 비우기)
        if p <= sl:
            tg(f"🛑 <b>[손절 도달 · 시그널 종료]</b>\n📌 {pos.get('name', code)}\n현재가: {p:,.0f}원\nSL: {sl:,.0f}원 이탈\n❌ 포지션 종료 및 슬롯 반환")
            del st["positions"][code]

        # 2. TP3 최종 목표가 도달 시 (+10% 달성 시그널 종료 및 슬롯 비우기)
        elif p >= tp3:
            tg(f"🏆 <b>[TP3 최종 목표가 도달 · 시그널 마감]</b>\n📌 {pos.get('name', code)}\n현재가: {p:,.0f}원 (+10% 달성)\n🎉 최고 수익 달성 완료 및 슬롯 반환")
            del st["positions"][code]

        # 3. TP2 도달 시 (+6% 달성, 손절가를 TP1 가격으로 상향 조정)
        elif not pos.get("tp2_hit") and p >= tp2:
            tg(f"🎯 <b>[TP2 2차 목표가 도달]</b>\n📌 {pos.get('name', code)}\n현재가: {p:,.0f}원 (+6%)\n🔒 손절가를 TP1 가격({tp1:,.0f}원)으로 상향 조정 (트레일링 스탑)")
            pos["tp2_hit"] = True
            pos["sl"] = tp1

        # 4. TP1 도달 시 (+3% 달성, 손절가를 진입가로 상향 조정)
        elif not pos.get("tp1_hit") and p >= tp1:
            tg(f"🎯 <b>[TP1 1차 목표가 도달]</b>\n📌 {pos.get('name', code)}\n현재가: {p:,.0f}원 (+3%)\n🔒 손절가를 진입가({entry:,.0f}원)로 상향 조정 (본전 방어 모드)")
            pos["tp1_hit"] = True
            pos["sl"] = entry


def main():
    print("====================================")
    print(f" KOREA STOCK HUNTER V13.1 (Max Slots: {MAX_SLOTS} | TP1~TP3)")
    print("====================================")
    print(f"KST Time: {datetime.now(KST).strftime('%H:%M')}")

    st = load_state()
    
    # 1. 전체 시장 시세 조회
    items = universe_and_quotes()
    if not items:
        tg("⚠️ <b>[국장 자동화 오류]</b>\n종목 마스터 수집 실패")
        return

    qs = get_market_quotes(items)

    # 2. 기존 포지션 모니터링 (손절/최종 TP3 도달 시 슬롯 반환)
    monitor(st, qs)
    save_state(st)

    # 3. 현재 활성화된 슬롯 확인
    current_slots = len(st["positions"])
    available_slots = MAX_SLOTS - current_slots
    print(f"[SLOT STATUS] Active: {current_slots}/{MAX_SLOTS} | Available Slots: {available_slots}")

    # 슬롯이 꽉 찼다면 새로운 스캔 없이 종료
    if available_slots <= 0:
        print("[SLOT FULL] All 8 slots are full. Skipping new scans until a slot frees up.")
        return

    if len(qs) < 10:
        tg(f"⚠️ <b>[국장 자동화 경고]</b>\n실시간 데이터 부족 (조회 성공: {len(qs)}/{len(items)})")
        return

    # 4. 빈 슬롯만큼 고확신 상승 종목 스캔
    pool = []
    for item in items:
        q = qs.get(item["code"])
        if not q:
            continue
        if q["turnover"] >= 3e8 or q["change"] >= 2.0:
            pool.append(item)

    pool.sort(
        key=lambda x: (qs[x["code"]]["turnover"], qs[x["code"]]["change"]),
        reverse=True,
    )
    pool = pool[:100]
    print("[SCAN] prefilter count:", len(pool))

    found = []
    for item in pool:
        if item["code"] in st["positions"]:
            continue
        
        a = analyze(qs[item["code"]], history(item["code"]))
        if a:
            found.append((a[0], item, qs[item["code"]], a))

    found.sort(key=lambda x: x[0], reverse=True)
    print("[SCAN] qualified candidates:", len(found))

    # 빈 슬롯 수만큼 상위 종목 시그널 발송
    sent_count = 0
    for _, item, q, a in found:
        if sent_count >= available_slots:
            break

        key = f"signal:{item['code']}"
        last = st["sent"].get(key)
        if last:
            try:
                if (
                    datetime.now(KST) - datetime.fromisoformat(last)
                    < timedelta(hours=COOLDOWN_HOURS)
                ):
                    continue
            except:
                pass

        msg, pos = build(item, q, a)
        if tg(msg):
            st["sent"][key] = datetime.now(KST).isoformat()
            st["positions"][item["code"]] = pos
            sent_count += 1
            print(f"[SIGNAL SENT] {item['name']} ({item['code']}) | Slot filled.")

    save_state(st)
    print(f"[DONE] Cycle completed. Newly sent: {sent_count} | Active slots now: {len(st['positions'])}")


if __name__ == "__main__":
    main()
