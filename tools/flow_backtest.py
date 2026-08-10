#!/usr/bin/env python3
"""주문흐름 신호 검정 — 가격이 아닌 거래량 기반이라 bid-ask bounce 편향이 구조적으로 없다.

신호 후보:
  1. 글로벌 테이커 매수비중      바이낸스 klines[10]/klines[7]  (시장가 매수 우위)
  2. Δ 테이커 매수비중           최근 4h 평균 − 직전 24h 평균
  3. 국내 거래 비중              업비트 거래대금 / (업비트+글로벌)  ← 두 거래소 필요, 유일성 있음
  4. Δ 국내 거래 비중            국내 참여가 늘고 있는가

모든 검정은 GAP 0/1 을 함께 본다. 거래량 신호는 GAP 0에서도 편향이 없어야 정상이며,
GAP 0과 1이 크게 다르면 그 자체가 경고 신호다.
"""
import json, math, os, statistics as st, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

UP = "https://api.upbit.com/v1"
BI = "https://api.binance.com/api/v3"
BARS = 1000
MIN_TURNOVER = 5e8
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_cache.json")
FEE = 0.10


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
    """close + 거래대금(원)"""
    out, to = {}, None
    while len(out) < bars:
        u = f"{UP}/candles/minutes/60?market={market}&count=200" + (f"&to={to}" if to else "")
        d = get(u)
        if not d:
            break
        for c in d:
            out[c["candle_date_time_utc"][:13]] = (c["trade_price"], c["candle_acc_trade_price"])
        to = d[-1]["candle_date_time_utc"]
        time.sleep(0.14)
        if len(d) < 200:
            break
    return out


def binance_klines(sym, bars=BARS):
    """close, quoteVolume(USDT), takerBuyQuoteVolume(USDT)"""
    d = get(f"{BI}/klines?symbol={sym}USDT&interval=1h&limit={bars}")
    out = {}
    for k in d:
        key = time.strftime("%Y-%m-%dT%H", time.gmtime(k[0] / 1000))
        out[key] = (float(k[4]), float(k[7]), float(k[10]))
    return out


def build():
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
    json.dump(obj, open(CACHE, "w"))
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


def main():
    obj = json.load(open(CACHE)) if os.path.exists(CACHE) else build()
    data = obj["data"]
    # fx도 (종가, 거래대금) 튜플로 저장돼 있다. 환율은 종가만 쓴다.
    fx = {k: (v[0] if isinstance(v, (list, tuple)) else v) for k, v in obj["fx"].items()}

    px, taker, kshare = {}, {}, {}
    for s, d in data.items():
        u, b = d["up"], d["bi"]
        keys = sorted(set(u) & set(b) & set(fx))
        if len(keys) < 300:
            continue
        p = {k: (u[k][0] / (b[k][0] * fx[k]) - 1) * 100 for k in keys}
        if abs(st.median(p.values())) > 5:
            continue
        px[s] = {k: u[k][0] for k in keys}
        taker[s] = {k: (b[k][2] / b[k][1]) if b[k][1] > 0 else None for k in keys}
        # 국내 비중 = 업비트 거래대금(원) / (업비트 + 바이낸스 거래대금 원화환산)
        ks = {}
        for k in keys:
            g = b[k][1] * fx[k]
            tot = u[k][1] + g
            ks[k] = (u[k][1] / tot) if tot > 0 else None
        kshare[s] = ks

    times = sorted(set.intersection(*[set(v) for v in px.values()]))
    print(f"검정 대상 {len(px)}종목 / 시각 {len(times)}개 "
          f"({times[0]} ~ {times[-1]}, {len(times)/24:.1f}일)\n", file=sys.stderr)

    def avg(series, s, i, n):
        vals = [series[s][times[j]] for j in range(i - n + 1, i + 1)
                if times[j] in series[s] and series[s][times[j]] is not None]
        return st.mean(vals) if len(vals) >= max(2, n // 2) else None

    SIGNALS = {
        "글로벌 테이커매수비중(4h)":  lambda s, i: avg(taker, s, i, 4),
        "Δ테이커매수비중(4h-24h)":   lambda s, i: (lambda a, b: a - b if a is not None and b is not None else None)(avg(taker, s, i, 4), avg(taker, s, i, 24)),
        "국내 거래비중(4h)":         lambda s, i: avg(kshare, s, i, 4),
        "Δ국내 거래비중(4h-24h)":    lambda s, i: (lambda a, b: a - b if a is not None and b is not None else None)(avg(kshare, s, i, 4), avg(kshare, s, i, 24)),
    }

    for H in (1, 4, 12):
        print("=" * 78)
        print(f"예측 지평 {H}h   (시장중립 · 비중첩 · 왕복수수료 {FEE}%p)")
        print("=" * 78)
        for name, fn in SIGNALS.items():
            line = f"  {name:<24}"
            for gap in (0, 1):
                ics, sp = [], []
                for i in range(24, len(times) - H - gap, max(1, H)):
                    sig, fwd = [], []
                    for s in px:
                        v = fn(s, i)
                        ts, te = times[i + gap], times[i + gap + H]
                        if v is None or ts not in px[s] or te not in px[s]:
                            continue
                        sig.append(v)
                        fwd.append((px[s][te] / px[s][ts] - 1) * 100)
                    if len(fwd) < 15:
                        continue
                    m = st.mean(fwd)
                    fwdn = [x - m for x in fwd]
                    ic = spearman(sig, fwdn)
                    if ic is not None:
                        ics.append(ic)
                    k = max(2, len(fwd) // 5)
                    o = sorted(range(len(fwd)), key=lambda j: sig[j])
                    sp.append(st.mean([fwdn[j] for j in o[-k:]]) - st.mean([fwdn[j] for j in o[:k]]))
                line += f"  GAP{gap} {stat(ics)} 스프레드 {st.mean(sp) if sp else 0:+.3f}%p"
            print(line)
        print()


if __name__ == "__main__":
    main()
