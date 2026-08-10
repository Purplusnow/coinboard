#!/usr/bin/env python3
"""Δ김프 검정 v3 — 마이크로구조 편향 제거.

핵심 검정: 신호 시점 t의 가격이 미래 수익률 시작가로도 쓰이면, 체결이 매수/매도
호가 어디에 찍혔느냐만으로 가짜 음의 상관이 생긴다(bid-ask bounce).
t와 수익률 시작점 사이에 GAP시간을 비워서 같은 가격이 양쪽에 들어가지 않게 한다.
GAP을 두고도 효과가 남으면 진짜, 사라지면 착시.

부가 검정:
  - 바이낸스(글로벌) 수익률 예측력: 국내 프리미엄이 글로벌 가격을 예측하면 안 된다.
  - 유동성 구간별: 스프레드가 넓은 소형주에서만 나오면 마이크로구조 편향이다.
캔들은 디스크에 캐시해 재분석을 빠르게 한다.
"""
import json, math, os, statistics as st, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

UP = "https://api.upbit.com/v1"
BI = "https://api.binance.com/api/v3"
BARS = 1000
MIN_TURNOVER = 5e8
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kimp_cache.json")


def get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.7 * (i + 1))


def upbit_candles(market, bars=BARS):
    out, to = {}, None
    while len(out) < bars:
        u = f"{UP}/candles/minutes/60?market={market}&count=200" + (f"&to={to}" if to else "")
        d = get(u)
        if not d:
            break
        for c in d:
            out[c["candle_date_time_utc"][:13]] = c["trade_price"]
        to = d[-1]["candle_date_time_utc"]
        time.sleep(0.14)
        if len(d) < 200:
            break
    return out


def binance_klines(sym, bars=BARS):
    d = get(f"{BI}/klines?symbol={sym}USDT&interval=1h&limit={bars}")
    return {time.strftime("%Y-%m-%dT%H", time.gmtime(k[0] / 1000)): float(k[4]) for k in d}


def build_cache():
    up_all = get(f"{UP}/ticker/all?quote_currencies=KRW")
    bi_info = get(f"{BI}/exchangeInfo?permissions=SPOT")
    bi_syms = {x["baseAsset"] for x in bi_info["symbols"]
               if x["quoteAsset"] == "USDT" and x["status"] == "TRADING"}
    turn = {t["market"].split("-")[1]: t["acc_trade_price_24h"] for t in up_all}
    uni = [s for s in turn if s != "USDT" and s in bi_syms and turn[s] >= MIN_TURNOVER]
    print(f"유니버스 {len(uni)}종목 수집 중…", file=sys.stderr)

    fx = upbit_candles("KRW-USDT")

    def load(s):
        try:
            return s, upbit_candles(f"KRW-{s}"), binance_klines(s)
        except Exception as e:
            print(f"  ! {s}: {e}", file=sys.stderr)
            return s, None, None

    data = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for s, u, b in ex.map(load, uni):
            if u and b:
                data[s] = {"up": u, "bi": b}
    obj = {"fx": fx, "turnover": turn, "data": data, "built": time.time()}
    with open(CACHE, "w") as f:
        json.dump(obj, f)
    return obj


def rank(v):
    n = len(v)
    order = sorted(range(n), key=lambda i: v[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    if len(xs) < 8:
        return None
    rx, ry = rank(xs), rank(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def stat(ics):
    if not ics:
        return "표본없음"
    m, sd = st.mean(ics), st.pstdev(ics)
    t = m / (sd / math.sqrt(len(ics))) if sd else 0
    return f"IC {m:+.4f} (t={t:+6.2f}, n={len(ics):3d}, 양수 {sum(1 for x in ics if x>0)/len(ics)*100:3.0f}%)"


def main():
    if os.path.exists(CACHE):
        obj = json.load(open(CACHE))
        print(f"캐시 사용 ({(time.time()-obj['built'])/60:.0f}분 전 수집)", file=sys.stderr)
    else:
        obj = build_cache()

    fx, turn, data = obj["fx"], obj["turnover"], obj["data"]

    prem, up_px, bi_px = {}, {}, {}
    for s, d in data.items():
        u, b = d["up"], d["bi"]
        keys = sorted(set(u) & set(b) & set(fx))
        if len(keys) < 300:
            continue
        p = {k: (u[k] / (b[k] * fx[k]) - 1) * 100 for k in keys}
        if abs(st.median(p.values())) > 5:
            continue
        prem[s], up_px[s], bi_px[s] = p, {k: u[k] for k in keys}, {k: b[k] for k in keys}

    times = sorted(set.intersection(*[set(v) for v in prem.values()]))
    print(f"검정 대상 {len(prem)}종목 / 시각 {len(times)}개 "
          f"({times[0]} ~ {times[-1]}, {len(times)/24:.1f}일)\n", file=sys.stderr)

    def run(lb, H, gap, px=up_px, subset=None):
        """신호: prem[t]-prem[t-lb].  수익률: px[t+gap] → px[t+gap+H]."""
        syms = subset or list(prem)
        ics, spreads = [], []
        stride = max(1, H)
        for i in range(lb, len(times) - H - gap, stride):
            t0, tb = times[i], times[i - lb]
            ts, te = times[i + gap], times[i + gap + H]
            sig, fwd = [], []
            for s in syms:
                try:
                    sig.append(prem[s][t0] - prem[s][tb])
                    fwd.append((px[s][te] / px[s][ts] - 1) * 100)
                except KeyError:
                    continue
            if len(fwd) < 15:
                continue
            m = st.mean(fwd)
            fwdn = [x - m for x in fwd]
            ic = spearman(sig, fwdn)
            if ic is not None:
                ics.append(ic)
            k = max(2, len(fwd) // 5)
            order = sorted(range(len(fwd)), key=lambda j: sig[j])
            spreads.append(st.mean([fwdn[j] for j in order[:k]]) -
                           st.mean([fwdn[j] for j in order[-k:]]))
        return ics, spreads

    print("=" * 74)
    print("검정 1: 신호-수익률 사이 시차(GAP)  — bid-ask bounce 편향 제거")
    print("=" * 74)
    for H in (1, 4):
        print(f"\n  [지평 {H}h / 신호 4h]")
        for gap in (0, 1, 2, 3):
            ics, sp = run(4, H, gap)
            m = st.mean(sp) if sp else 0
            tag = "  ← 같은 가격 양쪽 사용(편향 있음)" if gap == 0 else ""
            print(f"    GAP {gap}h  {stat(ics)}  분위스프레드 {m:+.3f}%p{tag}")

    print("\n" + "=" * 74)
    print("검정 2: 국내 프리미엄이 '글로벌(바이낸스)' 가격도 예측하는가")
    print("       → 예측하면 안 된다. 예측하면 신호가 아니라 공통 요인이다.")
    print("=" * 74)
    for gap in (0, 1):
        ics_u, _ = run(4, 4, gap, up_px)
        ics_b, _ = run(4, 4, gap, bi_px)
        print(f"  GAP {gap}h  업비트 수익률 {stat(ics_u)}")
        print(f"          바이낸스 수익률 {stat(ics_b)}")

    print("\n" + "=" * 74)
    print("검정 3: 유동성 구간별 — 소형주에서만 나오면 마이크로구조 편향")
    print("=" * 74)
    ranked = sorted(prem, key=lambda s: turn.get(s, 0), reverse=True)
    half = len(ranked) // 2
    for name, sub in (("대형(거래대금 상위 1/2)", ranked[:half]),
                      ("소형(하위 1/2)", ranked[half:])):
        for gap in (0, 1):
            ics, sp = run(4, 4, gap, up_px, sub)
            print(f"  {name:<22} GAP {gap}h  {stat(ics)}  스프레드 {st.mean(sp) if sp else 0:+.3f}%p")


if __name__ == "__main__":
    main()
