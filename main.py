
import os,json,time,requests
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import FinanceDataReader as fdr

TZ=ZoneInfo("Asia/Seoul")
TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT=(os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
MAX_POS=min(8,max(1,int(os.getenv("STOCK_MAX_POSITIONS","8"))))
MAX_SCAN=min(40,max(1,int(os.getenv("KIS_MAX_SYMBOLS","40"))))
MIN_PRICE=int(os.getenv("STOCK_MIN_PRICE_KRW","1000"))
MAX_PRICE=int(os.getenv("STOCK_MAX_PRICE_KRW","50000"))
MIN_GAIN=float(os.getenv("STOCK_MIN_OPEN_GAIN_PCT","0.40"))
MIN_VOL=float(os.getenv("STOCK_MIN_VOLUME_SURGE","1.50"))
STATE="stock_intraday_state.json"
NAVER="https://polling.finance.naver.com/api/realtime/domestic/stock/"

def now():
    return datetime.now(TZ)

def send(text):
    if not TOKEN or not CHAT:
        raise RuntimeError("TELEGRAM_TOKEN과 CHAT_ID Secrets를 확인하세요.")
    r=requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id":CHAT,"text":text,"disable_web_page_preview":True},
        timeout=15)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram API 전송 실패")

def number(v):
    try:
        return float(str(v).replace(",","").replace("+","").strip())
    except (TypeError,ValueError):
        return 0.0

def load_state():
    try:
        with open(STATE,encoding="utf-8") as f:
            s=json.load(f)
        s.setdefault("positions",{})
        s.setdefault("sent_today",{})
        return s
    except (OSError,ValueError):
        return {"positions":{},"sent_today":{}}

def save_state(s):
    with open(STATE,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)

def get_quote(code):
    r=requests.get(
        NAVER+code,
        headers={"User-Agent":"Mozilla/5.0"},
        timeout=12)
    r.raise_for_status()
    data=r.json().get("datas",[])
    if not data:
        return None
    x=data[0]
    price=number(x.get("closePrice"))
    op=number(x.get("openPrice"))
    volume=number(x.get("accumulatedTradingVolume"))
    if price<=0 or op<=0:
        return None
    return {
        "price":price,"open":op,"volume":volume,
        "gain":(price/op-1)*100,
        "name":x.get("stockName",code)
    }

def get_universe():
    df=fdr.StockListing("KRX")
    code_col=next((c for c in ("Code","Symbol") if c in df.columns),None)
    name_col=next((c for c in ("Name",) if c in df.columns),None)
    if not code_col or not name_col:
        raise RuntimeError("KRX 종목 목록 형식을 확인하세요.")
    df["code"]=df[code_col].astype(str).str.zfill(6)
    df["name"]=df[name_col].astype(str)
    df=df[df["code"].str.fullmatch(r"\d{6}")]
    df=df[~df["name"].str.contains(
        "ETF|ETN|스팩|리츠|인버스|레버리지",
        case=False,na=False)]
    rank=next((c for c in ("Marcap","Amount","MarketCap") if c in df.columns),None)
    if rank:
        df["_rank"]=pd.to_numeric(df[rank],errors="coerce").fillna(0)
        df=df.sort_values("_rank",ascending=False)
    return df.head(MAX_SCAN)[["code","name"]].to_dict("records")

def get_history(code):
    start=(now().date()-timedelta(days=100)).isoformat()
    df=fdr.DataReader(code,start)
    if df is None or len(df)<25:
        return None
    close=pd.to_numeric(df["Close"],errors="coerce").dropna()
    volume=pd.to_numeric(df["Volume"],errors="coerce").fillna(0)
    if len(close)<25:
        return None
    ema5=float(close.ewm(span=5,adjust=False).mean().iloc[-1])
    ema20=float(close.ewm(span=20,adjust=False).mean().iloc[-1])
    avgvol=float(volume.tail(20).mean())
    return {"ema5":ema5,"ema20":ema20,"avgvol":avgvol}

def create_signal(code,name,q,h):
    price=q["price"]
    if not MIN_PRICE<=price<=MAX_PRICE:
        return None
    if q["gain"]<MIN_GAIN:
        return None
    if h["ema5"]<=h["ema20"] or price<=h["ema5"]:
        return None
    if h["avgvol"]<=0:
        return None

    minute=now().hour*60+now().minute
    elapsed=max(0.05,min(1.0,(minute-540)/390))
    expected=max(1.0,h["avgvol"]*elapsed)
    ratio=q["volume"]/expected
    if ratio<MIN_VOL:
        return None

    entry=price
    return {
        "code":code,"name":name,"entry":entry,
        "tp1":entry*1.01,"tp2":entry*1.02,
        "tp3":entry*1.03,"sl":entry*0.988,
        "initial_sl":entry*0.988,
        "tp1_done":False,"tp2_done":False,
        "created":now().isoformat(),
        "volume_ratio":ratio
    }

def won(v):
    return f"{v:,.0f}원"

def change(entry,price):
    return (price/entry-1)*100

def send_entry(p,slot):
    e=p["entry"]
    send(
        "🚨 국내 주식 신규 시그널\n"
        "━━━━━━━━━━━━━━\n"
        f"종목: {p['name']} ({p['code']})\n"
        f"가상 슬롯: {slot}/{MAX_POS}\n"
        f"진입가: {won(e)} (0.00%)\n"
        f"TP1: {won(p['tp1'])} (+{change(e,p['tp1']):.2f}%)\n"
        f"TP2: {won(p['tp2'])} (+{change(e,p['tp2']):.2f}%)\n"
        f"TP3: {won(p['tp3'])} (+{change(e,p['tp3']):.2f}%)\n"
        f"손절: {won(p['initial_sl'])} ({change(e,p['initial_sl']):.2f}%)\n"
        "━━━━━━━━━━━━━━\n"
        "📊 시그널 근거\n"
        "• 시가 대비 상승률 조건 충족\n"
        "• 일봉 EMA5 > EMA20\n"
        "• 현재가가 EMA5 상단\n"
        f"• 시간 보정 거래량 비율: {p['volume_ratio']:.2f}배\n"
        f"차트: https://finance.naver.com/item/main.naver?code={p['code']}\n"
        "⚠️ 공개 데이터 기반 가상 시그널이며 실제 주문이 아닙니다."
    )

def close_signal(s,code,reason,price):
    p=s["positions"].pop(code)
    e=p["entry"]
    send(
        f"🏁 시그널 종료 | {p['name']} ({code})\n"
        f"종료 사유: {reason}\n"
        f"확인 가격: {won(price)}\n"
        f"진입 대비 변화: {change(e,price):+.2f}%\n"
        f"남은 슬롯: {MAX_POS-len(s['positions'])}/{MAX_POS}\n"
        "※ 실제 체결 수익이 아닌 조회 가격 기준입니다."
    )
    send("🔓 슬롯 해제: 신규 종목 검색을 재개합니다.")

def monitor(s,code,q):
    p=s["positions"].get(code)
    if not p:
        return
    price=q["price"]
    e=p["entry"]

    # TP3가 한 번의 조회에서 확인된 경우 중간 목표도 기록
    if price>=p["tp3"]:
        if not p["tp1_done"]:
            p["tp1_done"]=True
            send(f"🎯 TP1 도달 확인 | {p['name']} ({code})\n가격: {won(price)}")
        if not p["tp2_done"]:
            p["tp2_done"]=True
            send(f"🎯 TP2 도달 확인 | {p['name']} ({code})\n가격: {won(price)}")
        close_signal(s,code,"TP3 최종 목표 도달",price)
        return

    if price<=p["sl"]:
        close_signal(s,code,"손절 또는 가상 추적 손절 도달",price)
        return

    if price>=p["tp2"] and not p["tp2_done"]:
        p["tp2_done"]=True
        p["sl"]=p["tp1"]
        send(
            f"🎯 TP2 도달 | {p['name']} ({code})\n"
            f"현재가: {won(price)}\n"
            f"가상 손절선을 TP1로 상향: {won(p['sl'])}\n"
            f"진입 대비: {change(e,price):+.2f}%"
        )

    if price>=p["tp1"] and not p["tp1_done"]:
        p["tp1_done"]=True
        p["sl"]=e
        send(
            f"🎯 TP1 도달 | {p['name']} ({code})\n"
            f"현재가: {won(price)}\n"
            f"가상 손절선을 진입가로 상향: {won(e)}\n"
            f"진입 대비: {change(e,price):+.2f}%"
        )

def main():
    if not TOKEN or not CHAT:
        raise RuntimeError("TELEGRAM_TOKEN과 CHAT_ID Secrets를 설정하세요.")

    t=now()
    print("KST:",t.isoformat())
    if t.weekday()>=5 or not (9*60<=t.hour*60+t.minute<=15*60+30):
        print("정규장 시간 외: 스캔하지 않습니다.")
        return

    s=load_state()
    today=t.date().isoformat()
    if s.get("day")!=today:
        s["day"]=today
        s["sent_today"]={}
        s["full_notice_day"]=""

    universe=get_universe()
    names={x["code"]:x["name"] for x in universe}
    for code,p in s["positions"].items():
        names[code]=p["name"]

    # 기존 포지션을 먼저 확인해 신규 검색보다 우선 추적
    codes=list(s["positions"].keys())
    codes += [c for c in names if c not in s["positions"]]

    for code in codes:
        try:
            q=get_quote(code)
            if not q:
                continue

            if code in s["positions"]:
                monitor(s,code,q)
                continue

            if len(s["positions"])>=MAX_POS:
                continue
            if code in s["sent_today"]:
                continue

            h=get_history(code)
            if not h:
                continue
            p=create_signal(code,names[code],q,h)
            if not p:
                continue

            s["positions"][code]=p
            s["sent_today"][code]=t.isoformat()
            slot=list(s["positions"]).index(code)+1
            send_entry(p,slot)
            time.sleep(0.2)

        except Exception as e:
            print(f"종목 처리 오류 {code}: {type(e).__name__}: {e}")

    if len(s["positions"])>=MAX_POS and s.get("full_notice_day")!=today:
        send(f"⏸️ 슬롯 {MAX_POS}개 모두 사용 중입니다.\n기존 시그널만 추적합니다.")
        s["full_notice_day"]=today

    s["last_run"]=now().isoformat()
    save_state(s)
    print("활성 시그널:",len(s["positions"]))

if __name__=="__main__":
    main()
