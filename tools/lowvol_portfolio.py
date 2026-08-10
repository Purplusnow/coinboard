#!/usr/bin/env python3
"""저변동성 규칙을 실제 포트폴리오로 돌리면 얼마인가 — IC를 사용자 언어로 번역.

규칙: 7일마다 30일 실현변동성이 낮은 하위 20%를 동일가중 매수. 업비트 원화 기준.
비용: 왕복 0.2% (0.05%×2 + 슬리피지), 실제 교체된 비중에만 부과.
비교군: (a) 전 종목 동일가중 = '아무 알트나 골고루 산 사람', (b) 비트코인 보유,
        (c) 고변동 상위 20% = '급등주 쫓아다닌 사람'
"""
import json, math, os, statistics as st, sys

HERE = os.path.dirname(os.path.abspath(__file__))
UCACHE = os.path.join(HERE, "upbit_daily_cache.json")
FEE = 0.002          # 왕복
REBAL = 7
QUINTILE = 0.2


def main():
    c = json.load(open(UCACHE))
    up = c["data"]
    dates = sorted(set().union(*[set(v) for v in up.values()]))
    px = {s: {d: v[0] for d, v in u.items()} for s, u in up.items()}

    def med_abs(s):
        ds = sorted(px[s])
        r = [abs(math.log(px[s][ds[i]] / px[s][ds[i - 1]]))
             for i in range(1, len(ds)) if px[s][ds[i - 1]] > 0]
        return st.median(r) * 100 if r else 0

    syms = [s for s in px if len(px[s]) >= 200 and med_abs(s) >= 0.15]
    print(f"유니버스 {len(syms)}종목 · {len(dates)}일 ({dates[0]} ~ {dates[-1]})", file=sys.stderr)

    def vol30(s, i):
        r = []
        for j in range(i - 29, i + 1):
            a, b = px[s].get(dates[j]), px[s].get(dates[j - 1])
            if a and b and b > 0:
                r.append(math.log(a / b))
        if len(r) < 20:
            return None
        m = sum(r) / len(r)
        return math.sqrt(sum((x - m) ** 2 for x in r) / len(r))

    def simulate(pick):
        """pick(cands) -> 보유 종목 리스트. 자산가치 시계열과 주간 수익률 반환."""
        val, prev, weekly = 1.0, [], []
        for i in range(35, len(dates) - REBAL, REBAL):
            cands = []
            for s in syms:
                v = vol30(s, i)
                a, b = px[s].get(dates[i]), px[s].get(dates[i + REBAL])
                if v is None or not a or not b:
                    continue
                cands.append((v, s, (b / a - 1)))
            if len(cands) < 20:
                continue
            held = pick(cands)
            if not held:
                continue
            r = sum(x[2] for x in held) / len(held)
            names = {x[1] for x in held}
            turn = 1.0 if not prev else len(names - prev) / len(names)
            r -= FEE * turn
            prev = names
            val *= (1 + r)
            weekly.append(r)
        return val, weekly

    def lowq(c):
        c = sorted(c)
        return c[:max(3, int(len(c) * QUINTILE))]

    def highq(c):
        c = sorted(c)
        return c[-max(3, int(len(c) * QUINTILE)):]

    strategies = [
        ("저변동 하위 20%", lowq),
        ("고변동 상위 20%", highq),
        ("전 종목 동일가중", lambda c: c),
    ]

    years = (len(dates) - 35) / 365.25
    print(f"\n{'전략':<20}{'누적':>10}{'연율':>10}{'주간승률':>10}{'MDD':>9}{'주간표준편차':>13}")
    print("-" * 72)
    res = {}
    for name, fn in strategies:
        val, w = simulate(fn)
        cagr = (val ** (1 / years) - 1) * 100
        # MDD
        peak, mdd, v = 1.0, 0.0, 1.0
        for r in w:
            v *= (1 + r)
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        res[name] = (val, cagr, w)
        print(f"{name:<20}{(val-1)*100:+9.1f}%{cagr:+9.1f}%"
              f"{sum(1 for x in w if x>0)/len(w)*100:9.0f}%{mdd*100:8.1f}%"
              f"{st.pstdev(w)*100:12.2f}%")

    # 비트코인 단순 보유
    if "BTC" in px:
        a, b = px["BTC"].get(dates[35]), px["BTC"].get(dates[-1])
        if a and b:
            v = b / a
            print(f"{'비트코인 보유':<20}{(v-1)*100:+9.1f}%{(v**(1/years)-1)*100:+9.1f}%")

    lo, hi, eq = res["저변동 하위 20%"], res["고변동 상위 20%"], res["전 종목 동일가중"]
    print(f"\n같은 기간({years:.1f}년) 요약")
    print(f"  저변동 vs 전종목평균 : 연 {lo[1]-eq[1]:+.1f}%p")
    print(f"  저변동 vs 고변동     : 연 {lo[1]-hi[1]:+.1f}%p")

    # 초과수익의 통계적 유의성 (주간, 벤치마크 대비)
    n = min(len(lo[2]), len(eq[2]))
    d = [lo[2][i] - eq[2][i] for i in range(n)]
    t = st.mean(d) / (st.pstdev(d) / math.sqrt(n)) if st.pstdev(d) else 0
    print(f"  주간 초과수익 평균 {st.mean(d)*100:+.3f}%p (t={t:+.2f}, n={n}, "
          f"승률 {sum(1 for x in d if x>0)/n*100:.0f}%)")


if __name__ == "__main__":
    main()
