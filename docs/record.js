/* 공개 실적 장부 렌더러.
 *
 * summary.json은 journal 브랜치(append-only)에서 읽는다. data 브랜치와 달리
 * force-push하지 않으므로 히스토리 전체가 GitHub에 증거로 남는다.
 */
(() => {
  "use strict";

  // config.js의 board URL에서 저장소를 추론한다. 별도 설정을 두지 않기 위해서다.
  const m = /raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\//.exec(window.BOARD_DATA_URL || "");
  const OWNER = m ? m[1] : null;
  const REPO = m ? m[2] : null;
  const SUMMARY_URL = OWNER
    ? `https://raw.githubusercontent.com/${OWNER}/${REPO}/journal/summary.json`
    : null;
  const BRANCH_URL = OWNER ? `https://github.com/${OWNER}/${REPO}/tree/journal` : null;

  const H = 7;   // 기본 표시 지평
  const $ = (id) => document.getElementById(id);

  const fmt = (v, d = 2) =>
    v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d);
  const cls = (v) => (v == null ? "" : v > 0 ? "good" : v < 0 ? "bad" : "");

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderEmpty(msg) {
    $("status").innerHTML = msg;
    $("tbl").querySelector("tbody").innerHTML =
      `<tr><td colspan="8" class="note">아직 채점된 회차가 없습니다.</td></tr>`;
    $("tbl-trade").querySelector("tbody").innerHTML =
      `<tr><td colspan="10" class="note">아직 종결된 매매가 없습니다.</td></tr>`;
    $("tbl-picks").querySelector("tbody").innerHTML =
      `<tr><td colspan="8" class="note">아직 기록된 추천이 없습니다.</td></tr>`;
  }

  function render(s) {
    const rules = s.rules || {};
    const ns = Object.values(rules).map((r) => (r.h?.[String(H)]?.n) || 0);
    const maxN = ns.length ? Math.max(...ns) : 0;

    // 표본 상태를 숨기지 않는다. 몇 주치로 결론 내는 것이 가장 흔한 오류다.
    let verdict;
    if (maxN === 0) {
      verdict = `<b>기록 시작</b> — 엔트리 ${s.entries}회 누적, 아직 7일 만기가 도래하지 않았습니다.
        첫 채점까지 기다려야 합니다.`;
    } else if (maxN < 12) {
      verdict = `<b>표본 ${maxN}회 — 아직 아무 결론도 낼 수 없습니다.</b>
        7일 지평은 1년에 52회뿐이라, 최소 수십 회는 쌓여야 의미가 생깁니다.`;
    } else if (maxN < 52) {
      verdict = `<b>표본 ${maxN}회 — 참고 수준입니다.</b> 방향은 볼 수 있지만 통계적 결론은 이릅니다.`;
    } else {
      verdict = `<b>표본 ${maxN}회 (약 ${(maxN / 52).toFixed(1)}년치).</b>
        이제 규칙 간 비교를 볼 수 있습니다. 다만 여전히 단일 국면일 수 있습니다.`;
    }
    $("status").innerHTML =
      `${verdict}<br><span class="note">최초 기록 ${s.first_day || "—"} ·
       엔트리 ${s.entries}회 · 갱신 ${s.updated_kst || "—"} KST</span>`;

    // 기준선(랜덤·BTC)이 먼저 보이도록 정렬하지 않고, 7일 누적 순으로 세운다
    const order = Object.keys(rules).sort((a, b) => {
      const A = rules[a].h?.[String(H)], B = rules[b].h?.[String(H)];
      return ((B?.cum) ?? -1e9) - ((A?.cum) ?? -1e9);
    });

    const rows = order.map((rid) => {
      const r = rules[rid];
      const d = r.h?.[String(H)] || {};
      const base = rid === "random" || rid === "btc" || rid === "eqw";
      if (!d.n) {
        return `<tr${base ? ' class="hl"' : ""}>
          <td>${esc(r.name)}${base ? ' <span class="badge-base">기준선</span>' : ""}</td>
          <td colspan="7" class="note">채점 대기</td></tr>`;
      }
      return `<tr${base ? ' class="hl"' : ""}>
        <td>${esc(r.name)}${base ? ' <span class="badge-base">기준선</span>' : ""}</td>
        <td>${d.n}</td>
        <td class="${cls(d.avg)}">${fmt(d.avg)}%</td>
        <td>${d.win}%</td>
        <td class="${cls(d.cum)}">${fmt(d.cum, 1)}%</td>
        <td class="bad">${fmt(d.mdd, 1)}%</td>
        <td class="${cls(d.vs_btc_avg)}">${fmt(d.vs_btc_avg)}%</td>
        <td>${d.vs_btc_win == null ? "—" : d.vs_btc_win + "%"}</td>
      </tr>`;
    }).join("");

    $("tbl").querySelector("tbody").innerHTML = rows;
    renderTrades(s, order);
    setupPicks(s);
    $("fee-note").textContent =
      `수익률은 왕복 수수료 ${s.fee}%p를 뺀 순수익 기준입니다. ` +
      `누적은 지평(${H}일)만큼 건너뛴 비중첩 회차만 곱해 계산합니다.`;
    if (BRANCH_URL) {
      $("raw-link").innerHTML =
        `원본 장부(수정 불가 기록) · <a href="${BRANCH_URL}">${esc(OWNER)}/${esc(REPO)} @ journal</a>`;
    }
  }

  // 목표가/손절가 매매. 대표 지표는 승률이 아니라 기대값이다 —
  // 목표가를 좁히고 손절가를 넓히면 승률은 얼마든지 만들 수 있기 때문이다.
  function renderTrades(s, order) {
    const tb = $("tbl-trade").querySelector("tbody");
    const any = Object.values(s.rules || {}).some((r) => (r.trade?.n || 0) > 0);
    if (!any) {
      tb.innerHTML = `<tr><td colspan="10" class="note">
        아직 종결된 매매가 없습니다. 목표가 또는 손절가에 닿거나 7일이 지나야 기록됩니다.
        </td></tr>`;
      return;
    }
    // 기대값 순으로 세운다. 승률 순으로 세우면 그 자체가 오해를 만든다.
    const ord = [...order].sort((a, b) =>
      ((s.rules[b].trade?.ev) ?? -1e9) - ((s.rules[a].trade?.ev) ?? -1e9));

    tb.innerHTML = ord.map((rid) => {
      const r = s.rules[rid], t = r.trade || {};
      const base = rid === "random" || rid === "btc" || rid === "eqw";
      const nm = `${esc(r.name)}${base ? ' <span class="badge-base">기준선</span>' : ""}`;
      if (!t.n) return `<tr${base ? ' class="hl"' : ""}><td>${nm}</td>
        <td colspan="9" class="note">종결된 매매 없음</td></tr>`;
      return `<tr${base ? ' class="hl"' : ""}>
        <td>${nm}</td>
        <td>${t.n}</td>
        <td>${t.win}%</td>
        <td class="${cls(t.ev)}"><b>${fmt(t.ev)}%</b></td>
        <td class="good">${fmt(t.avg_win)}%</td>
        <td class="bad">${fmt(t.avg_loss)}%</td>
        <td>${t.rr == null ? "—" : t.rr}</td>
        <td class="note">${t.tp} / ${t.sl} / ${t.expire}</td>
        <td class="${cls(t.cum)}">${fmt(t.cum, 1)}%</td>
        <td class="note">${t.hours}h</td>
      </tr>`;
    }).join("");
  }

  // ── 진행 중인 추천 ─────────────────────────────────────
  // 집계만 공개하고 실제 종목을 숨기면 검증 가능한 기록이 아니다.
  const OUT_LABEL = { OPEN: "진행 중", TP: "목표가 도달", SL: "손절", EXPIRE: "기간 만료",
                      PENDING: "채점 대기", HOLD: "보유 중(목표가 없음)", CLOSED: "기간 종료" };
  let PICKS = [], LIVE = new Map(), curRule = null;

  function renderPicks() {
    const tb = $("tbl-picks").querySelector("tbody");
    const rows = PICKS.filter((p) => p.rule === curRule);
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="8" class="note">이 규칙의 기록이 없습니다.</td></tr>`;
      return;
    }
    tb.innerHTML = rows.slice(0, 60).map((p) => {
      const cur = LIVE.get(p.m);
      // 종결된 건은 확정 손익, 진행 중인 건은 현재가 기준 평가손익
      const pnl = p.net != null ? p.net : (cur ? (cur / p.p - 1) * 100 : null);
      const open = p.out === "OPEN" || p.out === "HOLD";
      return `<tr>
        <td><b>${esc(p.name)}</b> <span class="note">${esc(p.m.split("-")[1])}</span></td>
        <td class="note">${esc(p.day)}</td>
        <td>${num(p.p)}</td>
        <td>${open && cur ? num(cur) : "—"}</td>
        <td class="${cls(pnl)}">${pnl == null ? "—" : fmt(pnl)}%</td>
        <td class="good">${p.tp == null ? "—" : num(p.tp)}</td>
        <td class="bad">${p.sl == null ? "—" : num(p.sl)}</td>
        <td><span class="tag st-${p.out.toLowerCase()}">${OUT_LABEL[p.out] || p.out}</span></td>
      </tr>`;
    }).join("");
  }

  function num(v) {
    if (v == null) return "—";
    return v >= 100 ? Math.round(v).toLocaleString("ko-KR")
      : v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(4) : v.toFixed(6);
  }

  async function setupPicks(s) {
    PICKS = s.picks || [];
    const latest = s.latest_day;
    $("picks-meta").textContent = latest
      ? `— ${latest} 기록 · 진행 중 ${s.open_count || 0}건`
      : "— 아직 없음";
    if (!PICKS.length) {
      $("tbl-picks").querySelector("tbody").innerHTML =
        `<tr><td colspan="8" class="note">아직 기록된 추천이 없습니다.</td></tr>`;
      return;
    }
    const rules = [...new Set(PICKS.map((p) => p.rule))];
    curRule = rules[0];
    $("pick-tabs").innerHTML = rules.map((r, i) =>
      `<button class="tab${i === 0 ? " is-on" : ""}" data-rule="${esc(r)}">${esc(
        (s.rules[r] && s.rules[r].name) || r)}</button>`).join("");
    $("pick-tabs").addEventListener("click", (e) => {
      const b = e.target.closest(".tab");
      if (!b) return;
      curRule = b.dataset.rule;
      for (const x of $("pick-tabs").children) x.classList.toggle("is-on", x === b);
      renderPicks();
    });
    renderPicks();

    // 진행 중인 종목만 현재가를 붙인다 (업비트 REST 1회)
    const open = [...new Set(PICKS.filter((p) => p.out === "OPEN" || p.out === "HOLD").map((p) => p.m))];
    if (!open.length) return;
    try {
      const res = await fetch(
        "https://api.upbit.com/v1/ticker?markets=" + open.slice(0, 100).join(","));
      if (!res.ok) return;
      for (const t of await res.json()) LIVE.set(t.market, t.trade_price);
      renderPicks();
    } catch (e) {
      console.warn("현재가 조회 실패:", e.message);
    }
  }

  async function boot() {
    if (!SUMMARY_URL) {
      renderEmpty("<b>장부 위치를 확인할 수 없습니다.</b> config.js의 저장소 설정이 필요합니다.");
      return;
    }
    try {
      const res = await fetch(`${SUMMARY_URL}?v=${Math.floor(Date.now() / 300000)}`,
                             { cache: "no-cache" });
      if (res.status === 404) {
        renderEmpty("<b>아직 장부가 없습니다.</b> 첫 기록이 생성되면 여기에 표시됩니다.");
        return;
      }
      if (!res.ok) throw new Error("HTTP " + res.status);
      render(await res.json());
    } catch (e) {
      renderEmpty(`<b>장부를 불러오지 못했습니다.</b> <span class="note">${esc(e.message)}</span>`);
    }
  }

  boot();
})();
