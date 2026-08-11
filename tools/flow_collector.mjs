#!/usr/bin/env node
/* 체결 흐름 수집기 — 캔들에 남지 않는 정보를 1분 단위로 적재한다.
 *
 * 왜 필요한가:
 *   지금까지 검정한 22개 신호는 전부 캔들 기반이라 누구나 보는 데이터였다.
 *   체결 단위 주문 흐름(테이커 방향, 대량 체결, 호가 불균형)은
 *   ① 사람이 200종목을 동시에 볼 수 없고
 *   ② 업비트가 과거 데이터를 주지 않아 모아둔 사람만 쓸 수 있다.
 *   즉 지금부터 모아야 몇 주 뒤에 검정할 수 있다.
 *
 * 저장 설계:
 *   원시 틱은 하루 0.75GB라 감당이 안 된다(실측 초당 19.2건).
 *   1분 × 종목 단위로 집계하면 하루 약 204k행 / gzip 2.5MB.
 *
 * 무결성:
 *   연결이 끊긴 구간은 gaps.jsonl에 그대로 남긴다. 빠진 구간을 모르면
 *   나중에 "그 시간에 아무 일도 없었다"와 구분할 수 없다.
 *
 * 의존성 없음 — Node 22+ 내장 WebSocket 사용.
 */
import fs from "node:fs";
import path from "node:path";

const OUT_DIR = process.argv.includes("--out")
  ? process.argv[process.argv.indexOf("--out") + 1]
  : "flow";
const MIN_TURNOVER = 3e8;        // 24h 거래대금 3억 미만은 잡음
const BIG_TRADE_KRW = 5_000_000; // 대량 체결 기준 (실측: 60초에 15건)
const UNIVERSE_REFRESH_MS = 6 * 3600_000;
const PING_MS = 50_000;

const state = {
  bars: new Map(),      // "min|market" -> bar
  codes: [],
  ws: null,
  retry: 0,
  pingTimer: null,
  connectedSince: null,
  lastMsg: 0,
  totals: { trades: 0, flushed: 0 },
};

const minuteOf = (ms) => Math.floor(ms / 60000) * 60000;
const iso = (ms) => new Date(ms).toISOString().replace(".000Z", "Z");
const day = (ms) => new Date(ms).toISOString().slice(0, 10);

fs.mkdirSync(OUT_DIR, { recursive: true });

// 이중 실행 방지. 두 개가 같은 파일에 쓰면 같은 분·종목이 중복 적재되고
// 나중에 "체결이 두 배로 몰렸다"는 가짜 신호가 된다. 조용히 오염되는 게 최악이다.
const LOCK = path.join(OUT_DIR, ".collector.lock");
if (fs.existsSync(LOCK)) {
  const pid = parseInt(fs.readFileSync(LOCK, "utf8").trim(), 10);
  let alive = false;
  try { process.kill(pid, 0); alive = true; } catch {}
  if (alive) {
    console.error(`[lock] 이미 실행 중 (pid ${pid}). 중복 적재를 막기 위해 종료합니다.`);
    process.exit(1);
  }
  console.error(`[lock] 죽은 잠금 파일 정리 (pid ${pid})`);
}
fs.writeFileSync(LOCK, String(process.pid));
const releaseLock = () => { try { fs.unlinkSync(LOCK); } catch {} };
process.on("exit", releaseLock);

function logGap(from, to, reason) {
  const rec = { from: iso(from), to: iso(to), sec: Math.round((to - from) / 1000), reason };
  fs.appendFileSync(path.join(OUT_DIR, "gaps.jsonl"), JSON.stringify(rec) + "\n");
  console.error(`[gap] ${rec.sec}s (${reason})`);
}

// ── 유니버스 ────────────────────────────────────────────
async function loadUniverse() {
  const r = await fetch("https://api.upbit.com/v1/ticker/all?quote_currencies=KRW");
  if (!r.ok) throw new Error("ticker/all HTTP " + r.status);
  const arr = await r.json();
  const codes = arr
    .filter((t) => (t.acc_trade_price_24h || 0) >= MIN_TURNOVER)
    .sort((a, b) => b.acc_trade_price_24h - a.acc_trade_price_24h)
    .map((t) => t.market);
  state.codes = codes;
  console.error(`[universe] ${codes.length}종목`);
  return codes;
}

// ── 집계 ────────────────────────────────────────────────
function bar(minute, market) {
  const key = minute + "|" + market;
  let b = state.bars.get(key);
  if (!b) {
    b = {
      minute, market,
      n: 0, nBuy: 0,
      buyKrw: 0, sellKrw: 0,
      bigBuyKrw: 0, bigSellKrw: 0, maxKrw: 0,
      open: null, high: -Infinity, low: Infinity, close: null,
      spreadSum: 0, imbSum: 0, quoteN: 0,
    };
    state.bars.set(key, b);
  }
  return b;
}

function onTrade(d) {
  const ts = d.trade_timestamp || d.timestamp || Date.now();
  const b = bar(minuteOf(ts), d.code);
  const krw = d.trade_price * d.trade_volume;

  b.n++;
  if (d.ask_bid === "BID") { b.nBuy++; b.buyKrw += krw; if (krw >= BIG_TRADE_KRW) b.bigBuyKrw += krw; }
  else { b.sellKrw += krw; if (krw >= BIG_TRADE_KRW) b.bigSellKrw += krw; }
  if (krw > b.maxKrw) b.maxKrw = krw;

  const p = d.trade_price;
  if (b.open === null) b.open = p;
  b.close = p;
  if (p > b.high) b.high = p;
  if (p < b.low) b.low = p;

  // 체결 메시지에 최우선 호가가 같이 온다 — 캔들에는 없는 정보다
  const ba = d.best_ask_price, bb = d.best_bid_price;
  const as = d.best_ask_size, bs = d.best_bid_size;
  if (ba && bb && ba > 0) {
    b.spreadSum += ((ba - bb) / ((ba + bb) / 2)) * 10000;   // bp
    if (as + bs > 0) b.imbSum += (bs - as) / (bs + as);      // +1 매수우위 ~ -1 매도우위
    b.quoteN++;
  }
  state.totals.trades++;
}

const HEADER = "minute,market,n,n_buy,buy_krw,sell_krw,big_buy_krw,big_sell_krw," +
               "max_krw,open,high,low,close,spread_bp,imbalance\n";

function flush(force = false) {
  const cutoff = minuteOf(Date.now()) - (force ? 0 : 60000); // 완료된 분만
  const byDay = new Map();
  for (const [key, b] of state.bars) {
    if (b.minute > cutoff) continue;
    const row = [
      iso(b.minute), b.market, b.n, b.nBuy,
      Math.round(b.buyKrw), Math.round(b.sellKrw),
      Math.round(b.bigBuyKrw), Math.round(b.bigSellKrw), Math.round(b.maxKrw),
      b.open, b.high === -Infinity ? "" : b.high, b.low === Infinity ? "" : b.low, b.close,
      b.quoteN ? (b.spreadSum / b.quoteN).toFixed(2) : "",
      b.quoteN ? (b.imbSum / b.quoteN).toFixed(4) : "",
    ].join(",");
    const d = day(b.minute);
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d).push(row);
    state.bars.delete(key);
  }
  for (const [d, rows] of byDay) {
    const f = path.join(OUT_DIR, `${d}.csv`);
    if (!fs.existsSync(f)) fs.writeFileSync(f, HEADER);
    fs.appendFileSync(f, rows.join("\n") + "\n");
    state.totals.flushed += rows.length;
  }
}

// ── WebSocket ───────────────────────────────────────────
function connect() {
  let ws;
  try {
    ws = new WebSocket("wss://api.upbit.com/websocket/v1");
  } catch (e) {
    return scheduleReconnect(e.message);
  }
  state.ws = ws;
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    if (state.connectedSince === null && state.disconnectedAt) {
      logGap(state.disconnectedAt, Date.now(), "reconnect");
      state.disconnectedAt = null;
    }
    state.connectedSince = Date.now();
    state.retry = 0;
    ws.send(JSON.stringify([
      { ticket: "flow-" + Math.random().toString(36).slice(2, 8) },
      { type: "trade", codes: state.codes },
      { format: "DEFAULT" },
    ]));
    console.error(`[ws] connected · ${state.codes.length}종목 구독`);
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("PING");
    }, PING_MS);
  };

  ws.onmessage = (ev) => {
    state.lastMsg = Date.now();
    let d;
    try {
      d = JSON.parse(typeof ev.data === "string" ? ev.data : new TextDecoder().decode(ev.data));
    } catch { return; }
    if (d && d.code && d.trade_price) onTrade(d);
  };

  ws.onclose = () => {
    clearInterval(state.pingTimer);
    if (state.connectedSince !== null) {
      state.disconnectedAt = Date.now();
      state.connectedSince = null;
    }
    scheduleReconnect("close");
  };
  ws.onerror = () => { try { ws.close(); } catch {} };
}

function scheduleReconnect(reason) {
  // 업비트는 WS 연결 자체에 한도가 있어 빠른 재시도가 429를 부른다
  const base = Math.min(60_000, 2000 * Math.pow(2, state.retry++));
  const wait = base * (0.7 + Math.random() * 0.6);
  console.error(`[ws] ${reason} → ${Math.round(wait / 1000)}s 후 재연결`);
  setTimeout(connect, wait);
}

// ── 부팅 ────────────────────────────────────────────────
await loadUniverse();
connect();

setInterval(() => flush(), 30_000);

// 데이터가 멈추면 조용히 죽은 것이다. 감지해서 재연결한다.
setInterval(() => {
  if (state.ws && state.lastMsg && Date.now() - state.lastMsg > 180_000) {
    console.error("[ws] 3분간 무수신 — 강제 재연결");
    try { state.ws.close(); } catch {}
  }
}, 60_000);

setInterval(async () => {
  try {
    const before = state.codes.length;
    await loadUniverse();
    if (state.codes.length !== before && state.ws?.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify([
        { ticket: "flow-re" }, { type: "trade", codes: state.codes }, { format: "DEFAULT" }]));
      console.error("[universe] 구독 갱신");
    }
  } catch (e) { console.error("[universe] 갱신 실패:", e.message); }
}, UNIVERSE_REFRESH_MS);

setInterval(() => {
  const up = state.connectedSince ? Math.round((Date.now() - state.connectedSince) / 60000) : 0;
  console.error(`[stat] 체결 ${state.totals.trades} · 적재 ${state.totals.flushed}행 · 연결 ${up}분`);
}, 600_000);

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.error("\n[exit] 미완료 분 flush");
    flush(true);
    process.exit(0);
  });
}
