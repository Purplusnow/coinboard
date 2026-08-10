#!/usr/bin/env python3
"""공개 실적 장부 — 추천을 동결 기록하고 사후 채점한다.

무결성 규약 (깨면 장부의 가치가 0이 된다):
  1. 동결   기록된 엔트리는 절대 수정·삭제하지 않는다. append-only 브랜치에 커밋한다.
            git 커밋 해시와 시각이 곧 위변조 증거다.
  2. 전건   모든 규칙, 모든 회차를 기록한다. 성적이 나빠도 규칙을 죽이지 않는다.
            잘 된 것만 남기면 실적표가 아니라 광고가 된다.
  3. 사전   채점 규칙(진입가·수수료·지평)을 코드에 미리 박는다. 사후 변경 금지.
  4. 재현   랜덤 기준선의 시드는 날짜에서 결정론적으로 만든다. 뽑고 나서 고를 수 없다.
  5. 기준선 랜덤과 BTC 보유를 같이 굴린다. 이 둘을 못 이기면 아무 의미가 없다.

이 장부는 실전 out-of-sample이다. 여기 결과를 보고 규칙을 튜닝하면 그 순간
out-of-sample이 아니게 되고 장부의 유일한 가치가 사라진다. 연구는 과거 데이터로만 한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

UP = "https://api.upbit.com/v1"
KST = timezone(timedelta(hours=9))

FEE_ROUNDTRIP = 0.2      # % — 업비트 현물 0.05%×2 + 슬리피지 여유
HOLD_N = 12              # 규칙당 보유 종목 수
HORIZONS = [1, 7]        # 채점 지평(일)
MIN_TURNOVER = 1_000_000_000   # 편입 최소 24h 거래대금 10억
STABLE = {"USDT", "USDC", "USDE", "RLUSD", "USD1", "DAI", "TUSD", "FDUSD", "BUSD", "PYUSD"}

ENTRIES = "entries.jsonl"
RESULTS = "results.jsonl"
SUMMARY = "summary.json"


def fetch(path: str, tries: int = 4):
    url = f"{UP}{path}"
    delay = 0.8
    for i in range(tries):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=20
            ) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(delay)
            delay *= 2


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


# ============================================================================
# 규칙 — 여기 있는 것이 '실전층'이다. 추가만 하고 고치거나 지우지 않는다.
# ============================================================================
RULES = {
    "lowvol":  {"name": "저변동 하위 20%", "since": "2026-08-11",
                "desc": "30일 실현변동성이 낮은 순. 팩터 검정을 통과한 유일한 신호"},
    "gainers": {"name": "24H 급등 추종", "since": "2026-08-11",
                "desc": "전일 대비 상승률 상위. 사람들이 실제로 하는 매매"},
    "korea":   {"name": "국내 단독 상승", "since": "2026-08-11",
                "desc": "업비트 상승분 중 국내 성분이 큰 순. 우리 분해 신호"},
    "random":  {"name": "랜덤 12종목", "since": "2026-08-11",
                "desc": "기준선. 날짜 해시로 시드를 고정해 재현 가능"},
    "btc":     {"name": "비트코인 보유", "since": "2026-08-11",
                "desc": "기준선. 아무것도 안 하는 전략"},
    "eqw":     {"name": "전 종목 동일가중", "since": "2026-08-11",
                "desc": "기준선. 아무 알트나 골고루"},
}


def pick_holdings(rule: str, day: str, universe: list[dict], board: dict) -> list[str]:
    """규칙별 보유 종목. universe는 편입 자격을 통과한 종목의 티커 정보."""
    if rule == "btc":
        return ["KRW-BTC"]
    if rule == "eqw":
        return [u["market"] for u in universe]
    if rule == "random":
        # 시드를 날짜에서 결정론적으로 만든다 — 뽑고 나서 마음에 드는 걸 고를 수 없게
        seed = int(hashlib.sha256(f"coinboard:{day}".encode()).hexdigest()[:16], 16)
        rng = random.Random(seed)
        return rng.sample([u["market"] for u in universe], min(HOLD_N, len(universe)))
    if rule == "gainers":
        s = sorted(universe, key=lambda u: u["chg"], reverse=True)
        return [u["market"] for u in s[:HOLD_N]]
    if rule == "lowvol":
        v = board.get("vol30") or {}
        cand = [u for u in universe if u["market"] in v]
        s = sorted(cand, key=lambda u: v[u["market"]]["vol"])
        return [u["market"] for u in s[:HOLD_N]]
    if rule == "korea":
        cross = board.get("cross") or {}
        cand = []
        for u in universe:
            c = cross.get(u["market"])
            d = c.get("4h") if c else None
            if not d or d["korea"] <= 0:
                continue
            # 사이트의 '국내 단독' 탭과 같은 우세도 정렬.
            # 하드 필터(|국내|>|글로벌|)를 걸면 1~2종목만 남아 다른 규칙과 비교가 안 된다.
            cand.append((abs(d["korea"]) - abs(d["global"]), u["market"]))
        cand.sort(reverse=True)
        return [m for _, m in cand[:HOLD_N]]
    return []


# ============================================================================
# 엔트리 기록
# ============================================================================
def make_entry(journal_dir: str, board: dict) -> dict:
    now = datetime.now(timezone.utc)
    day = now.astimezone(KST).strftime("%Y-%m-%d")

    existing = {e["day"] for e in read_jsonl(os.path.join(journal_dir, ENTRIES))}
    if day in existing:
        print(f"  {day} 엔트리가 이미 있음 — 기록 생략(동결 규약)", file=sys.stderr)
        return {}

    tickers = fetch("/ticker/all?quote_currencies=KRW")
    universe = []
    for t in tickers:
        sym = t["market"].split("-")[1]
        if sym in STABLE:
            continue
        if (t.get("acc_trade_price_24h") or 0) < MIN_TURNOVER:
            continue
        universe.append({
            "market": t["market"],
            "price": t["trade_price"],
            "chg": (t.get("signed_change_rate") or 0) * 100,
            "turnover": t["acc_trade_price_24h"],
        })
    px = {u["market"]: u["price"] for u in universe}
    btc = fetch("/ticker?markets=KRW-BTC")[0]["trade_price"]
    px["KRW-BTC"] = btc

    rules = {}
    for rid in RULES:
        held = pick_holdings(rid, day, universe, board)
        held = [m for m in held if m in px]
        rules[rid] = {"holdings": [{"m": m, "p": px[m]} for m in held]}
        print(f"  {rid:<8} {len(held):>3}종목  {', '.join(m.split('-')[1] for m in held[:5])}",
              file=sys.stderr)

    entry = {
        "day": day,
        "ts": now.isoformat(timespec="seconds"),
        "universe": len(universe),
        "fee": FEE_ROUNDTRIP,
        "horizons": HORIZONS,
        "btc_price": btc,
        "rules": rules,
    }
    append_jsonl(os.path.join(journal_dir, ENTRIES), entry)
    print(f"  → 엔트리 기록 {day} (유니버스 {len(universe)}종목)", file=sys.stderr)
    return entry


# ============================================================================
# 채점
# ============================================================================
def score_due(journal_dir: str) -> int:
    entries = read_jsonl(os.path.join(journal_dir, ENTRIES))
    results = read_jsonl(os.path.join(journal_dir, RESULTS))
    done = {(r["day"], r["rule"], r["h"]) for r in results}
    today = datetime.now(timezone.utc).astimezone(KST).date()

    due = []
    for e in entries:
        d0 = datetime.strptime(e["day"], "%Y-%m-%d").date()
        for h in e.get("horizons", HORIZONS):
            if (today - d0).days < h:
                continue
            for rid in e["rules"]:
                if (e["day"], rid, h) not in done:
                    due.append((e, rid, h))
    if not due:
        print("  채점할 항목 없음", file=sys.stderr)
        return 0

    need = {"KRW-BTC"}
    for e, rid, h in due:
        for x in e["rules"][rid]["holdings"]:
            need.add(x["m"])
    now_px = {}
    markets = sorted(need)
    for i in range(0, len(markets), 90):
        chunk = markets[i:i + 90]
        try:
            for t in fetch("/ticker?markets=" + ",".join(chunk)):
                now_px[t["market"]] = t["trade_price"]
        except Exception as ex:
            print(f"  ! 시세 조회 실패: {ex}", file=sys.stderr)
        time.sleep(0.3)

    n = 0
    for e, rid, h in due:
        hold = e["rules"][rid]["holdings"]
        rets, missing = [], []
        for x in hold:
            cur = now_px.get(x["m"])
            if cur is None or not x["p"]:
                missing.append(x["m"])      # 상장폐지 등 — 평균에서 제외하고 기록에 남긴다
                continue
            rets.append((cur / x["p"] - 1) * 100)
        if not rets:
            continue
        gross = sum(rets) / len(rets)
        net = gross - FEE_ROUNDTRIP
        b0, b1 = e.get("btc_price"), now_px.get("KRW-BTC")
        btc_ret = ((b1 / b0 - 1) * 100) if (b0 and b1) else None
        rec = {
            "day": e["day"], "rule": rid, "h": h,
            "n": len(rets), "gross": round(gross, 3), "net": round(net, 3),
            "btc": None if btc_ret is None else round(btc_ret, 3),
            "vs_btc": None if btc_ret is None else round(net - btc_ret, 3),
            "missing": missing,
            "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        append_jsonl(os.path.join(journal_dir, RESULTS), rec)
        n += 1
    print(f"  → {n}건 채점", file=sys.stderr)
    return n


# ============================================================================
# 요약 (사이트용)
# ============================================================================
def build_summary(journal_dir: str) -> dict:
    entries = read_jsonl(os.path.join(journal_dir, ENTRIES))
    results = read_jsonl(os.path.join(journal_dir, RESULTS))

    summary = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_kst": datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        "entries": len(entries),
        "first_day": entries[0]["day"] if entries else None,
        "fee": FEE_ROUNDTRIP,
        "rules": {},
    }
    for rid, meta in RULES.items():
        per_h = {}
        for h in HORIZONS:
            rs = sorted([r for r in results if r["rule"] == rid and r["h"] == h],
                        key=lambda r: r["day"])
            if not rs:
                per_h[str(h)] = {"n": 0}
                continue
            nets = [r["net"] for r in rs]
            vs = [r["vs_btc"] for r in rs if r["vs_btc"] is not None]
            # 비중첩 누적 — h일마다 한 번씩만 사용해 중복 계산을 피한다
            cum, peak, mdd = 1.0, 1.0, 0.0
            for r in rs[::h] if h > 1 else rs:
                cum *= (1 + r["net"] / 100)
                peak = max(peak, cum)
                mdd = min(mdd, cum / peak - 1)
            per_h[str(h)] = {
                "n": len(rs),
                "avg": round(sum(nets) / len(nets), 3),
                "win": round(sum(1 for x in nets if x > 0) / len(nets) * 100),
                "cum": round((cum - 1) * 100, 2),
                "mdd": round(mdd * 100, 2),
                "vs_btc_avg": round(sum(vs) / len(vs), 3) if vs else None,
                "vs_btc_win": round(sum(1 for x in vs if x > 0) / len(vs) * 100) if vs else None,
                "series": [{"d": r["day"], "net": r["net"], "vs": r["vs_btc"]} for r in rs[-120:]],
            }
        summary["rules"][rid] = {**meta, "h": per_h}

    with open(os.path.join(journal_dir, SUMMARY), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="journal", help="장부 작업 디렉터리(append-only 브랜치)")
    ap.add_argument("--board", default="docs/data/board.json")
    ap.add_argument("--skip-entry", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    board = {}
    if os.path.exists(args.board):
        board = json.load(open(args.board, encoding="utf-8"))
    else:
        print(f"  ! board.json 없음({args.board}) — lowvol/korea 규칙은 이번 회차 건너뜀",
              file=sys.stderr)

    print("[1/3] 엔트리 기록", file=sys.stderr)
    if not args.skip_entry:
        make_entry(args.dir, board)
    print("[2/3] 만기 채점", file=sys.stderr)
    score_due(args.dir)
    print("[3/3] 요약 생성", file=sys.stderr)
    s = build_summary(args.dir)

    print(f"\n  누적 엔트리 {s['entries']}회 (최초 {s['first_day']})", file=sys.stderr)
    for rid, r in s["rules"].items():
        h7 = r["h"].get("7", {})
        if h7.get("n"):
            print(f"    {r['name']:<16} 7일: {h7['n']}회 평균 {h7['avg']:+.2f}% "
                  f"승률 {h7['win']}% 누적 {h7['cum']:+.1f}% BTC대비 {h7['vs_btc_avg']:+.2f}%",
                  file=sys.stderr)
        else:
            print(f"    {r['name']:<16} 7일: 채점 대기", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
