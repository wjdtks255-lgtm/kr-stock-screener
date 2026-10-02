import os,re,json,requests
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pandas as pd

TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID=(os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
MODE=os.getenv("MODE","").strip().lower()
FORCE=os.getenv("FORCE_SCAN","").strip().lower()
STATE="active_positions.json"
KST=ZoneInfo("Asia/Seoul")
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0","Referer":"https://finance.naver.com/"})
TIMEOUT=8
UNIVERSE=300
MAX_ANALYZE=70
TOP=3

def num(x,d=0.):
    try:return float(str(x).replace(",","").replace("%","").strip())
    except:return d

def tg(msg):
    if not TOKEN or not CHAT_ID:
        print("[TELEGRAM] credentials missing")
        return False
    try:
        r=S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id":CHAT_ID,"text":msg,"disable_web_page_preview":"true"},
            timeout=TIMEOUT
        )
        print("[TELEGRAM]",r.status_code)
        return r.ok
    except Exception as e:
        print("[TG]",e)
        return False

def load():
    try:
        with open(STATE,encoding="utf-8") as f:x=json.load(f)
    except:
        x={}
    x.setdefault("positions",{})
    x.setdefault("sent",{})
    return x

def save(x):
    with open(STATE,"w",encoding="utf-8") as f:
        json.dump(x,f,ensure_ascii=False,indent=2)

def mode():
    if FORCE in ("1","true","yes","on") and MODE in ("morning","intraday","close","monitor"):
        return MODE
    m=datetime.now(KST).hour*60+datetime.now(KST).minute
    if 540<=m<=570:return "morning"
    if 571<=m<=905:return "intraday"
    if 910<=m<=920:return "close"
    return "monitor"

def universe():
    out=[]
    seen=set()

    for market in (0,1):
        for page in range(1,9):
            try:
                u=f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={market}&page={page}"
                t=S.get(u,timeout=TIMEOUT).text

                rows=re.findall(
                    r'/item/main\.naver\?code=(\d{6})[^>]*>\s*([^<]+?)\s*</a>',
                    t
                )

                for code,name in rows:
                    name=re.sub("<.*?>","",name).strip()
                    if code not in seen and name:
                        seen.add(code)
                        out.append({"code":code,"name":name})

            except Exception as e:
                print("[LIST]",e)

    print("[LIST]",len(out))
    return out[:UNIVERSE]

def quotes(items):
    out={}

    for i in range(0,len(items),40):
        batch=items[i:i+40]
        q="|".join(x["code"] for x in batch)

        try:
            u="https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:"+q
            j=S.get(u,timeout=TIMEOUT).json()

            for area in j.get("areas",[]):
                for d in area.get("datas",[]):
                    code=str(d.get("cd") or "").zfill(6)
                    price=num(d.get("nv"))

                    if not code or not price:
                        continue

                    out[code]={
                        "price":price,
                        "change":num(d.get("cr")),
                        "open":num(d.get("ov")),
                        "high":num(d.get("hv")),
                        "low":num(d.get("lv")),
                        "volume":num(d.get("aq")),
                        "turnover":num(d.get("aa"))
                    }

        except Exception as e:
            print("[QUOTE]",i,e)

    print("[QUOTE]",len(out),"/",len(items))
    return out

def hist(code):
    try:
        end=datetime.now(KST).date()
        start=end-timedelta(days=180)

        u=f"https://api.stock.naver.com/chart/domestic/item/{code}"
        j=S.get(
            u,
            params={
                "periodType":"dayCandle",
                "startDateTime":start.strftime("%Y%m%d"),
                "endDateTime":end.strftime("%Y%m%d")
            },
            timeout=TIMEOUT
        ).json()

        d=pd.DataFrame(j.get("priceInfo") or [])

        d=d.rename(columns={
            "closePrice":"close",
            "openPrice":"open",
            "highPrice":"high",
            "lowPrice":"low",
            "accumulatedTradingVolume":"volume"
        })

        need=["close","open","high","low","volume"]

        if len(d)<60 or any(c not in d for c in need):
            return None

        for c in need:
            d[c]=pd.to_numeric(d[c],errors="coerce")

        return d.dropna(subset=need).reset_index(drop=True)

    except Exception as e:
        print("[HIST]",code,e)
        return None

def analyze(q,d,md):
    if d is None or len(d)<60:
        return None

    c=q["price"]
    cl=d.close

    ma5=cl.tail(5).mean()
    ma20=cl.tail(20).mean()
    ma60=cl.tail(60).mean()

    avg=d.volume.tail(20).mean()
    vr=q["volume"]/avg if avg else 0

    hi=cl.tail(20).max()
    lo=cl.tail(20).min()

    pos=(c-lo)/(hi-lo) if hi>lo else .5

    score=0
    why=[]

    checks=[
        (c>ma20,2,"20일선 위"),
        (ma5>ma20,2,"단기 정배열"),
        (c>ma60,1,"60일선 위")
    ]

    for ok,pts,text in checks:
        if ok:
            score+=pts
            why.append(text)

    if vr>=3:
        score+=3
        why.append(f"거래량 {vr:.1f}배")
    elif vr>=2:
        score+=2
        why.append(f"거래량 {vr:.1f}배")
    elif vr>=1.3:
        score+=1
        why.append(f"거래량 {vr:.1f}배")

    if q["change"]>=7:
        score+=2
        why.append(f"상승률 +{q['change']:.1f}%")
    elif q["change"]>=3:
        score+=1
        why.append(f"상승률 +{q['change']:.1f}%")

    if pos>=.8:
        score+=2
        why.append("20일 고점권")
    elif pos>=.65:
        score+=1
        why.append("상단 매물대")

    if c>=hi:
        score+=3
        why.append("20일 신고가")
    elif c>=hi*.98:
        score+=1
        why.append("20일 고점 근접")

    if q["turnover"]>=50e9:
        score+=2
        why.append("거래대금 500억+")
    elif q["turnover"]>=10e9:
        score+=1
        why.append("거래대금 100억+")

    threshold={
        "morning":7,
        "intraday":8,
        "close":9
    }.get(md,8)

    if score<threshold:
        return None

    return score,why,vr

def news(name):
    try:
        u="https://news.google.com/rss/search"

        t=S.get(
            u,
            params={
                "q":f'"{name}" 주식',
                "hl":"ko",
                "gl":"KR",
                "ceid":"KR:ko"
            },
            timeout=TIMEOUT
        ).text

        return [
            re.sub("<.*?>","",x).strip()
            for x in re.findall(
                r"<item>.*?<title>(.*?)</title>.*?</item>",
                t,re.S|re.I
            )[:2]
        ]

    except:
        return []

def candidate(item,q,a,md):
    score,why,vr=a

    p=q["price"]
    sl=p*.95
    tp1=p*1.03
    tp2=p*1.06

    z=[
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

    n=news(item["name"])

    if n:
        z+=["","📰 최근 뉴스"]
        z += [f"• {x}" for x in n]

    z += [
        "",
        f"https://finance.naver.com/item/main.naver?code={item['code']}"
    ]

    pos={
        "name":item["name"],
        "entry":p,
        "sl":sl,
        "tp1":tp1,
        "tp2":tp2,
        "tp1_hit":False
    }

    return "\n".join(z),pos

def monitor(st,qs):
    for code,pos in list(st["positions"].items()):
        q=qs.get(code)

        if not q:
            continue

        price=q["price"]
        sl=num(pos.get("sl"))
        entry=num(pos.get("entry"))
        tp1=num(pos.get("tp1"))
        tp2=num(pos.get("tp2"))

        if price<=sl:
            tg(
                f"🛑 [손절 도달]\n"
                f"{pos.get('name',code)} ({code})\n"
                f"현재가: {price:,.0f}원\n"
                f"SL: {sl:,.0f}원"
            )
            del st["positions"][code]

        elif not pos.get("tp1_hit") and price>=tp1:
            tg(
                f"🎯 [TP1 도달]\n"
                f"{pos.get('name',code)} ({code})\n"
                f"현재가: {price:,.0f}원\n"
                f"TP1: {tp1:,.0f}원\n"
                f"🔒 SL을 진입가로 이동"
            )
            pos["tp1_hit"]=True
            pos["sl"]=entry

        elif price>=tp2:
            tg(
                f"🏁 [TP2 도달]\n"
                f"{pos.get('name',code)} ({code})\n"
                f"현재가: {price:,.0f}원\n"
                f"TP2: {tp2:,.0f}원\n"
                f"추적 종료"
            )
            del st["positions"][code]

def main():
    md=mode()

    print("====================================")
    print(" KOREA STOCK HUNTER V7.0")
    print("====================================")
    print("MODE:",md,"FORCE:",FORCE)
    print("TOKEN:",bool(TOKEN),"CHAT_ID:",bool(CHAT_ID))

    state=load()
    items=universe()

    if not items:
        tg("⚠️ [국장 자동화]\n종목 목록을 가져오지 못했습니다.")
        return

    qs=quotes(items)

    if md=="monitor":
        monitor(state,qs)
        save(state)
        return

    if len(qs)<20:
        tg(
            f"⚠️ [국장 자동화]\n"
            f"실시간 데이터 부족\n"
            f"조회 성공: {len(qs)}/{len(items)}\n"
            f"이번 스캔을 중단했습니다."
        )
        return

    pool=[]

    for item in items:
        q=qs.get(item["code"])

        if not q:
            continue

        if q["turnover"]>=3e8 or q["change"]>=1.5:
            pool.append((
                q["turnover"],
                q["change"],
                q["volume"],
                item
            ))

    pool.sort(reverse=True)
    pool=pool[:MAX_ANALYZE]

    found=[]

    for _,_,_,item in pool:
        a=analyze(
            qs[item["code"]],
            hist(item["code"]),
            md
        )

        if a:
            found.append(
                (
                    a[0],
                    item,
                    qs[item["code"]],
                    a
                )
            )

    print("[CANDIDATES]",len(found))

    found.sort(reverse=True,key=lambda x:x[0])

    for _,item,q,a in found[:TOP]:
        key=f"{md}:{item['code']}"
        last=state["sent"].get(key)

        if last:
            try:
                if datetime.now(KST)-datetime.fromisoformat(last)<timedelta(hours=4):
                    continue
            except:
                pass

        text,pos=candidate(item,q,a,md)

        if tg(text):
            state["sent"][key]=datetime.now(KST).isoformat()

            if md=="close":
                state["positions"][item["code"]]=pos

    save(state)

if __name__=="__main__":
    main()
