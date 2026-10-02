import os,json,re,requests
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID=(os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
MODE=os.getenv("MODE","").strip().lower()
FORCE=os.getenv("FORCE_SCAN","").strip().lower()

STATE_FILE="active_positions.json"
KST=ZoneInfo("Asia/Seoul")
BASE="https://m.stock.naver.com"
LIST_BASE="https://m.stock.naver.com/front-api"
POLL="https://polling.finance.naver.com/api/realtime/domestic/stock/"
TIMEOUT=8
S=requests.Session()

S.headers.update({
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
    "Accept":"application/json,text/plain,*/*"
})

def n(v,d=0.):
    try:return float(str(v).replace(",","").replace("%","").strip())
    except:return d

def walk(x):
    if isinstance(x,dict):
        yield x
        for v in x.values():yield from walk(v)
    elif isinstance(x,list):
        for v in x:yield from walk(v)

def tg(text):
    if not TOKEN or not CHAT_ID:return False
    try:
        r=S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id":CHAT_ID,
                "text":text,
                "disable_web_page_preview":"true"
            },
            timeout=TIMEOUT
        )
        print("[TELEGRAM]",r.status_code)
        return r.ok
    except Exception as e:
        print("[TELEGRAM]",e)
        return False

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:s=json.load(f)
    except:s={}
    s.setdefault("positions",{})
    s.setdefault("sent",{})
    return s

def save_state(s):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)

def run_mode():
    if FORCE in ("1","true","yes","on") and MODE in ("morning","intraday","close","monitor"):
        return MODE

    m=datetime.now(KST).hour*60+datetime.now(KST).minute

    if 540<=m<=570:return "morning"
    if 571<=m<=905:return "intraday"
    if 910<=m<=920:return "close"
    return "monitor"

def list_page(category,sort_type,page=1,size=100):
    try:
        u=f"{LIST_BASE}/stock/domestic/stockList"

        r=S.get(
            u,
            params={
                "sortType":sort_type,
                "category":category,
                "page":page,
                "pageSize":size
            },
            timeout=TIMEOUT
        )

        if r.status_code!=200:
            print("[LIST HTTP]",category,sort_type,page,r.status_code)
            return []

        return r.json()

    except Exception as e:
        print("[LIST ERR]",category,sort_type,page,e)
        return []

def extract_items(data):
    out=[]
    seen=set()

    for d in walk(data):
        code=str(
            d.get("itemCode") or
            d.get("code") or
            d.get("itemcode") or
            ""
        ).strip()

        name=str(
            d.get("itemName") or
            d.get("name") or
            ""
        ).strip()

        if re.fullmatch(r"\d{6}",code) and name and code not in seen:
            seen.add(code)
            out.append({
                "code":code,
                "name":name
            })

    return out

def universe():
    out=[]
    seen=set()

    # 상승률/거래량/시총 상위에서 후보군을 구성
    for market in ("KOSPI","KOSDAQ"):
        for sort_type in ("up","quantTop","marketValue"):
            for page in (1,2,3):

                data=list_page(
                    market,
                    sort_type,
                    page,
                    100
                )

                got=extract_items(data)

                print(
                    f"[LIST] {market}/{sort_type}/p{page}: {len(got)}"
                )

                for x in got:
                    if x["code"] not in seen:
                        seen.add(x["code"])
                        out.append(x)

    print("[UNIVERSE]",len(out))

    return out

def quotes(items):
    out={}

    for i in range(0,len(items),40):

        batch=items[i:i+40]
        codes=",".join(x["code"] for x in batch)

        try:
            r=S.get(
                POLL+codes,
                timeout=TIMEOUT
            )

            if r.status_code!=200:
                print("[QUOTE]",i,r.status_code)
                continue

            j=r.json()

            for d in j.get("datas",[]):

                code=str(
                    d.get("itemCode") or
                    d.get("cd") or
                    ""
                ).strip()

                if not code:
                    continue

                p=n(
                    d.get("closePrice")
                    if "closePrice" in d
                    else d.get("nv")
                )

                if p<=0:
                    continue

                out[code]={
                    "price":p,
                    "change":n(
                        d.get("fluctuationsRatio")
                        if "fluctuationsRatio" in d
                        else d.get("cr")
                    ),
                    "open":n(
                        d.get("openPrice")
                        if "openPrice" in d
                        else d.get("ov")
                    ),
                    "high":n(
                        d.get("highPrice")
                        if "highPrice" in d
                        else d.get("hv")
                    ),
                    "low":n(
                        d.get("lowPrice")
                        if "lowPrice" in d
                        else d.get("lv")
                    ),
                    "volume":n(
                        d.get("accumulatedTradingVolume")
                        if "accumulatedTradingVolume" in d
                        else d.get("aq")
                    ),
                    "turnover":n(
                        d.get("accumulatedTradingValue")
                        if "accumulatedTradingValue" in d
                        else d.get("aa")
                    )
                }

        except Exception as e:
            print("[QUOTE ERR]",i,e)

    print("[QUOTE]",len(out),"/",len(items))

    return out

def history(code):
    try:
        r=S.get(
            f"{BASE}/api/stock/{code}/price",
            params={
                "pageSize":100,
                "page":1
            },
            timeout=TIMEOUT
        )

        if r.status_code!=200:
            return []

        data=r.json()

        if isinstance(data,dict):
            for key in ("priceInfo","result","data"):
                if isinstance(data.get(key),list):
                    data=data[key]
                    break

        if not isinstance(data,list):
            return []

        bars=[]

        for d in data:
            c=n(d.get("closePrice"))
            v=n(d.get("accumulatedTradingVolume"))

            if c>0:
                bars.append({
                    "close":c,
                    "open":n(d.get("openPrice")),
                    "high":n(d.get("highPrice")),
                    "low":n(d.get("lowPrice")),
                    "volume":v,
                    "date":str(d.get("localTradedAt",""))[:10]
                })

        return bars

    except Exception as e:
        print("[HIST ERR]",code,e)
        return []

def avg(a):
    return sum(a)/len(a) if a else 0

def analyze(q,bars,md):

    if len(bars)<30:
        return None

    closes=[x["close"] for x in bars]
    vols=[x["volume"] for x in bars]

    p=q["price"]

    ma5=avg(closes[:5])
    ma20=avg(closes[:20])
    ma60=avg(closes[:60]) if len(closes)>=60 else avg(closes)

    avgv=avg(vols[:20])
    vr=q["volume"]/avgv if avgv else 0

    hi=max(closes[:20])
    lo=min(closes[:20])

    pos=(p-lo)/(hi-lo) if hi>lo else .5

    score=0
    why=[]

    checks=[
        (p>ma20,2,"20일선 위"),
        (ma5>ma20,2,"단기 정배열"),
        (p>ma60,1,"60일선 위")
    ]

    for ok,pts,msg in checks:
        if ok:
            score+=pts
            why.append(msg)

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
        why.append("상단 가격대")

    if p>=hi:
        score+=3
        why.append("20일 신고가")
    elif p>=hi*.98:
        score+=1
        why.append("20일 고점 근접")

    # 50억원 / 10억원
    if q["turnover"]>=50e8:
        score+=2
        why.append("거래대금 50억+")

    elif q["turnover"]>=10e8:
        score+=1
        why.append("거래대금 10억+")

    need={
        "morning":7,
        "intraday":8,
        "close":9
    }.get(md,8)

    if score<need:
        return None

    return score,why,vr

def news(code):
    try:
        r=S.get(
            f"{BASE}/api/news/list/integration",
            params={
                "itemCode":code,
                "page":1,
                "pageSize":3
            },
            timeout=TIMEOUT
        )

        if r.status_code!=200:
            return []

        out=[]

        for d in walk(r.json()):
            title=str(
                d.get("title") or
                d.get("newsTitle") or
                ""
            ).strip()

            if title and len(title)>4:
                title=re.sub("<.*?>","",title)

                if title not in out:
                    out.append(title)

            if len(out)>=2:
                break

        return out

    except:
        return []

def build(item,q,a,md):

    score,why,vr=a

    p=q["price"]
    sl=p*.95
    tp1=p*1.03
    tp2=p*1.06

    lines=[
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

    ns=news(item["code"])

    if ns:
        lines+=["","📰 최근 뉴스"]
        lines += [f"• {x}" for x in ns]

    lines += [
        "",
        f"https://finance.naver.com/item/main.naver?code={item['code']}"
    ]

    position={
        "name":item["name"],
        "entry":p,
        "sl":sl,
        "tp1":tp1,
        "tp2":tp2,
        "tp1_hit":False
    }

    return "\n".join(lines),position

def monitor(st,qs):

    for code,pos in list(st["positions"].items()):

        q=qs.get(code)

        if not q:
            continue

        p=q["price"]
        sl=n(pos.get("sl"))
        entry=n(pos.get("entry"))
        tp1=n(pos.get("tp1"))
        tp2=n(pos.get("tp2"))

        if p<=sl:

            tg(
                f"🛑 [손절 도달]\n"
                f"{pos.get('name',code)}\n"
                f"현재가: {p:,.0f}원\n"
                f"SL: {sl:,.0f}원"
            )

            del st["positions"][code]

        elif not pos.get("tp1_hit") and p>=tp1:

            tg(
                f"🎯 [TP1 도달]\n"
                f"{pos.get('name',code)}\n"
                f"현재가: {p:,.0f}원\n"
                f"TP1: {tp1:,.0f}원\n"
                f"🔒 SL → 진입가"
            )

            pos["tp1_hit"]=True
            pos["sl"]=entry

        elif p>=tp2:

            tg(
                f"🏁 [TP2 도달]\n"
                f"{pos.get('name',code)}\n"
                f"현재가: {p:,.0f}원\n"
                f"TP2: {tp2:,.0f}원\n"
                f"추적 종료"
            )

            del st["positions"][code]

def main():

    md=run_mode()

    print("====================================")
    print(" KOREA STOCK HUNTER V8.0")
    print("====================================")
    print("MODE:",md,"FORCE:",FORCE)
    print("TOKEN:",bool(TOKEN),"CHAT_ID:",bool(CHAT_ID))

    st=load_state()

    items=universe()

    if not items:

        tg(
            "⚠️ [국장 자동화]\n"
            "종목 목록 API 응답을 받지 못했습니다.\n"
            "이번 스캔을 중단했습니다."
        )

        return

    qs=quotes(items)

    if md=="monitor":

        monitor(st,qs)
        save_state(st)
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
            pool.append(item)

    pool.sort(
        key=lambda x:(
            qs[x["code"]]["turnover"],
            qs[x["code"]]["change"]
        ),
        reverse=True
    )

    pool=pool[:70]

    print("[SCAN] prefilter:",len(pool))

    found=[]

    for item in pool:

        a=analyze(
            qs[item["code"]],
            history(item["code"]),
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

    found.sort(
        key=lambda x:x[0],
        reverse=True
    )

    print("[SCAN] final:",len(found))

    for _,item,q,a in found[:3]:

        key=f"{md}:{item['code']}"
        last=st["sent"].get(key)

        if last:

            try:

                if datetime.now(KST)-datetime.fromisoformat(last)<timedelta(hours=4):
                    continue

            except:
                pass

        msg,pos=build(
            item,
            q,
            a,
            md
        )

        if tg(msg):

            st["sent"][key]=datetime.now(KST).isoformat()

            if md=="close":
                st["positions"][item["code"]]=pos

    save_state(st)

if __name__=="__main__":
    main()
