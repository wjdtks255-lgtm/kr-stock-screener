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
    print("⚡ [15분봉 초단기 폭발 & 바닥 돌파] 스캐너 가동 중...")
    
    market_dict = get_upbit_market_details()
    tracked_cache = load_cache()
    current_time = time.time()
    
    # 12시간 지난 캐시는 자동 정리 (15분봉이므로 회전율 빠르게)
    tracked_cache = {k: v for k, v in tracked_cache.items() if current_time - v.get('time', 0) < 43200}
    
    notifications = []

    for market, korean_name in market_dict.items():
        try:
            # 15분봉 데이터 30개 가져오기
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
            
            ma5 = np.mean(closes[-5:])
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

            # --- [CASE 2: 15분봉 기준 바닥 슈팅 및 강력 돌파 포착 (아스타 패턴)] ---
            # 거래량 2.5배 이상 폭증 + 상승률 +3% ~ +25% + 볼린저 상단 돌파 (정배열 조건 제거로 바닥권 슈팅 포착)
            is_volume_explosion = vol_ratio >= 2.5
            is_surge_range = (3.0 <= change_rate <= 25.0)
            is_breakout = current_price >= upper_band
            
            if is_volume_explosion and is_surge_range and is_breakout:
                recent_atr = np.mean(highs[-5:] - lows[-5:])
                if recent_atr == 0: recent_atr = current_price * 0.01
                
                tp1 = current_price + (recent_atr * 1.5)
                tp2 = current_price + (recent_atr * 3.0)
                tp3 = current_price + (recent_atr * 5.0)
                sl = min(np.min(lows[-3:]), ma20 * 0.95)
                
                tracked_cache[market] = {
                    "time": current_time, 
                    "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, 
                    "reached_targets": []
                }
                
                new_msg = (
                    f"🚨🔥 **[15분봉 바닥 슈팅 / 수급 폭발 포착]** 🔥🚨\n\n"
                    f"📌 **종목명**: `{korean_name}` (`{market}`)\n"
                    f"💰 **현재가**: `{current_price:,.0f}원` (`+{change_rate:.2f}%`)\n\n"
                    f"🎯 **1차 목표**: `{tp1:,.0f}원` (`+{((tp1-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **2차 목표**: `{tp2:,.0f}원` (`+{((tp2-current_price)/current_price)*100:.1f}%`)\n"
                    f"🎯 **3차 목표**: `{tp3:,.0f}원` (`+{((tp3-current_price)/current_price)*100:.1f}%`)\n"
                    f"🛑 **손절가**: `{sl:,.0f}원` (`{((sl-current_price)/current_price)*100:.1f}%`)\n\n"
                    f"📊 **돌파 근거**:\n"
                    f"• 15분봉 기준 평소 대비 **{vol_ratio:.1f}배** 거래량 광속 유입\n"
                    f"• 볼린저밴드 상단 강한 돌파 및 장대양봉 발생"
                )
                notifications.append(new_msg)

        except Exception as e:
            pass

    for msg in notifications:
        send_telegram(msg)

    save_cache(tracked_cache)
    print("15분봉 스캔 완료.")
