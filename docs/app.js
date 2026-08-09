/* COIN BOARD — 업비트 실시간 전광판
 *
 * 데이터 경로 두 갈래:
 *   1) board.json  — GitHub Actions가 5분마다 캔들·지표·스코어를 계산해 만든 추천 결과
 *   2) WebSocket   — 업비트 public 스트림. 인증 없음, 초 단위 체결가
 *
 * 브라우저에서 업비트 REST를 부를 때는 Origin 헤더 때문에 group=origin 쿼터로
 * 강등되어 사실상 1req/s에서 429가 난다. 그래서 REST는 최초 스냅샷 1회만 쓰고
 * 이후 갱신은 전부 WebSocket으로 받는다.
 */
(() => {
  "use strict";

  const DATA_URL = window.BOARD_DATA_URL || "data/board.json";
  const WS_URL = "wss://api.upbit.com/websocket/v1";
  const SNAPSHOT_URL = "https://api.upbit.com/v1/ticker/all?quote_currencies=KRW";

  const BOARD_REFRESH_MS = 60_000;   // board.json 재확인 주기
  const RESORT_MS = 2500;            // 실시간 뷰 재정렬 주기 (매 틱마다 하면 못 읽는다)
  const MARQUEE_REFRESH_MS = 3000;
  const MIN_TURNOVER_SUB = 100_000_000; // 구독 대상 최소 24h 거래대금 (1억)
  const TOP_ROWS = 12;
  const MARQUEE_COUNT = 40;

  // ── 상태 ────────────────────────────────────────────────
  const state = {
    board: null,
    names: {},          // market -> 한글명
    live: new Map(),    // market -> { price, chg, turnover, ts }
    view: "reco",
    subscribed: [],
    rows: new Map(),    // market -> { el, els..., lastPrice, lastFlipAt }
    lastResort: 0,
    pending: new Set(), // rAF로 합쳐서 그릴 대상
    rafQueued: false,
  };

  const $ = (id) => document.getElementById(id);

  // ── 포맷터 ──────────────────────────────────────────────
  function fmtPrice(p) {
    if (p == null || !isFinite(p)) return "-";
    if (p >= 100) return Math.round(p).toLocaleString("ko-KR");
    if (p >= 1) return p.toFixed(2);
    if (p >= 0.01) return p.toFixed(4);
    return p.toFixed(6);
  }

  function fmtPct(v) {
    if (v == null || !isFinite(v)) return "-";
    return (v > 0 ? "+" : "") + v.toFixed(2) + "%";
  }

  function fmtTurnover(v) {
    if (!v) return "-";
    if (v >= 1e12) return (v / 1e12).toFixed(2) + "조";
    if (v >= 1e8) return Math.round(v / 1e8).toLocaleString("ko-KR") + "억";
    if (v >= 1e4) return Math.round(v / 1e4).toLocaleString("ko-KR") + "만";
    return Math.round(v).toLocaleString("ko-KR");
  }

  const dirClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

  // ── 시계 ────────────────────────────────────────────────
  function tickClock() {
    const kst = new Date(Date.now() + (new Date().getTimezoneOffset() * 60000) + 9 * 3600000);
    $("clock").textContent =
      String(kst.getHours()).padStart(2, "0") + ":" +
      String(kst.getMinutes()).padStart(2, "0") + ":" +
      String(kst.getSeconds()).padStart(2, "0") + " KST";
  }

  function setConn(stateName, label) {
    const el = $("conn");
    el.dataset.state = stateName;
    $("conn-label").textContent = label;
  }

  // ── board.json ──────────────────────────────────────────
  async function fetchBoardJson() {
    // 5분 캐시를 우회하려는 게 아니라, CDN이 오래된 사본을 물고 있을 때
    // 최소한 5분 버킷 단위로는 새 URL이 되게 한다.
    const bucket = Math.floor(Date.now() / 300_000);
    const urls = DATA_URL === "data/board.json" ? [DATA_URL] : [DATA_URL, "data/board.json"];
    let lastErr;
    for (const u of urls) {
      try {
        const res = await fetch(`${u}?v=${bucket}`, { cache: "no-cache" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        return await res.json();
      } catch (e) {
        lastErr = e;  // data 브랜치가 아직 없으면 저장소에 커밋된 사본으로 떨어진다
      }
    }
    throw lastErr;
  }

  async function loadBoard() {
    try {
      const b = await fetchBoardJson();
      state.board = b;
      if (b.names) state.names = b.names;

      $("board-meta").textContent =
        `${b.universe_size}개 종목 분석 · 스캔 ${b.generated_at_kst || "-"} KST`;
      $("foot-scan").textContent = `${b.generated_at_kst || "-"} KST 기준`;

      renderWatchMarquee();
      if (state.view === "reco") renderRows(true);
    } catch (e) {
      console.warn("board.json 로드 실패:", e.message);
      if (!state.board) {
        $("board-meta").textContent =
          "추천 데이터를 불러오지 못했습니다 — 실시간 시세만 표시합니다";
        if (state.view === "reco") switchView("gainers");
      }
    }
  }

  // ── 최초 스냅샷 (REST 1회) ──────────────────────────────
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
          ts: t.timestamp || Date.now(),
        });
      }
      // 구독 대상: 거래대금이 어느 정도 있는 종목만. 전 종목을 구독하면
      // 모바일에서 의미 없는 트래픽이 늘고, 유동성 없는 코인은 어차피 안 움직인다.
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

  // ── WebSocket ───────────────────────────────────────────
  let ws = null;
  let retry = 0;
  let pingTimer = null;

  function connect() {
    if (!state.subscribed.length) return;
    setConn("connecting", "연결 중");

    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      scheduleReconnect();
      return;
    }
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
      let txt;
      if (typeof ev.data === "string") txt = ev.data;
      else txt = new TextDecoder("utf-8").decode(ev.data);

      let d;
      try { d = JSON.parse(txt); } catch { return; }
      if (!d || !d.code) return; // PONG 응답 등은 무시

      state.live.set(d.code, {
        price: d.trade_price,
        chg: (d.signed_change_rate || 0) * 100,
        turnover: d.acc_trade_price_24h || 0,
        ts: d.timestamp || Date.now(),
      });
      queuePaint(d.code);
    };

    ws.onclose = () => {
      clearInterval(pingTimer);
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }

  function scheduleReconnect() {
    setConn("down", "재연결 대기");
    const wait = Math.min(30_000, 1000 * Math.pow(2, retry++));
    setTimeout(() => {
      if (document.hidden) return setConn("down", "일시중지");
      connect();
    }, wait);
  }

  // ── 페인트 스케줄러 ─────────────────────────────────────
  // WS는 초당 수십 건이 들어온다. 매 건마다 DOM을 만지면 애니메이션이 뭉개지므로
  // rAF 한 프레임에 모아서 처리한다.
  function queuePaint(market) {
    state.pending.add(market);
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
      if (row) updateRow(row, state.live.get(mk));
    }

    // 실시간 뷰는 주기적으로만 순위를 다시 매긴다
    if (state.view !== "reco" && Date.now() - state.lastResort > RESORT_MS) {
      state.lastResort = Date.now();
      renderRows(false);
    }
  }

  // ── 스플릿플랩 가격 ─────────────────────────────────────
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
    if (flaps.length !== str.length) { buildFlaps(container, str); return; }
    for (let i = 0; i < str.length; i++) {
      const el = flaps[i];
      const ch = str[i];
      if (el.textContent === ch) continue;
      if (!animate) { el.textContent = ch; continue; }
      el.classList.remove("flip");
      void el.offsetWidth;          // 리플로우로 애니메이션 재시작
      el.classList.add("flip");
      setTimeout(() => { el.textContent = ch; }, 130); // 뒤집힌 순간에 교체
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
      market: mk,
      el,
      rank: el.querySelector(".c-rank"),
      ko: el.querySelector(".n-ko"),
      sym: el.querySelector(".n-sym"),
      price: el.querySelector(".c-price"),
      chg: el.querySelector(".c-chg"),
      score: el.querySelector(".c-score"),
      lastPrice: null,
      lastFlipAt: 0,
    };
    state.rows.set(mk, row);
    return row;
  }

  function updateRow(row, live) {
    if (!live) return;
    const now = Date.now();
    const prev = row.lastPrice;
    const changed = prev != null && live.price !== prev;

    // 초당 여러 번 뒤집히면 읽을 수가 없다. 행당 최소 220ms 간격.
    const animate = changed && now - row.lastFlipAt > 220;
    setFlaps(row.price, fmtPrice(live.price), animate);
    if (animate) row.lastFlipAt = now;
    row.lastPrice = live.price;

    row.price.classList.toggle("up", live.chg > 0);
    row.price.classList.toggle("down", live.chg < 0);

    row.chg.textContent = fmtPct(live.chg);
    row.chg.className = "c-chg " + dirClass(live.chg);

    if (changed) {
      const cls = live.price > prev ? "tick-up" : "tick-down";
      row.el.classList.remove("tick-up", "tick-down");
      void row.el.offsetWidth;
      row.el.classList.add(cls);
      setTimeout(() => row.el.classList.remove(cls), 600);
    }
  }

  function renderScoreCell(row, item) {
    if (state.view === "reco" && item) {
      const tags = (item.tags || []).map((t) => {
        const warn = t === "과열" || t === "급등후" ? " warn" : "";
        return `<span class="tag${warn}">${escapeHtml(t)}</span>`;
      }).join("");
      row.score.innerHTML = `
        <span class="score-line">
          <span class="score-num">${Number(item.score).toFixed(1)}</span>
          <span class="gauge"><i style="width:${Math.max(4, Math.min(100, item.score))}%"></i></span>
          <span class="tags">${tags}</span>
        </span>`;
    } else {
      const live = state.live.get(row.market) || {};
      row.score.innerHTML =
        `<span class="live-note">24H 거래대금 <b>${fmtTurnover(live.turnover)}</b></span>`;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── 목록 산출 ───────────────────────────────────────────
  function currentList() {
    if (state.view === "reco") {
      const items = (state.board && state.board.items) || [];
      return items.map((it) => ({ market: it.market, item: it }));
    }
    const arr = state.subscribed
      .map((mk) => ({ mk, l: state.live.get(mk) }))
      .filter((x) => x.l);

    if (state.view === "gainers") arr.sort((a, b) => b.l.chg - a.l.chg);
    else arr.sort((a, b) => b.l.turnover - a.l.turnover);

    return arr.slice(0, TOP_ROWS).map((x) => ({ market: x.mk, item: null }));
  }

  function renderRows(full) {
    const list = currentList();
    const container = $("rows");

    if (!list.length) {
      container.innerHTML = `<div class="skeleton">표시할 데이터가 없습니다</div>`;
      return;
    }
    const sk = container.querySelector(".skeleton");
    if (sk) sk.remove();

    const seen = new Set();
    list.forEach((entry, i) => {
      const mk = entry.market;
      seen.add(mk);
      let row = state.rows.get(mk);
      if (!row) row = createRow(mk);

      // 순서가 바뀌었을 때만 DOM을 움직인다
      if (container.children[i] !== row.el) {
        container.insertBefore(row.el, container.children[i] || null);
      }

      row.rank.textContent = i + 1;
      const nm = (entry.item && entry.item.name) || state.names[mk] || mk.split("-")[1];
      const caution = entry.item && entry.item.caution;
      row.ko.innerHTML = escapeHtml(nm) +
        (caution ? `<span class="badge-caution">유의</span>` : "");
      row.sym.textContent = mk.split("-")[1] + " / KRW";

      const live = state.live.get(mk);
      if (live) {
        if (full || row.lastPrice == null) {
          setFlaps(row.price, fmtPrice(live.price), false);
          row.lastPrice = live.price;
          row.price.classList.toggle("up", live.chg > 0);
          row.price.classList.toggle("down", live.chg < 0);
          row.chg.textContent = fmtPct(live.chg);
          row.chg.className = "c-chg " + dirClass(live.chg);
        }
      } else if (entry.item) {
        setFlaps(row.price, fmtPrice(entry.item.price), false);
        row.chg.textContent = fmtPct(entry.item.chg24);
        row.chg.className = "c-chg " + dirClass(entry.item.chg24);
      }

      renderScoreCell(row, entry.item);
    });

    // 목록에서 빠진 행 정리
    for (const [mk, row] of state.rows) {
      if (!seen.has(mk)) {
        row.el.remove();
        state.rows.delete(mk);
      }
    }
  }

  // ── 마퀴 ────────────────────────────────────────────────
  function marqueeItem(mk) {
    const el = document.createElement("span");
    el.className = "mq-item";
    el.innerHTML = `<span class="mq-sym"></span><span class="mq-price"></span><span class="mq-chg"></span>`;
    el.dataset.market = mk;
    return el;
  }

  function fillMarquee(trackId, markets, withScore) {
    const track = $(trackId);
    track.textContent = "";
    // 이어붙여 무한 스크롤을 만들기 위해 같은 내용을 두 벌 넣는다 (-50% 이동)
    for (let pass = 0; pass < 2; pass++) {
      for (const m of markets) {
        const mk = typeof m === "string" ? m : m.market;
        const el = marqueeItem(mk);
        if (withScore && typeof m !== "string") el.dataset.score = m.score;
        track.appendChild(el);
      }
    }
    paintMarquee(trackId);
  }

  function paintMarquee(trackId) {
    const track = $(trackId);
    for (const el of track.children) {
      const mk = el.dataset.market;
      const live = state.live.get(mk);
      const sym = mk.split("-")[1];
      const name = state.names[mk];
      el.querySelector(".mq-sym").textContent = name ? `${name}(${sym})` : sym;
      if (live) {
        el.querySelector(".mq-price").textContent = fmtPrice(live.price);
        el.querySelector(".mq-chg").textContent =
          el.dataset.score ? `★${el.dataset.score}` : fmtPct(live.chg);
        el.className = "mq-item " + dirClass(live.chg);
      } else {
        el.querySelector(".mq-price").textContent = "-";
        el.querySelector(".mq-chg").textContent = el.dataset.score ? `★${el.dataset.score}` : "";
      }
    }
  }

  function renderTopMarquee() {
    fillMarquee("track-top", state.subscribed.slice(0, MARQUEE_COUNT), false);
  }

  function renderWatchMarquee() {
    const w = (state.board && state.board.watch) || [];
    if (!w.length) return;
    fillMarquee("track-bottom", w, true);
  }

  // ── 탭 ──────────────────────────────────────────────────
  function switchView(view) {
    state.view = view;
    for (const b of $("tabs").children) b.classList.toggle("is-on", b.dataset.view === view);

    // 뷰가 바뀌면 행을 전부 버리고 다시 만든다 (컬럼 의미가 달라진다)
    $("rows").textContent = "";
    state.rows.clear();
    state.lastResort = Date.now();
    renderRows(true);
  }

  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (b) switchView(b.dataset.view);
  });

  // 탭 전환·백그라운드 복귀 시 재연결
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && (!ws || ws.readyState > 1)) {
      retry = 0;
      connect();
    }
  });

  // ── 부팅 ────────────────────────────────────────────────
  async function boot() {
    tickClock();
    setInterval(tickClock, 1000);

    await loadBoard();          // 이름 맵 + 추천 목록
    const ok = await loadSnapshot();  // 업비트 REST 1회
    if (!ok) setConn("down", "시세 연결 실패");

    renderTopMarquee();
    renderRows(true);
    connect();

    setInterval(loadBoard, BOARD_REFRESH_MS);
    setInterval(() => {
      paintMarquee("track-top");
      paintMarquee("track-bottom");
    }, MARQUEE_REFRESH_MS);
  }

  boot();
})();
