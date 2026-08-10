#!/usr/bin/env python3
"""국내 거래비중 신호가 진짜인지 마지막 관문.

의심: 이 신호는 코인별로 거의 고정된 '수준'이다. 순위가 안 변하면 56개 시점의 IC는
      독립 관측 56개가 아니라 "이 코인들이 이 10일간 떨어졌다"는 관측 1개에 불과하다.

검정:
  A. 신호 지속성 — 시차별 순위 자기상관. 1에 가까우면 사실상 고정 랭킹.
  B. 종목내 편차 신호 — 각 코인의 자기 평균을 뺀 값(코인 고정효과 제거)이 예측하는가.
     이게 죽고 수준만 살면 '신호'가 아니라 '이 코인들이 떨어졌다'는 사후 서술이다.
  C. 기간 분할 — 전반부/후반부에서 부호가 같은가.
  D. 대안 설명 — 단순 회전율(거래대금/시총 대용)로 대체해도 같은 결과가 나오는가.
"""
import json, math, os, statistics as st, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "flow_cache.json")


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


obj = json.load(open(CACHE))
data = obj["data"]
fx = {k: (v[0] if isinstance(v, (list, tuple)) else v) for k, v in obj["fx"].items()}

px, kshare, upturn = {}, {}, {}
for s, d in data.items():
    u, b = d["up"], d["bi"]
    keys = sorted(set(u) & set(b) & set(fx))
    if len(keys) < 300:
        continue
    p = [(u[k][0] / (b[k][0] * fx[k]) - 1) * 100 for k in keys]
    if abs(st.median(p)) > 5:
        continue
    px[s] = {k: u[k][0] for k in keys}
    upturn[s] = {k: u[k][1] for k in keys}
    ks = {}
    for k in keys:
        g = b[k][1] * fx[k]
        tot = u[k][1] + g
        ks[k] = (u[k][1] / tot) if tot > 0 else None
    kshare[s] = ks

times = sorted(set.intersection(*[set(v) for v in px.values()]))
syms = list(px)
print(f"종목 {len(syms)} / 시각 {len(times)} ({len(times)/24:.1f}일)\n")


def avg(series, s, i, n):
    vals = [series[s][times[j]] for j in range(i - n + 1, i + 1)
            if times[j] in series[s] and series[s][times[j]] is not None]
    return st.mean(vals) if len(vals) >= max(2, n // 2) else None


# 신호 패널 구성: sig[i][s]
SIG = []
for i in range(24, len(times)):
    SIG.append({s: avg(kshare, s, i, 4) for s in syms})
IDX0 = 24

print("=" * 70)
print("A. 신호 지속성 (순위 자기상관) — 1에 가까울수록 고정 랭킹")
print("=" * 70)
for lag in (1, 6, 24, 72, 120):
    cors = []
    for i in range(0, len(SIG) - lag, max(1, lag)):
        a, b = SIG[i], SIG[i + lag]
        common = [s for s in syms if a.get(s) is not None and b.get(s) is not None]
        if len(common) < 15:
            continue
        c = spearman([a[s] for s in common], [b[s] for s in common])
        if c is not None:
            cors.append(c)
    if cors:
        print(f"  lag {lag:>3}h  순위상관 {st.mean(cors):+.3f}  (n={len(cors)})")

print("\n" + "=" * 70)
print("B. 종목내 편차 신호 (코인 고정효과 제거) vs 원래 수준 신호")
print("=" * 70)
# 각 코인의 전체 기간 평균 국내비중
coin_mean = {s: st.mean([v for v in kshare[s].values() if v is not None]) for s in syms}

for H in (4, 12):
    for mode in ("수준", "종목내 편차"):
        ics, sps = [], []
        for k in range(0, len(SIG) - H - 1, H):
            i = IDX0 + k
            if i + 1 + H >= len(times):
                break
            sig, fwd = [], []
            for s in syms:
                v = SIG[k].get(s)
                ts, te = times[i + 1], times[i + 1 + H]
                if v is None or ts not in px[s] or te not in px[s]:
                    continue
                sig.append(v - coin_mean[s] if mode == "종목내 편차" else v)
                fwd.append((px[s][te] / px[s][ts] - 1) * 100)
            if len(fwd) < 15:
                continue
            m = st.mean(fwd)
            fwdn = [x - m for x in fwd]
            c = spearman(sig, fwdn)
            if c is not None:
                ics.append(c)
            q = max(2, len(fwd) // 5)
            o = sorted(range(len(fwd)), key=lambda j: sig[j])
            sps.append(st.mean([fwdn[j] for j in o[-q:]]) - st.mean([fwdn[j] for j in o[:q]]))
        print(f"  지평 {H:>2}h  {mode:<8} {stat(ics)}  스프레드 {st.mean(sps) if sps else 0:+.3f}%p")

print("\n" + "=" * 70)
print("C. 기간 분할 — 전반부/후반부 부호 일치 여부")
print("=" * 70)
H = 4
half = len(SIG) // 2
for label, rng in (("전반부", range(0, half - H, H)), ("후반부", range(half, len(SIG) - H - 1, H))):
    ics = []
    for k in rng:
        i = IDX0 + k
        if i + 1 + H >= len(times):
            break
        sig, fwd = [], []
        for s in syms:
            v = SIG[k].get(s)
            ts, te = times[i + 1], times[i + 1 + H]
            if v is None or ts not in px[s] or te not in px[s]:
                continue
            sig.append(v); fwd.append((px[s][te] / px[s][ts] - 1) * 100)
        if len(fwd) < 15:
            continue
        m = st.mean(fwd)
        c = spearman(sig, [x - m for x in fwd])
        if c is not None:
            ics.append(c)
    print(f"  {label}  {stat(ics)}")

print("\n" + "=" * 70)
print("D. 대안 설명 — 업비트 거래대금(관심도)만으로도 같은 결과가 나오는가")
print("=" * 70)
for H in (4, 12):
    ics = []
    for k in range(0, len(SIG) - H - 1, H):
        i = IDX0 + k
        if i + 1 + H >= len(times):
            break
        sig, fwd = [], []
        for s in syms:
            v = avg(upturn, s, i, 4)
            ts, te = times[i + 1], times[i + 1 + H]
            if v is None or ts not in px[s] or te not in px[s]:
                continue
            sig.append(math.log(max(v, 1))); fwd.append((px[s][te] / px[s][ts] - 1) * 100)
        if len(fwd) < 15:
            continue
        m = st.mean(fwd)
        c = spearman(sig, [x - m for x in fwd])
        if c is not None:
            ics.append(c)
    print(f"  지평 {H:>2}h  업비트 거래대금(log) {stat(ics)}")
