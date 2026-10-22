# KOREA MULTI-STRATEGY HUNTER & STATS ENGINE V32.1.2
import os, json, time, requests
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import FinanceDataReader as fdr

V = "32.1.2"
STATE = "active_positions.json"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("CHAT_ID", "")
KST = timezone(timedelta(hours=9))

MAX_SLOTS = 8

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
        print(f"[TG CONFIG ERROR] Token or Chat ID is missing.")
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
    default_stats = {
        "total_closed": 0,
        "tp3_end": 0,
        "tp2_end": 0,
        "tp1_end": 0,
        "loss_end": 0,
        "draws": 0
    }
    default_state = {
        "positions": [], 
        "sent": [], 
        "last_bear_state": None,  # 이전 방어 모드 상태 기록용
        "total_stats": dict(default_stats),
        "monthly_stats": {"month": current_month, **default_stats}
    }
    s = load(STATE, default_state)
    if not isinstance(s, dict):
        s = default_state
    s.setdefault("positions", [])
    s.setdefault("sent", [])
    s.setdefault("last_bear_state", None)
    s.setdefault("total_stats", dict(default_stats))
    
    for k in default_stats:
        if k != "month":
            s["total_stats"].setdefault(k, 0)
    
    monthly = s.get("monthly_stats", {"month": current_month, **default_stats})
    if monthly.get("month") != current_month:
        s["monthly_stats"] = {"month": current_month, **default_stats}
    else:
        for k in default_stats:
            if k != "month":
                monthly.setdefault(k, 0)
        
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

def check_market_shield_and_report(last_bear_state):
    is_bear_market = False
    reasons = []
    
    try:
        kospi_df = fdr.DataReader('KS11', datetime.now() - timedelta(days=40))
        kosdaq_df = fdr.DataReader('KQ11', datetime.now() - timedelta(days=40))
        
        kospi_close = float(kospi_df['Close'].iloc[-1])
        kospi_prev = float(kospi_df['Close'].iloc[-2])
        kospi_chg = ((kospi_close - kospi_prev) / kospi_prev) * 100
        kospi_ma20 = float(kospi_df['Close'].rolling(20).mean().iloc[-1])

        kosdaq_close = float(kosdaq_df['Close'].iloc[-1])
        kosdaq_prev = float(kosdaq_df['Close'].iloc[-2])
        kosdaq_chg = ((kosdaq_close - kosdaq_prev) / kosdaq_prev) * 100
        kosdaq_ma20 = float(kosdaq_df['Close'].rolling(20).mean().iloc[-1])

        if kospi_close < kospi_ma20:
            is_bear_market = True
            reasons.append(f"코스피 지수({kospi_close:,.2f})가 20일 이평선({kospi_ma20:,.2f}) 하회")
            
        if kosdaq_close < kosdaq_ma20:
            is_bear_market = True
            reasons.append(f"코스닥 지수({kosdaq_close:,.2f})가 20일 이평선({kosdaq_ma20:,.2f}) 하회")
            
        if kospi_chg <= -2.0 or kosdaq_chg <= -2.0:
            is_bear_market = True
            reasons.append(f"시장 지수 당일 급락 발생 (코스피: {kospi_chg:+.2f}%, 코스닥: {kosdaq_chg:+.2f}%)")

        # 💡 상태 변경 시에만 알림 발송 (중복 방지)
        if last_bear_state != is_bear_market:
            if is_bear_market:
                reason_str = "\n".join([f"• {r}" for r in reasons])
                tg(f"""🛡 <b>[하락장 방어 모드 자동 발동]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>시장 지수 악화로 신규 멀티 전략 스캔 및 매수를 일시 중단합니다.</b>

📊 <b>[현재 시장 지수 현황]</b>
• <b>코스피:</b> <code>{kospi_close:,.2f}</code> ({kospi_chg:+.2f}% | 20일선: {kospi_ma20:,.2f})
• <b>코스닥:</b> <code>{kosdaq_close:,.2f}</code> ({kosdaq_chg:+.2f}% | 20일선: {kosdaq_ma20:,.2f})

🔍 <b>[방어 모드 발동 사유]</b>
{reason_str}
━━━━━━━━━━━━━━━━━━━━━━━""")
            else:
                tg(f"""📊 <b>[상승/횡보장 전환: 스캐너 가동]</b> 📈
━━━━━━━━━━━━━━━━━━━━━━━
🇰🇷 <b>코스피:</b> <code>{kospi_close:,.2f}</code> ({kospi_chg:+.2f}%)
🇰🇷 <b>코스닥:</b> <code>{kosdaq_close:,.2f}</code> ({kosdaq_chg:+.2f}%)
📈 <i>지수 회복 완료: 멀티 전략(눌림목·종가베팅·낙폭과대) 스캐너가 다시 가동됩니다.</i>
━━━━━━━━━━━━━━━━━━━━━━━""")
        else:
            print(f"[MARKET SHIELD] 시장 상태 변동 없음 (현재 방어모드: {is_bear_market}). 메시지 발송 건너뜀.")

        return is_bear_market
    except Exception as e:
        print("[MARKET SHIELD ERROR]", e)
        return False

def format_stats_report(stats_dict, title_prefix="누적"):
    total = stats_dict.get("total_closed", 0)
    tp3 = stats_dict.get("tp3_end", 0)
    tp2 = stats_dict.get("tp2_end", 0)
    tp1 = stats_dict.get("tp1_end", 0)
    loss = stats_dict.get("loss_end", 0)
    draws = stats_dict.get("draws", 0)
    
    wins_count = tp1 + tp2 + tp3
    win_rate = (wins_count / total * 100) if total > 0 else 0.0

    return f"""📊 <b>[시그널 {title_prefix} 성과 통계]</b>
━━━━━━━━━━━━━━━━━━━━━━━
• <b>총 종료 시그널:</b> <b>{total}건</b>
• 🎯 <b>TP3 종료:</b> {tp3}건
• 🎯 <b>TP2 종료:</b> {tp2}건
• 🎯 <b>TP1 종료:</b> {tp1}건
• 🛑 <b>손절 종료:</b> {loss}건
• ⏱ <b>타임스톱/무승부:</b> {draws}건

🏆 <b>최종 승률: {win_rate:.1f}%</b>
📈 승리 {wins_count}건 · 손절 {loss}건
━━━━━━━━━━━━━━━━━━━━━━━"""

def update_stats_record(stats_dict, end_type):
    stats_dict["total_closed"] += 1
    if end_type == 'TP3':
        stats_dict["tp3_end"] += 1
    elif end_type == 'TP2':
        stats_dict["tp2_end"] += 1
    elif end_type == 'TP1':
        stats_dict["tp1_end"] += 1
    elif end_type == 'LOSS':
        stats_dict["loss_end"] += 1
    elif end_type == 'DRAW':
        stats_dict["draws"] += 1

def main():
    print("====================================")
    print(f" KOREA MULTI-STRATEGY HUNTER V{V}")
    print("====================================")

    if not is_market_open():
        print(f"[MARKET CLOSED] 현재는 장 운영 시간이 아닙니다. (KST: {ts()}) 작업을 건너뜁니다.")
        return

    state = get_state()
    total_stats = state.get("total_stats")
    monthly_stats = state.get("monthly_stats")
    last_bear_state = state.get("last_bear_state")

    is_bear_market = check_market_shield_and_report(last_bear_state)
    state["last_bear_state"] = is_bear_market  # 최신 상태 업데이트

    positions = state.get("positions", [])
    print(f"[STATUS] 현재 유지 중인 포지션: {len(positions)} / {MAX_SLOTS}")

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
            highest_reached = pos.get("highest_reached", "NONE")
            
            entry_time_str = pos.get("time", ts())
            try:
                days_elapsed = (now().date() - datetime.strptime(entry_time_str.split()[0], "%Y-%m-%d").date()).days
            except:
                days_elapsed = 0

            if current_price >= tp3:
                update_stats_record(total_stats, 'TP3')
                update_stats_record(monthly_stats, 'TP3')
                stats_msg = format_stats_report(total_stats, "누적")
                tg(f"""🏆 <b>[최종 TP3 종료 리포트]</b> 🔥
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b> [전략: {pos.get('strategy', '일반')}]
최종 청산가: <code>{m(current_price)}</code>
🎉 <i>TP3 도달로 시그널이 최종 완수되었습니다!</i>
-----------------------------------------------
{stats_msg}
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            if current_price >= tp2 and highest_reached != 'TP3' and highest_reached != 'TP2':
                pos["highest_reached"] = 'TP2'
                pos["sl"] = tp1
                tg(f"""🎯 <b>[2차 목표가 도달]</b> - 손절가를 1차 목표가({m(tp1)})로 상향합니다. ({pos.get('name')})""")
            elif current_price >= tp1 and highest_reached not in ['TP3', 'TP2', 'TP1']:
                pos["highest_reached"] = 'TP1'
                pos["sl"] = entry
                tg(f"""🎯 <b>[1차 목표가 도달]</b> - 손절가를 본전({m(entry)})으로 상향합니다. ({pos.get('name')})""")

            if days_elapsed >= 7:
                if highest_reached in ['TP1', 'TP2']:
                    stat_key = highest_reached
                    update_stats_record(total_stats, stat_key)
                    update_stats_record(monthly_stats, stat_key)
                    report_title = f"🏆 <b>[수익 확보({stat_key}) 타임스톱 최종 종료]</b> 🔥"
                else:
                    update_stats_record(total_stats, 'DRAW')
                    update_stats_record(monthly_stats, 'DRAW')
                    report_title = "⏱ <b>[타임스톱 무승부 회전 청산]</b> ⚖️"
                
                stats_msg = format_stats_report(total_stats, "누적")
                tg(f"""{report_title}
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b> [전략: {pos.get('strategy', '일반')}]
보유기간: 7일 경과 / 청산가: <code>{m(current_price)}</code>
-----------------------------------------------
{stats_msg}
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            if current_price <= sl:
                if highest_reached in ['TP1', 'TP2'] or sl >= entry:
                    update_stats_record(total_stats, highest_reached if highest_reached in ['TP1', 'TP2'] else 'TP1')
                    update_stats_record(monthly_stats, highest_reached if highest_reached in ['TP1', 'TP2'] else 'TP1')
                    report_title = "🎯 <b>[수익 방어 후 최종 마감]</b> 🔥"
                else:
                    update_stats_record(total_stats, 'LOSS')
                    update_stats_record(monthly_stats, 'LOSS')
                    report_title = "🛑 <b>[손절 종료 (패배)]</b> ❌"
                
                stats_msg = format_stats_report(total_stats, "누적")
                tg(f"""{report_title}
━━━━━━━━━━━━━━━━━━━━━━━
종목명: <b>{pos.get('name')} ({code})</b> [전략: {pos.get('strategy', '일반')}]
청산가격: <code>{m(current_price)}</code>
-----------------------------------------------
{stats_msg}
━━━━━━━━━━━━━━━━━━━━━━━""")
                continue

            updated_positions.append(pos)
        except Exception as e:
            print(f"[ERROR] 종목 {code} 체크 중 오류:", e)
            updated_positions.append(pos)

    active_codes = [p.get("code") for p in updated_positions]
    
    if is_bear_market:
        print("[BEAR MARKET SHIELD] 하락장 방어 모드 발동 중이므로 신규 스캔을 하지 않습니다.")
    elif len(updated_positions) < MAX_SLOTS:
        print(f"[SCAN] 빈 슬롯 존재 ({len(updated_positions)}/{MAX_SLOTS}). 멀티 전략 스캔 시작")
        
        try:
            krx = fdr.StockListing('KRX')
            candidates = krx.dropna(subset=['Code', 'Name']).head(100)
            
            for _, row in candidates.iterrows():
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

                    volume = float(df['Volume'].iloc[-1])
                    trading_value = close * volume
                    if trading_value < 5_000_000_000:
                        continue

                    ma5 = float(df['Close'].rolling(5).mean().iloc[-1])
                    ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
                    vol_ma20 = float(df['Volume'].rolling(20).mean().iloc[-1])
                    
                    strategy_type = None
                    
                    if close > ma20 and ma5 > ma20 and volume > vol_ma20:
                        strategy_type = "주도주 눌림목"
                    elif close > float(df['Open'].iloc[-1]) and close >= float(df['High'].iloc[-1]) * 0.98 and volume > vol_ma20 * 1.5:
                        strategy_type = "종가 베팅"
                    else:
                        delta = df['Close'].diff()
                        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                        rs = gain / loss
                        rsi = 100 - (100 / (1 + rs))
                        current_rsi = float(rsi.iloc[-1])
                        
                        if current_rsi <= 32 and close > float(df['Low'].iloc[-1]):
                            strategy_type = "낙폭과대 반등"

                    if not strategy_type:
                        continue

                    atr_series = calculate_atr(df)
                    atr_val = float(atr_series.iloc[-1]) if not atr_series.empty and not np.isnan(atr_series.iloc[-1]) else close * 0.035
                    volatility_pct = max(0.02, min(0.045, atr_val / close))
                    
                    entry = close
                    sl = entry * (1 - volatility_pct)
                    tp1 = entry * (1 + volatility_pct * 1.5)
                    tp2 = entry * (1 + volatility_pct * 2.8)
                    tp3 = entry * (1 + volatility_pct * 4.2)

                    new_pos = {
                        "code": code,
                        "name": name,
                        "strategy": strategy_type,
                        "entry": entry,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "highest_reached": "NONE",
                        "time": ts()
                    }
                    
                    updated_positions.append(new_pos)
                    active_codes.append(code)

                    tg(f"""🎯 <b>[멀티 전략 종목 포착 리포트]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>포트폴리오 슬롯: [ {len(updated_positions)} / {MAX_SLOTS} ]</b>
⚡ <b>매매 전략: [ {strategy_type} ]</b>
🪙 <b>종목명:</b> <b>{name} ({code})</b>
💰 <b>추정 진입가:</b> <code>{m(entry)}</code>

🛡 <b>손절가 (SL):</b> <code>{m(sl)}</code> (-{volatility_pct*100:.1f}%)
🎯 <b>1차 목표가 (TP1):</b> <code>{m(tp1)}</code> (+{volatility_pct*1.5*100:.1f}%)
🎯 <b>2차 목표가 (TP2):</b> <code>{m(tp2)}</code> (+{volatility_pct*2.8*100:.1f}%)
🎯 <b>3차 목표가 (TP3):</b> <code>{m(tp3)}</code> (+{volatility_pct*4.2*100:.1f}%)
━━━━━━━━━━━━━━━━━━━━━━━
💡 <i>매매자가 원하는 전략 종목만 선택하여 자율적으로 대응하세요!</i>""")
                except Exception as inner_e:
                    print(f"[CANDIDATE ERROR] 종목 {code} 처리 중 예외:", inner_e)
                    continue
        except Exception as e:
            print("[SCAN ERROR] 멀티 전략 신규 스캔 중 오류:", e)
    else:
        print("[SCAN SKIP] 8개 슬롯이 모두 채워져 있습니다.")

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
