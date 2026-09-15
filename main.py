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

if __name__ == "__main__":
    print("🌐 [올라운드 15분봉 통합 스캐너] 가동 중 (약상승 + 급등 + 바닥슈팅 모두 포착)...")
    
    market_dict = get_upbit_market_details()
    tracked_cache = load_cache()
    current_time = time.time()
    
    # 12시간 지난 캐시는 자동 정리
    tracked_cache = {k: v for k, v in tracked_cache.items() if current_time - v.get('time', 0) < 43200}
    
    notifications = []

    for market, korean_name in market_dict.items():
        try:
            url = f"https://api.upbit.com/v1/candles/minutes/15?market={market}&count=30"
            res = requests.get(url).json()
            if len(res) < 25:
                continue
                
            res = list(reversed(res))
            closes = np.array([x['trade_price'] for x in res])
            highs = np.array([x['high_price'] for x in res])
            lows = np.array([x['low_price'] for x in res])
            volumes = np.array([x['candle_acc_trade_volume'] for x in res])
            
            current_price = closes[-1]
            prev_close = closes[-2]
            change_rate = ((current_price - prev_close) / prev_close) * 100
            
            ma20 = np.mean(closes[-20:])
            std20 = np.std(closes[-20:])
            upper_band = ma20 + (std20 * 2.0)
            
            avg_volume_20 = np.mean(volumes[-21:-1])
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
                    notifications.append(f"🛑 **[손절가 이탈]** `{korean_name} ({market})`\n- 현재가 `{current_price:,.0f}원`이 손절가를 이탈했습니다.")
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

            # --- [CASE 2-A: 화끈한 강한 돌파 / 바닥 슈팅 (아스타 패턴 포함)] ---
            # 조건: 거래량 2.2배 이상 + 상승률 +3% ~ +25% + (볼린저 돌파 또는 역배열 바닥 탈피)
            is_strong_vol = vol_ratio >= 2.2
            is_strong_change = (3.0 <= change_rate <= 25.0)
            
            if is_strong_vol and is_strong_change:
                tp1 = current_price + (recent_atr * 1.5)
                tp2 = current_price + (recent_atr * 3.0)
                tp3 = current_price + (recent_atr * 5.0)
                sl = min(np.min(lows[-3:]), ma20 * 0.95)
                
                tracked_cache[market] = {"time": current_time, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "reached_targets": []}
                
                new_msg = (
                    f"🔥 **[급등 / 바닥 슈팅 포착]** 🔥\n\n"
                    f"📌 **종목명**: `{korean_name}` (`{market}`)\n"
                    f"💰 **현재가**: `{current_price:,.0f}원` (`+{change_rate:.2f}%`)\n\n"
                    f"🎯 **1차 목표**: `{tp1:,.0f}원` (`+{((tp1-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **2차 목표**: `{tp2:,.0f}원` (`+{((tp2-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **3차 목표**: `{tp3:,.0f}원` (`+{((tp3-current_price)/current_price)*100:.1f}%`)\n"
                    f"🛑 **손절가**: `{sl:,.0f}원` (`{((sl-current_price)/current_price)*100:.1f}%`)\n\n"
                    f"📊 **포착 근거**: 평소 대비 거래량 `{vol_ratio:.1f}배` 폭발 및 강력한 수급 유입"
                )
                notifications.append(new_msg)
                continue

            # --- [CASE 2-B: 잔잔한 상승세 / 약상승 및 초입 수급] ---
            # 조건: 거래량 1.6배 이상 + 상승률 +0.5% ~ +3.0% 미만 (하락장 속 약상승도 포함)
            is_mild_vol = vol_ratio >= 1.6
            is_mild_change = (0.5 <= change_rate < 3.0)
            
            if is_mild_vol and is_mild_change:
                tp1 = current_price + (recent_atr * 1.0)
                tp2 = current_price + (recent_atr * 2.0)
                tp3 = current_price + (recent_atr * 3.5)
                sl = min(np.min(lows[-3:]), ma20 * 0.97)
                
                tracked_cache[market] = {"time": current_time, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "reached_targets": []}
                
                new_msg = (
                    f"⚡ **[약상승 / 수급 초입 포착]** ⚡\n\n"
                    f"📌 **종목명**: `{korean_name}` (`{market}`)\n"
                    f"💰 **현재가**: `{current_price:,.0f}원` (`+{change_rate:.2f}%`)\n\n"
                    f"🎯 **1차 목표**: `{tp1:,.0f}원` (`+{((tp1-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **2차 목표**: `{tp2:,.0f}원` (`+{((tp2-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **3차 목표**: `{tp3:,.0f}원` (`+{((tp3-current_price)/current_price)*100:.1f}%`)\n"
                    f"🛑 **손절가**: `{sl:,.0f}원` (`{((sl-current_price)/current_price)*100:.1f}%`)\n\n"
                    f"📊 **포착 근거**: 거래량 `{vol_ratio:.1f}배` 유입 + 잔잔한 상승 모멘텀 발생"
                )
                notifications.append(new_msg)

        except Exception as e:
            pass

    for msg in notifications:
        send_telegram(msg)

    save_cache(tracked_cache)
    print("올라운드 스캔 완료.")

