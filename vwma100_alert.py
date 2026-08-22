"""
VWMA(100) 신규 돌파 알림 봇 — Bitget USDT 무기한선물 (크립토만, RWA 제외)

조건:
1. 일봉 최신 봉의 몸통(시가·종가 둘 다)이 VWMA(100) 위
2. 최근 20거래일 이내에 몸통이 선 위가 아니었던 적 있음 (신규 돌파)
3. 그 중 VWMA100 대비 +2% ~ +10% 구간만 (막 돌파한 "신선한" 구간)

vwma100_alert_state.json에 "이미 알림 보낸 심볼"을 기록해서, 같은 돌파를 반복 알림하지 않음.
- 심볼이 2~10% 구간에서 벗어나면(10% 초과로 더 올라가거나, 다시 밴드 밑으로 빠지면) state에서 제거
  -> 나중에 다시 2~10% 구간으로 들어오면(예: 되돌림 후 재돌파) 다시 알림 가능

GitHub Actions에서 30분마다 실행 (.github/workflows/scan.yml).
읽기 전용 — 주문 없음, 공개 시세 데이터만 사용.
"""

import sys
import os
import json
import ccxt
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

VWMA_LEN = 100
LOOKBACK_DAYS = 20
FETCH_LIMIT = VWMA_LEN + LOOKBACK_DAYS + 5
PCT_MIN = 2.0
PCT_MAX = 10.0

STATE_PATH = os.path.join(os.path.dirname(__file__), "vwma100_alert_state.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[경고] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 전송 생략")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.status_code != 200:
            print(f"[실패] 텔레그램 전송: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[실패] 텔레그램 전송: {type(e).__name__}: {e}")


def fetch_ohlcv_paginated(ex, symbol, timeframe, total_needed):
    day_ms = 24 * 60 * 60 * 1000
    since = ex.milliseconds() - (total_needed + 5) * day_ms
    all_rows, seen_ts = [], set()
    for _ in range(6):
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=200)
        if not batch:
            break
        new_rows = [r for r in batch if r[0] not in seen_ts]
        if not new_rows:
            break
        all_rows.extend(new_rows)
        seen_ts.update(r[0] for r in new_rows)
        since = batch[-1][0] + day_ms
        if len(all_rows) >= total_needed or since > ex.milliseconds():
            break
    all_rows.sort(key=lambda r: r[0])
    return all_rows


def compute_vwma_series(ohlcv, length):
    vwma = {}
    for i in range(length - 1, len(ohlcv)):
        window = ohlcv[i - length + 1: i + 1]
        vol_sum = sum(row[5] for row in window)
        if vol_sum == 0:
            continue
        vwma[i] = sum(row[4] * row[5] for row in window) / vol_sum
    return vwma


def is_rwa(market):
    return market.get("info", {}).get("isRwa") == "YES"


def check_symbol(ex, symbol):
    try:
        ohlcv = fetch_ohlcv_paginated(ex, symbol, "1d", FETCH_LIMIT)
    except Exception:
        return None
    if len(ohlcv) < VWMA_LEN + 2:
        return None

    vwma = compute_vwma_series(ohlcv, VWMA_LEN)
    last_idx = len(ohlcv) - 1
    if last_idx not in vwma:
        return None

    last_o, last_c = ohlcv[last_idx][1], ohlcv[last_idx][4]
    last_body_low = min(last_o, last_c)
    last_vwma = vwma[last_idx]

    if not (last_body_low > last_vwma):
        return None

    was_below_recently = False
    start = max(VWMA_LEN - 1, last_idx - LOOKBACK_DAYS)
    for i in range(start, last_idx):
        if i not in vwma:
            continue
        o, c = ohlcv[i][1], ohlcv[i][4]
        if not (min(o, c) > vwma[i]):
            was_below_recently = True
            break

    if not was_below_recently:
        return None

    pct_above = (last_c - last_vwma) / last_vwma * 100
    return {"symbol": symbol, "close": last_c, "vwma100": last_vwma, "pct_above": pct_above}


def main():
    ex = ccxt.bitget({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    markets = ex.load_markets()
    symbols = [
        m["symbol"] for m in markets.values()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True) and not is_rwa(m)
    ]

    state = load_state()
    now_in_range = {}

    for sym in symbols:
        info = check_symbol(ex, sym)
        if info and PCT_MIN <= info["pct_above"] <= PCT_MAX:
            now_in_range[sym] = info

    new_alerts = [info for sym, info in now_in_range.items() if sym not in state]

    for info in sorted(new_alerts, key=lambda x: x["pct_above"]):
        sym_short = info["symbol"].replace("/USDT:USDT", "")
        msg = (
            f"[VWMA100 신규 돌파] {sym_short}\n"
            f"종가: {info['close']:.8g}\n"
            f"VWMA100: {info['vwma100']:.8g}\n"
            f"위로: {info['pct_above']:.2f}%"
        )
        send_telegram(msg)
        print(msg)

    # state 갱신: 지금 범위 안에 있는 것만 유지 (범위 이탈하면 제거 -> 재진입시 다시 알림)
    save_state({sym: info for sym, info in now_in_range.items()})

    print(f"\n완료. 현재 2~10% 구간: {len(now_in_range)}개, 신규 알림: {len(new_alerts)}개")


if __name__ == "__main__":
    main()
