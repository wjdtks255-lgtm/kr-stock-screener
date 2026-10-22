# KOREA MULTI-STRATEGY PROFIT MAXIMIZER & LIVE MONITOR V32.1.8
import os, json, time, requests, re
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import FinanceDataReader as fdr

V = "32.1.8"
STATE = "active_positions.json"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("CHAT_ID", "")
KST = timezone(timedelta(hours=9))

MAX_SLOTS = 8  # 최대 슬롯 8개 고정

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, (np.bool_, bool)): return bool(obj)
        return super(NpEncoder, self).default(obj)

def now(): return datetime.now(KST)
def ts(): return now().strftime("%Y-%m-%d %H:%M:%S")

def m(x):
    try:
        val = float(x)
        if np.isnan(val) or np.isinf(val): return "0원"
        return f"{int(val):,}원"
    except: return "0원"

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
    if not TOKEN or not CHAT: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": x, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        return r.ok
    except: return False

def get_state():
    current_month = now().strftime("%Y-%m")
    default_stats = {"total_closed": 0, "tp3_end": 0, "tp2_end": 0, "tp1_end": 0, "loss_end": 0, "draws": 0}
    default_state = {
        "positions": [], "sent": [], "last_bear_state": None,
        "total_stats": dict(default_stats),
        "monthly_stats": {"month": current_month, **default_stats}
    }
    s = load(STATE, default_state)
    if not isinstance(s, dict): s = default_state
    s.setdefault("positions", [])
    s.setdefault("sent", [])
    s.setdefault("last_bear_state", None)
    s.setdefault("total_stats", dict(default_stats))
    for k in default_stats:
        if k != "month": s["total_stats"].setdefault(k, 0)
    
    monthly = s.get("monthly_stats", {"month": current_month, **default_stats})
    if monthly.get("month") != current_month:
        s["monthly_stats"] = {"month": current_month, **default_stats}
    else:
        for k in default_stats:
            if k != "month": monthly.setdefault(k, 0)
    return s

def calculate_atr(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def is_market_open():
    n = now()
    if n.weekday() >= 5: return False
    cur = n.hour * 60 + n.minute
    return (9 * 60) <= cur <= (15 * 60 + 30)

def get_stock_news(code):
    try:
        url = f"https://finance.naver.com/item/news_news.naver?code={code}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5)
        titles = re.findall(r'<a href=".*?article_id=.*?".*?class="tit".*?>(.*?)</a>', res.text)
        clean_titles = []
        for t in titles[:2]:
            clean_t = re.sub(r'<[^>]+>', '', t).strip()
            if clean_t: clean_titles.append(f"• {clean_t}")
        return "\n".join(clean_titles) if clean_titles else None
    except:
        return None

def get_company_info(krx_row):
    try:
        sector = krx_row.get('Sector', '')
        industry = krx_row.get('Industry', '')
        info_parts = []
        if pd.notna(sector) and sector: info_parts.append(f"업종: {sector}")
        if pd.notna(industry) and industry: info_parts.append(f"주요제품: {industry}")
        return " | ".join(info_parts) if info_parts else None
    except:
        return None

def check_market_shield_and_report(last_bear_state):
    is_bear_market = False
    reasons = []
    try:
        kospi_df = fdr.DataReader('KS11', datetime.now() - timedelta(days=40))
        kosdaq_df = fdr.DataReader('KQ11', datetime.now() - timedelta(days=40))
        
        kospi_close, kospi_prev = float(kospi_df['Close'].iloc[-1]), float(kospi_df['Close'].iloc[-2])
        kospi_chg = ((kospi_close - kospi_prev) / kospi_prev) * 100
        kospi_ma20 = float(kospi_df['Close'].rolling(20).mean().iloc[-1])

        kosdaq_close, kosdaq_prev = float(kosdaq_df['Close'].iloc[-1]), float(kosdaq_df['Close'].iloc[-2])
        kosdaq_chg = ((kosdaq_close - kosdaq_prev) / kosdaq_prev) * 100
        kosdaq_ma20 = float(kosdaq_df['Close'].rolling(20).mean().iloc[-1])

        if kospi_close < kospi_ma20: 
            is_bear_market = True
            reasons.append(f"코스피 지수({kospi_close:,.2f})가 20일 이평선 하회")
        if kosdaq_close < kosdaq_ma20: 
            is_bear_market = True
            reasons.append(f"코스닥 지수({kosdaq_close:,.2f})가 20일 이평선 하회")
        if kospi_chg <= -2.0 or kosdaq_chg <= -2.0: 
            is_bear_market = True
            reasons.append(f"시장 지수 급락 발생 (코스피: {kospi_chg:+.2f}%, 코스닥: {kosdaq_chg:+.2f}%)")

        if last_bear_state != is_bear_market:
            if is_bear_market:
                tg(f"""🛡 <b>[하락장 방어 모드 발동]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>원금 보존을 위해 신규 매수를 전면 차단합니다.</b>
• 코스피: <code>{kospi_close:,.2f}</code> ({kospi_chg:+.2f}%)
• 코스닥: <code>{kosdaq_close:,.2f}</code> ({kosdaq_chg:+.2f}%)
━━━━━━━━━━━━━━━━━━━━━━━""")
            else:
                tg(f"""📊 <b>[수익 추구 장세 재개]</b> 📈
━━━━━━━━━━━━━━━━━━━━━━━
📈 <i>지수 안정세: 고수익 멀티 전략 스캐너가 가동됩니다.</i>
━━━━━━━━━━━━━━━━━━━━━━━""")
        return is_bear_market
    except:
        return False

def format_stats_report(stats_dict):
    total = stats_dict.get("total_closed", 0)
    tp3, tp2, tp1, loss, draws = stats_dict.get("tp3_end", 0), stats_dict.get("tp2_end", 0), stats_dict.get("tp1_end", 0), stats_dict.get("loss_end", 0), stats_dict.get("draws", 0)
    wins = tp1 + tp2 + tp3
    win_rate = (wins / total * 100) if total > 0 else 0.0
    return f"""📊 <b>[시그널 누적 성과 통계]</b>
━━━━━━━━━━━━━━━━━━━━━━━
• <b>총 종료 시그널:</b> <b>{total}건</b>
• 🎯 <b>TP3 종료:</b> {tp3}건
• 🎯 <b>TP2 종료:</b> {tp2}건
• 🎯 <b>TP1 종료:</b> {tp1}건
• 🛑 <b>손절 종료:</b> {loss}건
• ⏱ <b>타임스톱/무승부:</b> {draws}건

🏆 <b>최종 승률: {win_rate:.1f}%</b>
📈 승리 {wins}건 · 손절 {loss}건
━━━━━━━━━━━━━━━━━━━━━━━"""

def update_stats_record(stats_dict, end_type):
    stats_dict["total_closed"] += 1
    if end_type == 'TP3': stats_dict["tp3_end"] += 1
    elif end_type == 'TP2': stats_dict["tp2_end"] += 1
    elif end_type == 'TP1': stats_dict["tp1_end"] += 1
    elif end_type == 'LOSS': stats_dict["loss_end"] += 1
    elif end_type == 'DRAW': stats_dict["draws"] += 1

# 💡 장 시작 전 / 마감 후 정기 포트폴리오 슬롯 현황 전송 함수
def send_slot_status_report(positions, is_bear):
    bear_text = "하락장 방어 모드 유지 중" if is_bear else "정상 매매 모드 가동 중"
    used_slots = len(positions)
    avail_slots = MAX_SLOTS - used_slots
    
    msg_lines = [
        "📊 <b>[실시간 포트폴리오 슬롯 현황]</b> 📊",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"📌 <b>보유 슬롯: [ {used_slots} / {MAX_SLOTS} ]</b> ({avail_slots}개 가용 중)",
        f"🛡 <b>시장 상태:</b> {bear_text}\n",
        "🪙 <b>[현재 보유 포지션 리스트]</b>\n"
    ]
    
    if not positions:
        msg_lines.append("현재 보유 중인 포지션이 없습니다.")
    else:
        for idx, pos in enumerate(positions, 1):
            code = pos.get("code")
            name = pos.get("name")
            entry = pos.get("entry", 0)
            sl = pos.get("sl", 0)
            tp1 = pos.get("tp1", 0)
            
            curr_price = entry
            pct_chg = 0.0
            try:
                df = fdr.DataReader(code)
                if not df.empty:
                    curr_price = float(df['Close'].iloc[-1])
                    pct_chg = ((curr_price - entry) / entry) * 100
            except: pass
            
            sl_pct = ((sl - entry) / entry) * 100
            tp1_pct = ((tp1 - entry) / entry) * 100
            naver_url = f"https://m.stock.naver.com/domestic/stock/{code}/total"
            tv_url = f"https://www.tradingview.com/chart/?symbol=KRX:{code}"
            
            item_txt = f"""{idx}️⃣ <b>{name} ({code})</b>
• <b>진입가:</b> {m(entry)}
• <b>현재가:</b> {m(curr_price)} (<code>{pct_chg:+.2f}%</code>)
• <b>손절가(SL):</b> {m(sl)} ({sl_pct:.1f}%)
• <b>1차 목표가(TP1):</b> {m(tp1)} (+{tp1_pct:.1f}%)
• <b>상태:</b> 손절가 상회 홀딩 중 🟢
📲 차트: <a href="{naver_url}">네이버</a> | <a href="{tv_url}">트레이딩뷰</a>"""
            msg_lines.append(item_txt)
            
    msg_lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━")
    msg_lines.append("💡 <i>장중 5분 주기로 손절가 및 목표가 도착 여부를 실시간 감시하고 있습니다.</i>")
    tg("\n".join(msg_lines))

def main():
    print(f" KOREA MULTI-STRATEGY PROFIT MAXIMIZER V{V}")
    
    state = get_state()
    total_stats, monthly_stats, last_bear_state = state.get("total_stats"), state.get("monthly_stats"), state.get("last_bear_state")
    is_bear_market = check_market_shield_and_report(last_bear_state)
    state["last_bear_state"] = is_bear_market
    positions = state.get("positions", [])

    # 💡 정기 알림 체크 (08:55 장 시작 전, 15:35 장 마감 후 슬롯 리포트 발송)
    n = now()
    cur_time_str = n.strftime("%H:%M")
    sent_reports = state.get("sent_reports", [])
    today_str = n.strftime("%Y-%m-%d")
    
    if cur_time_str in ["08:55", "15:35"]:
        report_key = f"{today_str}_{cur_time_str}"
        if report_key not in sent_reports:
            send_slot_status_report(positions, is_bear_market)
            sent_reports.append(report_key)
            state["sent_reports"] = sent_reports[-10:] # 최근 10개만 유지

    if not is_market_open(): 
        save(STATE, state)
        return

    updated_positions = []

    for pos in positions:
        code = pos.get("code")
        try:
            df = fdr.DataReader(code)
            if df.empty:
                updated_positions.append(pos)
                continue
            
            current_price = float(df['Close'].iloc[-1])
            sl, entry, tp1, tp2, tp3 = pos.get("sl", 0), pos.get("entry", 0), pos.get("tp1", 0), pos.get("tp2", 0), pos.get("tp3", 0)
            highest_reached = pos.get("highest_reached", "NONE")
            
            entry_time_str = pos.get("time", ts())
            try: days_elapsed = (now().date() - datetime.strptime(entry_time_str.split()[0], "%Y-%m-%d").date()).days
            except: days_elapsed = 0

            if current_price >= tp3:
                update_stats_record(total_stats, 'TP3')
                update_stats_record(monthly_stats, 'TP3')
                tg(f"🏆 <b>[최종 TP3 수익 극대화 완료]</b> ({pos.get('name')})\n{format_stats_report(total_stats)}")
                continue

            if current_price >= tp2 and highest_reached not in ['TP3', 'TP2']:
                pos["highest_reached"] = 'TP2'
                pos["sl"] = tp1
                tg(f"🎯 <b>[2차 목표가 달성]</b> - 손절가를 1차 목표가({m(tp1)})로 올려 수익 방어! ({pos.get('name')})")
            elif current_price >= tp1 and highest_reached not in ['TP3', 'TP2', 'TP1']:
                pos["highest_reached"] = 'TP1'
                pos["sl"] = entry
                tg(f"🎯 <b>[1차 목표가 달성]</b> - 손절가를 본전({m(entry)})으로 올려 무위험 전환! ({pos.get('name')})")

            if days_elapsed >= 7:
                stat_key = highest_reached if highest_reached in ['TP1', 'TP2'] else 'DRAW'
                update_stats_record(total_stats, stat_key)
                update_stats_record(monthly_stats, stat_key)
                tg(f"⏱ <b>[기간 달성 회전 청산]</b> ({pos.get('name')})\n{format_stats_report(total_stats)}")
                continue

            if current_price <= sl:
                if highest_reached in ['TP1', 'TP2'] or sl >= entry:
                    update_stats_record(total_stats, highest_reached if highest_reached in ['TP1', 'TP2'] else 'TP1')
                    update_stats_record(monthly_stats, highest_reached if highest_reached in ['TP1', 'TP2'] else 'TP1')
                    tg(f"🎯 <b>[무손실 본전 방어 마감]</b> ({pos.get('name')})\n{format_stats_report(total_stats)}")
                else:
                    update_stats_record(total_stats, 'LOSS')
                    update_stats_record(monthly_stats, 'LOSS')
                    tg(f"🛑 <b>[최소 손절 마감]</b> ({pos.get('name')})\n{format_stats_report(total_stats)}")
                continue

            # 💡 보유 종목 이슈/뉴스 업데이트 감시 (중복 방지)
            latest_news = get_stock_news(code)
            saved_news = pos.get("last_news", "")
            if latest_news and latest_news != saved_news:
                pos["last_news"] = latest_news
                pct_chg = ((current_price - entry) / entry) * 100
                naver_chart_url = f"https://m.stock.naver.com/domestic/stock/{code}/total"
                tv_chart_url = f"https://www.tradingview.com/chart/?symbol=KRX:{code}"
                
                tg(f"""🔔 <b>[보유 종목 실시간 이슈 포착]</b> 🔥
━━━━━━━━━━━━━━━━━━━━━━━
🪙 <b>종목명:</b> <b>{pos.get('name')} ({code})</b>
📌 <b>현재 상태:</b> 보유 중 (진입가 대비 <code>{pct_chg:+.2f}%</code>)

🔥 <b>[새로운 소식 및 뉴스]</b>
{latest_news}

📈 <b>[실시간 차트 바로가기]</b>
• <a href="{naver_chart_url}">📲 네이버 증권 차트 보기</a>
• <a href="{tv_chart_url}">📊 트레이딩뷰 차트 보기</a>
━━━━━━━━━━━━━━━━━━━━━━━
💡 <i>기존 손절가 및 목표가는 유지되며, 지속 모니터링 중입니다.</i>""")

            updated_positions.append(pos)
        except:
            updated_positions.append(pos)

    active_codes = [p.get("code") for p in updated_positions]
    
    if not is_bear_market and len(updated_positions) < MAX_SLOTS:
        try:
            krx = fdr.StockListing('KRX')
            candidates = krx.dropna(subset=['Code', 'Name']).head(100)
            
            for _, row in candidates.iterrows():
                if len(updated_positions) >= MAX_SLOTS: break
                code, name = str(row['Code']), str(row['Name'])
                if code in active_codes: continue
                
                try:
                    df = fdr.DataReader(code)
                    if len(df) < 30: continue
                    close = float(df['Close'].iloc[-1])
                    if not (3000 <= close <= 1000000): continue
                    
                    volume = float(df['Volume'].iloc[-1])
                    if (close * volume) < 7_000_000_000: continue

                    ma5 = float(df['Close'].rolling(5).mean().iloc[-1])
                    ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
                    vol_ma20 = float(df['Volume'].rolling(20).mean().iloc[-1])
                    
                    strategy_type, extra_reason = None, None
                    
                    if close > ma20 and ma5 > ma20 and volume > vol_ma20:
                        strategy_type = "주도주 눌림목"
                        if volume > vol_ma20 * 2.0:
                            extra_reason = "• 수급/모멘텀: 평균 거래량 대비 2배 이상의 강한 거래대금 유입"
                    elif close > float(df['Open'].iloc[-1]) and close >= float(df['High'].iloc[-1]) * 0.98 and volume > vol_ma20 * 1.5:
                        strategy_type = "종가 베팅"
                        extra_reason = "• 수급/모멘텀: 당일 고가 부근 마감 강세 및 기관/외인 선호 패턴"
                    else:
                        delta = df['Close'].diff()
                        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                        rsi = 100 - (100 / (1 + (gain / loss)))
                        current_rsi = float(rsi.iloc[-1])
                        if current_rsi <= 30 and close > float(df['Low'].iloc[-1]):
                            strategy_type = "낙폭과대 반등"
                            extra_reason = f"• 수급/모멘텀: 단기 과매도 구간(RSI {current_rsi:.1f}) 탈출 및 저가 매수세"

                    if not strategy_type: continue

                    atr_series = calculate_atr(df)
                    atr_val = float(atr_series.iloc[-1]) if not atr_series.empty and not np.isnan(atr_series.iloc[-1]) else close * 0.025
                    volatility_pct = max(0.015, min(0.03, atr_val / close))
                    
                    entry = close
                    sl = entry * (1 - volatility_pct)
                    tp1 = entry * (1 + volatility_pct * 1.8)
                    tp2 = entry * (1 + volatility_pct * 3.2)
                    tp3 = entry * (1 + volatility_pct * 5.0)

                    reasons = [f"• 기술적 조건: {strategy_type} 패턴 부합 및 5/20일선 추세 유지"]
                    if extra_reason: reasons.append(extra_reason)
                    
                    comp_info = get_company_info(row)
                    if comp_info: reasons.append(f"• 기업 정보: {comp_info}")
                    
                    initial_news = get_stock_news(code)
                    if initial_news: reasons.append(f"• 최근 관련 뉴스/이슈:\n{initial_news}")

                    reasons_text = "\n".join(reasons)

                    naver_chart_url = f"https://m.stock.naver.com/domestic/stock/{code}/total"
                    tv_chart_url = f"https://www.tradingview.com/chart/?symbol=KRX:{code}"

                    updated_positions.append({
                        "code": code, "name": name, "strategy": strategy_type,
                        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "highest_reached": "NONE", "time": ts(),
                        "last_news": initial_news
                    })
                    active_codes.append(code)

                    tg(f"""🎯 <b>[고승률 변동성 종목 포착 리포트]</b> 🚨
━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>포트폴리오 슬롯: [ {len(updated_positions)} / {MAX_SLOTS} ]</b>
⚡ <b>매매 전략: [ {strategy_type} ]</b>
🪙 <b>종목명:</b> <b>{name} ({code})</b>
💰 <b>추정 진입가:</b> <code>{m(entry)}</code>

🔥 <b>[상승 이유 및 기업/뉴스 정보]</b>
{reasons_text}

🛡 <b>손절가 (SL):</b> <code>{m(sl)}</code> (-{volatility_pct*100:.1f}%)
🎯 1차 목표가 (TP1): <code>{m(tp1)}</code> (+{volatility_pct*1.8*100:.1f}%)
🎯 2차 목표가 (TP2): <code>{m(tp2)}</code> (+{volatility_pct*3.2*100:.1f}%)
🎯 3차 목표가 (TP3): <code>{m(tp3)}</code> (+{volatility_pct*5.0*100:.1f}%)

📈 <b>[실시간 차트 바로가기]</b>
• <a href="{naver_chart_url}">📲 네이버 증권 차트 보기</a>
• <a href="{tv_chart_url}">📊 트레이딩뷰 차트 보기</a>
━━━━━━━━━━━━━━━━━━━━━━━
💡 <i>전체 자금의 5~7% 이내로 분할 매수하고, 본 종목은 승률 70% 타겟 리스크 관리 모드가 적용됩니다.</i>""")
                except: continue
        except: pass

    state["positions"] = updated_positions
    state["total_stats"] = total_stats
    state["monthly_stats"] = monthly_stats
    save(STATE, state)

if __name__ == "__main__":
    main()
