/* COIN BOARD — 업비트 × 바이낸스 실시간 분해 전광판
 *
 * 이 사이트가 보여주는 것은 예측이 아니라 서술이다.
 *   업비트 4시간 변동 = 글로벌 성분 + 국내 성분
 *   글로벌 성분 = (1+바이낸스 4h 변동)(1+USDT/원 4h 변동) − 1
 *   국내 성분   = 업비트 변동 − 글로벌 성분      (= 프리미엄의 변화)
 *
 * 국내 성분에 예측력이 있는지는 직접 검정했고 기각됐다(research.html).
 * 그래서 순위·점수로 "이걸 사라"는 신호를 만들지 않는다.
 *
 * 데이터 경로:
 *   board.json      Actions가 5분마다 — 기준가(4h/24h 전), 이름, 환율, 분해 스냅샷
 *   업비트 WebSocket 초 단위 체결가 (인증·CORS 제약 없음)
 *   바이낸스 REST    60초마다 글로벌 4h 변동 (CORS 열려 있음)
 * 업비트 REST는 브라우저에서 Origin 쿼터에 걸리므로 최초 스냅샷 1회만 쓴다.
 */
(() => {
  "use strict";

  const DATA_URL = window.BOARD_DATA_URL || "data/board.json";
  const WS_URL = "wss://api.upbit.com/websocket/v1";
  const SNAPSHOT_URL = "https://api.upbit.com/v1/ticker/all?quote_currencies=KRW";
  const BINANCE_BASE = "https://api.binance.com/api/v3";
  const BINANCE_URL = BINANCE_BASE + "/ticker";

  const BOARD_REFRESH_MS = 60_000;
  const BINANCE_REFRESH_MS = 60_000;
  const RESORT_MS = 4000;
  const MARQUEE_REFRESH_MS = 3000;
  const MIN_TURNOVER_SUB = 100_000_000;
  const TOP_ROWS = 12;
  const MARQUEE_COUNT = 40;

  const VIEWS = {
    btc:      { title: "BTC 대비", decomp: false, btc: true },
    korea:    { title: "국내 단독 움직임", decomp: true },
    global:   { title: "글로벌 동반 움직임", decomp: true },
    lowvol:   { title: "저변동성", decomp: false, vol: true },
    gainers:  { title: "실시간 급등", decomp: false },
    turnover: { title: "거래대금 상위", decomp: false },
  };

  const state = {
    board: null,
    names: {},
    vol30: {},           // market -> { vol, pctile }
    btcrel: {},          // market -> { r7, rel7, r30, rel30 }
    breadth: {},         // BTC를 이긴 코인 수
    evidence: null,      // 화면에 올린 신호의 측정 근거
    base: new Map(),     // market -> { px4h, px24h, binance }
    globalChg: new Map(),// market -> 바이낸스 4h 변동(%)
    fxChg4h: 0,
    live: new Map(),
    view: "btc",
    subscribed: [],
    rows: new Map(),
    lastResort: 0,
    pending: new Set(),
    rafQueued: false,
  };

  const $ = (id) => document.getElementById(id);

  // ── 포맷 ────────────────────────────────────────────────
  const fmtPrice = (p) =>
    p == null || !isFinite(p) ? "-"
    : p >= 100 ? Math.round(p).toLocaleString("ko-KR")
    : p >= 1 ? p.toFixed(2)
    : p >= 0.01 ? p.toFixed(4)
    : p.toFixed(6);

  const fmtPct = (v) =>
    v == null || !isFinite(v) ? "-" : (v > 0 ? "+" : "") + v.toFixed(2) + "%";

  const fmtPp = (v) =>
    v == null || !isFinite(v) ? "-" : (v > 0 ? "+" : "") + v.toFixed(2) + "%p";

  const fmtTurnover = (v) =>
    !v ? "-"
    : v >= 1e12 ? (v / 1e12).toFixed(2) + "조"
    : v >= 1e8 ? Math.round(v / 1e8).toLocaleString("ko-KR") + "억"
    : Math.round(v / 1e4).toLocaleString("ko-KR") + "만";

  const dirClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

  function tickClock() {
    const d = new Date();
    const kst = new Date(d.getTime() + (d.getTimezoneOffset() * 60000) + 9 * 3600000);
    $("clock").textContent =
      [kst.getHours(), kst.getMinutes(), kst.getSeconds()]
        .map((x) => String(x).padStart(2, "0")).join(":") + " KST";
  }

  function setConn(s, label) {
    $("conn").dataset.state = s;
    $("conn-label").textContent = label;
  }

  // ── 분해 계산 ───────────────────────────────────────────
  // 업비트 실시간가와 기준가(4h 전)로 업비트 수익률을 매 틱 다시 구하고,
  // 글로벌 성분은 60초마다 갱신되는 바이낸스 값을 쓴다.
  function decompose(mk) {
    const b = state.base.get(mk);
    const live = state.live.get(mk);
    if (!b || !b.px4h || !live) return null;
    const up = (live.price / b.px4h - 1) * 100;
    const g = state.globalChg.get(mk);
    if (g == null) return null;
    const global = ((1 + g / 100) * (1 + state.fxChg4h / 100) - 1) * 100;
    return { up, global, korea: up - global };
  }

  // BTC 대비 4시간 상대 성과 — 원화로 올라도 BTC가 더 올랐으면 진 것이다.
  // 백테스트에서 알트 동일가중 2.6년 -77% vs BTC +54%였던 게 이 축의 근거다.
  function btcRel4h(mk) {
    if (mk === "KRW-BTC") return 0;
    const b = state.base.get(mk), bt = state.base.get("KRW-BTC");
    const lv = state.live.get(mk), lb = state.live.get("KRW-BTC");
    if (!b || !b.px4h || !bt || !bt.px4h || !lv || !lb) return null;
    const rc = lv.price / b.px4h - 1;
    const rb = lb.price / bt.px4h - 1;
    return ((1 + rc) / (1 + rb) - 1) * 100;
  }

  // 현재 뷰에서 등락 컬럼에 무엇을 띄울지. BTC 뷰는 원화 등락이 아니라
  // 'BTC 대비'가 의미 있는 숫자다.
  function shownChange(mk, live) {
    if (VIEWS[state.view].btc) {
      const r = btcRel4h(mk);
      if (r != null) return r;
    }
    return live ? live.chg : 0;
  }

  function verdict(d) {
    if (!d) return null;
    const { up, global: g, korea: k } = d;
    if (Math.abs(up) < 0.7) return { text: "보합", kind: "flat" };
    if (up > 0) {
      if (k > 0 && g <= 0.3) return { text: "국내 단독 상승", kind: "korea" };
      if (k > g) return { text: "국내 주도 상승", kind: "korea" };
      if (Math.abs(k) < Math.max(0.5, Math.abs(g) * 0.3))
        return { text: "글로벌 동반 상승", kind: "global" };
      return { text: "글로벌 주도 상승", kind: "global" };
    }
    if (k < 0 && g >= -0.3) return { text: "국내 단독 하락", kind: "korea" };
    if (k < g) return { text: "국내 주도 하락", kind: "korea" };
    return { text: "글로벌 동반 하락", kind: "global" };
  }

  // ── board.json ──────────────────────────────────────────
  async function fetchBoardJson() {
    const bucket = Math.floor(Date.now() / 300_000);
    const urls = DATA_URL === "data/board.json" ? [DATA_URL] : [DATA_URL, "data/board.json"];
    let lastErr;
    for (const u of urls) {
      try {
        const res = await fetch(`${u}?v=${bucket}`, { cache: "no-cache" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        return await res.json();
      } catch (e) { lastErr = e; }
    }
    throw lastErr;
  }

  async function loadBoard() {
    try {
      const b = await fetchBoardJson();
      state.board = b;
      if (b.names) state.names = b.names;

      state.vol30 = b.vol30 || {};
      state.btcrel = b.btcrel || {};
      state.breadth = b.breadth || {};
      state.evidence = b.evidence || null;
      if (b.fx && b.fx.now && b.fx.h4) {
        state.fxChg4h = (b.fx.now / b.fx.h4 - 1) * 100;
      }
      // 기준가는 base(업비트만으로 생성, 항상 존재)에서 먼저 채우고,
      // cross(스캔 시점 바이낸스 스냅샷, 러너가 451로 막히면 비어 있음)로 보강한다.
      for (const [mk, v] of Object.entries(b.base || {})) {
        if (v && v.px4h) state.base.set(mk, { px4h: v.px4h, px24h: v.px24h });
      }
      for (const it of b.items || []) {
        if (it.px_4h && !state.base.has(it.market)) {
          state.base.set(it.market, { px4h: it.px_4h, px24h: it.px_24h });
        }
      }
      for (const [mk, c] of Object.entries(b.cross || {})) {
        const cur = state.base.get(mk) || {};
        state.base.set(mk, {
          px4h: c.px4h != null ? c.px4h : cur.px4h,
          px24h: c.px24h != null ? c.px24h : cur.px24h,
          binance: c.binance,
        });
        // 스캔 시점의 글로벌 값을 우선 채워두고, 바이낸스 호출이 오면 덮어쓴다
        if (c["4h"] && !state.globalChg.has(mk)) {
          state.globalChg.set(mk, c["4h"].global);
        }
      }

      $("board-meta").textContent =
        `${state.base.size}종목 · 스캔 ${b.generated_at_kst || "-"} KST`;
      $("foot-scan").textContent = `${b.generated_at_kst || "-"} KST`;

      renderWatchMarquee();
      renderRows(true);
    } catch (e) {
      console.warn("board.json 실패:", e.message);
      if (!state.board) {
        $("board-meta").textContent = "분해 데이터를 불러오지 못했습니다 — 실시간 시세만 표시합니다";
        if (VIEWS[state.view].decomp) switchView("gainers");
      }
    }
  }

  // ── 업비트 최초 스냅샷 (REST 1회) ───────────────────────
  async function loadSnapshot() {
    try {
      const res = await fetch(SNAPSHOT_URL);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const arr = await res.json();
      for (const t of arr) {
        state.live.set(t.market, {
          price: t.trade_price,
          chg: (t.signed_change_rate || 0) * 100,
          turnover: t.acc_trade_price_24h || 0,
        });
      }
      state.subscribed = arr
        .filter((t) => (t.acc_trade_price_24h || 0) >= MIN_TURNOVER_SUB)
        .sort((a, b) => b.acc_trade_price_24h - a.acc_trade_price_24h)
        .map((t) => t.market);
      return true;
    } catch (e) {
      console.warn("스냅샷 실패:", e.message);
      return false;
    }
  }

  // ── 바이낸스 대응 종목 매핑 (브라우저에서 직접) ─────────
  // 스캐너는 GitHub Actions(미국 IP)에서 도는데 바이낸스가 451로 막는다.
  // 그래서 어느 코인이 바이낸스에 있는지, 티커가 같아도 다른 토큰은 아닌지를
  // 브라우저가 직접 확인한다. 사용자는 바이낸스에 접근 가능한 곳에서 보기 때문이다.
  async function loadBinanceSymbols() {
    try {
      const res = await fetch(`${BINANCE_BASE}/ticker/price`);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const prices = new Map();
      for (const x of await res.json()) prices.set(x.symbol, parseFloat(x.price));

      const fxNow = state.board && state.board.fx && state.board.fx.now;
      let matched = 0, collided = 0;
      for (const [mk, b] of state.base) {
        const sym = mk.split("-")[1];
        if (sym === "USDT") continue;
        const bp = prices.get(sym + "USDT");
        if (!bp) continue;
        // 티커가 같아도 다른 토큰인 경우가 있다(예: DATA에서 김프 +23,776% 관측).
        // 김프가 상식 밖이면 매칭에서 제외한다.
        const live = state.live.get(mk);
        if (live && fxNow) {
          const kimp = (live.price / (bp * fxNow) - 1) * 100;
          if (Math.abs(kimp) > 5) { collided++; continue; }
        }
        b.binance = sym;
        matched++;
      }
      console.info(`바이낸스 매칭 ${matched}종목 (티커충돌 제외 ${collided})`);
    } catch (e) {
      console.warn("바이낸스 심볼 목록 실패:", e.message);
    }
  }

  // ── 바이낸스 글로벌 4h 변동 (CORS 열려 있어 브라우저 직접 호출) ──
  async function loadGlobal() {
    const syms = [];
    const map = new Map();
    for (const [mk, b] of state.base) {
      if (b.binance) {
        const s = b.binance + "USDT";
        syms.push(s);
        map.set(s, mk);
      }
    }
    if (!syms.length) return;
    try {
      const q = encodeURIComponent(JSON.stringify(syms));
      const res = await fetch(`${BINANCE_URL}?symbols=${q}&windowSize=4h`);
      if (!res.ok) throw new Error("HTTP " + res.status);
      for (const x of await res.json()) {
        const mk = map.get(x.symbol);
        if (mk) state.globalChg.set(mk, parseFloat(x.priceChangePercent));
      }
      if (VIEWS[state.view].decomp) renderRows(false);
    } catch (e) {
      console.warn("바이낸스 실패:", e.message);
    }
  }

  // ── WebSocket ───────────────────────────────────────────
  let ws = null, retry = 0, pingTimer = null;

  function connect() {
    if (!state.subscribed.length) return;
    setConn("connecting", "연결 중");
    try { ws = new WebSocket(WS_URL); } catch { return scheduleReconnect(); }
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      retry = 0;
      setConn("live", "LIVE");
      ws.send(JSON.stringify([
        { ticket: "coinboard-" + Math.random().toString(36).slice(2, 8) },
        { type: "ticker", codes: state.subscribed },
        { format: "DEFAULT" },
      ]));
      clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send("PING");
      }, 50_000);
    };

    ws.onmessage = (ev) => {
      const txt = typeof ev.data === "string" ? ev.data : new TextDecoder("utf-8").decode(ev.data);
      let d;
      try { d = JSON.parse(txt); } catch { return; }
      if (!d || !d.code) return;
      state.live.set(d.code, {
        price: d.trade_price,
        chg: (d.signed_change_rate || 0) * 100,
        turnover: d.acc_trade_price_24h || 0,
      });
      queuePaint(d.code);
    };

    ws.onclose = () => { clearInterval(pingTimer); scheduleReconnect(); };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }

  function scheduleReconnect() {
    // 업비트는 WS 연결 자체에 한도가 있어(초당 5회·분당 100회) 실패 시 빠르게
    // 재시도하면 429를 스스로 악화시킨다. 시작 간격을 늘리고 지터를 섞는다.
    const base = Math.min(60_000, 2000 * Math.pow(2, retry++));
    const wait = base * (0.7 + Math.random() * 0.6);
    setConn("down", `재연결 ${Math.round(wait / 1000)}초 후`);
    setTimeout(() => {
      if (document.hidden) return setConn("down", "일시중지");
      connect();
    }, wait);
  }

  function queuePaint(mk) {
    state.pending.add(mk);
    if (state.rafQueued) return;
    state.rafQueued = true;
    requestAnimationFrame(flushPaint);
  }

  function flushPaint() {
    state.rafQueued = false;
    const marks = state.pending;
    state.pending = new Set();
    for (const mk of marks) {
      const row = state.rows.get(mk);
      if (row) updateRow(row);
    }
    if (Date.now() - state.lastResort > RESORT_MS) {
      state.lastResort = Date.now();
      renderRows(false);
    }
  }

  // ── 스플릿플랩 ──────────────────────────────────────────
  function buildFlaps(container, str) {
    container.textContent = "";
    const frag = document.createDocumentFragment();
    for (const ch of str) {
      const s = document.createElement("span");
      s.className = "flap" + (/[0-9]/.test(ch) ? "" : " sep");
      s.textContent = ch;
      frag.appendChild(s);
    }
    container.appendChild(frag);
  }

  function setFlaps(container, str, animate) {
    const flaps = container.children;
    if (flaps.length !== str.length) return buildFlaps(container, str);
    for (let i = 0; i < str.length; i++) {
      const el = flaps[i];
      if (el.textContent === str[i]) continue;
      if (!animate) { el.textContent = str[i]; continue; }
      el.classList.remove("flip");
      void el.offsetWidth;
      el.classList.add("flip");
      const ch = str[i];
      setTimeout(() => { el.textContent = ch; }, 130);
    }
  }

  // ── 행 ──────────────────────────────────────────────────
  function createRow(mk) {
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML = `
      <span class="c-rank"></span>
      <span class="c-name"><span class="n-ko"></span><span class="n-sym"></span></span>
      <span class="c-price"></span>
      <span class="c-chg"></span>
      <span class="c-score"></span>`;
    const row = {
      market: mk, el,
      rank: el.querySelector(".c-rank"),
      ko: el.querySelector(".n-ko"),
      sym: el.querySelector(".n-sym"),
      price: el.querySelector(".c-price"),
      chg: el.querySelector(".c-chg"),
      score: el.querySelector(".c-score"),
      lastPrice: null, lastFlipAt: 0,
    };
    state.rows.set(mk, row);
    return row;
  }

  function updateRow(row) {
    const live = state.live.get(row.market);
    if (!live) return;
    const now = Date.now();
    const prev = row.lastPrice;
    const changed = prev != null && live.price !== prev;
    const animate = changed && now - row.lastFlipAt > 220;

    setFlaps(row.price, fmtPrice(live.price), animate);
    if (animate) row.lastFlipAt = now;
    row.lastPrice = live.price;

    // 같은 d로 등락률과 분해를 함께 갱신해야 항등식이 화면에서도 맞는다
    const d = VIEWS[state.view].decomp ? decompose(row.market) : null;
    const shown = d ? d.up : shownChange(row.market, live);
    row.price.classList.toggle("up", shown > 0);
    row.price.classList.toggle("down", shown < 0);
    row.chg.textContent = fmtPct(shown);
    row.chg.className = "c-chg " + dirClass(shown);
    if (VIEWS[state.view].decomp) paintDecomp(row, d);

    if (changed) {
      const cls = live.price > prev ? "tick-up" : "tick-down";
      row.el.classList.remove("tick-up", "tick-down");
      void row.el.offsetWidth;
      row.el.classList.add(cls);
      setTimeout(() => row.el.classList.remove(cls), 600);
    }
  }

  // 분해 셀은 한 번만 조립하고 이후엔 텍스트/폭만 갱신한다.
  // 매 틱 innerHTML을 다시 쓰면 느릴 뿐 아니라, 가격과 분해 숫자가 서로 다른
  // 시점의 값이 되어 '업비트 = 글로벌 + 국내' 항등식이 화면에서 깨져 보인다.
  function buildScoreCell(row) {
    row.score.innerHTML = `
      <span class="score-line">
        <span class="dbar"><i class="axis"></i><i class="sg"></i><i class="sk"></i></span>
        <span class="dnums">
          <b class="c-g"></b><span class="plus">+</span><b class="c-k"></b>
        </span>
        <span class="tag verdict"></span>
      </span>`;
    row.segG = row.score.querySelector(".sg");
    row.segK = row.score.querySelector(".sk");
    row.numG = row.score.querySelector(".c-g");
    row.numK = row.score.querySelector(".c-k");
    row.verdictEl = row.score.querySelector(".verdict");
    row.built = "decomp";
  }

  function paintDecomp(row, d) {
    if (!d) {
      row.score.innerHTML =
        `<span class="live-note">글로벌 대응 종목 없음 (바이낸스 미상장)</span>`;
      row.built = "none";
      return;
    }
    if (row.built !== "decomp") buildScoreCell(row);

    const scale = state.barScale || 2;
    const pos = (v) => Math.max(0, Math.min(100, 50 + (v / scale) * 50));
    const gEnd = pos(d.global), tEnd = pos(d.up);
    const place = (el, a, b) => {
      const l = Math.min(a, b);
      el.style.left = l + "%";
      el.style.width = Math.max(0, Math.max(a, b) - l) + "%";
    };
    place(row.segG, 50, gEnd);
    place(row.segK, gEnd, tEnd);

    row.numG.textContent = fmtPp(d.global);
    row.numK.textContent = fmtPp(d.korea);
    const v = verdict(d);
    row.verdictEl.textContent = v.text;
    row.verdictEl.className = "tag verdict " + v.kind;
  }

  function renderScoreCell(row) {
    if (VIEWS[state.view].decomp) {
      paintDecomp(row, decompose(row.market));
    } else if (VIEWS[state.view].btc) {
      const r = state.btcrel[row.market];
      if (!r) {
        row.score.innerHTML = `<span class="live-note">일봉 데이터 없음</span>`;
        row.built = "none";
        return;
      }
      const s = state.barScale || 20;
      const pos = (v) => Math.max(0, Math.min(100, 50 + (v / s) * 50));
      const end = pos(r.rel7);
      const l = Math.min(50, end), w = Math.abs(end - 50);
      row.score.innerHTML = `
        <span class="score-line">
          <span class="dbar"><i class="axis"></i>
            <i class="sr ${r.rel7 > 0 ? "up" : "down"}" style="left:${l}%;width:${w}%"></i></span>
          <span class="dnums">7일 <b class="${dirClass(r.rel7)}">${fmtPct(r.rel7)}</b>
            <span class="plus">·</span> 30일
            <b class="${dirClass(r.rel30)}">${r.rel30 == null ? "-" : fmtPct(r.rel30)}</b></span>
        </span>`;
      row.built = "btc";
    } else if (VIEWS[state.view].vol) {
      const v = state.vol30[row.market];
      row.score.innerHTML = v
        ? `<span class="score-line">
             <span class="gauge"><i style="width:${Math.max(3, v.pctile)}%"></i></span>
             <span class="live-note">일간 변동성 <b>${v.vol.toFixed(2)}%</b>
               · 유니버스 하위 <b>${v.pctile}%</b></span>
           </span>`
        : `<span class="live-note">변동성 데이터 없음</span>`;
      row.built = "vol";
    } else {
      const live = state.live.get(row.market) || {};
      row.score.innerHTML =
        `<span class="live-note">24H 거래대금 <b>${fmtTurnover(live.turnover)}</b></span>`;
      row.built = "turnover";
    }
  }

  // ── 목록 ────────────────────────────────────────────────
  function currentList() {
    const view = state.view;
    if (view === "korea" || view === "global") {
      const arr = [];
      for (const mk of state.base.keys()) {
        const d = decompose(mk);
        if (!d) continue;
        const live = state.live.get(mk);
        if (!live || live.turnover < MIN_TURNOVER_SUB) continue;
        arr.push({ mk, d });
      }
      // |국내| 절대값으로만 줄세우면 글로벌이 더 크게 움직인 종목이 '국내 단독'
      // 1위로 올라온다. 우세도(한쪽이 다른 쪽보다 얼마나 큰가)로 정렬해야 한다.
      const dom = (d) => Math.abs(d.korea) - Math.abs(d.global);
      if (view === "korea") arr.sort((a, b) => dom(b.d) - dom(a.d));
      else arr.sort((a, b) => dom(a.d) - dom(b.d));
      return arr.slice(0, TOP_ROWS).map((x) => x.mk);
    }
    if (view === "btc") {
      // 순위는 7일 상대성과(안정적)로, 표시되는 등락은 실시간 4시간 상대성과로.
      return Object.entries(state.btcrel)
        .filter(([mk]) => (state.live.get(mk) || {}).turnover >= MIN_TURNOVER_SUB)
        .sort((a, b) => b[1].rel7 - a[1].rel7)
        .slice(0, TOP_ROWS)
        .map(([mk]) => mk);
    }
    if (view === "lowvol") {
      return Object.entries(state.vol30)
        .filter(([mk]) => (state.live.get(mk) || {}).turnover >= MIN_TURNOVER_SUB)
        .sort((a, b) => a[1].vol - b[1].vol)
        .slice(0, TOP_ROWS)
        .map(([mk]) => mk);
    }
    const arr = state.subscribed
      .map((mk) => ({ mk, l: state.live.get(mk) }))
      .filter((x) => x.l);
    if (view === "gainers") arr.sort((a, b) => b.l.chg - a.l.chg);
    else arr.sort((a, b) => b.l.turnover - a.l.turnover);
    return arr.slice(0, TOP_ROWS).map((x) => x.mk);
  }

  function renderRows(full) {
    const list = currentList();
    const container = $("rows");
    if (!list.length) {
      container.innerHTML = `<div class="skeleton">표시할 데이터가 없습니다</div>`;
      state.rows.clear();
      return;
    }
    const sk = container.querySelector(".skeleton");
    if (sk) sk.remove();

    // 막대 공통 스케일 — 행끼리 크기를 비교할 수 있어야 한다
    let scale = 2;
    if (VIEWS[state.view].decomp) {
      for (const mk of list) {
        const d = decompose(mk);
        if (d) scale = Math.max(scale, Math.abs(d.up), Math.abs(d.global));
      }
      scale = Math.ceil(scale * 1.1);
    } else if (VIEWS[state.view].btc) {
      for (const mk of list) {
        const r = state.btcrel[mk];
        if (r) scale = Math.max(scale, Math.abs(r.rel7));
      }
      scale = Math.ceil(scale * 1.1);
    }
    state.barScale = scale;

    const seen = new Set();
    list.forEach((mk, i) => {
      seen.add(mk);
      let row = state.rows.get(mk) || createRow(mk);
      if (container.children[i] !== row.el) {
        container.insertBefore(row.el, container.children[i] || null);
      }
      row.rank.textContent = i + 1;
      row.ko.textContent = state.names[mk] || mk.split("-")[1];
      row.sym.textContent = mk.split("-")[1] + " / KRW";

      const live = state.live.get(mk);
      if (live && (full || row.lastPrice == null)) {
        setFlaps(row.price, fmtPrice(live.price), false);
        row.lastPrice = live.price;
      }
      if (live) {
        const d = VIEWS[state.view].decomp ? decompose(mk) : null;
        const shown = d ? d.up : shownChange(mk, live);
        row.price.classList.toggle("up", shown > 0);
        row.price.classList.toggle("down", shown < 0);
        row.chg.textContent = fmtPct(shown);
        row.chg.className = "c-chg " + dirClass(shown);
      }
      renderScoreCell(row);
    });

    for (const [mk, row] of state.rows) {
      if (!seen.has(mk)) { row.el.remove(); state.rows.delete(mk); }
    }
  }

  // ── 마퀴 ────────────────────────────────────────────────
  function fillMarquee(trackId, markets) {
    const track = $(trackId);
    track.textContent = "";
    for (let pass = 0; pass < 2; pass++) {
      for (const mk of markets) {
        const el = document.createElement("span");
        el.className = "mq-item";
        el.dataset.market = mk;
        el.innerHTML = `<span class="mq-sym"></span><span class="mq-price"></span><span class="mq-chg"></span>`;
        track.appendChild(el);
      }
    }
    paintMarquee(trackId);
  }

  function paintMarquee(trackId) {
    for (const el of $(trackId).children) {
      const mk = el.dataset.market;
      const live = state.live.get(mk);
      const sym = mk.split("-")[1];
      const name = state.names[mk];
      el.querySelector(".mq-sym").textContent = name ? `${name}(${sym})` : sym;
      if (!live) continue;
      el.querySelector(".mq-price").textContent = fmtPrice(live.price);
      const d = decompose(mk);
      if (d) {
        el.querySelector(".mq-chg").textContent = `국내 ${fmtPp(d.korea)}`;
        el.className = "mq-item " + dirClass(d.korea);
      } else {
        el.querySelector(".mq-chg").textContent = fmtPct(live.chg);
        el.className = "mq-item " + dirClass(live.chg);
      }
    }
  }

  const renderTopMarquee = () => fillMarquee("track-top", state.subscribed.slice(0, MARQUEE_COUNT));

  function renderWatchMarquee() {
    // 국내 성분이 글로벌보다 우세한 종목들을 하단에 흘린다
    const arr = [];
    for (const mk of state.base.keys()) {
      const d = decompose(mk);
      if (d) arr.push({ mk, k: Math.abs(d.korea) - Math.abs(d.global) });
    }
    arr.sort((a, b) => b.k - a.k);
    if (arr.length) fillMarquee("track-bottom", arr.slice(0, 14).map((x) => x.mk));
  }

  // 신호를 화면에 올릴 때는 반드시 측정된 근거를 같이 보여준다.
  // 검정을 통과하지 못한 것은 애초에 올리지 않는다.
  function renderExplain() {
    const el = $("explain");
    if (VIEWS[state.view].decomp) {
      el.style.display = "";
      el.innerHTML = `업비트의 4시간 변동을 <b class="c-g">글로벌 성분</b>(바이낸스 · 환율 반영)과
        <b class="c-k">국내 성분</b>(그 차이)으로 나눈 값입니다.
        같은 <b>+5%</b>라도 글로벌이 함께 오른 것인지 국내에서만 오른 것인지는 완전히 다른 상황이며,
        이 구분은 두 거래소를 동시에 봐야만 나옵니다.
        <span class="ev">이건 예측이 아니라 항등식에 의한 분해입니다.</span>`;
      return;
    }
    if (VIEWS[state.view].btc) {
      el.style.display = "";
      const b7 = state.breadth["7d"] || {}, b30 = state.breadth["30d"] || {};
      const e = state.evidence && state.evidence.portfolio;
      const pct = (x) => (x && x.total ? Math.round(x.beat / x.total * 100) : null);
      el.innerHTML = `원화 등락률만 보면 <b>"BTC 대신 이 알트를 들 이유가 있나"</b>가 안 보입니다.
        원화로 +5% 올라도 BTC가 +8% 올랐으면 진 것입니다.
        ${b7.total ? `지금 <b>7일간 BTC를 이긴 코인 ${b7.beat}/${b7.total}개(${pct(b7)}%)</b>,
        30일 기준 <b>${b30.beat}/${b30.total}개(${pct(b30)}%)</b>입니다.` : ""}
        ${e ? `<span class="ev">근거 — ${e.period}(${e.years}년) 백테스트:
        알트 전 종목 동일가중 <b class="down">${e.equal.cum}%</b>,
        저변동 하위 20% <b class="down">${e.lowvol.cum}%</b>,
        고변동 상위 20% <b class="down">${e.highvol.cum}%</b> ·
        <b class="up">비트코인 보유 +${e.btc.cum}%</b>.
        알트를 고르는 것보다 고르지 않는 편이 나았습니다.
        <a href="research.html">검정 상세</a></span>` : ""}`;
      return;
    }
    if (VIEWS[state.view].vol) {
      el.style.display = "";
      const e = state.evidence && state.evidence.vol30;
      el.innerHTML = `30일 실현변동성이 낮은 순입니다. 팩터 검정에서 <b>유일하게 살아남은 신호</b>입니다.
        ${e ? `<span class="ev">측정(${e.basis}): 7일 지평 IC <b>+${e.ic_7d}</b> (t=${e.t_7d},
        비중첩 표본 ${e.samples_7d}개) · 1일 IC +${e.ic_1d} (t=${e.t_1d}, ${e.samples_1d}개)
        · ${e.days}일 ${e.symbols}종목, ${e.period}
        · 분위 스프레드 <b>+${e.spread_7d}%p</b>/7일 vs 왕복비용 ${e.fee}%p.
        <a href="research.html">검정 상세</a></span>` : ""}`;
      return;
    }
    el.style.display = "none";
  }

  // ── 탭 ──────────────────────────────────────────────────
  function switchView(view) {
    state.view = view;
    for (const b of $("tabs").children) b.classList.toggle("is-on", b.dataset.view === view);
    $("view-title").textContent = VIEWS[view].title;
    // 글로벌/국내 범례는 분해 뷰에서만 의미가 있다
    const lg = $("legend");
    if (lg) lg.style.display = VIEWS[view].decomp ? "" : "none";
    renderExplain();
    $("col-head").querySelector(".c-score").textContent =
      VIEWS[view].decomp ? "글로벌 / 국내 분해"
      : VIEWS[view].vol ? "30일 실현변동성"
      : VIEWS[view].btc ? "BTC 대비 7일 / 30일" : "거래대금";
    $("col-head").querySelector(".c-chg").textContent =
      VIEWS[view].decomp ? "4H" : VIEWS[view].btc ? "BTC대비 4H" : "24H";
    $("rows").textContent = "";
    state.rows.clear();
    state.lastResort = Date.now();
    renderRows(true);
  }

  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (b) switchView(b.dataset.view);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && (!ws || ws.readyState > 1)) { retry = 0; connect(); }
  });

  // ── 부팅 ────────────────────────────────────────────────
  async function boot() {
    tickClock();
    setInterval(tickClock, 1000);

    await loadBoard();
    const ok = await loadSnapshot();
    if (!ok) setConn("down", "시세 연결 실패");

    await loadBinanceSymbols();
    await loadGlobal();
    const lg0 = $("legend");
    if (lg0) lg0.style.display = VIEWS[state.view].decomp ? "" : "none";
    renderExplain();
    renderTopMarquee();
    renderWatchMarquee();
    renderRows(true);
    connect();

    setInterval(loadBoard, BOARD_REFRESH_MS);
    setInterval(loadGlobal, BINANCE_REFRESH_MS);
    setInterval(() => {
      paintMarquee("track-top");
      paintMarquee("track-bottom");
    }, MARQUEE_REFRESH_MS);
  }

  boot();
})();
