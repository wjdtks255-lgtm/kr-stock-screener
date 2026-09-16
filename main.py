import os
import requests
import numpy as np
import json
import time

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CACHE_FILE = "tracked_coins.json"

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ 텔레그램 토큰 또는 챗 아이디가 설정되지 않았습니다!")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    res = requests.post(url, json=payload)
    print(f"텔레그램 전송 응답: {res.text}")

def format_price(price):
    if price < 10:
        return f"{price:.2f}원"
    elif price < 1000:
        return f"{price:.1f}원"
    else:
        return f"{price:,.0f}원"

def calculate_dynamic_duration(target_pct, vol_ratio, change_rate):
    speed_factor = max(vol_ratio, 1.0) * max(change_rate, 0.5)
    estimated_hours = (target_pct * 12.0) / speed_factor
    estimated_hours = max(2, min(estimated_hours, 168.0))
    
    if estimated_hours < 12:
        return f"약 {int(estimated_hours)}시간 이내 (초단기 폭발형)"
    elif estimated_hours < 24:
        return f"약 {int(estimated_hours)}시간 이내 (당일 슈팅형)"
    elif estimated_hours < 72:
        days = round(estimated_hours / 24, 1)
        return f"약 {days}일 이내 (단기 스윙형)"
    else:
        days = round(estimated_hours / 24)
        return f"약 {days}일 소요 예상 (중기 추세형)"

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"캐시 저장 에러: {e}")

def get_upbit_market_details():
    url = "https://api.upbit.com/v1/market/all"
    res = requests.get(url).json()
    market_dict = {}
    for item in res:
        if item['market'].startswith('KRW-') and item['market'] != 'KRW-BTC':
            market_dict[item['market']] = item['korean_name']
    return market_dict

def get_24h_trade_prices(markets):
    """ 업비트 복수 티커 API를 통해 24시간 거래대금 일괄 조회 """
    url = f"https://api.upbit.com/v1/ticker?markets={','.join(markets)}"
    try:
        res = requests.get(url).json()
        price_map = {}
        for item in res:
            price_map[item['market']] = item['acc_trade_price_24h']
        return price_map
    except:
        return {}

if __name__ == "__main__":
    print("🌐 [진짜 주도주 포착형 15분봉 스캐너] 고성능 필터 가동 중...")
    
    market_dict = get_upbit_market_details()
    market_list = list(market_dict.keys())
    
    # 24시간 거래대금 조회 (유동성 필터용)
    trade_prices_24h = get_24h_trade_prices(market_list)
    
    tracked_cache = load_cache()
    current_time = time.time()
    
    tracked_cache = {k: v for k, v in tracked_cache.items() if current_time - v.get('time', 0) < 43200}
    notifications = []

    for market, korean_name in market_dict.items():
        try:
            # 유동성 필터: 24시간 거래대금 300억 원 미만인 코인은 원천 차단 (노이즈 제거)
            acc_trade_price = trade_prices_24h.get(market, 0)
            if acc_trade_price < 30,000,000,000: # 300억 미만 스킵
                continue

            url = f"https://api.upbit.com/v1/candles/minutes/15?market={market}&count=30"
            res = requests.get(url).json()
            if len(res) < 25:
                continue
                
            res = list(reversed(res))
            opens = np.array([x['opening_price'] for x in res])
            closes = np.array([x['trade_price'] for x in res])
            highs = np.array([x['high_price'] for x in res])
            lows = np.array([x['low_price'] for x in res])
            volumes = np.array([x['candle_acc_trade_volume'] for x in res])
            
            current_price = closes[-1]
            current_open = opens[-1]
            prev_close = closes[-2]
            change_rate = ((current_price - prev_close) / prev_close) * 100
            
            # 양봉 몸통 크기 검증 (음봉이거나 윗꼬리가 너무 긴 가짜 돌파 배제)
            candle_body = current_price - current_open
            candle_range = highs[-1] - lows[-1]
            if candle_range > 0:
                body_ratio = candle_body / candle_range
            else:
                body_ratio = 0

            ma20 = np.mean(closes[-20:])
            std20 = np.std(closes[-20:])
            
            avg_volume_20 = np.mean(volumes[-21:-1]) if len(volumes) >= 21 else np.mean(volumes[:-1])
            current_volume = volumes[-1]
            vol_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 0
            
            # --- [CASE 1: 이미 추적 중인 종목 모니터링] ---
            if market in tracked_cache:
                info = tracked_cache[market]
                tp1 = info['tp1']
                tp2 = info['tp2']
                tp3 = info['tp3']
                sl = info['sl']
                reached = info.get('reached_targets', [])
                
                if current_price <= sl:
                    notifications.append(f"🛑 **[손절가 이탈]** `{korean_name} ({market})`\n- 현재가 `{format_price(current_price)}`이 손절가를 이탈했습니다.")
                    del tracked_cache[market]
                    continue
                
                if 3 not in reached and current_price >= tp3:
                    notifications.append(f"🎯🔥 **[3차 목표가 최종 달성!]** `{korean_name} ({market})`\n- 최종 3차 목표가 돌파 완료!")
                    del tracked_cache[market]
                    continue
                elif 2 not in reached and current_price >= tp2:
                    notifications.append(f"🎯🚀 **[2차 목표가 달성!]** `{korean_name} ({market})`\n- 2차 목표가 도달!")
                    reached.append(2)
                elif 1 not in reached and current_price >= tp1:
                    notifications.append(f"🎯✨ **[1차 목표가 달성!]** `{korean_name} ({market})`\n- 1차 목표가 도달!")
                    reached.append(1)
                
                info['reached_targets'] = reached
                tracked_cache[market] = info
                continue

            recent_atr = np.mean(highs[-5:] - lows[-5:])
            if recent_atr == 0: recent_atr = current_price * 0.01

            # --- [CASE 2-A: 강력한 메이저 급등 / 주도주 돌파] ---
            # 조건 강화: 거래량 2.5배 이상 폭발 + 상승률 3.5% 이상 + 몸통이 전체 캔들의 40% 이상인 꽉 찬 양봉
            is_strong_vol = vol_ratio >= 2.5
            is_strong_change = (3.5 <= change_rate <= 25.0)
            is_valid_body = body_ratio >= 0.4
            
            if is_strong_vol and is_strong_change and is_valid_body:
                tp1 = current_price + (recent_atr * 1.3)
                tp2 = current_price + (recent_atr * 2.6)
                tp3 = current_price + (recent_atr * 4.2)
                
                tp1 = max(tp1, current_price * 1.035)
                tp2 = max(tp2, tp1 * 1.025)
                tp3 = max(tp3, tp2 * 1.025)
                
                sl = min(np.min(lows[-3:]), ma20 * 0.96)
                
                target_pct = ((tp3 - current_price) / current_price) * 100
                dynamic_duration = calculate_dynamic_duration(target_pct, vol_ratio, change_rate)
                
                tracked_cache[market] = {"time": current_time, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "reached_targets": []}
                
                new_msg = (
                    f"🔥 **[진짜 주도주 급등 포착]** 🔥\n\n"
                    f"📌 **종목명**: `{korean_name}` (`{market}`)\n"
                    f"💰 **현재가**: `{format_price(current_price)}` (`+{change_rate:.2f}%`)\n"
                    f"💸 **24h 대금**: `{acc_trade_price / 100_000_000:,.0f}억원`\n\n"
                    f"🎯 **1차 목표**: `{format_price(tp1)}` (`+{((tp1-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **2차 목표**: `{format_price(tp2)}` (`+{((tp2-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **3차 목표**: `{format_price(tp3)}` (`+{((tp3-current_price)/current_price)*100:.1f}%`)\n"
                    f"🛑 **손절가**: `{format_price(sl)}` (`{((sl-current_price)/current_price)*100:.1f}%`)\n\n"
                    f"⏱ **예상 소요 기간**: `{dynamic_duration}`\n"
                    f"📊 **포착 근거**: 거래량 `{vol_ratio:.1f}배` 폭발 + 꽉 찬 강세 양봉"
                )
                notifications.append(new_msg)
                continue

            # --- [CASE 2-B: 확실한 수급 초기 돌파] ---
            # 조건 강화: 거래량 2.0배 이상 + 상승률 1.0% ~ 3.5% + 유동성 뒷받침
            is_mild_vol = vol_ratio >= 2.0
            is_mild_change = (1.0 <= change_rate < 3.5)
            
            if is_mild_vol and is_mild_change and is_valid_body:
                tp1 = current_price + (recent_atr * 1.1)
                tp2 = current_price + (recent_atr * 2.2)
                tp3 = current_price + (recent_atr * 3.5)
                
                tp1 = max(tp1, current_price * 1.03)
                tp2 = max(tp2, tp1 * 1.02)
                tp3 = max(tp3, tp2 * 1.02)
                
                sl = min(np.min(lows[-3:]), ma20 * 0.98)
                
                target_pct = ((tp3 - current_price) / current_price) * 100
                dynamic_duration = calculate_dynamic_duration(target_pct, vol_ratio, change_rate)
                
                tracked_cache[market] = {"time": current_time, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "reached_targets": []}
                
                new_msg = (
                    f"⚡ **[수급 초기 돌파 포착]** ⚡\n\n"
                    f"📌 **종목명**: `{korean_name}` (`{market}`)\n"
                    f"💰 **현재가**: `{format_price(current_price)}` (`+{change_rate:.2f}%`)\n"
                    f"💸 **24h 대금**: `{acc_trade_price / 100_000_000:,.0f}억원`\n\n"
                    f"🎯 **1차 목표**: `{format_price(tp1)}` (`+{((tp1-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **2차 목표**: `{format_price(tp2)}` (`+{((tp2-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **3차 목표**: `{format_price(tp3)}` (`+{((tp3-current_price)/current_price)*100:.1f}%`)\n"
                    f"🛑 **손절가**: `{format_price(sl)}` (`{((sl-current_price)/current_price)*100:.1f}%`)\n\n"
                    f"⏱ **예상 소요 기간**: `{dynamic_duration}`\n"
                    f"📊 **포착 근거**: 거래량 `{vol_ratio:.1f}배` + 유의미한 수급 초기 집중"
                )
                notifications.append(new_msg)

        except Exception as e:
            pass

    for msg in notifications:
        send_telegram(msg)

    save_cache(tracked_cache)
    print("고성능 정제 필터 스캔 완료.")
