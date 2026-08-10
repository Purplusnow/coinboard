#!/usr/bin/env python3
"""기각 1~3 재검정 — 이번엔 10.5일이 아니라 1007일로.

앞선 검정에서 Δ김프·테이커·국내거래비중을 기각했지만, 표본이 10.5일뿐이었다.
비유동성 팩터가 201일에서 t=-3.55였다가 1007일에서 소멸한 사례를 보면,
짧은 표본은 없는 것을 만들기도 하고 있는 것을 가리기도 한다. 그래서 다시 본다.

정합성: 업비트 일봉 candle_date_time_utc가 00:00:00 = UTC 일 경계이고,
바이낸스 klines 기본값도 UTC라 그대로 정렬된다. BTC 김프가 0% 근처인지로 실증한다.
"""
import json, math, os, statistics as st, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

UP = "https://api.upbit.com/v1"
DAYS = 1000
HERE = os.path.dirname(os.path.abspath(__file__))
FCACHE = os.path.join(HERE, "factor_cache.json")
UCACHE = os.path.join(HERE, "upbit_daily_cache.json")


def get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.7 * (i + 1))


def upbit_daily(market, bars=DAYS):
    out, to = {}, None
    while len(out) < bars:
        u = f"{UP}/candles/days?market={market}&count=200" + (f"&to={to}" if to else "")
        d = get(u)
        if not d:
            break
        for c in d:
            out[c["candle_date_time_utc"][:10]] = (c["trade_price"], c["candle_acc_trade_price"])
        to = d[-1]["candle_date_time_utc"]
        time.sleep(0.14)
        if len(d) < 200:
            break
    return out


def load_upbit(syms):
    if os.path.exists(UCACHE):
        c = json.load(open(UCACHE))
        print(f"업비트 캐시 사용 ({(time.time()-c['built'])/60:.0f}분 전)", file=sys.stderr)
        return c["fx"], c["data"]
    print(f"업비트 일봉 수집 {len(syms)}종목 (약 2~3분)…", file=sys.stderr)
    fx = upbit_daily("KRW-USDT")

    def load(s):
        try:
            return s, upbit_daily(f"KRW-{s}")
        except Exception as e:
            print(f"  ! {s}: {e}", file=sys.stderr)
            return s, None

    data = {}
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for s, d in ex.map(load, syms):
            done += 1
            if d and len(d) >= 200:
                data[s] = d
            if done % 50 == 0:
                print(f"    …{done}/{len(syms)}", file=sys.stderr)
    json.dump({"fx": fx, "data": data, "built": time.time()}, open(UCACHE, "w"))
    return fx, data


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


def tstat(v):
    if not v:
        return 0
    sd = st.pstdev(v)
    return st.mean(v) / (sd / math.sqrt(len(v))) if sd else 0


def main():
    fc = json.load(open(FCACHE))["data"]
    bi = {s: {r["t"]: r for r in v["k"]} for s, v in fc.items()}
    fx_raw, up_raw = load_upbit(sorted(fc))

    fx = {d: v[0] for d, v in fx_raw.items()}
    dates = sorted(set().union(*[set(v) for v in up_raw.values()]))
    print(f"\n날짜축 {len(dates)}일 ({dates[0]} ~ {dates[-1]})", file=sys.stderr)

    prem, upx, kshare, upturn = {}, {}, {}, {}
    dropped = 0
    for s, u in up_raw.items():
        b = bi.get(s)
        if not b:
            continue
        keys = [d for d in dates if d in u and d in b and d in fx]
        if len(keys) < 200:
            continue
        p = {d: (u[d][0] / (b[d]["c"] * fx[d]) - 1) * 100 for d in keys}
        med = st.median(p.values())
        if abs(med) > 5:
            dropped += 1
            continue
        prem[s] = p
        upx[s] = {d: u[d][0] for d in keys}
        upturn[s] = {d: u[d][1] for d in keys}
        ks = {}
        for d in keys:
            g = b[d]["qv"] * fx[d]
            tot = u[d][1] + g
            ks[d] = (u[d][1] / tot) if tot > 0 else None
        kshare[s] = ks

    # ── 정합성 실증: 정렬이 맞으면 BTC 김프는 0% 근처여야 한다 ──
    if "BTC" in prem:
        v = list(prem["BTC"].values())
        print(f"정합성 체크 — BTC 김프 중앙값 {st.median(v):+.2f}% "
              f"(표준편차 {st.pstdev(v):.2f}%p, 범위 {min(v):+.1f}~{max(v):+.1f}%)", file=sys.stderr)

    # 스테이블코인 제외
    def med_abs(s):
        ds = sorted(upx[s])
        r = [abs(math.log(upx[s][ds[i]] / upx[s][ds[i-1]])) * 100
             for i in range(1, len(ds)) if upx[s][ds[i-1]] > 0]
        return st.median(r) if r else 0

    stables = [s for s in prem if med_abs(s) < 0.15]
    syms = [s for s in prem if s not in set(stables)]
    print(f"검정 대상 {len(syms)}종목 (티커충돌 제외 {dropped}, 스테이블 제외 {len(stables)}: "
          f"{', '.join(stables)})\n", file=sys.stderr)

    idx = {d: i for i, d in enumerate(dates)}

    def at(series, s, d):
        return series[s].get(d)

    def avg(series, s, i, n):
        v = [series[s][dates[j]] for j in range(i - n + 1, i + 1)
             if dates[j] in series[s] and series[s][dates[j]] is not None]
        return st.mean(v) if len(v) >= max(2, n // 2) else None

    def dprem(s, i, lb):
        a, b = at(prem, s, dates[i]), at(prem, s, dates[i - lb])
        return a - b if a is not None and b is not None else None

    SIGNALS = {
        "김프 수준":             lambda s, i: at(prem, s, dates[i]),
        "Δ김프 1d":             lambda s, i: dprem(s, i, 1),
        "Δ김프 3d":             lambda s, i: dprem(s, i, 3),
        "Δ김프 7d":             lambda s, i: dprem(s, i, 7),
        "국내 거래비중 (7d)":     lambda s, i: avg(kshare, s, i, 7),
        "Δ국내비중 7d−30d":      lambda s, i: (lambda a, b: a - b if a is not None and b is not None else None)(avg(kshare, s, i, 7), avg(kshare, s, i, 30)),
        "[대조] 업비트 거래대금":   lambda s, i: (lambda v: math.log(v) if v else None)(avg(upturn, s, i, 7)),
    }

    def run(fn, H, gap, lo=None, hi=None):
        ics, sps = [], []
        for i in range(35, len(dates) - H - gap, H):
            if lo is not None and not (lo <= i < hi):
                continue
            sig, fwd = [], []
            for s in syms:
                v = fn(s, i)
                a, b = at(upx, s, dates[i + gap]), at(upx, s, dates[i + gap + H])
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

    # ── 증분 검정 ────────────────────────────────────────────────────────
    # 국내 거래비중이 '업비트 거래대금'과 거의 같은 IC를 낸다. 크로스 거래소 정보가
    # 정말 뭔가 더하는지 보려면, 매 시점 두 신호를 순위화한 뒤 거래대금 성분을 뺀
    # 잔차로 검정해야 한다. 잔차에 예측력이 없으면 더한 것이 없는 것이다.
    def run_resid(H, gap, mode):
        ics, sps = [], []
        for i in range(35, len(dates) - H - gap, H):
            ks, tv, fwd = [], [], []
            for s in syms:
                a = avg(kshare, s, i, 7)
                t7 = avg(upturn, s, i, 7)
                pa, pb = at(upx, s, dates[i + gap]), at(upx, s, dates[i + gap + H])
                if a is None or not t7 or not pa or not pb:
                    continue
                ks.append(a); tv.append(math.log(t7))
                fwd.append((pb / pa - 1) * 100)
            if len(fwd) < 20:
                continue
            n = len(ks)
            rk = [x / (n - 1) for x in rank(ks)]
            rt = [x / (n - 1) for x in rank(tv)]
            sig = ([a - b for a, b in zip(rk, rt)] if mode == "k_resid"
                   else [b - a for a, b in zip(rk, rt)])
            m = st.mean(fwd)
            fwdn = [x - m for x in fwd]
            c = spearman(sig, fwdn)
            if c is not None:
                ics.append(c)
            q = max(3, n // 5)
            o = sorted(range(n), key=lambda j: sig[j])
            sps.append(st.mean([fwdn[j] for j in o[-q:]]) - st.mean([fwdn[j] for j in o[:q]]))
        return ics, sps

    print("=" * 96)
    print("증분 검정 — 국내 거래비중이 '업비트 거래대금'을 넘어서는 것이 있는가")
    print("  (매 시점 순위 정규화 후 차감. 잔차 IC가 0이면 크로스 거래소 정보의 기여 없음)")
    print("=" * 96)
    print(f"  {'잔차 신호':<34}{'지평':>6}{'IC':>11}{'t':>8}{'양수':>7}{'스프레드':>11}")
    for mode, label in (("k_resid", "국내비중 − 거래대금 (크로스 기여분)"),
                        ("t_resid", "거래대금 − 국내비중 (업비트 단독분)")):
        for H in (1, 3, 7):
            ics, sps = run_resid(H, 1, mode)
            if not ics:
                continue
            mark = " ★" if abs(tstat(ics)) >= 2.5 and abs(st.mean(ics)) >= 0.02 else ""
            print(f"  {label:<34}{H:>5}d{st.mean(ics):+11.4f}{tstat(ics):+8.2f}"
                  f"{sum(1 for x in ics if x>0)/len(ics)*100:6.0f}%"
                  f"{st.mean(sps):+10.2f}%p{mark}")
    print()

    # ── 거래대금 vs 저변동성: 서로 다른 팩터인가 ─────────────────────────
    # 둘 다 채택하려면 서로를 넘어서는 게 있어야 한다. 거래 많은 코인이 그냥
    # 변동성 큰 코인이면 하나를 두 번 세는 것이다.
    def vol30(s, i):
        r = []
        for j in range(i - 29, i + 1):
            a, b = at(upx, s, dates[j]), at(upx, s, dates[j - 1])
            if a and b and b > 0:
                r.append(math.log(a / b))
        if len(r) < 20:
            return None
        m = sum(r) / len(r)
        return math.sqrt(sum((x - m) ** 2 for x in r) / len(r)) * 100

    def run_pair(H, gap, mode):
        ics, sps = [], []
        for i in range(35, len(dates) - H - gap, H):
            vv, tv, fwd = [], [], []
            for s in syms:
                v = vol30(s, i)
                t7 = avg(upturn, s, i, 7)
                pa, pb = at(upx, s, dates[i + gap]), at(upx, s, dates[i + gap + H])
                if v is None or not t7 or not pa or not pb:
                    continue
                vv.append(-v)               # 저변동성 = −vol
                tv.append(-math.log(t7))    # 저거래대금 = −log(turnover)
                fwd.append((pb / pa - 1) * 100)
            if len(fwd) < 20:
                continue
            n = len(vv)
            rv = [x / (n - 1) for x in rank(vv)]
            rt = [x / (n - 1) for x in rank(tv)]
            sig = ({"vol": rv, "turn": rt,
                    "vol_resid": [a - b for a, b in zip(rv, rt)],
                    "turn_resid": [b - a for a, b in zip(rv, rt)]})[mode]
            m = st.mean(fwd)
            fwdn = [x - m for x in fwd]
            c = spearman(sig, fwdn)
            if c is not None:
                ics.append(c)
            q = max(3, n // 5)
            o = sorted(range(n), key=lambda j: sig[j])
            sps.append(st.mean([fwdn[j] for j in o[-q:]]) - st.mean([fwdn[j] for j in o[:q]]))
        return ics, sps

    print("=" * 96)
    print("중복 검정 — 저변동성과 저거래대금은 같은 팩터인가")
    print("=" * 96)
    print(f"  {'신호':<34}{'지평':>6}{'IC':>11}{'t':>8}{'양수':>7}{'스프레드':>11}")
    for mode, label in (("vol", "저변동성 (단독)"),
                        ("turn", "저거래대금 (단독)"),
                        ("vol_resid", "저변동성 − 저거래대금 (변동성 증분)"),
                        ("turn_resid", "저거래대금 − 저변동성 (거래대금 증분)")):
        for H in (1, 7):
            ics, sps = run_pair(H, 1, mode)
            if not ics:
                continue
            mark = " ★" if abs(tstat(ics)) >= 2.5 and abs(st.mean(ics)) >= 0.02 else ""
            print(f"  {label:<34}{H:>5}d{st.mean(ics):+11.4f}{tstat(ics):+8.2f}"
                  f"{sum(1 for x in ics if x>0)/len(ics)*100:6.0f}%"
                  f"{st.mean(sps):+10.2f}%p{mark}")
    print()

    half = (35 + len(dates)) // 2
    for H in (1, 3, 7):
        print("=" * 96)
        print(f"예측 지평 {H}일   (업비트 원화 수익률 · 시장중립 · 비중첩 · 왕복비용 0.20%p)")
        print("=" * 96)
        print(f"  {'신호':<24}{'IC(GAP1)':>11}{'t':>8}{'양수':>7}{'스프레드':>11}"
              f"{'IC(GAP0)':>11}{'전반부':>9}{'후반부':>9}")
        for name, fn in SIGNALS.items():
            ics, sps = run(fn, H, 1)
            if not ics:
                print(f"  {name:<24}{'표본 부족':>11}")
                continue
            ics0, _ = run(fn, H, 0)
            a, _ = run(fn, H, 1, 35, half)
            b, _ = run(fn, H, 1, half, len(dates))
            m, t = st.mean(ics), tstat(ics)
            mark = " ★" if abs(t) >= 2.5 and abs(m) >= 0.02 else ""
            print(f"  {name:<24}{m:+11.4f}{t:+8.2f}"
                  f"{sum(1 for x in ics if x>0)/len(ics)*100:6.0f}%{st.mean(sps):+10.2f}%p"
                  f"{st.mean(ics0):+11.4f}{st.mean(a) if a else 0:+9.4f}"
                  f"{st.mean(b) if b else 0:+9.4f}{mark}")
        print(f"  (n={len(run(SIGNALS['Δ김프 1d'], H, 1)[0])} 비중첩 시점)"
              f"   ★ = |t|≥2.5 이고 |IC|≥0.02\n")


if __name__ == "__main__":
    main()
