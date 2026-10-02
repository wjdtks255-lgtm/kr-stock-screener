import os,json,time,re,requests,xml.etree.ElementTree as ET
from datetime import datetime,timedelta
from urllib.parse import quote

import pandas as pd
import pytz
import FinanceDataReader as fdr

# ============================================================
# KOREA STOCK HUNTER V6.4
# ============================================================

TOKEN=os.getenv("TELEGRAM_TOKEN","")
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID","")
MODE_ENV=os.getenv("MODE","").lower().strip()
FORCE=os.getenv("FORCE_SCAN","").lower().strip()

STATE_FILE="active_positions.json"
KST=pytz.timezone("Asia/Seoul")

UNIVERSE_SIZE=350
HISTORY_DAYS=90
MIN_TURNOVER=500_000_000
TOP_SIGNAL_COUNT=3
MIN_REALTIME=30

SL_PCT=.05
TP1_PCT=.03
TP2_PCT=.06

COOLDOWN_HOURS=4
TIMEOUT=7

HEADERS={
    "User-Agent":"Mozilla/5.0 Chrome/140 Safari/537.36",
    "Accept":"application/json,text/plain,*/*",
    "Referer":"https://finance.naver.com/"
}


# ============================================================
# BASIC
# ============================================================

def now():
    return datetime.now(KST)

def money(v):
    try:
        v=float(v)
        if v>=1e12:return f"{v/1e12:.2f}조원"
        if v>=1e8:return f"{v/1e8:.1f}억원"
        return f"{int(v):,}원"
    except:return "-"

def price(v):
    try:return f"{float(v):,.0f}원"
    except:return "-"

def pct(v):
    try:return f"{float(v):+.2f}%"
    except:return "-"

def clean(s):
    return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",str(s or ""))).strip()


# ============================================================
# TELEGRAM
# ============================================================

def telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("[TELEGRAM] TOKEN/CHAT_ID 없음")
        return False

    try:
        r=requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id":CHAT_ID,
                "text":msg,
                "disable_web_page_preview":True
            },
            timeout=15
        )
        print("[TELEGRAM]",r.status_code)
        return r.ok
    except Exception as e:
        print("[TELEGRAM ERROR]",e)
        return False


# ============================================================
# STATE
# ============================================================

def load_state():
    default={"positions":{},"sent_signals":{},"last_run":""}

    if not os.path.exists(STATE_FILE):
        return default

    try:
        with open(STATE_FILE,encoding="utf-8") as f:
            s=json.load(f)

        if not isinstance(s,dict):
            return default

        s.setdefault("positions",{})
        s.setdefault("sent_signals",{})
        s.setdefault("last_run","")
        return s
    except:
        return default

def save_state(s):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)

def recent_signal(state,code,mode):
    raw=state.get("sent_signals",{}).get(f"{mode}:{code}")
    if not raw:return False

    try:
        return now()-datetime.fromisoformat(raw)<timedelta(hours=COOLDOWN_HOURS)
    except:
        return False

def mark_signal(state,code,mode):
    state.setdefault("sent_signals",{})[f"{mode}:{code}"]=now().isoformat()


# ============================================================
# UNIVERSE
# ============================================================

def format_universe(df):
    if df is None or df.empty:
        return pd.DataFrame()

    if "Code" not in df.columns:
        if "Symbol" in df.columns:
            df=df.rename(columns={"Symbol":"Code"})
        else:
            return pd.DataFrame()

    df["Code"]=(
        df["Code"].astype(str)
        .str.replace(".0","",regex=False)
        .str.zfill(6)
    )

    if "Name" not in df.columns:
        df["Name"]=df["Code"]

    if "Amount" in df.columns:
        df["Amount"]=pd.to_numeric(
            df["Amount"],errors="coerce"
        ).fillna(0)
        df=df.sort_values("Amount",ascending=False)

    return df[["Code","Name"]].drop_duplicates("Code").head(UNIVERSE_SIZE)

def get_universe():
    print("KRX 종목 목록 다운로드...")

    try:
        df=format_universe(fdr.StockListing("KRX"))
        if not df.empty:
            print(f"KRX universe: {len(df)}개")
            return df
    except Exception as e:
        print("[FDR]",e)

    try:
        url="https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
        df=pd.read_html(url,header=0,encoding="cp949")[0]
        df["Code"]=df["종목코드"].astype(str).str.zfill(6)
        df["Name"]=df["회사명"]
        df=df[["Code","Name"]].drop_duplicates("Code")
        print(f"KRX fallback: {len(df)}개")
        return df.head(UNIVERSE_SIZE)
    except Exception as e:
        print("[KRX FALLBACK]",e)

    return pd.DataFrame()


# ============================================================
# REALTIME
# ============================================================

def realtime_one(code):
    url=f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"

    for _ in range(3):
        try:
            r=requests.get(url,headers=HEADERS,timeout=TIMEOUT)
            if r.status_code!=200:
                time.sleep(.4)
                continue

            ds=r.json().get("datas",[])
            if not ds:
                time.sleep(.4)
                continue

            x=ds[0]

            def n(k):
                try:return float(x.get(k))
                except:return 0

            return {
                "price":n("closePrice"),
                "change":n("fluctuationsRatio"),
                "volume":n("accumulatedTradingVolume"),
                "value":n("accumulatedTradingValue"),
                "open":n("openPrice"),
                "high":n("highPrice"),
                "low":n("lowPrice")
            }
        except Exception:
            time.sleep(.4)

    return None

def realtime(codes):
    out={}
    print(f"실시간 시세 조회: {len(codes)}개")

    for i,code in enumerate(codes,1):
        q=realtime_one(code)

        if q and q["price"]>0:
            out[code]=q

        if i%50==0:
            print(f"진행 {i}/{len(codes)} / 성공 {len(out)}")

        time.sleep(.08)

    print(f"실시간 데이터: {len(out)}/{len(codes)}개")
    return out


# ============================================================
# HISTORY
# ============================================================

def history(code):
    try:
        end=datetime.now()
        start=end-timedelta(days=HISTORY_DAYS)

        df=fdr.DataReader(
            code,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d")
        )

        if df is None or len(df)<25:
            return None

        return df
    except Exception as e:
        print("[HISTORY]",code,e)
        return None


# ============================================================
# TECHNICAL
# ============================================================

def analyze(code,name,q,df):
    try:
        close=pd.to_numeric(df["Close"],errors="coerce").dropna()
        vol=pd.to_numeric(df["Volume"],errors="coerce").reindex(close.index).fillna(0)

        if len(close)<25:return None

        p=float(q["price"])
        ch=float(q["change"])
        v=float(q["volume"])

        value=float(q["value"] or 0)
        if value<=0:value=p*v

        ma5=close.rolling(5).mean().iloc[-1]
        ma20=close.rolling(20).mean().iloc[-1]
        ma60=close.rolling(60).mean().iloc[-1]

        avgv=vol.rolling(20).mean().iloc[-1]
        vr=v/avgv if avgv>0 else 0

        h20=float(close.tail(20).max())
        l20=float(close.tail(20).min())

        pos=(p-l20)/(h20-l20) if h20>l20 else .5
        high_gap=(p/h20-1)*100 if h20 else 0

        op=float(q["open"] or p)
        intraday=(p/op-1)*100 if op else 0

        score=0
        reasons=[]
        warnings=[]

        # 추세
        if p>ma20:
            score+=2
            reasons.append("현재가 20일선 위")

        if ma5>ma20:
            score+=2
            reasons.append("5일선이 20일선 상향 정렬")

        if p>ma60:
            score+=1
            reasons.append("중기 60일선 위")

        # 거래량
        if vr>=3:
            score+=3
            reasons.append(f"거래량 평균 대비 {vr:.2f}배")
        elif vr>=2:
            score+=2
            reasons.append(f"거래량 평균 대비 {vr:.2f}배")
        elif vr>=1.3:
            score+=1
            reasons.append(f"거래량 증가 {vr:.2f}배")
        else:
            warnings.append("거래량 증가폭 제한적")

        # 상승 모멘텀
        if ch>=7:
            score+=2
            reasons.append(f"당일 강한 상승 {ch:+.2f}%")
        elif ch>=3:
            score+=1
            reasons.append(f"당일 상승 모멘텀 {ch:+.2f}%")

        # 가격 위치
        if pos>=.8:
            score+=2
            reasons.append("최근 20일 가격 상단권")
        elif pos>=.65:
            score+=1
            reasons.append("최근 가격 범위 상단권")

        # 고점
        if p>=h20:
            score+=3
            reasons.append("최근 20일 고점 돌파")
        elif high_gap>=-2:
            score+=1
            reasons.append("최근 20일 고점 근접")

        # 장중 강도
        if intraday>=3:
            score+=1
            reasons.append(f"시가 대비 {intraday:+.2f}%")

        # 거래대금
        if value>=50_000_000_000:
            score+=2
            reasons.append(f"거래대금 {money(value)}")
        elif value>=10_000_000_000:
            score+=1
            reasons.append(f"거래대금 {money(value)}")

        # 과열
        if ch>=15:
            warnings.append("당일 +15% 이상 급등")
        elif ch>=10:
            warnings.append("당일 +10% 이상 급등")
        elif ch>=7:
            warnings.append("단기 급등 변동성 주의")

        return {
            "code":code,
            "name":name,
            "price":p,
            "change":ch,
            "value":value,
            "vr":vr,
            "score":score,
            "reasons":reasons,
            "warnings":warnings
        }

    except Exception as e:
        print("[ANALYZE]",code,e)
        return None


# ============================================================
# NEWS
# ============================================================

def news(name,code):
    url=(
        "https://news.google.com/rss/search?"
        f"q={quote(f'{name} {code}')}"
        "&hl=ko&gl=KR&ceid=KR:ko"
    )

    try:
        r=requests.get(
            url,
            headers=HEADERS,
            timeout=8
        )

        if r.status_code!=200:
            return []

        root=ET.fromstring(r.content)
        result=[]

        for x in root.findall(".//item")[:3]:
            title=clean(x.findtext("title"))
            source=clean(x.findtext("source"))

            if title:
                result.append(
                    f"{title}"
                    + (f" ({source})" if source else "")
                )

        return result[:2]

    except Exception as e:
        print("[NEWS]",name,e)
        return []


# ============================================================
# MESSAGE
# ============================================================

def mode_name(mode):
    return {
        "morning":"장초 상승 후보",
        "intraday":"장중 상승 후보",
        "close":"15:20 종가 후보"
    }.get(mode,"주식 후보")

def message(c,mode):
    p=c["price"]

    sl=p*(1-SL_PCT)
    tp1=p*(1+TP1_PCT)
    tp2=p*(1+TP2_PCT)

    ns=news(c["name"],c["code"])

    m=(
        f"🚨 [{mode_name(mode)}]\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 {c['name']} ({c['code']})\n\n"
        f"💰 현재가: {price(p)}\n"
        f"📈 등락률: {pct(c['change'])}\n"
        f"🔥 거래대금: {money(c['value'])}\n"
        f"📊 거래량: {c['vr']:.2f}배\n"
        f"⭐ 기술점수: {c['score']}\n\n"
        f"🟢 진입 기준: {price(p)}\n"
        f"🛡️ SL: {price(sl)}\n"
        f"🎯 TP1: {price(tp1)}\n"
        f"🎯 TP2: {price(tp2)}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 상승 근거\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )

    for x in c["reasons"][:6]:
        m+=f"• {x}\n"

    m+="\n📰 최근 뉴스/소식\n━━━━━━━━━━━━━━━━━━\n"

    if ns:
        for x in ns:
            m+=f"• {x}\n"
    else:
        m+="• 최근 주요 뉴스/소식 확인 안 됨\n"

    if c["warnings"]:
        m+=(
            "\n⚠️ 주의사항\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )
        for x in c["warnings"][:3]:
            m+=f"• {x}\n"

    m+=(
        "\n🔗 네이버 차트\n"
        f"https://finance.naver.com/item/main.naver?code={c['code']}\n\n"
        "ℹ️ 기술적 조건과 뉴스/소식을 "
        "종합해 표시한 참고용 신호입니다."
    )

    return m


# ============================================================
# POSITION
# ============================================================

def add_position(state,c,mode):
    p=c["price"]

    state["positions"][c["code"]]={
        "code":c["code"],
        "name":c["name"],
        "direction":"LONG",
        "entry":p,
        "sl":p*(1-SL_PCT),
        "tp1":p*(1+TP1_PCT),
        "tp2":p*(1+TP2_PCT),
        "tp1_hit":False,
        "mode":mode,
        "created_at":now().isoformat()
    }

def monitor(state,quotes):
    positions=state.get("positions",{})

    if not positions:
        print("추적 포지션 없음")
        return

    print(f"기존 포지션 {len(positions)}개 모니터링")

    remove=[]

    for code,pos in list(positions.items()):
        q=quotes.get(code)

        if not q:
            continue

        p=q["price"]
        entry=pos["entry"]
        sl=pos["sl"]
        tp1=pos["tp1"]
        tp2=pos["tp2"]
        name=pos["name"]

        if p<=sl:
            telegram(
                f"🛑 [주식 SL 도달]\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 {name} ({code})\n"
                f"💰 현재가: {price(p)}\n"
                f"🛡️ SL: {price(sl)}\n"
                f"🟢 진입가: {price(entry)}\n\n"
                f"추적 종료"
            )
            remove.append(code)
            continue

        if p>=tp2:
            telegram(
                f"🎯 [주식 TP2 도달]\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 {name} ({code})\n"
                f"💰 현재가: {price(p)}\n"
                f"🎯 TP2: {price(tp2)}\n"
                f"🟢 진입가: {price(entry)}\n\n"
                f"추적 종료"
            )
            remove.append(code)
            continue

        if p>=tp1 and not pos.get("tp1_hit"):
            pos["tp1_hit"]=True
            pos["sl"]=entry

            telegram(
                f"🎯 [주식 TP1 도달]\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 {name} ({code})\n"
                f"💰 현재가: {price(p)}\n"
                f"🎯 TP1: {price(tp1)}\n\n"
                f"🛡️ SL → 진입가 {price(entry)}\n"
                f"손익분기점 보호 모드"
            )

    for code in remove:
        positions.pop(code,None)


# ============================================================
# SCAN
# ============================================================

def threshold(mode):
    return {
        "morning":7,
        "intraday":8,
        "close":9
    }.get(mode,999)

def scan(state,universe,quotes,mode):
    results=[]
    limit=threshold(mode)

    print(f"{mode.upper()} SCAN / threshold {limit}")

    for _,row in universe.iterrows():
        code=str(row["Code"]).zfill(6)
        name=str(row["Name"])

        q=quotes.get(code)
        if not q or q["price"]<=0:
            continue

        if q["value"]<=0:
            q["value"]=q["price"]*q["volume"]

        if q["value"]<MIN_TURNOVER:
            continue

        df=history(code)
        if df is None:
            continue

        c=analyze(code,name,q,df)

        if c and c["score"]>=limit:
            results.append(c)

    results.sort(
        key=lambda x:(
            x["score"],
            x["vr"],
            x["value"],
            x["change"]
        ),
        reverse=True
    )

    return results[:TOP_SIGNAL_COUNT]


# ============================================================
# MODE
# ============================================================

def get_mode():
    if MODE_ENV in {
        "morning","intraday","close","monitor"
    } and FORCE in {"","true","1","yes"}:
        return MODE_ENV

    t=now().hour*60+now().minute

    if 540<=t<=570:
        return "morning"

    if 571<=t<=905:
        return "intraday"

    if 910<=t<=920:
        return "close"

    return "monitor"


# ============================================================
# MAIN
# ============================================================

def main():
    print("====================================")
    print(" KOREA STOCK HUNTER V6.4")
    print(f" KST: {now().isoformat()}")
    print("====================================")

    mode=get_mode()

    print("MODE:",mode)
    print("FORCE_SCAN:",FORCE or "-")

    state=load_state()
    state["last_run"]=now().isoformat()

    universe=get_universe()

    if universe.empty:
        telegram(
            "⚠️ [국장 자동화]\n"
            "KRX 종목 목록을 가져오지 못했습니다."
        )
        save_state(state)
        return

    codes=universe["Code"].astype(str).str.zfill(6).tolist()
    quotes=realtime(codes)

    # 기존 포지션은 검색 실패와 관계없이 확인
    monitor(state,quotes)

    if len(quotes)<MIN_REALTIME:
        telegram(
            "⚠️ [국장 자동화]\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"실시간 데이터 부족\n"
            f"조회 성공: {len(quotes)}/{len(codes)}\n\n"
            "신규 후보 검색을 중단했습니다.\n"
            "기존 포지션만 확인했습니다."
        )
        save_state(state)
        return

    if mode not in {"morning","intraday","close"}:
        print("스크리닝 시간이 아니므로 모니터링만 실행")
        save_state(state)
        return

    candidates=scan(
        state,
        universe,
        quotes,
        mode
    )

    print(f"{mode.upper()} candidates: {len(candidates)}")

    if not candidates:
        telegram(
            f"🔎 [{mode_name(mode)}]\n\n"
            "현재 조건을 만족하는 후보가 없습니다."
        )
        save_state(state)
        return

    sent=0

    # 종목별 개별 Telegram
    for c in candidates:
        code=c["code"]

        if recent_signal(state,code,mode):
            print("[DUPLICATE]",c["name"],code)
            continue

        msg=message(c,mode)

        if telegram(msg):
            mark_signal(state,code,mode)
            sent+=1

            # 종가 후보만 추적
            if mode=="close":
                add_position(state,c,mode)

        time.sleep(1)

    print(f"개별 알림: {sent}개")
    print(f"추적 포지션: {len(state['positions'])}개")

    save_state(state)

    print("====================================")
    print("RUN COMPLETE")
    print("====================================")


if __name__=="__main__":
    main()
