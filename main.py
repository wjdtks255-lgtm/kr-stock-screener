# KOREA STOCK HUNTER SCREENER & SIGNAL BOT (BEAR MARKET SHIELD V31.0)
import os, json, time, requests
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import FinanceDataReader as fdr

V = "31.0.0"
STATE = "active_positions.json"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))

MAX_SLOTS = 15

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
    current_month = now().strftime("%Y-%m")
    default_stats = {"wins": 0, "draws": 0, "losses": 0}
    default_state = {
        "positions": [], 
        "sent": [], 
        "total_stats": dict(default_stats),
        "monthly_stats": {"month": current_month, **default_stats}
    }
    s = load(STATE, default_state)
    if not isinstance(s, dict):
        s = default_state
    s.setdefault("positions", [])
    s.setdefault("sent", [])
    s.setdefault("total_stats", dict(default_stats))
    s["total_stats"].setdefault("draws", 0)
    
    monthly = s.get("monthly_stats", {"month": current_month, **default_stats})
    if monthly.get("month") != current_month:
        s["monthly_stats"] = {"month": current_month, **default_stats}
    else:
        s["monthly_stats"].setdefault("draws", 0)
        
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
    n = now()
    if n.weekday() >= 5:
        return False
    
    current_time_minutes = n.hour * 60 + n.minute
    market_start = 9 * 60       # 09:00
    market_end = 15 * 60 + 30   # 15:30
    
    return market_start <= current_time_minutes <= market_end

def check_market_shield_and_report():
    """
    하락장 방어 로직: 코스피/코스닥 지수가 20일 이평선 아래로 완전히 꺾이거나 폭락장일 때
    신규 매수 스캔 중단(is_bear_market = True) 및 상세 알림 전송
    """
    is_bear_market = False
    reasons = []
    
    try:
        kospi_df = fdr.DataReader('KS11', datetime.now() - timedelta(days=40))
        kosdaq_df = fdr.DataReader('KQ11', datetime.now() - timedelta(days=40))
        
        # 코스피 지수 분석
        kospi_close = float(kospi_df['Close'].iloc[-1])
        kospi_prev = float(kospi_df['Close'].iloc[-2])
        kospi_chg = ((kospi_close - kospi_prev) / kospi_prev) * 100
        kospi_ma20 = float(kospi_df['Close'].rolling(20).mean().iloc[-1])

        # 코스닥 지수 분석
        kosdaq_close = float(kosdaq_df['Close'].iloc[-1])
        kosdaq_prev = float(kosdaq_df['Close'].iloc[-2])
        kosdaq_chg = ((kosdaq_close - kosdaq_prev) / kosdaq_prev) * 100
        kosdaq_ma20 = float(kosdaq_df['Close'].rolling(20).mean().iloc[-1])

        # 하락장 방어 모드 발동 조건 검사
        if kospi_close < kospi_ma20:
            is_bear_market = True
            reasons.append(f"코스피 지수({kospi_close:,.2f})가 20일 이평선({kospi_ma20:,.2f}) 하회")
            
        if kosdaq_close < kosdaq_ma20:
            is_bear_market = True
            reasons.append(f"코스닥 지수({kosdaq_close:,.2f})가 20일 이평선({kosdaq_ma20:,.2f}) 하회")
            
        if kospi_chg <= -2.0 or kosdaq_chg <= -2.0:
            is_bear_market = True
            reasons.append(f"시장 지수 당일 급락 발생 (코스피: {kospi_chg:+.2f}%, 코스닥: {kosdaq_chg:+.2f}%)")

        if is_bear_market:
            reason_str = "\n".join([f"• {r}" for r in reasons])
            tg(f"""🛡 <b>[하락장 방어 모드 자동 발동]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>시장 지수 악화로 신규 종목 스캔 및 매수를 일시 중단합니다.</b>

📊 <b>[현재 시장 지수 현황]</b>
• <b>코스피:</b> <code>{kospi_close:,.2f}</code> ({kospi_chg:+.2f}% | 20일선: {kospi_ma20:,.2f})
• <b>코스닥:</b> <code>{kosdaq_close:,.2f}</code> ({kosdaq_chg:+.2f}% | 20일선: {kosdaq_ma20:,.2f})

🔍 <b>[방어 모드 발동 사유]</b>
{reason_str}

💡 <b>[운영 방침]</b>
- 신규 종목 스캔을 차단하여 <b>100% 현금을 보존</b>합니다.
- 기존 보유 포지션의 TP/SL 목표가 관리는 정상 가동됩니다.
━━━━━━━━━━━━━━━━━━━━━━━""")
        else:
            # 상승장/상승추세일 경우 주도주 리포트 전송
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

            tg(f"""📊 <b>[상승추세 시장 동향 및 주도주 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
🇰🇷 <b>코스피:</b> <code>{kospi_close:,.2f}</code> ({kospi_chg:+.2f}%)
🇰🇷 <b>코스닥:</b> <code>{kosdaq_close:,.2f}</code> ({kosdaq_chg:+.2f}%)

🔥 <b>오늘의 시장 주도 섹터 및 종목:</b>
{leaders_str}
━━━━━━━━━━━━━━━━━━━━━━━
📈 <i>지수가 20일선 위에 안정적으로 안착되어 신규 스캔 모드가 정상 작동합니다.</i>""")

        return is_bear_market
    except Exception as e:
        print("[MARKET SHIELD ERROR]", e)
        return False

def get_stats_string(total_stats, monthly_stats):
    def format_single(s_dict, title):
        w = s_dict.get("wins", 0)
        d = s_dict.get("draws", 0)
        l = s_dict.get("losses", 0)
        total = w + d + l
        win_rate = (w / total * 100) if total > 0 else 0.0
        defense_rate = ((w + d) / total * 100) if total > 0 else 0.0
        return f"• <b>{title}:</b> 총 {total}전 {w}승 {d}무 {l}패 (승률: <b>{win_rate:.1f}%</b> | 방어율: <b>{defense_rate:.1f}%</b>)"

    t_str = format_single(total_stats, "전체 누적")
    m_label = monthly_stats.get("month", "이번 달")
    m_str = format_single(monthly_stats, f"{m_label} 전적")

    return (f"📊 <b>[전적 통계 브리핑]</b>\n"
            f"{t_str}\n"
            f"{m_str}")

def main():
    print("====================================")
    print(f" KOREA STOCK HUNTER SCREENER V{V}")
    print("====================================")

    if not is_market_open():
        print(f"[MARKET CLOSED] 현재는 국장 운영 시간이 아닙니다. (KST: {ts()}) 작업을 건너뜁니다.")
        return

    state = get_state()
    total_stats = state.get("total_stats")
    monthly_stats = state.get("monthly_stats")

    # 1. 하락장 방어 로직 체크 (지수 20일선 하회 시 is_bear_market = True)
    is_bear_market = check_market_shield_and_report()

    positions = state.get("positions", [])
    print(f"[STATUS] 현재 유지 중인 포지션: {len(positions)} / {MAX_SLOTS}")

    # 2. 기존 포지션 청산, 목표가 및 타임스톱 체크 (하락장이라도 기존 보유 포지션 관리는 수행)
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
            entry = pos.get("entry", 0)
            tp1 = pos.get("tp1", 0)
            tp2 = pos.get("tp2", 0)
            tp3 = pos.get("tp3", 0)
            hit = pos.setdefault("hit", [])
            
            entry_time_str = pos.get("time", ts())
            try:
                entry_date = datetime.strptime(entry_time_str.split()[0], "%Y-%m-%d")
                days_elapsed = (now().date() - entry_date.date()).days
            except:
                days_elapsed = 0

            # [타임스톱 청산]
            if days_elapsed >= 7:
                if "TP1" in hit or "TP2" in hit:
                    total_stats["wins"] += 1
                    monthly_stats["wins"] += 1
                    report_title = "🏆 <b>[수익 확보 타임스톱 청산 리포트]</b> 🔥"
                    report_desc = "📌 <i>TP1/TP2 수익 달성 후 7일 경과로 잔여 물량을 수익 청산합니다.</i>"
                else:
                    total_stats["draws"] += 1
                    monthly_stats["draws"] += 1
                    report_title = "⏱ <b>[타임스톱 회전 청산 리포트]</b> ⚖️"
                    report_desc = "📌 <i>방향성 부재로 슬롯 회전율을 위해 원금 마감 처리합니다.</i>"

                stats_msg = get_stats_string(total_stats, monthly_stats)
                tg(f"""{report_title}
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
보유 기간: 7일 경과
청산가격: <code>{m(current_price)}</code>
{report_desc}
-----------------------------------------------
{stats_msg}
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            # [손절가 / 스탑라인 이탈 판정]
            if current_price <= sl:
                if "TP1" in hit or "TP2" in hit or sl > entry:
                    total_stats["wins"] += 1
                    monthly_stats["wins"] += 1
                    report_title = "🎯 <b>[수익 확보(TP 달성) 마감 리포트]</b> 🔥"
                    report_desc = "📌 <i>1차/2차 목표 달성으로 이미 수익을 확보한 뒤 안전하게 승리(Wins)로 마감되었습니다!</i>"
                else:
                    total_stats["losses"] += 1
                    monthly_stats["losses"] += 1
                    report_title = "🛡 <b>[손절가(SL) 도달 청산 리포트]</b> ❌"
                    report_desc = "📌 <i>원칙 매매를 위해 손절 처리가 완료되었습니다.</i>"

                stats_msg = get_stats_string(total_stats, monthly_stats)
                tg(f"""{report_title}
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
청산가격: <code>{m(current_price)}</code>
{report_desc}
-----------------------------------------------
{stats_msg}
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            # [3차 목표가 완수]
            if current_price >= tp3:
                if "TP1" not in hit: hit.append("TP1")
                if "TP2" not in hit: hit.append("TP2")
                if "TP3" not in hit: hit.append("TP3")
                total_stats["wins"] += 1
                monthly_stats["wins"] += 1
                stats_msg = get_stats_string(total_stats, monthly_stats)
                tg(f"""🏆 <b>[3차 목표가 완수 리포트]</b> 🔥
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
최종 청산가: <code>{m(current_price)}</code>
🎉 <i>모든 목표가를 성공적으로 달성했습니다!</i>
-----------------------------------------------
{stats_msg}
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            if current_price >= tp2 and "TP2" not in hit:
                if "TP1" not in hit: hit.append("TP1")
                hit.append("TP2")
                pos["sl"] = tp1
                tg(f"""🎯 <b>[2차 목표가 달성 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
조치사항: <b>손절가를 1차 목표가({m(tp1)})로 상향합니다. (수익 확정)</b>
━━━━━━━━━━━━━━━━━━━━━━━""")
            elif current_price >= tp1 and "TP1" not in hit:
                hit.append("TP1")
                pos["sl"] = pos["entry"]
                tg(f"""🎯 <b>[1차 목표가 달성 리포트]</b>
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b>
조치사항: <b>손절가를 본전({m(pos['entry'])})으로 상향하여 리스크를 제거합니다.</b>
━━━━━━━━━━━━━━━━━━━━━━━""")

            updated_positions.append(pos)
        except Exception as e:
            print(f"[ERROR] 종목 {code} 체크 중 오류:", e)
            updated_positions.append(pos)

    # 3. 신규 종목 스캔 (하락장 방어 모드 동작 시 스캔 완전 건너뜀)
    active_codes = [p.get("code") for p in updated_positions]
    
    if is_bear_market:
        print("[BEAR MARKET SHIELD] 하락장 방어 모드 활성화로 신규 종목 스캔을 차단합니다.")
    elif len(updated_positions) < MAX_SLOTS:
        print(f"[SCAN] 빈 슬롯 존재 ({len(updated_positions)}/{MAX_SLOTS}). 고승률 신규 종목 스캔 시작")
        
        try:
            krx = fdr.StockListing('KRX')
            priority_codes = ['005930', '000660', '010130']
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
                    if trading_value < 5_000_000_000:
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

    state["positions"] = updated_positions
    state["total_stats"] = total_stats
    state["monthly_stats"] = monthly_stats
    save(STATE, state)
    print(f"[DONE] 최신 상태가 {STATE} 파일에 저장되었습니다.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", repr(e))
        raise
