import os,json,time,re,requests
from datetime import datetime,timedelta
from urllib.parse import quote
import pandas as pd
import numpy as np

TOKEN=os.getenv("TELEGRAM_TOKEN","")
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID","")
MODE=os.getenv("MODE","").strip().lower()
FORCE=os.getenv("FORCE_SCAN","").strip().lower()
STATE_FILE="active_positions.json"

REALTIME_URL="https://polling.finance.naver.com/api/realtime"
CHART_URL="https://api.stock.naver.com/chart/domestic/item/{}/"
NEWS_URL="https://news.google.com/rss/search"

KST=__import__("zoneinfo").ZoneInfo("Asia/Seoul")
TOP_SCAN=100
UNIVERSE=350
BATCH=40
SL=0.05
TP1=0.03
TP2=0.06
COOLDOWN=4
NEWS_DAYS=7
TIMEOUT=8

S=requests.Session()
S.headers.update({
    "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept":"application/json,text/plain,*/*"
})

def now():
    return datetime.now(KST)

def fmt(v):
    try:return f"{float(v):,.0f}"
    except:return "-"

def pct(v):
    try:return f"{float(v):+.2f}%"
    except:return "-"

def send(msg):
    if not TOKEN or not CHAT_ID:
        print("[TELEGRAM] TOKEN/CHAT_ID 없음")
        return False
    try:
        r=S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"disable_web_page_preview":True},
            timeout=10
        )
        print("[TELEGRAM]",r.status_code)
        return r.ok
    except Exception as e:
        print("[TELEGRAM ERROR]",e)
        return False

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
    except:return {"positions":{},"signals":{}}

def save_state(s):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)

def mode():
    if MODE in {"morning","intraday","close","monitor"} and FORCE in {"","1","true","yes"}:
        return MODE
    t=now().hour*60+now().minute
    if 540<=t<=570:return "morning"
    if 571<=t<=905:return "intraday"
    if 910<=t<=920:return "close"
    return "monitor"

def universe():
    print("종목 목록 수집...")
    rows=[]
    for market in (0,1):
        for page in range(1,8):
            url=f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={market}&page={page}"
            try:
                r=S.get(url,timeout=TIMEOUT)
                r.encoding="euc-kr"
                tables=pd.read_html(r.text)
                table=None
                for x in tables:
                    if "종목명" in x.columns:
                        table=x;break
                if table is None:continue
                for _,x in table.iterrows():
                    name=str(x.get("종목명","")).strip()
                    if not name or name=="nan":continue
                    link=""
                    try:
                        links=re.findall(r'/item/main\.naver\?code=(\d{6})',r.text)
                    except:links=[]
                    break
            except Exception as e:
                print("[UNIVERSE]",market,page,e)
    if rows:return rows[:UNIVERSE]

    # fallback: KRX listing
    try:
        u="https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
        r=S.get(u,timeout=15)
        r.encoding="euc-kr"
        df=pd.read_html(r.text)[0]
        for _,x in df.iterrows():
            code=str(x.get("종목코드","")).zfill(6)
            name=str(x.get("회사명","")).strip()
            if code.isdigit() and name:rows.append({"code":code,"name":name})
        print("KRX fallback:",len(rows))
    except Exception as e:
        print("Universe error:",e)
    return rows[:UNIVERSE]

def universe_fast():
    rows=[]
    for market in (0,1):
        for page in range(1,8):
            url=f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={market}&page={page}"
            try:
                r=S.get(url,timeout=TIMEOUT)
                r.encoding="euc-kr"
                links=re.findall(r'/item/main\.naver\?code=(\d{6})',r.text)
                names=[]
                tables=pd.read_html(r.text)
                for t in tables:
                    if "종목명" in t.columns:
                        names=t["종목명"].astype(str).tolist()
                        break
                for code,name in zip(links,names):
                    name=name.strip()
                    if name and name!="nan" and not code.startswith(("0","9")):
                        rows.append({"code":code,"name":name})
            except Exception as e:
                print("[LIST]",market,page,e)
    # remove duplicates
    out=[];seen=set()
    for x in rows:
        if x["code"] not in seen:
            seen.add(x["code"]);out.append(x)
    print("종목 목록:",len(out))
    return out[:UNIVERSE]

def realtime(items):
    out={}
    print("실시간 배치 조회:",len(items),"개")
    for i in range(0,len(items),BATCH):
        part=items[i:i+BATCH]
        q="|".join("SERVICE_ITEM:"+x["code"] for x in part)
        try:
            r=S.get(REALTIME_URL,params={"query":q},timeout=TIMEOUT)
            j=r.json()
            for area in j.get("areas",[]):
                for d in area.get("datas",[]):
                    c=str(d.get("cd","")).zfill(6)
                    p=float(d.get("nv",0) or 0)
                    if c and p:
                        out[c]={
                            "price":p,
                            "change":float(d.get("cr",0) or 0),
                            "diff":float(d.get("cv",0) or 0),
                            "prev":float(d.get("sv",0) or 0),
                            "open":float(d.get("ov",0) or 0),
                            "high":float(d.get("hv",0) or 0),
                            "low":float(d.get("lv",0) or 0),
                            "volume":float(d.get("aq",0) or 0),
                            "turnover":float(d.get("aa",0) or 0),
                            "status":d.get("ms","")
                        }
        except Exception as e:
            print("[REALTIME]",i,e)
        time.sleep(.15)
    print("실시간 성공:",len(out),"/",len(items))
    return out

def history(code):
    end=now().strftime("%Y%m%d")
    start=(now()-timedelta(days=130)).strftime("%Y%m%d")
    url=CHART_URL.format(code)
    try:
        r=S.get(url,params={
            "periodType":"dayCandle",
            "startDateTime":start,
            "endDateTime":end
        },timeout=TIMEOUT)
        j=r.json()
        rows=j.get("priceInfo",[])
        if not rows:
            return None
        df=pd.DataFrame(rows)
        for c in ["closePrice","openPrice","highPrice","lowPrice","accumulatedTradingVolume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.dropna(subset=["closePrice","accumulatedTradingVolume"])
    except Exception as e:
        return None

def analyze(x,df):
    if df is None or len(df)<30:return None
    p=x["price"]
    c=df["closePrice"]
    v=df["accumulatedTradingVolume"]
    ma5=c.rolling(5).mean().iloc[-1]
    ma20=c.rolling(20).mean().iloc[-1]
    ma60=c.rolling(60).mean().iloc[-1]
    avgv=v.rolling(20).mean().iloc[-1]
    vr=x["volume"]/avgv if avgv else 0
    hi=c.tail(20).max()
    lo=c.tail(20).min()
    pos=(p-lo)/(hi-lo) if hi>lo else .5
    intraday=(p/x["open"]-1)*100 if x["open"] else 0
    score=0
    reasons=[]

    if p>ma20:
        score+=2;reasons.append("20일선 위")
    if ma5>ma20:
        score+=2;reasons.append("단기 상승 배열")
    if p>ma60:
        score+=1;reasons.append("60일선 위")
    if vr>=3:
        score+=3;reasons.append(f"거래량 {vr:.1f}배")
    elif vr>=2:
        score+=2;reasons.append(f"거래량 {vr:.1f}배")
    elif vr>=1.3:
        score+=1;reasons.append(f"거래량 {vr:.1f}배")
    if x["change"]>=7:
        score+=2;reasons.append(f"당일 {x['change']:+.1f}%")
    elif x["change"]>=3:
        score+=1;reasons.append(f"당일 {x['change']:+.1f}%")
    if pos>=.8:
        score+=2;reasons.append("최근 고점권")
    elif pos>=.65:
        score+=1;reasons.append("고점권 접근")
    if p>=hi:
        score+=3;reasons.append("20일 고점 돌파")
    elif p>=hi*.98:
        score+=1;reasons.append("20일 고점 접근")
    if intraday>=3:
        score+=1;reasons.append("시가 대비 강세")
    if x["turnover"]>=50_000_000_000:
        score+=2;reasons.append("거래대금 500억+")
    elif x["turnover"]>=10_000_000_000:
        score+=1;reasons.append("거래대금 100억+")

    return {
        **x,
        "score":score,
        "ma20":ma20,
        "ma60":ma60,
        "vr":vr,
        "high20":hi,
        "pos":pos,
        "intraday":intraday,
        "reasons":reasons
    }

def news(name,code):
    try:
        q=quote(f'"{name}" "{code}"')
        r=S.get(NEWS_URL,params={"q":q,"hl":"ko","gl":"KR","ceid":"KR:ko"},timeout=NEWS_TIMEOUT)
        import xml.etree.ElementTree as ET
        root=ET.fromstring(r.text)
        out=[]
        for item in root.findall(".//item"):
            title=item.findtext("title","").strip()
            link=item.findtext("link","").strip()
            pub=item.findtext("pubDate","")
            if title:
                title=re.sub(r"<.*?>","",title)
                out.append((title,link,pub))
            if len(out)>=2:break
        return out
    except:
        return []

def catalyst(title):
    keys=[
        ("수주","수주"),
        ("공급계약","공급계약"),
        ("계약","계약"),
        ("실적","실적"),
        ("영업이익","실적"),
        ("신제품","신제품"),
        ("증설","증설"),
        ("공장","증설"),
        ("AI","AI"),
        ("반도체","반도체"),
        ("배터리","2차전지"),
        ("정책","정책"),
        ("정부","정책"),
        ("MOU","협력"),
        ("협약","협력")
    ]
    for k,v in keys:
        if k.lower() in title.lower():return v
    return "시장 이슈"

def threshold(m):
    return {"morning":7,"intraday":8,"close":9}.get(m,99)

def signal_recent(state,code,m):
    key=f"{m}:{code}"
    t=state.get("signals",{}).get(key)
    if not t:return False
    try:
        return datetime.now(KST)-datetime.fromisoformat(t)<timedelta(hours=COOLDOWN)
    except:return False

def mark_signal(state,code,m):
    state.setdefault("signals",{})[f"{m}:{code}"]=datetime.now(KST).isoformat()

def message(c,m,news_items):
    name=c["name"];p=c["price"]
    sl=p*(1-SL);tp1=p*(1+TP1);tp2=p*(1+TP2)
    title={
        "morning":"🌅 장초 상승 후보",
        "intraday":"🚀 장중 상승 후보",
        "close":"🌙 15:20 종가 후보"
    }.get(m,"📊 주식 후보")
    rs=" · ".join(c["reasons"][:5]) or "기술적 강세 조건 확인"
    lines=[
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"📌 {name} ({c['code']})",
        f"💰 현재가: {fmt(p)}원",
        f"📈 등락률: {pct(c['change'])}",
        f"💹 거래대금: {fmt(c['turnover']/100000000)}억원",
        f"📊 거래량: {c['vr']:.2f}배",
        f"⭐ 기술점수: {c['score']}점",
        "",
        "🔎 상승 근거",
        f"• {rs}",
        "",
        f"🟢 진입 기준: {fmt(p)}원",
        f"🛡️ SL: {fmt(sl)}원",
        f"🎯 TP1: {fmt(tp1)}원",
        f"🎯 TP2: {fmt(tp2)}원"
    ]
    if news_items:
        lines+=["","📰 최근 소식"]
        for t,l,_ in news_items[:2]:
            lines.append(f"• [{catalyst(t)}] {t}")
    else:
        lines+=["","📰 최근 주요 보도: 확인된 자료 없음"]
    if c["change"]>=15:
        lines+=["","⚠️ 단기 급등 구간: 추격 진입 주의"]
    elif c["change"]>=10:
        lines+=["","⚠️ 단기 급등: 변동성 확대 구간"]
    lines+=["",f"🔗 https://finance.naver.com/item/main.naver?code={c['code']}"]
    return "\n".join(lines)

def create_position(state,c,m):
    p=c["price"]
    state.setdefault("positions",{})[c["code"]]={
        "name":c["name"],"entry":p,"sl":p*(1-SL),
        "tp1":p*(1+TP1),"tp2":p*(1+TP2),
        "tp1_hit":False,"mode":m,"created_at":now().isoformat()
    }

def monitor(state,quotes):
    positions=state.get("positions",{})
    for code,pos in list(positions.items()):
        q=quotes.get(code)
        if not q:continue
        p=q["price"];name=pos["name"]
        if p<=pos["sl"]:
            send(f"🛑 [손절 감지]\n{name} ({code})\n현재가: {fmt(p)}원\nSL: {fmt(pos['sl'])}원")
            del positions[code]
            continue
        if p>=pos["tp2"]:
            send(f"🎯 [TP2 도달]\n{name} ({code})\n현재가: {fmt(p)}원\nTP2: {fmt(pos['tp2'])}원\n추적 종료")
            del positions[code]
            continue
        if p>=pos["tp1"] and not pos.get("tp1_hit"):
            pos["tp1_hit"]=True
            pos["sl"]=pos["entry"]
            send(f"🎯 [TP1 도달]\n{name} ({code})\n현재가: {fmt(p)}원\nTP1: {fmt(pos['tp1'])}원\n🛡️ SL → 진입가 상향")
    print("추적 포지션:",len(positions))

def scan(state,items,quotes,m):
    cand=[]
    for x in items:
        q=quotes.get(x["code"])
        if not q or q["price"]<=0:continue
        # 장중 실시간 강도 기준으로 먼저 압축
        if q["turnover"]<300_000_000 and q["change"]<1.5:continue
        cand.append({**x,**q})
    cand.sort(key=lambda z:(z["turnover"],z["change"],z["volume"]),reverse=True)
    cand=cand[:TOP_SCAN]
    print("기술분석 대상:",len(cand))

    result=[]
    for i,x in enumerate(cand,1):
        a=analyze(x,history(x["code"]))
        if a and a["score"]>=threshold(m):
            result.append(a)
        if i%20==0:print("분석",i,"/",len(cand))
    result.sort(key=lambda x:(x["score"],x["turnover"],x["change"]),reverse=True)
    return result[:3]

def main():
    m=mode()
    print("====================================")
    print(" KOREA STOCK HUNTER V6.5")
    print(" KST:",now().isoformat())
    print("====================================")
    print("MODE:",m)
    print("FORCE:",FORCE or "auto")

    state=load_state()
    items=universe_fast()
    if not items:
        send("⚠️ [국장 자동화]\n종목 목록을 가져오지 못했습니다.")
        return

    quotes=realtime(items)
    monitor(state,quotes)

    if m=="monitor":
        save_state(state)
        print("모니터 모드 종료")
        return

    if len(quotes)<50:
        msg=f"⚠️ [국장 자동화]\n실시간 데이터 부족\n조회 성공: {len(quotes)}/{len(items)}\n신규 후보 검색을 중단했습니다."
        send(msg)
        save_state(state)
        return

    result=scan(state,items,quotes,m)
    print("최종 후보:",len(result))

    if not result:
        send(f"📊 [{m.upper()}]\n현재 조건을 만족하는 신규 후보가 없습니다.")
    for c in result:
        if signal_recent(state,c["code"],m):
            print("쿨다운:",c["name"])
            continue
        ns=news(c["name"],c["code"])
        if send(message(c,m,ns)):
            mark_signal(state,c["code"],m)
            if m=="close":create_position(state,c,m)
            time.sleep(.5)

    save_state(state)
    print("완료")

if __name__=="__main__":
    main()
