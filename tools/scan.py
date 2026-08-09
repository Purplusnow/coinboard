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
