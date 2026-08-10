#!/usr/bin/env python3
"""팩터 스터디 — 앞선 검정의 얕은 점(3신호/10일/단기지평)을 정면으로 보완.

바뀐 것:
  기간   10.5일  →  최대 1000일 (바이낸스 일봉은 호출 1회로 1000일)
  지평   1~12h   →  1d / 3d / 7d
  신호   3개     →  12개 (반전·모멘텀·변동성·유동성·거래량·테이커·펀딩비)
  데이터 현물만   →  선물 펀딩비 추가 (레버리지 쏠림, 현물에 없는 정보)

검정 규약은 그대로 유지한다:
  - 시장 중립화 (횡단면 평균 차감)
  - 비중첩 표본 (stride = 지평)
  - GAP 1일 (신호 종가가 수익률 시작가로 쓰이는 편향 제거) — GAP0도 같이 찍어 비교
  - 전/후반 분할로 안정성 확인
  - 분위 스프레드를 왕복 수수료와 함께 표시
"""
import json, math, os, statistics as st, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BI = "https://api.binance.com/api/v3"
FAPI = "https://fapi.binance.com/fapi/v1"
UP = "https://api.upbit.com/v1"
DAYS = 1000
FEE = 0.20          # 왕복 (현물 0.05%×2 + 슬리피지 여유)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_cache.json")


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.6 * (i + 1))


def build():
    up = get(f"{UP}/ticker/all?quote_currencies=KRW")
    info = get(f"{BI}/exchangeInfo?permissions=SPOT")
    bi_syms = {x["baseAsset"] for x in info["symbols"]
               if x["quoteAsset"] == "USDT" and x["status"] == "TRADING"}
    # 업비트 KRW에 상장된 것만 = 이 사이트가 실제로 다루는 유니버스
    uni = sorted({t["market"].split("-")[1] for t in up} & bi_syms - {"USDT"})
    print(f"유니버스 {len(uni)}종목 (업비트 KRW ∩ 바이낸스 USDT)", file=sys.stderr)

    def load(s):
        try:
            k = get(f"{BI}/klines?symbol={s}USDT&interval=1d&limit={DAYS}")
            rows = [{
                "t": time.strftime("%Y-%m-%d", time.gmtime(x[0] / 1000)),
                "c": float(x[4]), "h": float(x[2]), "l": float(x[3]),
                "qv": float(x[7]), "tbq": float(x[10]), "n": int(x[8]),
            } for x in k]
        except Exception as e:
            print(f"  ! {s} klines: {e}", file=sys.stderr)
            return s, None, None
        fund = None
        try:                                    # 선물 미상장 종목은 그냥 없음 처리
            f = get(f"{FAPI}/fundingRate?symbol={s}USDT&limit=1000")
            agg = {}
            for x in f:
                d = time.strftime("%Y-%m-%d", time.gmtime(x["fundingTime"] / 1000))
                agg.setdefault(d, []).append(float(x["fundingRate"]))
            fund = {d: sum(v) / len(v) for d, v in agg.items()}
        except Exception:
            pass
        return s, rows, fund

    data = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for s, rows, fund in ex.map(load, uni):
            if rows and len(rows) >= 200:
                data[s] = {"k": rows, "f": fund}
    n_f = sum(1 for v in data.values() if v["f"])
    print(f"수집 {len(data)}종목 (펀딩비 있음 {n_f}종목)", file=sys.stderr)
    json.dump({"data": data, "built": time.time()}, open(CACHE, "w"))
    return {"data": data}


def rank(v):
    n = len(v)
    order = sorted(range(n), key=lambda i: v[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(xs, ys):
    if len(xs) < 10:
        return None
    rx, ry = rank(xs), rank(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def main():
    obj = json.load(open(CACHE)) if os.path.exists(CACHE) else build()
    data = obj["data"]

    # 날짜축은 합집합으로. 교집합을 쓰면 신규 상장 코인 하나가 전체 표본을 잘라먹는다
    # (교집합 201일 vs 합집합 1000일). 종목별로 데이터가 있는 날만 횡단면에 참여시키고,
    # 한 시점에 20종목 미만이면 그 시점을 버린다.
    dates = sorted(set().union(*[{r["t"] for r in v["k"]} for v in data.values()]))
    syms = list(data)
    cover = [sum(1 for s in syms if any(r["t"] == dates[0] for r in data[s]["k"][:1]))]
    print(f"날짜축 {len(dates)}일 ({dates[0]} ~ {dates[-1]})", file=sys.stderr)
    hist = sorted(len(v["k"]) for v in data.values())
    print(f"종목별 보유일수 중앙값 {hist[len(hist)//2]}일 / 최대 {hist[-1]}일\n", file=sys.stderr)

    S = {}
    for s in syms:
        m = {r["t"]: r for r in data[s]["k"]}
        S[s] = [m.get(d) for d in dates]

    def px(s, i):
        r = S[s][i]
        return r["c"] if r else None

    def ret(s, i, n):
        a, b = px(s, i), px(s, i - n)
        return (a / b - 1) * 100 if a and b else None

    def vol(s, i, n):
        rs = []
        for j in range(i - n + 1, i + 1):
            a, b = px(s, j), px(s, j - 1)
            if a and b:
                rs.append(math.log(a / b))
        return st.pstdev(rs) * 100 if len(rs) >= n // 2 else None

    def qv(s, i, n):
        v = [S[s][j]["qv"] for j in range(i - n + 1, i + 1) if S[s][j]]
        return st.mean(v) if len(v) >= n // 2 else None

    def taker(s, i, n):
        rs = [S[s][j]["tbq"] / S[s][j]["qv"]
              for j in range(i - n + 1, i + 1) if S[s][j] and S[s][j]["qv"] > 0]
        return st.mean(rs) if len(rs) >= n // 2 else None

    def funding(s, i, n):
        f = data[s].get("f")
        if not f:
            return None
        v = [f[dates[j]] for j in range(i - n + 1, i + 1) if dates[j] in f]
        return st.mean(v) * 1e4 if len(v) >= n // 2 else None   # bp

    def amihud(s, i, n):
        v = []
        for j in range(i - n + 1, i + 1):
            a, b = px(s, j), px(s, j - 1)
            if a and b and S[s][j] and S[s][j]["qv"] > 0:
                v.append(abs(math.log(a / b)) / S[s][j]["qv"] * 1e9)
        return st.mean(v) if len(v) >= n // 2 else None

    SIGNALS = {
        "반전 1d (−수익률)":      lambda s, i: (lambda r: -r if r is not None else None)(ret(s, i, 1)),
        "반전 7d (−수익률)":      lambda s, i: (lambda r: -r if r is not None else None)(ret(s, i, 7)),
        "모멘텀 30d":            lambda s, i: ret(s, i, 30),
        "모멘텀 90d":            lambda s, i: ret(s, i, 90),
        "저변동성 (−vol30)":      lambda s, i: (lambda v: -v if v is not None else None)(vol(s, i, 30)),
        "거래대금 (log qv7)":     lambda s, i: (lambda v: math.log(v) if v else None)(qv(s, i, 7)),
        "거래대금 급증 7d/30d":   lambda s, i: (lambda a, b: a / b if a and b else None)(qv(s, i, 7), qv(s, i, 30)),
        "테이커매수비중 7d":       lambda s, i: taker(s, i, 7),
        "Δ테이커 7d−30d":        lambda s, i: (lambda a, b: a - b if a is not None and b is not None else None)(taker(s, i, 7), taker(s, i, 30)),
        "비유동성 (Amihud 30d)":  lambda s, i: amihud(s, i, 30),
        "펀딩비 7d (bp)":        lambda s, i: funding(s, i, 7),
        "Δ펀딩비 7d−30d":        lambda s, i: (lambda a, b: a - b if a is not None and b is not None else None)(funding(s, i, 7), funding(s, i, 30)),
    }

    def run(fn, H, gap, lo=None, hi=None):
        ics, sps = [], []
        start = max(95, gap)
        rng = range(start, len(dates) - H - gap, H)
        for i in rng:
            if lo is not None and not (lo <= i < hi):
                continue
            sig, fwd = [], []
            for s in syms:
                v = fn(s, i)
                a, b = px(s, i + gap), px(s, i + gap + H)
                if v is None or not a or not b:
                    continue
                sig.append(v)
                fwd.append((b / a - 1) * 100)
            if len(fwd) < 20:
                continue
            m = st.mean(fwd)
            fwdn = [x - m for x in fwd]
            c = spearman(sig, fwdn)
            if c is not None:
                ics.append(c)
            q = max(3, len(fwd) // 5)
            o = sorted(range(len(fwd)), key=lambda j: sig[j])
            sps.append(st.mean([fwdn[j] for j in o[-q:]]) - st.mean([fwdn[j] for j in o[:q]]))
        return ics, sps

    def tstat(v):
        if not v:
            return 0
        sd = st.pstdev(v)
        return st.mean(v) / (sd / math.sqrt(len(v))) if sd else 0

    # ── 스테이블코인 배제 검정 ────────────────────────────────────────────
    # 시장중립(평균 차감) 기준에서 스테이블코인은 수익률이 상시 0이라, 알트 평균이
    # 내린 기간에는 자동으로 '평균 초과'가 된다. 저변동성 IC가 전부 이것 때문일 수 있다.
    def med_abs_ret(s):
        v = []
        for i in range(1, len(S[s])):
            a, b = px(s, i), px(s, i - 1)
            if a and b:
                v.append(abs(math.log(a / b)) * 100)
        return st.median(v) if v else 0

    mar = {s: med_abs_ret(s) for s in syms}
    stables = [s for s in syms if mar[s] < 0.15]
    print(f"스테이블코인 판정({len(stables)}종목, 일간 |수익률| 중앙값 <0.15%): "
          f"{', '.join(stables)}", file=sys.stderr)

    all_syms = list(syms)
    lowvol_tail = sorted(syms, key=lambda s: mar[s])[:max(1, len(syms) // 10)]
    UNIVERSES = [
        ("전체", all_syms),
        ("스테이블 제외", [s for s in all_syms if s not in set(stables)]),
        ("스테이블+최저변동 10% 제외", [s for s in all_syms if s not in set(lowvol_tail)]),
    ]

    print("\n" + "=" * 92)
    print("저변동성 팩터 — 유니버스별 재검정 (스테이블코인 착시 여부)")
    print("=" * 92)
    print(f"  {'유니버스':<26}{'종목':>6}{'지평':>6}{'IC':>10}{'t':>8}{'양수':>7}{'스프레드':>11}")
    saved = syms
    for label, sub in UNIVERSES:
        for H in (1, 7):
            syms = sub
            ics, sps = run(SIGNALS["저변동성 (−vol30)"], H, 1)
            syms = saved
            if not ics:
                continue
            print(f"  {label:<26}{len(sub):>6}{H:>5}d{st.mean(ics):+10.4f}"
                  f"{tstat(ics):+8.2f}{sum(1 for x in ics if x>0)/len(ics)*100:6.0f}%"
                  f"{st.mean(sps):+10.2f}%p")
    print()

    for H in (1, 3, 7):
        print("=" * 92)
        print(f"예측 지평 {H}일   (시장중립 · 비중첩 · GAP 1일 · 왕복비용 {FEE}%p)")
        print("=" * 92)
        print(f"  {'신호':<22}{'IC(GAP1)':>12}{'t':>8}{'양수':>7}{'스프레드':>11}"
              f"{'IC(GAP0)':>11}{'전반부':>9}{'후반부':>9}")
        rows = []
        for name, fn in SIGNALS.items():
            ics, sps = run(fn, H, 1)
            if not ics:
                continue
            ics0, _ = run(fn, H, 0)
            half = (95 + len(dates)) // 2
            a, _ = run(fn, H, 1, 95, half)
            b, _ = run(fn, H, 1, half, len(dates))
            rows.append((abs(st.mean(ics)), name, ics, sps, ics0, a, b))
        rows.sort(reverse=True)
        for _, name, ics, sps, ics0, a, b in rows:
            m, t = st.mean(ics), tstat(ics)
            pos = sum(1 for x in ics if x > 0) / len(ics) * 100
            mark = " ★" if abs(t) >= 2.5 and abs(m) >= 0.02 else ""
            print(f"  {name:<22}{m:+12.4f}{t:+8.2f}{pos:6.0f}%{st.mean(sps):+10.2f}%p"
                  f"{st.mean(ics0):+11.4f}{st.mean(a) if a else 0:+9.4f}"
                  f"{st.mean(b) if b else 0:+9.4f}{mark}")
        print(f"  (n={len(rows[0][2]) if rows else 0} 비중첩 시점)   ★ = |t|≥2.5 이고 |IC|≥0.02\n")


if __name__ == "__main__":
    main()
