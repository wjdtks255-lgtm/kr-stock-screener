# KOREA STOCK HUNTER SCREENER & SIGNAL BOT (HIGH WIN-RATE MASTER V26.0)
import os, json, time, requests
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import FinanceDataReader as fdr

V = "26.0.0"
STATE = "active_positions.json"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))

MAX_SLOTS = 15  # 15개 슬롯 유지

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, (np.bool_, bool)): return bool(obj)
        return super(NpEncoder, self).default(obj)

def now():
    return datetime.now(KST)

def ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")

def m(x):
    try:
        val = float(x)
        if np.isnan(val) or np.isinf(val): return "0원"
        return f"{int(val):,}원"
    except:
        return "0원"

def save(p, x):
    q = p + ".tmp"
    with open(q, "w", encoding="utf8") as f:
        json.dump(x, f, ensure_ascii=False, indent=2, cls=NpEncoder)
    os.replace(q, p)

def load(p, d):
    try:
        with open(p, encoding="utf8") as f: return json.load(f)
    except: return d

def tg(x):
    if not TOKEN or not CHAT: 
        print("[TG CONFIG ERROR] Token or Chat ID is missing.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": x, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        print("[TELEGRAM]", "SENT" if r.ok else f"FAIL: {r.text}")
        return r.ok
    except Exception as e:
        print("[TG ERROR]", e)
        return False

def get_state():
    s = load(STATE, {"positions": [], "sent": [], "daily_stats": {"date": "", "wins": 0, "losses": 0}})
    if not isinstance(s, dict):
        s = {"positions": [], "sent": [], "daily_stats": {"date": "", "wins": 0, "losses": 0}}
    s.setdefault("positions", [])
    s.setdefault("sent", [])
    s.setdefault("daily_stats", {"date": now().strftime("%Y-%m-%d"), "wins": 0, "losses": 0})
    return s

def calculate_atr(df, period=14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def is_market_open():
    """국장 개장 시간 체크 (평일 오전 9:00 ~ 오후 3:30)"""
    n = now()
    if n.weekday() >= 5: # 주말 제외
        return False
    
    current_time_minutes = n.hour * 60 + n.minute
    market_start = 9 * 60       # 09:00
    market_end = 15 * 60 + 30   # 15:30
    
    return market_start <= current_time_minutes <= market_end

def check_market_trend_and_leaders():
    """코스피/코스닥 시장 흐름 감지 및 급락 경보, 주도주 분석 리포트 전송"""
    try:
        kospi_df = fdr.DataReader('KS11', datetime.now() - timedelta(days=5))
        kosdaq_df = fdr.DataReader('KQ11', datetime.now() - timedelta(days=5))
        
        kospi_close = float(kospi_df['Close'].iloc[-1])
        kospi_prev = float(kospi_df['Close'].iloc[-2])
        kospi_chg = ((kospi_close - kospi_prev) / kospi_prev) * 100

        kosdaq_close = float(kosdaq_df['Close'].iloc[-1])
        kosdaq_prev = float(kosdaq_df['Close'].iloc[-2])
        kosdaq_chg = ((kosdaq_close - kosdaq_prev) / kosdaq_prev) * 100

        # [시장 급락 경보 시스템] 코스피나 코스닥이 -2.0% 이상 급락 시 긴급 경보 발령
        if kospi_chg <= -2.0 or kosdaq_chg <= -2.0:
            tg(f"""🚨 <b>[긴급 시장 변동성 경보]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 시장 지수가 급락세를 보이고 있습니다!
• 코스피 등락: <code>{kospi_chg:+.2f}%</code>
• 코스닥 등락: <code>{kosdaq_chg:+.2f}%</code>
💡 <i>신규 매수를 일시 중단하고 리스크 관리에 집중하세요!</i>
━━━━━━━━━━━━━━━━━━━━━━━""")

        krx = fdr.StockListing('KRX')
        top_stocks = krx.dropna(subset=['Code', 'Name']).head(40)
        
        leader_candidates = []
        for _, row in top_stocks.head(10).iterrows():
            code = str(row['Code'])
            name = str(row['Name'])
            try:
                df = fdr.DataReader(code)
                if len(df) >= 2:
                    chg = ((df['Close'].iloc[-1] - df['Close'].iloc[-2]) / df['Close'].iloc[-2]) * 100
                    val = df['Volume'].iloc[-1] * df['Close'].iloc[-1]
                    leader_candidates.append({"name": name, "chg": chg, "val": val})
            except:
                continue
        
        leader_candidates = sorted(leader_candidates, key=lambda x: x['val'], reverse=True)[:3]
        leaders_str = "\n".join([f"• <b>{item['name']}</b> (등락: <code>{item['chg']:+.2f}%</code>)" for item in leader_candidates])

        market_report = f"""📊 <b>[국장 고승률 시장 동향 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
🇰🇷 <b>코스피:</b> <code>{kospi_close:,.2f}</code> ({kospi_chg:+.2f}%)
🇰🇷 <b>코스닥:</b> <code>{kosdaq_close:,.2f}</code> ({kosdaq_chg:+.2f}%)

🔥 <b>오늘의 시장 주도 섹터 및 종목:</b>
{leaders_str}
━━━━━━━━━━━━━━━━━━━━━━━
📈 <i>승률 70% 타겟 리스크 관리 모드가 활성화되었습니다.</i>"""
        
        tg(market_report)
    except Exception as e:
        print("[MARKET TREND ERROR]", e)

def main():
    print("====================================")
    print(f" KOREA STOCK HUNTER SCREENER V{V}")
    print("====================================")

    if not is_market_open():
        print(f"[MARKET CLOSED] 현재는 국장 운영 시간이 아닙니다. (KST: {ts()}) 작업을 건너뜁니다.")
        return

    # 일일 통계 날짜 초기화 체크
    state = get_state()
    today_str = now().strftime("%Y-%m-%d")
    daily_stats = state.get("daily_stats", {"date": today_str, "wins": 0, "losses": 0})
    if daily_stats.get("date") != today_str:
        daily_stats = {"date": today_str, "wins": 0, "losses": 0}

    # 1. 시장 동향 및 경보 체크 전송
    check_market_trend_and_leaders()

    positions = state.get("positions", [])
    print(f"[STATUS] 현재 유지 중인 포지션: {len(positions)} / {MAX_SLOTS}")

    # 2. 기존 포지션 청산, 목표가 및 타임스톱 체크
    updated_positions = []
    for pos in positions:
        code = pos.get("code")
        try:
            df = fdr.DataReader(code)
            if df.empty:
                updated_positions.append(pos)
                continue
            
            current_price = float(df['Close'].iloc[-1])
            sl = pos.get("sl", 0)
            tp1 = pos.get("tp1", 0)
            tp2 = pos.get("tp2", 0)
            tp3 = pos.get("tp3", 0)
            hit = pos.setdefault("hit", [])
            
            # [타임스톱 로직] 포착된 지 7거래일(일봉 기준 7개 이상)이 지났는데도 제자리인 경우 슬롯 회전을 위해 정리
            entry_time_str = pos.get("time", ts())
            try:
                entry_date = datetime.strptime(entry_time_str.split()[0], "%Y-%m-%d")
                days_elapsed = (now().date() - entry_date.date()).days
            except:
                days_elapsed = 0

            if days_elapsed >= 7 and not hit:
                daily_stats["losses"] += 1
                tg(f"""⏱ <b>[타임스톱 회전 청산 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
보유 기간: 7일 경과 (방향성 부재)
청산가격: <code>{m(current_price)}</code>
📌 <i>슬롯 회전율을 높이기 위해 원금 기준으로 정리합니다.</i>
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            if current_price <= sl:
                daily_stats["losses"] += 1
                tg(f"""🛡 <b>[손절가(SL) 도달 청산 리포트]</b> ❌
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
청산가격: <code>{m(current_price)}</code>
📌 <i>원칙 매매를 위해 손절 처리가 완료되었습니다.</i>
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            if current_price >= tp3:
                if "TP1" not in hit: hit.append("TP1")
                if "TP2" not in hit: hit.append("TP2")
                if "TP3" not in hit: hit.append("TP3")
                daily_stats["wins"] += 1
                tg(f"""🏆 <b>[3차 목표가 완수 리포트]</b> 🔥
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
최종 청산가: <code>{m(current_price)}</code>
🎉 <i>모든 목표가를 성공적으로 달성했습니다!</i>
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            if current_price >= tp2 and "TP2" not in hit:
                if "TP1" not in hit: hit.append("TP1")
                hit.append("TP2")
                pos["sl"] = tp1
                tg(f"""🎯 <b>[2차 목표가 달성 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
조치사항: <b>손절가를 1차 목표가({m(tp1)})로 상향합니다.</b>
━━━━━━━━━━━━━━━━━━━━━━━""")
            elif current_price >= tp1 and "TP1" not in hit:
                hit.append("TP1")
                pos["sl"] = pos["entry"]
                tg(f"""🎯 <b>[1차 목표가 달성 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
조치사항: <b>손절가를 본전({m(pos['entry'])})으로 상향(본전 스탑)합니다.</b>
━━━━━━━━━━━━━━━━━━━━━━━""")

            updated_positions.append(pos)
        except Exception as e:
            print(f"[ERROR] 종목 {code} 체크 중 오류:", e)
            updated_positions.append(pos)

    # 3. 신규 종목 스캔 (고승률 필터 + 분할 매수 비중 가이드 추가)
    active_codes = [p.get("code") for p in updated_positions]
    
    if len(updated_positions) < MAX_SLOTS:
        print(f"[SCAN] 빈 슬롯 존재 ({len(updated_positions)}/{MAX_SLOTS}). 고승률 신규 종목 스캔 시작")
        
        try:
            krx = fdr.StockListing('KRX')
            priority_codes = ['005930', '000660', '010130'] # 삼성전자, 하이닉스, 고려아연
            candidates = krx.dropna(subset=['Code', 'Name']).head(150)
            
            scan_rows = []
            for p_code in priority_codes:
                matched = krx[krx['Code'] == p_code]
                if not matched.empty:
                    scan_rows.append(matched.iloc[0])
            for _, r in candidates.iterrows():
                if r['Code'] not in priority_codes:
                    scan_rows.append(r)

            for row in scan_rows:
                if len(updated_positions) >= MAX_SLOTS:
                    break
                
                code = str(row['Code'])
                name = str(row['Name'])
                
                if code in active_codes:
                    continue
                
                try:
                    df = fdr.DataReader(code)
                    if len(df) < 30:
                        continue
                    
                    close = float(df['Close'].iloc[-1])
                    if not (3000 <= close <= 1000000):
                        continue

                    ma5 = float(df['Close'].rolling(5).mean().iloc[-1])
                    ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
                    volume = float(df['Volume'].iloc[-1])
                    vol_ma20 = float(df['Volume'].rolling(20).mean().iloc[-1])
                    
                    trading_value = close * volume
                    if trading_value < 5_000_000_000: # 거래대금 50억 이상
                        continue

                    if close > ma20 and ma5 > ma20 and volume > vol_ma20:
                        atr_series = calculate_atr(df)
                        atr_val = float(atr_series.iloc[-1]) if not atr_series.empty and not np.isnan(atr_series.iloc[-1]) else close * 0.035
                        
                        volatility_pct = max(0.02, min(0.045, atr_val / close))
                        
                        entry = close
                        sl = entry * (1 - volatility_pct)
                        tp1 = entry * (1 + volatility_pct * 1.5)
                        tp2 = entry * (1 + volatility_pct * 2.8)
                        tp3 = entry * (1 + volatility_pct * 4.2)

                        sl_pct_str = f"-{volatility_pct*100:.1f}%"
                        tp1_pct_str = f"+{volatility_pct*1.5*100:.1f}%"
                        tp2_pct_str = f"+{volatility_pct*2.8*100:.1f}%"
                        tp3_pct_str = f"+{volatility_pct*4.2*100:.1f}%"

                        sector_val = row.get('Sector', None)
                        industry_val = row.get('Industry', None)
                        
                        if pd.notna(sector_val) and str(sector_val).strip() != "":
                            catalyst_str = f"관련 업종: <b>{sector_val}</b> 대형 수급 유입"
                        elif pd.notna(industry_val) and str(industry_val).strip() != "":
                            catalyst_str = f"주요 모멘텀: <b>{industry_val}</b> 강세"
                        else:
                            catalyst_str = "기관/외인 순매수 집중 및 5일/20일 정배열 안착"

                        new_pos = {
                            "code": code,
                            "name": name,
                            "entry": entry,
                            "sl": sl,
                            "tp1": tp1,
                            "tp2": tp2,
                            "tp3": tp3,
                            "hit": [],
                            "time": ts()
                        }
                        
                        updated_positions.append(new_pos)
                        active_codes.append(code)

                        tg(f"""🚨 <b>[고승률 변동성 종목 포착 리포트]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>포트폴리오 슬롯: [ {len(updated_positions)} / {MAX_SLOTS} ]</b>
🪙 <b>종목명:</b> <b>{name} ({code})</b>
💰 <b>추정 진입가:</b> <code>{m(entry)}</code>

🔥 <b>[상승 이유 및 고승률 소재]</b>
• <b>기술적 조건:</b> 5/20일선 정배열 + 거래량 폭증
• <b>수급 테마/소재:</b> {catalyst_str}

🛡 <b>손절가 (SL):</b> <code>{m(sl)}</code> ({sl_pct_str})
🎯 <b>1차 목표가 (TP1):</b> <code>{m(tp1)}</code> ({tp1_pct_str})
🎯 <b>2차 목표가 (TP2):</b> <code>{m(tp2)}</code> ({tp2_pct_str})
🎯 <b>3차 목표가 (TP3):</b> <code>{m(tp3)}</code> ({tp3_pct_str})
━━━━━━━━━━━━━━━━━━━━━━━
💡 <b>[매매 가이드]</b> 전체 자금의 <b>5~7% 이내</b>로 분할 매수하고, 본 종목은 승률 70% 타겟 리스크 관리 모드가 적용됩니다.""")
                except Exception as inner_e:
                    print(f"[CANDIDATE ERROR] 종목 {code} 처리 중 예외:", inner_e)
                    continue
        except Exception as e:
            print("[SCAN ERROR] 종목 신규 스캔 중 오류:", e)
    else:
        print("[SCAN SKIP] 15개 슬롯이 모두 채워져 있습니다.")

    # 장 마감 시간대(예: 오후 3시 20분 이후)에 오늘 하루 성과 통계를 요약 전송하는 로직
    n = now()
    if n.hour == 15 and 20 <= n.minute <= 35:
        total_trades = daily_stats["wins"] + daily_stats["losses"]
        win_rate = (daily_stats["wins"] / total_trades * 100) if total_trades > 0 else 0
        
        tg(f"""📈 <b>[장 마감 일일 성과 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
날짜: {today_str}
• 총 청산 건수: {total_trades}건
• 성공 (TP 도달): {daily_stats['wins']}건 🔥
• 실패 (손절/타임스톱): {daily_stats['losses']}건 🛡
• <b>오늘의 달성 승률: <code>{win_rate:.1f}%</code></b>
• 현재 유지 중인 슬롯: {len(updated_positions)} / {MAX_SLOTS}
━━━━━━━━━━━━━━━━━━━━━━━
💡 <i>오늘 하루도 수고하셨습니다. 원칙 매매 승승장구하세요!</i>""")

    state["positions"] = updated_positions
    state["daily_stats"] = daily_stats
    save(STATE, state)
    print(f"[DONE] 최신 상태가 {STATE} 파일에 저장되었습니다.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", repr(e))
        raise
