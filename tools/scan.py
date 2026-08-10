#!/usr/bin/env python3
"""업비트 KRW 마켓 스캐너 — 캔들 수집 → 지표 계산 → 추천 스코어링 → board.json

브라우저에서는 Origin 헤더 때문에 캔들 API가 사실상 1req/s로 막히므로
(group=origin, 즉시 429) 이 작업은 반드시 서버 측(GitHub Actions)에서 돈다.
Origin 없이 호출하면 group=candles 로 분류되어 600req/분 · 10req/초 를 받는다.

표준 라이브러리만 사용한다. Actions에서 pip install 없이 그대로 실행된다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

API = "https://api.upbit.com/v1"
KST = timezone(timedelta(hours=9))

# --- 유니버스 필터 ------------------------------------------------------------
# 두 단계로 나눈다.
#  - 스코어링 유니버스는 넓게(3억+, ~120개). 백분위는 표본이 적으면 왜곡된다.
#  - 실제 추천 노출은 좁게(10억+). 얇은 호가창은 슬리피지·작전 위험이 크다.
MIN_TURNOVER_UNIVERSE = 300_000_000
MIN_TURNOVER_RECO = 1_000_000_000
UNIVERSE_SIZE = 120               # 거래대금 상위 N개만 캔들 수집 (호출 예산 관리)
TOP_N = 12                        # 전광판에 올릴 추천 종목 수

# --- 캔들 ---------------------------------------------------------------------
CANDLE_UNIT = 60                  # 60분봉
CANDLE_COUNT = 200                # 200시간 ≈ 8.3일
REQ_PER_SEC = 8                   # 한도 10/s 대비 여유
SLEEP = 1.0 / REQ_PER_SEC


# ============================================================================
# HTTP
# ============================================================================
def fetch(path: str, retries: int = 4) -> list | dict:
    """Origin 헤더를 절대 붙이지 않는다 (붙으면 오리진 쿼터로 강등된다)."""
    url = f"{API}{path}"
    delay = 0.6
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"unreachable: {path}")


# ============================================================================
# 지표
# ============================================================================
def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI. closes는 오래된 것 → 최신 순."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def atr_pct(highs, lows, closes, period: int = 14) -> float | None:
    """ATR을 종가 대비 %로. 변동성 대비 수익 효율 계산에 쓴다."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    last = closes[-1]
    return (a / last * 100.0) if last else None


def pct_return(closes: list[float], bars_back: int) -> float | None:
    if len(closes) <= bars_back:
        return None
    past = closes[-1 - bars_back]
    if not past:
        return None
    return (closes[-1] / past - 1.0) * 100.0


def pct_rank(values: list[float | None]) -> list[float]:
    """횡단면 백분위(0~1). None은 중앙값(0.5) 취급.

    임의의 상수 임계값 대신 그날의 시장 분포 안에서 상대평가한다.
    시장 전체가 오른 날 모든 코인이 만점을 받는 왜곡을 막는다.
    """
    idx = [i for i, v in enumerate(values) if v is not None and not math.isnan(v)]
    out = [0.5] * len(values)
    if len(idx) < 2:
        return out
    order = sorted(idx, key=lambda i: values[i])
    n = len(order)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # 동점은 평균 순위를 공유
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = avg_rank / (n - 1)
        i = j + 1
    return out


def rsi_quality(r: float | None) -> float | None:
    """RSI를 '건강한 상승'일수록 높은 0~1 점수로 변환.

    60 부근이 최적. 과매수(>80)는 추격매수 구간이라 급격히 감점하고,
    과매도(<35)는 하락 추세라 감점한다. 추천은 '이미 터진 것'이 아니라
    '터지는 중'을 잡아야 한다.
    """
    if r is None:
        return None
    if r <= 35:
        return max(0.0, (r - 20) / 15 * 0.3)
    if r <= 60:
        return 0.3 + (r - 35) / 25 * 0.7
    if r <= 72:
        return 1.0 - (r - 60) / 12 * 0.25
    if r <= 85:
        return 0.75 - (r - 72) / 13 * 0.65
    return max(0.0, 0.10 - (r - 85) / 15 * 0.10)


# ============================================================================
# 종목별 피처 추출
# ============================================================================
def build_features(market: str, candles: list[dict]) -> dict | None:
    """캔들(최신순으로 옴)을 시간순으로 뒤집어 피처를 만든다."""
    if not candles or len(candles) < 60:
        return None
    c = list(reversed(candles))  # 오래된 것 → 최신
    closes = [x["trade_price"] for x in c]
    highs = [x["high_price"] for x in c]
    lows = [x["low_price"] for x in c]
    turn = [x["candle_acc_trade_price"] for x in c]  # 봉당 거래대금(원)

    e20, e60 = ema(closes, 20), ema(closes, 60)
    last = closes[-1]

    # 거래대금 급증: 최근 3시간 평균 vs 직전 72시간 평균
    recent = turn[-3:]
    base = turn[-75:-3] if len(turn) >= 75 else turn[:-3]
    recent_avg = sum(recent) / len(recent) if recent else 0.0
    base_avg = sum(base) / len(base) if base else 0.0
    vol_surge = (recent_avg / base_avg) if base_avg > 0 else None

    r1 = pct_return(closes, 1)
    r4 = pct_return(closes, 4)
    r24 = pct_return(closes, 24)
    r168 = pct_return(closes, 168)
    rs = rsi(closes, 14)
    av = atr_pct(highs, lows, closes, 14)

    # 추세 정렬: 종가 > EMA20 > EMA60 이면 1.0
    trend = 0.0
    if e20 and e60:
        if last > e20:
            trend += 0.5
        if e20 > e60:
            trend += 0.5

    # 변동성 대비 효율: 같은 24h 상승이어도 덜 흔들리며 오른 쪽이 우수
    efficiency = (r24 / av) if (r24 is not None and av and av > 0) else None

    # 최근 8시간 고점 대비 위치 (눌림 없이 고점 부근이면 1에 근접)
    win = closes[-8:]
    hi, lo = max(win), min(win)
    pos_in_range = ((last - lo) / (hi - lo)) if hi > lo else 0.5

    return {
        "market": market,
        "price": last,
        # 크로스 거래소 분해용 기준가. 바이낸스와 '같은 창'으로 맞춰야 해서
        # 업비트 signed_change_rate(전일종가 대비)는 쓸 수 없다.
        "px_4h": closes[-5] if len(closes) >= 5 else None,
        "px_24h": closes[-25] if len(closes) >= 25 else None,
        "ret_1h": r1,
        "ret_4h": r4,
        "ret_24h": r24,
        "ret_7d": r168,
        "rsi": rs,
        "atr_pct": av,
        "vol_surge": vol_surge,
        "trend": trend,
        "efficiency": efficiency,
        "pos_in_range": pos_in_range,
        "turnover_1h": turn[-1],
    }


# ============================================================================
# 스코어링
# ============================================================================
WEIGHTS = {
    "vol_surge": 0.26,   # 거래대금 급증 = 신규 자금 유입. 가장 선행하는 신호
    "ret_4h": 0.16,      # 단기 모멘텀
    "ret_24h": 0.16,     # 중기 모멘텀
    "trend": 0.18,       # 추세 정렬 (역추세 반등 노이즈 배제)
    "rsi_q": 0.12,       # 과열 감점
    "efficiency": 0.12,  # 변동성 대비 효율
}


def score_universe(feats: list[dict]) -> None:
    """횡단면 백분위 기반 가중합으로 0~100 점수를 매긴다(in-place)."""
    cols = {
        "vol_surge": pct_rank([f["vol_surge"] for f in feats]),
        "ret_4h": pct_rank([f["ret_4h"] for f in feats]),
        "ret_24h": pct_rank([f["ret_24h"] for f in feats]),
        "trend": pct_rank([f["trend"] for f in feats]),
        "rsi_q": pct_rank([rsi_quality(f["rsi"]) for f in feats]),
        "efficiency": pct_rank([f["efficiency"] for f in feats]),
    }
    for i, f in enumerate(feats):
        parts = {k: cols[k][i] for k in WEIGHTS}
        raw = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)

        # --- 과열 캡: 이미 크게 튄 종목은 추격 위험이 커서 상단을 눌러둔다 ---
        penalty = []
        if f["rsi"] is not None and f["rsi"] >= 82:
            raw *= 0.75
            penalty.append("과열")
        if f["ret_24h"] is not None and f["ret_24h"] >= 45:
            raw *= 0.85
            penalty.append("급등후")

        f["score"] = round(raw * 100, 1)
        f["parts"] = {k: round(v * 100) for k, v in parts.items()}
        f["penalty"] = penalty


# ============================================================================
# 크로스 거래소 분해 — 이 사이트의 유일한 차별점
# ============================================================================
# GitHub Actions 러너는 미국 IP라 api.binance.com이 HTTP 451(Unavailable For Legal
# Reasons)로 막힌다. data-api.binance.vision은 바이낸스 공개 데이터 미러라 통과한다.
# 그래도 여기서 실패할 수 있으므로 브라우저가 바이낸스를 직접 부르는 경로가 본선이고,
# 이 스냅샷은 보조다.
BINANCE = "https://data-api.binance.vision/api/v3"
MAX_SANE_KIMP = 5.0   # |김프|가 이보다 크면 티커만 같고 다른 토큰이다 (예: DATA)


def fetch_binance(path: str):
    req = urllib.request.Request(f"{BINANCE}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def cross_exchange(feats: list[dict], fx: dict) -> dict:
    """업비트 수익률을 '글로벌 성분 + 국내 성분'으로 분해한다.

        업비트 원화 수익률 ≈ (1+바이낸스 USDT 수익률)(1+USDT/원 수익률) − 1  +  국내 성분

    국내 성분은 곧 프리미엄의 변화량이다. 예측력은 없다(검정으로 기각됨).
    다만 '지금 이 상승이 글로벌 동반인지 국내 단독인지'라는 사실은
    두 거래소를 동시에 봐야만 나오고, 어느 거래소 앱에도 없다.
    """
    try:
        info = fetch_binance("/exchangeInfo?permissions=SPOT")
    except Exception as e:
        print(f"    ! 바이낸스 조회 실패, 분해 생략: {e}", file=sys.stderr)
        return {}
    bi_syms = {x["baseAsset"] for x in info["symbols"]
               if x["quoteAsset"] == "USDT" and x["status"] == "TRADING"}

    pairs = [(f, f["market"].split("-")[1]) for f in feats]
    pairs = [(f, s) for f, s in pairs if s != "USDT" and s in bi_syms]
    if not pairs:
        return {}
    symbols = [s + "USDT" for _, s in pairs]

    def windowed(size: str) -> dict:
        out = {}
        for i in range(0, len(symbols), 100):     # 심볼당 weight가 붙어 나눠 호출
            chunk = symbols[i:i + 100]
            q = urllib.parse.quote(json.dumps(chunk, separators=(",", ":")))
            for x in fetch_binance(f"/ticker?symbols={q}&windowSize={size}"):
                out[x["symbol"]] = {
                    "chg": float(x["priceChangePercent"]),
                    "last": float(x["lastPrice"]),
                }
            time.sleep(0.3)
        return out

    try:
        w4, w24 = windowed("4h"), windowed("1d")
    except Exception as e:
        print(f"    ! 바이낸스 윈도우 조회 실패: {e}", file=sys.stderr)
        return {}

    # 환율 수익률도 같은 창으로
    fx_now, fx_4h, fx_24h = fx.get("now"), fx.get("h4"), fx.get("h24")
    r_fx_4 = (fx_now / fx_4h - 1) * 100 if fx_now and fx_4h else 0.0
    r_fx_24 = (fx_now / fx_24h - 1) * 100 if fx_now and fx_24h else 0.0

    out, unmatched = {}, 0
    for f, sym in pairs:
        b4, b24 = w4.get(sym + "USDT"), w24.get(sym + "USDT")
        if not b4 or not fx_now:
            continue
        kimp = (f["price"] / (b4["last"] * fx_now) - 1) * 100
        if abs(kimp) > MAX_SANE_KIMP:      # 티커 충돌 방어
            unmatched += 1
            continue

        # 기준가를 여기 같이 실어야 브라우저가 '전체 유니버스'를 실시간 재계산할 수 있다.
        # items(상위 12개)에만 실으면 분해 랭킹이 그 12개 안에서만 뽑힌다.
        rec = {"kimp": round(kimp, 2), "binance": sym,
               "px4h": f.get("px_4h"), "px24h": f.get("px_24h")}
        for label, upx, bwin, rfx in (("4h", f.get("px_4h"), b4, r_fx_4),
                                      ("24h", f.get("px_24h"), b24, r_fx_24)):
            if not upx or not bwin:
                continue
            r_up = (f["price"] / upx - 1) * 100
            r_glob = ((1 + bwin["chg"] / 100) * (1 + rfx / 100) - 1) * 100
            rec[label] = {
                "up": round(r_up, 2),
                "global": round(r_glob, 2),
                "korea": round(r_up - r_glob, 2),
            }
        out[f["market"]] = rec

    print(f"    분해 {len(out)}종목 (티커충돌 제외 {unmatched}개, "
          f"환율 4h {r_fx_4:+.2f}% / 24h {r_fx_24:+.2f}%)", file=sys.stderr)
    return out


def classify(rec: dict, window: str = "4h") -> tuple[str, str] | None:
    """분해 결과를 사람이 읽는 한 줄로. 예측이 아니라 서술이다."""
    d = rec.get(window)
    if not d:
        return None
    up, g, k = d["up"], d["global"], d["korea"]
    if abs(up) < 0.7:
        return ("보합", "flat")
    if up > 0:
        if k > 0 and g <= 0.3:
            return ("국내 단독 상승", "korea")
        if k > g:
            return ("국내 주도 상승", "korea")
        if abs(k) < max(0.5, abs(g) * 0.3):
            return ("글로벌 동반 상승", "global")
        return ("글로벌 주도 상승", "global")
    if k < 0 and g >= -0.3:
        return ("국내 단독 하락", "korea")
    if k < g:
        return ("국내 주도 하락", "korea")
    return ("글로벌 동반 하락", "global")


def _part_label(key: str, f: dict) -> str | None:
    """해당 지표가 이 종목의 강점일 때 붙일 라벨. 실제 수치를 넣는다."""
    if key == "vol_surge":
        vs = f.get("vol_surge")
        if vs is None:
            return None
        return f"거래대금 {vs:.1f}배" if vs >= 1.3 else "거래대금 상위"
    if key == "ret_4h":
        v = f.get("ret_4h")
        return f"4h +{v:.1f}%" if v is not None and v > 0.3 else None
    if key == "ret_24h":
        v = f.get("ret_24h")
        return f"24h +{v:.1f}%" if v is not None and v > 0.5 else None
    if key == "trend":
        return "정배열" if f.get("trend", 0) >= 1.0 else ("추세 회복" if f.get("trend", 0) >= 0.5 else None)
    if key == "rsi_q":
        r = f.get("rsi")
        return f"RSI {r:.0f}" if r is not None else None
    if key == "efficiency":
        e = f.get("efficiency")
        return "저변동 상승" if e is not None and e > 0 else None
    return None


def make_tags(f: dict) -> list[str]:
    """근거 태그. 점수만 보여주면 신뢰가 안 생긴다.

    모든 종목에 같은 문구가 붙으면 정보량이 0이 된다. 그래서 고정 규칙이 아니라
    '이 종목이 유니버스 대비 가장 앞선 지표'를 순서대로 골라 수치와 함께 붙인다.
    """
    parts = f.get("parts") or {}
    ranked = sorted(parts.items(), key=lambda kv: kv[1], reverse=True)

    tags: list[str] = []
    for key, pct in ranked:
        if pct < 55 or len(tags) >= 3:   # 상위 45% 밖이면 강점이라 부를 수 없다
            continue
        lbl = _part_label(key, f)
        if lbl and lbl not in tags:
            tags.append(lbl)

    if f.get("pos_in_range", 0) >= 0.92 and len(tags) < 3:
        tags.append("고점 부근")

    if not tags:                          # 최소 한 개는 붙인다
        r24 = f.get("ret_24h")
        tags.append(f"24h {r24:+.1f}%" if r24 is not None else "관망")

    tags.extend(f.get("penalty", []))
    return tags[:4]


# ============================================================================
# 메인
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/data/board.json")
    ap.add_argument("--universe", type=int, default=UNIVERSE_SIZE)
    ap.add_argument("--top", type=int, default=TOP_N)
    args = ap.parse_args()

    t0 = time.time()

    # 1) 마켓 메타 (한글명 + 투자유의 플래그)
    meta_raw = fetch("/market/all?isDetails=true")
    meta = {}
    for m in meta_raw:
        if not m["market"].startswith("KRW-"):
            continue
        ev = m.get("market_event") or {}
        caution = ev.get("caution") or {}
        meta[m["market"]] = {
            "name": m.get("korean_name") or m["market"],
            "en": m.get("english_name") or "",
            "warning": bool(ev.get("warning")),
            "caution": any(bool(v) for v in caution.values()),
        }
    print(f"[1/4] KRW 마켓 {len(meta)}개", file=sys.stderr)

    # 2) 전체 시세 1회 호출로 거래대금 상위 유니버스 선정
    tickers = fetch("/ticker/all?quote_currencies=KRW")
    tmap = {t["market"]: t for t in tickers}

    cand = []
    for mk, t in tmap.items():
        if mk not in meta:
            continue
        if meta[mk]["warning"]:          # 투자유의 종목은 추천 대상에서 제외
            continue
        if t.get("acc_trade_price_24h", 0) < MIN_TURNOVER_UNIVERSE:
            continue
        cand.append((t["acc_trade_price_24h"], mk))
    cand.sort(reverse=True)
    universe = [mk for _, mk in cand[: args.universe]]
    print(f"[2/4] 유니버스 {len(universe)}개 (거래대금 {MIN_TURNOVER_UNIVERSE/1e8:.0f}억+ 필터)", file=sys.stderr)

    # 3) 캔들 수집 (스로틀링)
    feats, failed = [], []
    for i, mk in enumerate(universe):
        try:
            candles = fetch(f"/candles/minutes/{CANDLE_UNIT}?market={mk}&count={CANDLE_COUNT}")
            f = build_features(mk, candles)
            if f:
                feats.append(f)
            else:
                failed.append(mk)
        except Exception as e:  # 개별 종목 실패가 전체를 죽이지 않게
            failed.append(mk)
            print(f"    ! {mk}: {e}", file=sys.stderr)
        time.sleep(SLEEP)
        if (i + 1) % 40 == 0:
            print(f"    ...{i+1}/{len(universe)}", file=sys.stderr)
    print(f"[3/4] 캔들 {len(feats)}개 성공 / {len(failed)}개 실패 ({time.time()-t0:.0f}s)", file=sys.stderr)

    if len(feats) < 10:
        print("ERROR: 유효 데이터 부족, 기존 board.json 유지", file=sys.stderr)
        return 1

    # 4) 스코어링
    score_universe(feats)
    feats.sort(key=lambda f: f["score"], reverse=True)

    # 점수는 넓은 유니버스에서 매기되, 노출은 유동성 기준을 통과한 것만
    liquid = [f for f in feats
              if tmap.get(f["market"], {}).get("acc_trade_price_24h", 0) >= MIN_TURNOVER_RECO]
    print(f"    추천 후보 {len(liquid)}개 (거래대금 {MIN_TURNOVER_RECO/1e8:.0f}억+)", file=sys.stderr)

    # 30일 실현변동성 — 팩터 스터디에서 유일하게 살아남은 신호
    # (1007일 · 910개 비중첩 표본 · 7일 지평 IC +0.118, 전후반 모두 유지)
    print("[3.5/4] 일봉 수집 (변동성)…", file=sys.stderr)
    # 스테이블코인은 랭킹 최상단을 늘 독점하는데 정보가 없다.
    # (팩터 자체는 이들을 빼도 유지된다 — 7d IC +0.118 → +0.115)
    STABLE = {"USDT", "USDC", "USDE", "RLUSD", "USD1", "DAI", "TUSD", "FDUSD", "BUSD", "PYUSD"}
    vol30, daily = {}, {}
    for f in feats:
        mk = f["market"]
        if mk.split("-")[1] in STABLE:
            continue
        try:
            d = fetch(f"/candles/days?market={mk}&count=31")
            closes = [c["trade_price"] for c in reversed(d)]
            daily[mk] = closes
            rets = [math.log(closes[i] / closes[i - 1])
                    for i in range(1, len(closes)) if closes[i - 1] > 0]
            if len(rets) >= 20:
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / len(rets)
                vol30[mk] = round(math.sqrt(var) * 100, 2)   # 일간 σ(%)
        except Exception:
            pass
        time.sleep(SLEEP)
    if vol30:
        ranked = sorted(vol30.values())
        for mk, v in vol30.items():
            pctile = sum(1 for x in ranked if x < v) / max(1, len(ranked) - 1)
            vol30[mk] = {"vol": v, "pctile": round(pctile * 100)}
        lo = sorted(vol30.items(), key=lambda kv: kv[1]["vol"])[:3]
        desc = ", ".join("{} {}%".format(meta[m]["name"], v["vol"]) for m, v in lo)
        print("    변동성 {}종목 · 최저 {}".format(len(vol30), desc), file=sys.stderr)

    # BTC 대비 상대 성과 — 원화 등락률만 보면 "BTC 대신 이 알트를 들 이유"가 안 보인다.
    # 백테스트에서 알트 동일가중은 2.6년 -77%, BTC 보유는 +54%였다. 그 격차가 이 축의 근거다.
    btcrel, breadth = {}, {}
    btc = daily.get("KRW-BTC")
    if btc and len(btc) >= 31:
        def rel(closes, n):
            if len(closes) <= n or len(btc) <= n or not closes[-1 - n] or not btc[-1 - n]:
                return None, None
            rc = closes[-1] / closes[-1 - n] - 1
            rb = btc[-1] / btc[-1 - n] - 1
            return round(rc * 100, 2), round(((1 + rc) / (1 + rb) - 1) * 100, 2)

        for mk, closes in daily.items():
            r7, x7 = rel(closes, 7)
            r30, x30 = rel(closes, 30)
            if x7 is None:
                continue
            btcrel[mk] = {"r7": r7, "rel7": x7, "r30": r30, "rel30": x30}

        for k, tag in (("rel7", "7d"), ("rel30", "30d")):
            vals = [v[k] for v in btcrel.values() if v.get(k) is not None and v is not btcrel.get("KRW-BTC")]
            if vals:
                breadth[tag] = {"beat": sum(1 for x in vals if x > 0), "total": len(vals)}
        b7 = breadth.get("7d", {})
        print("    BTC 대비 {}종목 · 7일간 BTC를 이긴 코인 {}/{}".format(
            len(btcrel), b7.get("beat", 0), b7.get("total", 0)), file=sys.stderr)

    # 크로스 거래소 분해 (환율은 업비트 KRW-USDT 봉에서 직접)
    fx = {}
    try:
        fxc = list(reversed(fetch("/candles/minutes/60?market=KRW-USDT&count=25")))
        fxp = [c["trade_price"] for c in fxc]
        fx = {"now": fxp[-1],
              "h4": fxp[-5] if len(fxp) >= 5 else None,
              "h24": fxp[-25] if len(fxp) >= 25 else None}
    except Exception as e:
        print(f"    ! 환율 조회 실패: {e}", file=sys.stderr)
    cross = cross_exchange(feats, fx) if fx else {}

    items = []
    for rank, f in enumerate(liquid[: args.top], start=1):
        mk = f["market"]
        t = tmap.get(mk, {})
        items.append({
            "rank": rank,
            "market": mk,
            "symbol": mk.split("-")[1],
            "name": meta[mk]["name"],
            "score": f["score"],
            "price": f["price"],
            "chg24": round((t.get("signed_change_rate") or 0) * 100, 2),
            "turnover24": t.get("acc_trade_price_24h", 0),
            "caution": meta[mk]["caution"],
            "px_4h": f.get("px_4h"),
            "px_24h": f.get("px_24h"),
            "cross": cross.get(mk),
            "verdict": (lambda c: (lambda r: {"text": r[0], "kind": r[1]} if r else None)(classify(c))
                        )(cross[mk]) if mk in cross else None,
            "tags": make_tags(f),
            "parts": f["parts"],
            "metrics": {
                "ret_1h": _r(f["ret_1h"]),
                "ret_4h": _r(f["ret_4h"]),
                "ret_24h": _r(f["ret_24h"]),
                "ret_7d": _r(f["ret_7d"]),
                "rsi": _r(f["rsi"], 1),
                "atr_pct": _r(f["atr_pct"]),
                "vol_surge": _r(f["vol_surge"]),
            },
        })

    now = datetime.now(timezone.utc)
    board = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_kst": now.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "universe_size": len(feats),
        "scanned": len(universe),
        "weights": WEIGHTS,
        "items": items,
        "reco_pool": len(liquid),
        # 환율과 전 종목 분해 결과. 브라우저가 WS 실시간 가격과 합쳐
        # 국내/글로벌 성분을 거의 실시간으로 다시 계산할 수 있게 한다.
        "fx": fx,
        "cross": cross,
        # 업비트만으로 만들 수 있는 기준가. 바이낸스가 막혀 cross가 비어도
        # 브라우저가 이것 + 바이낸스 직접 호출로 분해를 복원할 수 있다.
        "base": {f["market"]: {"px4h": f.get("px_4h"), "px24h": f.get("px_24h")}
                 for f in feats if f.get("px_4h")},
        "vol30": vol30,
        "btcrel": btcrel,
        "breadth": breadth,
        # 화면에 신호를 올릴 때는 측정된 근거를 함께 싣는다
        # 측정 기준은 '업비트 원화 수익률'이다. 바이낸스 USDT 기준으로 재면
        # 7일 IC가 +0.118로 더 좋게 나오지만, 국내 사용자가 실제로 얻는 수익률이 아니다.
        "evidence": {
            # 저변동성 규칙을 실제 포트폴리오로 돌린 결과. IC가 유의해도 수익은 다른 문제다.
            "portfolio": {
                "period": "2023-11-15 ~ 2026-08-10", "years": 2.6, "rebal": "7일",
                "fee": 0.2,
                "lowvol": {"cum": -63.7, "cagr": -31.9, "mdd": -79.5},
                "highvol": {"cum": -93.3, "cagr": -64.0, "mdd": -95.8},
                "equal": {"cum": -77.2, "cagr": -42.8, "mdd": -87.6},
                "btc": {"cum": 54.1, "cagr": 17.8},
                "excess_weekly": 0.215, "excess_t": 1.07,
            },
            "vol30": {
                "ic_1d": 0.0718, "ic_7d": 0.0874,
                "t_1d": 8.34, "t_7d": 3.98,
                "spread_7d": 0.71, "fee": 0.20,
                "samples_1d": 792, "samples_7d": 112,
                "days": 1000, "symbols": 170,
                "period": "2023-11-15 ~ 2026-08-10",
                "basis": "업비트 원화 수익률",
            }
        },
        "watch": [  # 13~24위: 전광판 하단 마퀴에 흘릴 관심 종목
            {"symbol": f["market"].split("-")[1], "name": meta[f["market"]]["name"],
             "score": f["score"], "market": f["market"]}
            for f in liquid[args.top: args.top + 12]
        ],
        # 전 종목 한글명. 브라우저가 /market/all(231KB)을 직접 부르지 않게 하려는 것.
        # 브라우저 호출은 Origin 쿼터(사실상 1req/s)에 걸리므로 호출 수를 최소화한다.
        "names": {mk: m["name"] for mk, m in meta.items()},
    }

    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(board, fp, ensure_ascii=False, separators=(",", ":"))
    print(f"[4/4] {out} 저장 ({os.path.getsize(out)/1024:.1f}KB, 총 {time.time()-t0:.0f}s)", file=sys.stderr)

    for it in items[:5]:
        print(f"    #{it['rank']} {it['name']:<12} {it['score']:>5}점  "
              f"24h {it['chg24']:+.2f}%  {' · '.join(it['tags'])}", file=sys.stderr)
    return 0


def _r(v, nd=2):
    return None if v is None else round(v, nd)


if __name__ == "__main__":
    sys.exit(main())
