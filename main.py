import os,re,json,time,requests
from datetime import datetime,timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
import pandas as pd

TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID=(os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
MODE=os.getenv("MODE","").strip().lower()
FORCE=os.getenv("FORCE_SCAN","").strip().lower()

STATE_FILE="active_positions.json"
KST=ZoneInfo("Asia/Seoul")
TIMEOUT=8
UNIVERSE_SIZE=350
BATCH_SIZE=40
ANALYZE_MAX=80
TOP_COUNT=3
COOLDOWN_HOURS=4
SL_PCT=.05
TP1_PCT=.03
TP2_PCT=.06

S=requests.Session()
S.headers.update({
    "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept":"*/*"
})

def now():
    return datetime.now(KST)

def num(v,default=0):
    try:return float(str(v).replace(",","").replace("+",""))
    except:return default

def won(v):
    return f"{num(v):,.0f}"

def send(msg):
    if not TOKEN or not CHAT_ID:
        print("[TELEGRAM] 환경변수 없음")
        print("TOKEN:",bool(TOKEN),"CHAT_ID:",bool(CHAT_ID))
        return False
    try:
        r=S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"disable_web_page_preview":True},
            timeout=10
        )
        print("[TELEGRAM]",r.status_code)
        if not r.ok:print(r.text[:500])
        return r.ok
    except Exception as e:
        print("[TELEGRAM ERROR]",e)
        return False

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:
            x=json.load(f)
            x.setdefault("positions",{})
            x.setdefault("signals",{})
            return x
    except:
        return {"positions":{},"signals":{}}

def save_state(s):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)

def get_mode():
    if MODE in {"morning","intraday","close","monitor"} and FORCE in {"true","1","yes"}:
        return MODE
    m=now().hour*60+now().minute
    if 540<=m<=570:return "morning"
    if 571<=m<=905:return "intraday"
    if 910<=m<=920:return "close"
    return "monitor"

def parse_naver_list(html):
    out=[]
    seen=set()
    pat=r'/item/main\.naver\?code=(\d{6})[^>]*>(.*?)</a>'
    for code,name in re.findall(pat,html,re.S|re.I):
        name=re.sub(r"<.*?>","",name)
        name=name.replace("&amp;","&").strip()
        if code not in seen and name and name!="nan":
            seen.add(code)
            out.append({"code":code,"name":name})
    return out

def get_universe():
    print("KRX 종목 목록 수집...")
    out=[]
    for market in (0,1):
        for page in range(1,9):
            try:
                url=f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={market}&page={page}"
                r=S.get(url,timeout=TIMEOUT)
                r.encoding="euc-kr"
                out.extend(parse_naver_list(r.text))
            except Exception as e:
                print("[NAVER LIST]",market,page,e)
            if len(out)>=UNIVERSE_SIZE*2:break

    unique=[]
    seen=set()
    for x in out:
        if x["code"] not in seen:
            seen.add(x["code"])
            unique.append(x])

    if len(unique)>=100:
        print("Naver 종목 목록:",len(unique))
        return unique[:UNIVERSE_SIZE]

    print("Naver 목록 부족 → KRX fallback")
    try:
        url="https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
        r=S.get(url,timeout=15)
        r.encoding="euc-kr"
        pairs=re.findall(
            r'<td[^>]*>\s*(.*?)\s*</td>.*?<td[^>]*>\s*(\d{6})\s*</td>',
            r.text,re.S|re.I
        )
        for name,code in pairs:
            name=re.sub(r"<.*?>","",name).strip()
            if code not in seen and name:
                seen.add(code)
                unique.append({"code":code,"name":name})
    except Exception as e:
        print("[KRX]",e)

    print("최종 종목:",len(unique))
    return unique[:UNIVERSE_SIZE]

def get_realtime(items):
    result={}
    print("실시간 배치 조회:",len(items),"개")

    for start in range(0,len(items),BATCH_SIZE):
        part=items[start:start+BATCH_SIZE]
        query="|".join("SERVICE_ITEM:"+x["code"] for x in part)

        try:
            r=S.get(
                "https://polling.finance.naver.com/api/realtime",
                params={"query":query},
                timeout=TIMEOUT
            )
            data=r.json()

            for area in data.get("areas",[]):
                for d in area.get("datas",[]):
                    code=str(d.get("cd","")).zfill(6)
                    price=num(d.get("nv"))

                    if not code or not price:
                        continue

                    result[code]={
                        "price":price,
                        "change":num(d.get("cr")),
                        "diff":num(d.get("cv")),
                        "prev":num(d.get("sv")),
                        "open":num(d.get("ov")),
                        "high":num(d.get("hv")),
                        "low":num(d.get("lv")),
                        "volume":num(d.get("aq")),
                        "turnover":num(d.get("aa"))
                    }

        except Exception as e:
            print("[REALTIME]",start,e)

        time.sleep(.15)

    print("실시간 데이터:",len(result),"/",len(items))
    return result

def get_history(code):
    end=now().strftime("%Y%m%d")
    start=(now()-timedelta(days=140)).strftime("%Y%m%d")
    url=f"https://api.stock.naver.com/chart/domestic/item/{code}/"

    try:
        r=S.get(
            url,
            params={
                "periodType":"dayCandle",
                "startDateTime":start,
                "endDateTime":end
            },
            timeout=TIMEOUT
        )
        j=r.json()
        rows=j.get("priceInfo",[])
        if not rows:return None

        df=pd.DataFrame(rows)

        for c in ["closePrice","openPrice","highPrice","lowPrice","accumulatedTradingVolume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")

        return df.dropna(subset=["closePrice","accumulatedTradingVolume"])

    except Exception:
        return None

def analyze(item,q,df,mode):
    if df is None or len(df)<30:return None

    p=q["price"]
    close=df["closePrice"]
    vol=df["accumulatedTradingVolume"]

    ma5=close.rolling(5).mean().iloc[-1]
    ma20=close.rolling(20).mean().iloc[-1]
    ma60=close.rolling(60).mean().iloc[-1]
    avgvol=vol.rolling(20).mean().iloc[-1]

    vr=q["volume"]/avgvol if avgvol else 0
    high20=close.tail(20).max()
    low20=close.tail(20).min()
    pos=(p-low20)/(high20-low20) if high20>low20 else .5
    intraday=(p/q["open"]-1)*100 if q["open"] else 0

    score=0
    reasons=[]

    if p>ma20:
        score+=2
        reasons.append("20일선 위")
    if ma5>ma20:
        score+=2
        reasons.append("단기 상승 배열")
    if p>ma60:
        score+=1
        reasons.append("60일선 위")

    if vr>=3:
        score+=3
        reasons.append(f"거래량 {vr:.1f}배")
    elif vr>=2:
        score+=2
        reasons.append(f"거래량 {vr:.1f}배")
    elif vr>=1.3:
        score+=1
        reasons.append(f"거래량 {vr:.1f}배")

    if q["change"]>=7:
        score+=2
        reasons.append(f"당일 {q['change']:+.1f}%")
    elif q["change"]>=3:
        score+=1
        reasons.append(f"당일 {q['change']:+.1f}%")

    if pos>=.8:
        score+=2
        reasons.append("최근 고점권")
    elif pos>=.65:
        score+=1
        reasons.append("고점권 접근")

    if p>=high20:
        score+=3
        reasons.append("20일 고점 돌파")
    elif p>=high20*.98:
        score+=1
        reasons.append("20일 고점 접근")

    if intraday>=3:
        score+=1
        reasons.append("시가 대비 강세")

    if q["turnover"]>=50_000_000_000:
        score+=2
        reasons.append("거래대금 500억+")
    elif q["turnover"]>=10_000_000_000:
        score+=1
        reasons.append("거래대금 100억+")

    limit={"morning":7,"intraday":8,"close":9}.get(mode,99)
    if score<limit:return None

    return {
        **item,
        **q,
        "score":score,
        "vr":vr,
        "ma20":ma20,
        "ma60":ma60,
        "pos":pos,
        "intraday":intraday,
        "reasons":reasons
    }

def get_news(name,code):
    try:
        q=quote(f'"{name}" "{code}"')
        r=S.get(
            "https://news.google.com/rss/search",
            params={"q":q,"hl":"ko","gl":"KR","ceid":"KR:ko"},
            timeout=8
        )

        import xml.etree.ElementTree as ET
        root=ET.fromstring(r.text)
        result=[]

        for item in root.findall(".//item"):
            title=item.findtext("title","").strip()
            link=item.findtext("link","").strip()

            if title:
                result.append((title,link))

            if len(result)>=2:break

        return result

    except Exception:
        return []

def news_type(title):
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

    low=title.lower()

    for key,label in keys:
        if key.lower() in low:
            return label

    return "관련 소식"

def recent_signal(state,code,mode):
    key=f"{mode}:{code}"
    t=state.get("signals",{}).get(key)

    if not t:return False

    try:
        return now()-datetime.fromisoformat(t)<timedelta(hours=COOLDOWN_HOURS)
    except:
        return False

def mark_signal(state,code,mode):
    state.setdefault("signals",{})[f"{mode}:{code}"]=now().isoformat()

def build_message(c,mode,news):
    p=c["price"]
    sl=p*(1-SL_PCT)
    tp1=p*(1+TP1_PCT)
    tp2=p*(1+TP2_PCT)

    title={
        "morning":"🌅 [장초 상승 후보]",
        "intraday":"🚀 [장중 상승 후보]",
        "close":"🌙 [15:20 종가 후보]"
    }.get(mode,"📊 [상승 후보]")

    turnover=c["turnover"]/100_000_000
    reason=" · ".join(c["reasons"][:5])

    lines=[
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"📌 {c['name']} ({c['code']})",
        f"💰 현재가: {won(p)}원",
        f"📈 등락률: {c['change']:+.2f}%",
        f"💹 거래대금: {turnover:,.0f}억원",
        f"📊 거래량: {c['vr']:.2f}배",
        f"⭐ 기술점수: {c['score']}점",
        "",
        "🔎 상승 근거",
        f"• {reason}",
        "",
        f"🟢 진입: {won(p)}원",
        f"🛡️ SL: {won(sl)}원",
        f"🎯 TP1: {won(tp1)}원",
        f"🎯 TP2: {won(tp2)}원"
    ]

    if news:
        lines+=["","📰 최근 소식"]
        for title,link in news:
            lines.append(f"• [{news_type(title)}] {title}")

    else:
        lines+=["","📰 최근 주요 보도: 확인된 자료 없음"]

    if c["change"]>=15:
        lines+=["","⚠️ 단기 급등 구간 — 추격 진입 주의"]
    elif c["change"]>=10:
        lines+=["","⚠️ 변동성 확대 구간"]

    lines+=["",f"🔗 https://finance.naver.com/item/main.naver?code={c['code']}"]

    return "\n".join(lines)

def create_position(state,c,mode):
    p=c["price"]

    state.setdefault("positions",{})[c["code"]]={
        "name":c["name"],
        "entry":p,
        "sl":p*(1-SL_PCT),
        "tp1":p*(1+TP1_PCT),
        "tp2":p*(1+TP2_PCT),
        "tp1_hit":False,
        "mode":mode,
        "created_at":now().isoformat()
    }

def monitor_positions(state,quotes):
    positions=state.get("positions",{})

    if not positions:
        print("추적 포지션 없음")
        return

    for code,pos in list(positions.items()):
        q=quotes.get(code)
        if not q:continue

        price=q["price"]
        name=pos["name"]

        if price<=pos["sl"]:
            send(
                f"🛑 [손절 감지]\n"
                f"{name} ({code})\n"
                f"현재가: {won(price)}원\n"
                f"SL: {won(pos['sl'])}원\n"
                f"추적 종료"
            )
            del positions[code]
            continue

        if price>=pos["tp2"]:
            send(
                f"🎯 [TP2 도달]\n"
                f"{name} ({code})\n"
                f"현재가: {won(price)}원\n"
                f"TP2: {won(pos['tp2'])}원\n"
                f"추적 종료"
            )
            del positions[code]
            continue

        if price>=pos["tp1"] and not pos.get("tp1_hit"):
            pos["tp1_hit"]=True
            pos["sl"]=pos["entry"]

            send(
                f"🎯 [TP1 도달]\n"
                f"{name} ({code})\n"
                f"현재가: {won(price)}원\n"
                f"TP1: {won(pos['tp1'])}원\n"
                f"🛡️ SL → 진입가"
            )

    print("추적 포지션:",len(positions))

def scan(state,items,quotes,mode):
    candidates=[]

    for item in items:
        q=quotes.get(item["code"])
        if not q:continue

        # 장중 후보 1차 압축
        if q["turnover"]<300_000_000 and q["change"]<1.5:
            continue

        candidates.append((item,q))

    candidates.sort(
        key=lambda x:(x[1]["turnover"],x[1]["change"],x[1]["volume"]),
        reverse=True
    )

    candidates=candidates[:ANALYZE_MAX]

    print("기술분석 대상:",len(candidates))

    result=[]

    for i,(item,q) in enumerate(candidates,1):
        df=get_history(item["code"])
        a=analyze(item,q,df,mode)

        if a:
            result.append(a)

        if i%20==0:
            print("분석:",i,"/",len(candidates))

    result.sort(
        key=lambda x:(x["score"],x["turnover"],x["change"]),
        reverse=True
    )

    return result[:TOP_COUNT]

def main():
    mode=get_mode()

    print("====================================")
    print(" KOREA STOCK HUNTER V6.6")
    print(" KST:",now().isoformat())
    print("====================================")
    print("MODE:",mode)
    print("FORCE:",FORCE or "auto")
    print("TOKEN:",bool(TOKEN))
    print("CHAT_ID:",bool(CHAT_ID))

    state=load_state()

    items=get_universe()

    if not items:
        send(
            "🚨 [국장 자동화 오류]\n"
            "종목 목록을 가져오지 못했습니다.\n"
            "신규 후보 검색을 중단했습니다."
        )
        return

    quotes=get_realtime(items)

    monitor_positions(state,quotes)

    if mode=="monitor":
        save_state(state)
        print("모니터 모드 종료")
        return

    if len(quotes)<50:
        send(
            f"⚠️ [국장 자동화]\n"
            f"실시간 데이터 부족\n"
            f"조회 성공: {len(quotes)}/{len(items)}\n"
            f"신규 후보 검색을 중단했습니다."
        )
        save_state(state)
        return

    result=scan(state,items,quotes,mode)

    print("최종 후보:",len(result))

    if not result:
        send(
            f"📊 [{mode.upper()}]\n"
            f"현재 조건을 만족하는 신규 후보가 없습니다."
        )
    else:
        for c in result:
            if recent_signal(state,c["code"],mode):
                print("쿨다운:",c["name"])
                continue

            news=get_news(c["name"],c["code"])

            if send(build_message(c,mode,news)):
                mark_signal(state,c["code"],mode)

                if mode=="close":
                    create_position(state,c,mode)

                time.sleep(.5)

    save_state(state)
    print("====================================")
    print("SCAN COMPLETE")
    print("====================================")

if __name__=="__main__":
    main()
