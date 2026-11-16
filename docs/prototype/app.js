/* Prototype shell: viewing-as selector, Upload, Settings (language, units, pauses), Report and
   History tabs, delete with confirmation + undo. In the real app the shell and the Report tab are
   server-rendered (Jinja + HTMX) and Settings are per-user preferences stored server-side; this
   file shows the intended markup and behaviour. The History tab uses the real module (charts.js). */
(function () {
  "use strict";
  const IC = window.InbodyCharts, I18N = window.InbodyI18n;
  const DATA = JSON.parse(document.getElementById("inbody-data").textContent);
  const $ = id => document.getElementById(id);
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem("inbody-proto:" + k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem("inbody-proto:" + k, JSON.stringify(v)); } catch (e) { /* storage blocked */ } }
  };
  function el(tag, attrs, text) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) if (attrs[k] != null && attrs[k] !== false) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function svgIcon(d, size) {
    const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg");
    [["width", size || 16], ["height", size || 16], ["viewBox", "0 0 16 16"], ["fill", "none"], ["stroke", "currentColor"],
      ["stroke-width", "1.6"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"]].forEach(([k, v]) => s.setAttribute(k, v));
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    s.append(p);
    return s;
  }
  const TRASH = "M2.5 4h11M6.2 4V2.6h3.6V4M4 4l.7 9.2a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9L12 4M6.7 6.6v5M9.3 6.6v5";

  // ------------------------------------------------------------------ settings (one place)
  const saved = store.get("settings") || {};
  const set = {
    lang: saved.lang === "cs" ? "cs" : "en",
    mass: saved.mass === "imperial" ? "imperial" : "metric",
    energy: saved.energy === "kJ" ? "kJ" : "kcal",
    theme: saved.theme === "light" || saved.theme === "system" ? saved.theme : "dark"   // dark is the default
  };
  // light / dark set <html data-theme>, system removes it (the OS decides); theme.js does the same before first paint
  const applyTheme = () => {
    const r = document.documentElement;
    if (set.theme === "system") r.removeAttribute("data-theme"); else r.setAttribute("data-theme", set.theme);
  };
  applyTheme();
  const units = () => ({ mass: set.mass, energy: set.energy });
  let S = IC.withUnits(I18N[set.lang], units());

  // two different random quotes for the side margins (decoration only). A new pair is drawn only when the
  // user lands on Report or History: tab switch, person change, report stepper (as in the app, where each is a
  // load of /report or /history). Upload / Sharing / Settings and the top bar leave the pair alone. The new pair
  // never contains either quote that was shown before.
  const pickQuotes = avoid => {
    const pool = [...Array(16).keys()].filter(i => !avoid.includes(i));
    const a = pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
    return [a, pool[Math.floor(Math.random() * pool.length)]];
  };
  let quoteIdx = pickQuotes([]);
  function paintQuotes() {
    [["quote-l", 0], ["quote-r", 1]].forEach(([id, k]) => {
      const q = $(id);
      q.className = "quote " + (k ? "quote-r" : "quote-l") + " q" + (quoteIdx[k] + 1);   // the look comes from the index
      q.firstElementChild.textContent = S.quotes[quoteIdx[k]];
    });
  }
  const rollQuotes = () => { quoteIdx = pickQuotes(quoteIdx); paintQuotes(); };
  const st = { who: DATA.viewer, tab: location.hash === "#report" ? "report" : "history", idx: null, confirming: false };
  let hist = null, undo = null, toastTimer = 0;

  // Prototype-only data preview: ?only=latest|first|none (local server URL) or the banner switch.
  // Keeps just the newest / oldest measurement of every person, or none, to show the n = 1 / n = 0 states.
  const ORIG = DATA.people.map(p => p.measurements.slice());
  let only = (() => { try { const v = new URLSearchParams(location.search).get("only"); return ["latest", "first", "none"].includes(v) ? v : "all"; } catch (e) { return "all"; } })();
  function applyOnly() {
    DATA.people.forEach((p, i) => {
      const ms = ORIG[i].slice().sort((a, b) => a.measured_date.localeCompare(b.measured_date));
      p.measurements = only === "latest" ? ms.slice(-1) : only === "first" ? ms.slice(0, 1) : only === "none" ? [] : ORIG[i].slice();
    });
  }
  applyOnly();

  const person = () => DATA.people.find(p => p.id === st.who) || DATA.people[0];
  const isOwn = () => person().relation === "self";
  const hasFriends = () => DATA.people.some(p => p.relation !== "self");
  const canDelete = () => isOwn() || !!DATA.viewerIsAdmin;          // never a shared viewer
  const sortRows = ms => ms.slice().sort((a, b) =>
    a.measured_date.localeCompare(b.measured_date) || String(a.measured_time).localeCompare(String(b.measured_time)));
  const rawRows = () => sortRows(person().measurements);
  const rows = () => rawRows().map(r => IC.toDisplay(r, S.U));       // display units for the report

  // ------------------------------------------------------------------ shell
  function renderShell() {
    document.documentElement.lang = set.lang;
    paintQuotes();
    const sel = $("who");
    sel.setAttribute("aria-label", S.shell.viewing);        // the visible label is gone
    sel.replaceChildren();
    DATA.people.forEach(p => {
      // your own entry says "(me)"; other people are just the name
      const o = el("option", { value: p.id }, p.name + (p.relation === "self" ? ` (${S.shell.you})` : ""));
      if (p.id === st.who) o.selected = true;
      sel.append(o);
    });
    // nobody shared with you: a plain name instead of a one-item dropdown
    const wrap = $("who-wrap"), stat = $("who-static");
    if (stat) stat.remove();
    sel.hidden = DATA.people.length < 2;
    if (DATA.people.length < 2) wrap.append(el("span", { class: "who-static", id: "who-static" }, `${person().name} (${S.shell.you})`));
    const up = $("upload-btn");
    $("upload-label").textContent = S.shell.upload;
    up.removeAttribute("aria-disabled"); up.removeAttribute("title");   // always about MY account, also while I look at a friend's data
    $("upload-pop").hidden = true;
    const sb = $("share-btn"), ST = S.shell.share;
    $("share-label").textContent = ST.title;
    sb.removeAttribute("aria-disabled"); sb.setAttribute("title", ST.title);
    if (!$("share-panel").hidden) renderShare();
    // settings panel texts + values
    const T = S.shell.settings;
    $("settings-label").textContent = T.title;
    $("set-lang-l").textContent = T.language;
    $("mobile-notice-text").textContent = T.mobileNotice;
    $("mobile-notice-x").setAttribute("aria-label", T.mobileClose);
    $("set-theme-l").textContent = T.appearance;
    $("set-dark").textContent = T.dark;
    $("set-light").textContent = T.light;
    $("set-system").textContent = T.system;
    $("set-units-l").textContent = T.units;
    $("set-metric").textContent = T.metric;
    $("set-imperial").textContent = T.imperial;
    $("set-energy-l").textContent = T.energy;
    $("set-stored").textContent = T.stored;
    $("set-lang-" + set.lang).checked = true;
    $("set-theme-" + set.theme).checked = true;
    $("set-mass-" + set.mass).checked = true;
    $("set-energy-" + (set.energy === "kJ" ? "kj" : "kcal")).checked = true;
    $("logout").textContent = S.shell.logout;
    $("tab-report").textContent = S.shell.tabReport;
    $("tab-history").textContent = S.shell.tabHistory;
    const local = DATA.meta && DATA.meta.mode === "local";
    $("banner").classList.toggle("local", local);
    $("banner-text").textContent = local ? S.shell.bannerLocal : S.shell.bannerExample;
    const dev = $("dev-switch");
    dev.replaceChildren(el("span", null, S.shell.preview));
    ["all", "latest", "first", "none"].forEach(v => {
      const b = el("button", { type: "button", "aria-pressed": String(only === v) }, S.shell.previewOpts[v]);
      b.addEventListener("click", () => {
        only = v;
        applyOnly();
        try { history.replaceState(null, "", (v === "all" ? location.pathname : "?only=" + v) + location.hash); } catch (e) { /* sandboxed */ }
        st.idx = null;
        st.confirming = false;
        renderShell();
        renderReport();
        hist.update({ measurements: rawRows(), onUpload: isOwn() ? openUpload : null, hasFriends: isOwn() && hasFriends() });
      });
      dev.append(b);
    });
    $("ro-notice").hidden = true;  // no read-only banner (user decision 2026-11-16)
    $("toast-undo").textContent = S.report.undo;
    $("footer-text").textContent = "© " + new Date().getFullYear() + " VojtaH";
    ["report", "history"].forEach(t => {
      $("tab-" + t).setAttribute("aria-selected", String(st.tab === t));
      $("tab-" + t).tabIndex = st.tab === t ? 0 : -1;
      $("p-" + t).hidden = st.tab !== t;
    });
  }

  // ------------------------------------------------------------------ report helpers
  const fmt = (key, v) => IC.fmtVal(key, v, S);
  function verdictIcon(kind) {
    return svgIcon(kind === "better" ? "M8 1.7a6.3 6.3 0 1 0 0 12.6A6.3 6.3 0 0 0 8 1.7ZM5.2 8.2l2 2 3.7-3.9" : "M8 1.8 14.6 13.6H1.4L8 1.8ZM8 6.3v3.3M8 11.6v.1", 13);
  }
  /** delta vs the most recent earlier measurement that has the value: arrow + sign + unit; colour by direction */
  function deltaNode(key, rs, idx) {
    const c = IC.compare(key, rs, idx), sp = el("span", { class: "delta" });
    if (!c) return sp;
    // nothing earlier to compare with: an empty cell that keeps its line, so the layout doesn't jump
    if (c.verdict === "none") { sp.classList.add("none"); sp.setAttribute("aria-hidden", "true"); sp.textContent = "\u00a0"; return sp; }
    sp.classList.add(c.verdict);              // up = green ▲, down = red ▼, same = neutral (colour is purely directional)
    const arrow = c.delta > 0 ? "▲" : c.delta < 0 ? "▼" : "=";
    const md = IC.METRICS[key] || {};
    // a % value changes in percentage points (pp / p.p.), never in %
    const txt = md.unit === "%" ? IC.withUnit(IC.fmtNum(c.delta, md.dp, S, true), "pp", S) : IC.fmtVal(key, c.delta, S, true);
    sp.append(document.createTextNode(arrow + "\u00a0" + txt));
    if (!c.immediate) sp.append(el("span", { class: "vs" }, S.report.vs(S.fmt.date(c.prevRow.measured_date))));
    return sp;
  }
  function rangeBar(key, r) {
    const v = r[key], lo = r[key + "_low"], hi = r[key + "_high"];
    if (v == null || lo == null || hi == null) return null;
    const a0 = Math.min(lo, v), b0 = Math.max(hi, v), span = (b0 - a0) || 1, a = a0 - span * 0.3, b = b0 + span * 0.3;
    const pos = x => ((x - a) / (b - a) * 100);
    const wrap = el("div", { class: "rb" }), track = el("div", { class: "rb-track" });
    const band = el("div", { class: "rb-band" }), dot = el("div", { class: "rb-dot" });
    band.style.left = pos(lo) + "%";
    band.style.width = (pos(hi) - pos(lo)) + "%";
    dot.style.left = pos(v) + "%";
    track.append(band, dot);
    // scale: clean ticks (1/2/2.5/5 x 10^k, about 4-6 across the bar), hairline + muted label under it
    const ticks = el("div", { class: "rb-ticks", "aria-hidden": "true" });
    const tks = rangeTicks(a, b), tdp = IC.stepDp(tks.length > 1 ? tks : [0, tks.step || 1]);
    tks.forEach(t => {
      const tk = el("span", { class: "rb-tick" }, IC.fmtNum(t, tdp, S));
      tk.style.left = pos(t) + "%";
      ticks.append(tk);
    });
    const scale = el("div", { class: "rb-scale" });
    scale.append(track, ticks);
    const md = IC.METRICS[key], f = x => IC.fmtNum(x, md.dp, S);
    wrap.append(scale, el("span", { class: "rb-text" }, IC.withUnit(S.report.normal(f(lo), f(hi)), md.unit, S)));
    return wrap;
  }
  // ---- evaluations: shown on their own line under the (bold) category name, icon + text, colour last
  function evalLabel(tone, text) {
    const sp = el("span", { class: "eval " + tone });
    sp.append(verdictIcon(tone === "good" ? "better" : "worse"), el("span", null, text));
    return sp;
  }
  /** printed InBody evaluation -> tone. body: BMI/PBF; lean / fat: segmental */
  function printedEval(kind, v) {
    if (!v) return null;
    const tone = kind === "body" ? (v === "normal" ? "good" : v === "over" ? "bad" : "warn")
      : kind === "lean" ? (v === "under" ? "warn" : "good")
      : (v === "over" ? "warn" : "good");
    return evalLabel(tone, S.report.eval[v] || v);
  }
  /** evaluation from the printed normal range: within = good, outside = warning (the range rule wins,
   *  the same rule the deltas use) */
  function rangeEval(key, r) {
    const v = r[key], lo = r[key + "_low"], hi = r[key + "_high"];
    if (v == null || lo == null || hi == null) return null;
    if (v >= lo && v <= hi) return evalLabel("good", S.report.rangeEval.within);
    return evalLabel("warn", v > hi ? S.report.rangeEval.above : S.report.rangeEval.below);
  }
  /** ticks inside [a, b] with a step from 1 / 2 / 2.5 / 5 x 10^k; the step whose count is closest
   *  to 5 wins (ties -> more ticks), so a bar gets about 4-6 ticks */
  function rangeTicks(a, b) {
    const mag = Math.pow(10, Math.floor(Math.log10((b - a) / 5)));
    let best = null;
    [0.1, 1, 10].forEach(mm => [1, 2, 2.5, 5].forEach(f => {
      const step = f * mag * mm, first = Math.ceil(a / step - 1e-9) * step;
      const n = Math.floor((b - first) / step + 1e-9) + 1;
      const score = Math.abs(n - 5) - (n > 5 ? 0.1 : 0);
      if (n >= 2 && (!best || score < best.score)) best = { step, first, n, score };
    }));
    const out = [];
    for (let i = 0; i < best.n; i++) out.push(+(best.first + i * best.step).toFixed(6));
    out.step = best.step;
    return out;
  }
  /** hide tick labels that would overlap a neighbour or stick out of the bar (greedy, left to right) */
  function fitTicks(root) {
    (root || document).querySelectorAll(".rb-ticks").forEach(row => {
      const box = row.getBoundingClientRect();
      let lastRight = -Infinity;
      row.querySelectorAll(".rb-tick").forEach(tk => {
        tk.classList.remove("hide");
        const r = tk.getBoundingClientRect();
        const hide = r.left < box.left - 2 || r.right > box.right + 2 || r.left < lastRight + 6;
        tk.classList.toggle("hide", hide);
        if (!hide) lastRight = r.right;
      });
    });
  }
  window.addEventListener("resize", () => fitTicks($("p-report")));
  const EXTRA = new Set(["dry_lean_mass_kg", "soft_lean_mass_kg"]);   // shown only when present
  /** ⓘ explainer for a Report section (same look as the History info boxes; <details>, closes on
   *  Esc or an outside click). keys: S.report.info.* paragraphs */
  // ⓘ per Report section: definitions from the SHARED S.def strings (the History charts use the
  // same ones) + a short reading note. Same <details> component as charts.js / templates/_ui.html.
  const REPORT_INFO = {
    muscleFat: [["weight_kg", "skeletal_muscle_mass_kg", "body_fat_mass_kg"], "noteBar"],
    obesity: [["bmi", "percent_body_fat", "visceral_fat_level", "waist_hip_ratio", "obesity_degree_pct"], "noteBar"],
    composition: [["total_body_water_l", "protein_kg", "minerals_kg", "fat_free_mass_kg", "dry_lean_mass_kg", "soft_lean_mass_kg"], "noteBar"],
    metabolism: [["bmr_kcal", "inbody_score", "smi_kg_m2", "recommended_intake_kcal"], "noteBar"],
    segmental: [["seg_lean", "seg_fat", "seg_pct"], "noteChange"]
  };
  const OPTIONAL_ROWS = new Set(["dry_lean_mass_kg", "soft_lean_mass_kg"]);
  const TERM = { seg_lean: () => S.chart.seg.lean, seg_fat: () => S.chart.seg.fat, seg_pct: () => S.chart.seg.pct };
  function infoBox(section, r) {
    const NS = "http://www.w3.org/2000/svg";
    const [keys, note] = REPORT_INFO[section];
    const d = el("details", { class: "ibc-info", "data-section": section });
    const sum = el("summary", { class: "ibc-info-btn", "aria-label": S.report.info.label, title: S.report.info.label });
    const svg = document.createElementNS(NS, "svg");
    [["width", "18"], ["height", "18"], ["viewBox", "0 0 18 18"], ["aria-hidden", "true"]].forEach(([k, v]) => svg.setAttribute(k, v));
    const ring = document.createElementNS(NS, "circle");
    [["cx", "9"], ["cy", "9"], ["r", "7.5"], ["class", "ibc-info-ring"]].forEach(([k, v]) => ring.setAttribute(k, v));
    const i = document.createElementNS(NS, "path");
    i.setAttribute("d", "M9 8v4.6M9 5.4v.2"); i.setAttribute("class", "ibc-info-i");
    svg.append(ring, i);
    sum.append(svg);
    const box = el("div", { class: "ibc-info-box", role: "note" });
    keys.filter(k => !OPTIONAL_ROWS.has(k) || r[k] != null).forEach(k => {
      const p = el("p", { class: "ibc-def" });
      p.append(el("b", null, TERM[k] ? TERM[k]() : S.m[k]), document.createTextNode(": " + S.def[k]));
      box.append(p);
    });
    box.append(el("p", { class: "ibc-info-note" }, S.report.info[note]));
    d.append(sum, box);
    return d;
  }
  function cardHead(title, section, r) {
    const h = el("div", { class: "rcard-head" });
    h.append(el("h2", null, title), infoBox(section, r));
    return h;
  }
  function kvCard(title, items, rs, idx, cls, section) {
    const r = rs[idx], c = el("section", { class: "rcard" + (cls ? " " + cls : "") });
    c.append(cardHead(title, section, r));
    const g = el("div", { class: "kv" });
    items.forEach(it => {
      const key = it.key || it;
      if (EXTRA.has(key) && r[key] == null) return;
      // fixed structure, same in every card: name | value, then pill | change, then the bar
      const row = el("div", { class: "kvrow" });
      const ev = it.eval ? printedEval("body", r[it.eval]) : rangeEval(key, r);
      const sub = el("div", { class: "sub" });
      if (ev) sub.append(ev);
      const d = el("span", { class: "d" });
      d.append(deltaNode(key, rs, idx));
      sub.append(d);
      row.append(el("div", { class: "k-name" }, S.m[key]),
        el("div", { class: "v" + (r[key] == null ? " na" : "") }, fmt(key, r[key])), sub);
      const bar = rangeBar(key, r);
      if (bar) { const b = el("div", { class: "bar" }); b.append(bar); row.append(b); }
      g.append(row);
    });
    c.append(g);
    return c;
  }
  function segCard(rs, idx) {
    const r = rs[idx], c = el("section", { class: "rcard wide" });
    c.append(cardHead(S.report.secSegmental, "segmental", r));
    const map = el("div", { class: "segmap" });
    const area = { right_arm: "ra", left_arm: "la", trunk: "tr", right_leg: "rl", left_leg: "ll" };
    IC.SEGS.forEach(s => {
      const cell = el("div", { class: "segcell" });
      cell.style.gridArea = area[s];
      cell.append(el("h3", null, S.seg[s]));
      [["lean", S.report.lean], ["fat", S.report.fat]].forEach(([m, lbl]) => {
        const kKey = `seg_${m}_${s}_kg`, pKey = `seg_${m}_${s}_pct`;
        const row = el("div", { class: "row" });
        const left = el("div", { class: "seg-l" }), right = el("div", { class: "seg-r" });
        left.append(el("span", { class: "seg-k" }, lbl));
        const ev = printedEval(m, r[`seg_${m}_${s}_eval`]);
        if (ev) left.append(ev);
        const b = el("b", null, fmt(kKey, r[kKey]));
        if (r[pKey] != null) b.append(el("small", null, " · " + IC.fmtVal(pKey, r[pKey], S) + " " + S.report.ofIdeal));
        right.append(b, deltaNode(kKey, rs, idx));
        row.append(left, right);
        cell.append(row);
      });
      map.append(cell);
    });
    c.append(map);
    return c;
  }

  // ------------------------------------------------------------------ delete (confirm in page, undo toast)
  function showToast(text, onUndo) {
    clearTimeout(toastTimer);
    $("toast-text").textContent = text;
    $("toast").hidden = false;
    undo = onUndo;
    toastTimer = setTimeout(() => { $("toast").hidden = true; undo = null; }, 8000);
  }
  function deleteMeasurement(row) {
    const p = person(), list = p.measurements, at = list.findIndex(x => x.id === row.id);
    if (at < 0) return;
    const [removed] = list.splice(at, 1);
    st.confirming = false;
    st.idx = null;
    renderReport();
    hist.update({ measurements: rawRows() });
    showToast(S.report.deleted(S.fmt.date(removed.measured_date)), () => {
      list.splice(at, 0, removed);
      st.idx = rawRows().findIndex(x => x.id === removed.id);
      renderReport();
      hist.update({ measurements: rawRows() });
    });
  }

  // ------------------------------------------------------------------ report tab
  function renderReport() {
    const p = $("p-report");
    p.replaceChildren();
    const rs = rows();
    if (!rs.length) {
      const box = el("div", { class: "empty" });
      box.append(el("p", null, isOwn() ? (hasFriends() ? S.shell.noDataFriends : S.shell.noData) : S.shell.noDataShared));
      if (isOwn()) {
        const b = el("button", { type: "button", class: "btn-primary" }, S.shell.upload);
        b.addEventListener("click", openUpload);
        box.append(b);
      }
      p.append(box);
      return;
    }
    if (st.idx == null || st.idx >= rs.length) st.idx = rs.length - 1;
    const i = st.idx, r = rs[i];

    const head = el("div", { class: "rep-head" });
    const h1 = el("h1", null, S.fmt.dateLong(r.measured_date));
    if (r.measured_time) h1.append(el("small", { title: S.report.printedTime }, String(r.measured_time).slice(0, 5)));
    const stp = el("div", { class: "stepper" });
    const bp = el("button", { type: "button", "aria-label": S.report.prev, title: S.report.prev }, "‹");
    const bn = el("button", { type: "button", "aria-label": S.report.next, title: S.report.next }, "›");
    bp.disabled = i === 0;
    bn.disabled = i === rs.length - 1;
    bp.addEventListener("click", () => { rollQuotes(); st.idx--; st.confirming = false; renderReport(); });
    bn.addEventListener("click", () => { rollQuotes(); st.idx++; st.confirming = false; renderReport(); });
    const sel = el("select", { id: "report-pick", "aria-label": S.report.measurement });
    rs.slice().reverse().forEach(x => {
      const j = rs.indexOf(x);
      const o = el("option", { value: String(j) }, S.fmt.date(x.measured_date));
      if (j === i) o.selected = true;
      sel.append(o);
    });
    sel.addEventListener("change", () => { rollQuotes(); st.idx = +sel.value; st.confirming = false; renderReport(); });
    stp.append(bp, sel, bn);
    if (canDelete()) {
      const del = el("button", { type: "button", class: "icon-danger", id: "delete-btn", "aria-label": S.report.deleteBtn, title: S.report.deleteBtn, "aria-expanded": String(st.confirming) });
      del.append(svgIcon(TRASH, 17));
      del.addEventListener("click", () => { st.confirming = true; renderReport(); });
      stp.append(del);
    }
    head.append(h1, stp);
    p.append(head);

    if (st.confirming && canDelete()) {
      const box = el("div", { class: "confirm", role: "alertdialog", "aria-labelledby": "confirm-text" });
      box.append(el("p", { id: "confirm-text" }, S.report.confirmDelete(S.fmt.date(r.measured_date))));
      const btns = el("div", { class: "btns" });
      const cancel = el("button", { type: "button", class: "btn-quiet" }, S.report.cancel);
      const ok = el("button", { type: "button", class: "btn-danger" }, S.report.del);
      cancel.addEventListener("click", () => { st.confirming = false; renderReport(); $("delete-btn") && $("delete-btn").focus(); });
      ok.addEventListener("click", () => deleteMeasurement(rawRows()[i]));
      box.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); cancel.click(); } });
      btns.append(cancel, ok);
      box.append(btns);
      p.append(box);
      setTimeout(() => cancel.focus(), 0);
    }

    const meta = el("div", { class: "rep-meta" });
    if (i === rs.length - 1) meta.append(el("span", { class: "chip latest" }, S.report.latest));
    meta.append(el("span", { class: "chip" }, S.source[r.origin] || r.origin));
    const gym = el("span", null, r.gym || S.source.gymMissing);
    meta.append(gym);
    if (!r.gym && isOwn()) {
      const add = el("button", { type: "button", class: "linkbtn" }, S.source.addGym);
      add.addEventListener("click", () => { gym.textContent = S.source.gymMissing + " · " + S.source.addGymTodo; add.remove(); });
      meta.append(add);
    }
    meta.append(el("span", { class: "rep-compare" }, i > 0 ? S.report.comparedWith(S.fmt.date(rs[i - 1].measured_date)) : S.report.firstOne));
    p.append(meta);

    const tiles = el("div", { class: "tiles" });
    ["weight_kg", "skeletal_muscle_mass_kg", "percent_body_fat", "inbody_score"].forEach(key => {
      const t = el("div", { class: "tile" }), md = IC.METRICS[key];
      t.append(el("div", { class: "lbl" }, S.m[key]));
      const v = el("div", { class: "val" }, r[key] == null ? "—" : IC.fmtNum(r[key], md.dp, S));
      if (r[key] != null && md.unit && md.unit !== "points" && md.unit !== "level") v.append(el("small", null, IC.unitLabel(md.unit, S)));
      t.append(v, deltaNode(key, rs, i));
      tiles.append(t);
    });
    p.append(tiles);

    const grid = el("div", { class: "rep-grid" });
    grid.append(kvCard(S.report.secMuscleFat, ["weight_kg", "skeletal_muscle_mass_kg", "body_fat_mass_kg"], rs, i, "", "muscleFat"));
    grid.append(kvCard(S.report.secObesity, [{ key: "bmi", eval: "bmi_eval" }, { key: "percent_body_fat", eval: "pbf_eval" }, "visceral_fat_level", "waist_hip_ratio", "obesity_degree_pct"], rs, i, "", "obesity"));
    grid.append(kvCard(S.report.secComposition, ["total_body_water_l", "protein_kg", "minerals_kg", "fat_free_mass_kg", "dry_lean_mass_kg", "soft_lean_mass_kg"], rs, i, "", "composition"));
    grid.append(kvCard(S.report.secMetabolism, ["bmr_kcal", "inbody_score", "smi_kg_m2", "recommended_intake_kcal"], rs, i, "", "metabolism"));
    grid.append(segCard(rs, i));
    p.append(grid);
    p.append(el("p", { class: "note" }, S.report.missingNote));
    requestAnimationFrame(() => fitTicks(p));
  }

  // ------------------------------------------------------------------ history tab
  function renderHistory() {
    if (!hist) {
      hist = IC.mountHistory($("p-history"), {
        measurements: rawRows(), lang: set.lang, units: units(), state: store.get("history"),
        onUpload: isOwn() ? openUpload : null, hasFriends: isOwn() && hasFriends(),
        onChange: s => store.set("history", s),
        onSelect: row => { st.idx = rawRows().findIndex(x => x.id === row.id); st.confirming = false; go("report"); }
      });
    }
  }

  // ------------------------------------------------------------------ app-bar panels (sharing, settings)
  // Dropdown panels: open on click, close on Esc (focus returns to the button) or an outside click.
  const PANELS = { share: ["share-btn", "share-panel"], settings: ["settings-btn", "settings-panel"] };
  function closePanel(name, focus) {
    const [b, p] = PANELS[name];
    if ($(p).hidden) return;
    $(p).hidden = true;
    $(b).setAttribute("aria-expanded", "false");
    if (focus) $(b).focus();
  }
  function openPanel(name) {
    Object.keys(PANELS).forEach(n => { if (n !== name) closePanel(n, false); });
    const [b, p] = PANELS[name];
    $(p).hidden = false;
    $(b).setAttribute("aria-expanded", "true");
  }
  document.addEventListener("pointerdown", e => {
    Object.keys(PANELS).forEach(n => { const [b, p] = PANELS[n]; if (!$(p).contains(e.target) && !$(b).contains(e.target)) closePanel(n, false); });
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    Object.keys(PANELS).forEach(n => closePanel(n, true));
    if (!$("upload-pop").hidden) $("upload-pop").hidden = true;
  });
  $("settings-btn").addEventListener("click", () => {
    if ($("settings-panel").hidden) { openPanel("settings"); $("set-lang-" + set.lang).focus(); } else closePanel("settings", false);
  });
  $("share-btn").addEventListener("click", () => {
    if ($("share-panel").hidden) { shareMsg = null; revoking = null; renderShare(); openPanel("share"); } else closePanel("share", false);
  });

  // ------------------------------------------------------------------ sharing (simulated in memory)
  // The server keeps the grants; the panel lists ONLY the viewer's own grants and the owners who
  // shared with the viewer, never the user directory. A username lookup is rate-limited (10/hour).
  const SH = DATA.sharing || { grants: [], directory: [] };
  const me = DATA.viewerUsername || DATA.viewer;
  const checks = [];
  let shareMsg = null, revoking = null;
  function renderShare() {
    const pan = $("share-panel"), T = S.shell.share;
    pan.replaceChildren();
    pan.append(el("h3", null, T.whoSees));
    const ul = el("ul");
    if (!SH.grants.length) ul.append(el("li", { class: "set-hint" }, T.nobody));
    SH.grants.forEach(n => {
      const li = el("li"), row = el("div", { class: "share-row" });
      const name = el("span", { class: "who-name" }, n);
      name.append(el("small", null, T.readOnly));
      const b = el("button", { type: "button", class: "icon-danger", "aria-label": T.revoke(n), title: T.revoke(n) });
      b.append(svgIcon(TRASH, 16));
      b.addEventListener("click", () => { revoking = n; shareMsg = null; renderShare(); });
      row.append(name, b);
      li.append(row);
      if (revoking === n) {
        const box = el("div", { class: "confirm", role: "alertdialog", "aria-labelledby": "revoke-text" });
        box.append(el("p", { id: "revoke-text" }, T.confirmRevoke(n)));
        const btns = el("div", { class: "btns" });
        const c = el("button", { type: "button", class: "btn-quiet" }, T.cancel);
        const ok = el("button", { type: "button", class: "btn-danger" }, T.revokeBtn);
        c.addEventListener("click", () => { revoking = null; renderShare(); });
        ok.addEventListener("click", () => { SH.grants = SH.grants.filter(x => x !== n); revoking = null; shareMsg = { ok: true, text: T.revoked(n) }; renderShare(); });
        box.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); c.click(); } });
        btns.append(c, ok);
        box.append(btns);
        li.append(box);
        setTimeout(() => c.focus(), 0);
      }
      ul.append(li);
    });
    pan.append(ul);
    const form = el("form", { class: "share-form", id: "share-form", novalidate: "" });
    const inp = el("input", { id: "share-user", name: "username", autocomplete: "off", autocapitalize: "none", spellcheck: "false" });
    form.append(el("label", { for: "share-user" }, T.username), inp, el("button", { type: "submit", class: "btn-primary" }, T.shareBtn));
    form.addEventListener("submit", e => { e.preventDefault(); shareWith(inp.value); });
    pan.append(form);
    pan.append(el("p", { class: "set-hint" }, T.fullHistory + " " + T.rateNote));
    if (shareMsg) pan.append(el("p", { class: "share-msg " + (shareMsg.ok ? "ok" : "err"), role: "status" }, shareMsg.text));
    pan.append(el("hr", { class: "share-sep" }));
    pan.append(el("h3", null, T.withMe));
    const owners = DATA.people.filter(p => p.relation === "shared");
    const ul2 = el("ul");
    if (!owners.length) ul2.append(el("li", { class: "set-hint" }, T.noneWithMe));
    owners.forEach(p => {
      const li = el("li"), b = el("button", { type: "button", class: "share-owner", title: T.openTip(p.name) }, p.name);
      b.addEventListener("click", () => { closePanel("share", false); switchTo(p.id); });
      li.append(b);
      ul2.append(li);
    });
    pan.append(ul2);
  }
  function shareWith(raw) {
    const T = S.shell.share, n = raw.trim().toLowerCase();
    if (!n) shareMsg = { ok: false, text: T.errEmpty };
    else if (n === me) shareMsg = { ok: false, text: T.errSelf };
    else if (SH.grants.includes(n)) shareMsg = { ok: false, text: T.errAlready(n) };
    else {
      const now = Date.now();
      while (checks.length && now - checks[0] > 3600e3) checks.shift();
      if (checks.length >= 10) shareMsg = { ok: false, text: T.errRate };
      else {
        checks.push(now);
        if (!(SH.directory || []).includes(n)) shareMsg = { ok: false, text: T.errNotFound(n) };
        else { SH.grants.push(n); shareMsg = { ok: true, text: T.done(n) }; }
      }
    }
    renderShare();
    const i = $("share-user");
    if (i) { if (!shareMsg.ok) i.value = raw; i.focus(); }
  }
  // the "not optimised for mobile" strip (CSS shows it only at <= 760 px); dismissal is remembered
  if (store.get("mobileNoticeOff")) document.documentElement.classList.add("notice-off");
  $("mobile-notice-x").addEventListener("click", () => { document.documentElement.classList.add("notice-off"); store.set("mobileNoticeOff", true); });
  $("settings-panel").addEventListener("submit", e => e.preventDefault());
  $("settings-panel").addEventListener("change", e => {
    const t = e.target;
    if (t.name === "lang") set.lang = t.value;
    if (t.name === "mass") set.mass = t.value;
    if (t.name === "energy") set.energy = t.value;
    if (t.name === "theme") { set.theme = t.value; applyTheme(); }
    store.set("settings", set);
    S = IC.withUnits(I18N[set.lang], units());
    renderShell();
    renderReport();
    hist.update({ lang: set.lang, units: units() });
    openPanel("settings");
  });

  // ------------------------------------------------------------------ wiring
  function go(tab) {
    rollQuotes();
    st.tab = tab;
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    renderShell();
    if (tab === "report") { renderReport(); window.scrollTo(0, 0); }
  }
  ["report", "history"].forEach(t => $("tab-" + t).addEventListener("click", () => go(t)));
  $("tab-report").parentElement.addEventListener("keydown", e => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const next = st.tab === "report" ? "history" : "report";
    go(next);
    $("tab-" + next).focus();
  });
  window.addEventListener("hashchange", () => { const t = location.hash === "#report" ? "report" : "history"; if (t !== st.tab) go(t); });
  function switchTo(id) {
    rollQuotes();
    $("who").value = id;
    st.who = id;
    st.idx = null;
    st.confirming = false;
    renderShell();
    renderReport();
    hist.update({ measurements: rawRows(), onUpload: isOwn() ? openUpload : null, hasFriends: isOwn() && hasFriends() });
  }
  $("who").addEventListener("change", e => switchTo(e.target.value));
  function openUpload() {
    const pop = $("upload-pop");
    pop.textContent = S.shell.uploadTodo;
    pop.hidden = false;
    window.scrollTo(0, 0);
  }
  $("upload-btn").addEventListener("click", () => {
    if (!$("upload-pop").hidden) { $("upload-pop").hidden = true; return; }
    openUpload();
  });
  $("toast-undo").addEventListener("click", () => {
    clearTimeout(toastTimer);
    $("toast").hidden = true;
    if (undo) { const u = undo; undo = null; u(); }
  });

  renderShell();
  renderReport();
  renderHistory();
})();
