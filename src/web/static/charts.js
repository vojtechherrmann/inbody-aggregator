/* InbodyCharts: history charts for the InBody Aggregator.
 *
 * Vanilla JS, no dependencies, CSP-safe: no eval, no inline event handlers, no `style`
 * attributes in markup (positions are set through the CSSOM, which `style-src 'self'` allows).
 * Colours come from CSS classes + custom properties (charts.css / ui.css), so a theme switch
 * needs no re-render. All text comes from the i18n dictionary (window.InbodyI18n[lang]) and
 * is inserted with textContent only.
 *
 * Public API (see docs/ui-design.md, "Chart module API"):
 *   InbodyCharts.mountHistory(root, opts) -> { update(partialOpts), destroy(), state }
 *   InbodyCharts.autoMount(scope?)            mounts every [data-inbody-history] element
 *   InbodyCharts.buildAxis(isoDates, {breaks, pauseMonths})
 *   InbodyCharts.lineChart(host, spec) / InbodyCharts.stackChart(host, spec)   (low level)
 *   InbodyCharts.toDisplay(row, units) / .withUnits(strings, units) / .unitLabel(unit, S)   (units)
 *   InbodyCharts.METRICS, .fmtNum, .fmtVal, .withUnit, .compare
 */
(function (g) {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const DAY = 864e5;
  const BAR_MAX = 20;          // px, stacked bar width cap (<= 24 per the mark spec)
  const SURFACE_GAP = 2;       // px between stacked segments
  const BREAK_F = 0.025;       // share of plot width per axis break
  const MIN_SEG_F = 0.09;      // minimum share of plot width per segment
  const SEGS = ["right_arm", "left_arm", "trunk", "right_leg", "left_leg"];

  // ------------------------------------------------------------------ metric registry
  // unit (stored, SI), display decimals, dir (+1 / -1 / 0: kept as metadata only; changes are
  // coloured purely by direction, see compare()).
  const METRICS = {};
  function m(key, unit, dp, dir) { METRICS[key] = { key, unit, dp, dir }; }
  m("weight_kg", "kg", 1, 0);
  m("skeletal_muscle_mass_kg", "kg", 1, +1);
  m("body_fat_mass_kg", "kg", 1, -1);
  m("percent_body_fat", "%", 1, -1);
  m("fat_free_mass_kg", "kg", 1, +1);
  m("total_body_water_l", "L", 1, 0);
  m("protein_kg", "kg", 1, 0);
  m("minerals_kg", "kg", 1, 0);
  m("dry_lean_mass_kg", "kg", 1, 0);
  m("soft_lean_mass_kg", "kg", 1, 0);
  m("bmi", "kg/m²", 1, 0);
  m("visceral_fat_level", "level", 0, -1);
  m("waist_hip_ratio", "", 2, -1);
  m("obesity_degree_pct", "%", 0, 0);
  m("bmr_kcal", "kcal", 0, 0);
  m("recommended_intake_kcal", "kcal", 0, 0);
  m("smi_kg_m2", "kg/m²", 1, +1);
  m("inbody_score", "points", 0, +1);
  SEGS.forEach(s => {
    // body parts: ALWAYS 2 decimals (kg and %); whole-body aggregates: 1 decimal
    m(`seg_lean_${s}_kg`, "kg", 2, +1);
    m(`seg_fat_${s}_kg`, "kg", 2, -1);
    m(`seg_lean_${s}_pct`, "%", 0, 0);  // % of ideal: whole numbers, as printed
    m(`seg_fat_${s}_pct`, "%", 0, 0);
  });

  // ------------------------------------------------------------------ units: ONE place for conversions
  // Data is stored in SI. Display units: mass kg | lb, energy kcal | kJ. Rows are converted once on
  // entry (toDisplay); labels map the stored unit to the display unit (unitLabel). Everything else
  // (scales, deltas, tables, tooltips, report) works on the converted numbers.
  const KG_TO_LB = 1 / 0.45359237, KCAL_TO_KJ = 4.184;
  const MASS_RE = /_kg(_low|_high)?$/, ENERGY_RE = /_kcal(_low|_high)?$/;
  function toDisplay(row, U) {
    if (!U || (U.mass !== "imperial" && U.energy !== "kJ")) return row;
    const o = Object.assign({}, row);
    for (const k in o) {
      if (typeof o[k] !== "number") continue;
      if (U.mass === "imperial" && MASS_RE.test(k)) o[k] = o[k] * KG_TO_LB;
      else if (U.energy === "kJ" && ENERGY_RE.test(k)) o[k] = o[k] * KCAL_TO_KJ;
    }
    return o;
  }
  /** strings + unit settings in one object: S.U = {mass: 'metric'|'imperial', energy: 'kcal'|'kJ'} */
  function withUnits(S, U) { const o = Object.create(S); o.U = Object.assign({ mass: "metric", energy: "kcal" }, U || {}); return o; }
  function unitLabel(unit, S) {
    const U = S.U || {};
    if (unit === "kg" && U.mass === "imperial") return S.unit.lb;
    if (unit === "kcal" && U.energy === "kJ") return S.unit.kJ;
    return S.unit[unit] != null ? S.unit[unit] : unit;
  }

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, text) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) if (attrs[k] != null && attrs[k] !== false) e.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function sv(tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  const num = v => (typeof v === "number" && isFinite(v) ? v : null);
  const tOf = iso => { const [y, mo, d] = iso.split("-").map(Number); return Date.UTC(y, mo - 1, d); };
  const addMonths = (t, n) => { const d = new Date(t); d.setUTCMonth(d.getUTCMonth() + n); return d.getTime(); };
  let uid = 0;

  const nfCache = new Map();
  function fmtNum(v, dp, S, signed) {
    const k = S.locale + "|" + dp + "|" + (signed ? 1 : 0);
    let f = nfCache.get(k);
    if (!f) {
      f = new Intl.NumberFormat(S.locale, { minimumFractionDigits: dp, maximumFractionDigits: dp, signDisplay: signed ? "exceptZero" : "auto" });
      nfCache.set(k, f);
    }
    return f.format(v).replace(/^-/, "−");
  }
  function withUnit(s, unit, S) {
    if (!unit || unit === "points" || unit === "level") return s;
    if (unit === "pp") return s + "\u00a0" + S.unit.pp;      // percentage points (changes of a % value)
    return s + (unit === "%" && !S.pctSpace ? "" : " ") + unitLabel(unit, S);
  }
  function fmtVal(key, v, S, signed) {
    const md = METRICS[key];
    if (v == null) return "—";
    return withUnit(fmtNum(v, md ? md.dp : 1, S, signed), md ? md.unit : "", S);
  }
  /** y-axis unit tag: [kg] [lb] [%] [kcal] [kJ] [kg/m²] [points] [level] */
  const axisUnit = (unit, S) => "[" + (unit ? unitLabel(unit, S) : "–") + "]";
  /** Change vs the most recent earlier measurement that has the value (rows sorted ascending).
   *  Purely directional: verdict "up" | "down" | "same" (rounded to display precision), "none" when
   *  there is no earlier value. No health semantics, no normal-range logic.
   *  Returns null when there is no current value. */
  function compare(key, rows, idx) {
    const md = METRICS[key], cur = num(rows[idx] && rows[idx][key]);
    if (cur == null) return null;
    for (let j = idx - 1; j >= 0; j--) {
      const p = num(rows[j][key]);
      if (p == null) continue;
      const f = Math.pow(10, md ? md.dp : 1);
      const delta = Math.round((cur - p) * f) / f;
      return { delta, verdict: delta > 0 ? "up" : delta < 0 ? "down" : "same", prevRow: rows[j], prevIdx: j, immediate: j === idx - 1 };
    }
    return { delta: null, verdict: "none", prevRow: null, prevIdx: -1, immediate: false };
  }

  function niceTicks(lo, hi, count) {
    const span = hi - lo || 1, raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(f => f * mag).find(s => s >= raw * 0.999);
    const a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step, out = [];
    for (let v = a; v <= b + step / 2; v += step) out.push(+v.toFixed(6));
    return out;
  }
  function stepDp(ticks) {
    if (ticks.length < 2) return 0;
    const s = String(+(ticks[1] - ticks[0]).toFixed(6)), i = s.indexOf(".");
    return i < 0 ? 0 : Math.min(3, s.length - i - 1);
  }

  // ------------------------------------------------------------------ time axis (with pauses)
  /** Builds the shared x-axis for a sorted list of ISO dates.
   *  A pause is a gap longer than `pauseMonths` calendar months (default 3, a user setting).
   *  breaks:'auto' ("Shrink pauses") shrinks each pause to a narrow break on the axis (segments keep a real time scale inside; width ~
   *  own time span, min MIN_SEG_F); breaks:'none' keeps one continuous time scale. Positions are
   *  fractions 0..1 of the plot width, so every chart on the page lines up at any pixel width. */
  function buildAxis(dates, opts) {
    const mode = (opts && opts.breaks) === "none" ? "none" : "auto";
    const pm = Math.max(1, Math.round((opts && opts.pauseMonths) || 3));
    const ts = dates.map(tOf);
    const isPause = (a, b) => b > addMonths(a, pm);
    const groups = [];
    let cur = null, pauses = 0;
    ts.forEach((t, i) => {
      const p = i > 0 && isPause(ts[i - 1], t);
      if (p) pauses++;
      if (i === 0 || (mode === "auto" && p)) { cur = [t]; groups.push(cur); } else cur.push(t);
    });
    if (!groups.length) groups.push([Date.UTC(2000, 0, 1)]);
    const n = groups.length, avail = 1 - (n - 1) * BREAK_F;
    const spans = groups.map(gr => gr[gr.length - 1] - gr[0]);
    let widths;
    if (n * MIN_SEG_F >= avail) widths = groups.map(() => avail / n);
    else {
      const fixed = new Array(n).fill(false);
      for (;;) {
        const nFixed = fixed.filter(Boolean).length, rem = avail - nFixed * MIN_SEG_F;
        const free = spans.reduce((a, s, i) => a + (fixed[i] ? 0 : s), 0);
        widths = spans.map((s, i) => fixed[i] ? MIN_SEG_F : (free ? rem * s / free : rem / (n - nFixed)));
        const bad = widths.findIndex((w, i) => !fixed[i] && w < MIN_SEG_F - 1e-9);
        if (bad < 0) break;
        fixed[bad] = true;
      }
    }
    let f = 0;
    const segs = groups.map((gr, i) => {
      const s = { t0: gr[0], t1: gr[gr.length - 1], f0: f, f1: f + widths[i], n: gr.length };
      f += widths[i] + BREAK_F;
      return s;
    });
    const breaks = segs.slice(1).map((s, i) => ({ f0: segs[i].f1, f1: s.f0, days: Math.round((s.t0 - segs[i].t1) / DAY) }));
    function segOf(t) {
      for (let i = 0; i < segs.length; i++) if (t <= segs[i].t1) return i;
      return segs.length - 1;
    }
    function frac(t) {
      const s = segs[segOf(t)];
      if (s.t1 === s.t0) return (s.f0 + s.f1) / 2;
      const inner = Math.min(0.025, (s.f1 - s.f0) / 4);
      return s.f0 + inner + (t - s.t0) / (s.t1 - s.t0) * (s.f1 - s.f0 - 2 * inner);
    }
    /** x tick candidates for a plot `pw` pixels wide: month starts, stepped so labels sit
     *  >= 70px apart inside each segment; a segment with no month start gets its own label. */
    function ticks(pw, S) {
      const out = [];
      segs.forEach(s => {
        const wpx = (s.f1 - s.f0) * pw, spanD = (s.t1 - s.t0) / DAY;
        let got = [];
        if (spanD > 0) {
          for (const step of [1, 2, 3, 6, 12, 24]) {
            if (wpx * step * 30.44 / spanD < 70) continue;
            const d0 = new Date(s.t0);
            let y = d0.getUTCFullYear(), mo = d0.getUTCMonth();
            if (Date.UTC(y, mo, 1) < s.t0) mo++;
            for (;;) {
              if (mo > 11) { y += 1; mo -= 12; }
              const t = Date.UTC(y, mo, 1);
              if (t > s.t1) break;
              if ((y * 12 + mo) % step === 0) got.push({ t, label: step >= 12 ? String(y) : S.fmt.monthYear(y, mo + 1) });
              mo++;
            }
            break;
          }
        }
        if (!got.length) { const d = new Date(s.t0); got = [{ t: s.t0, label: S.fmt.monthYear(d.getUTCFullYear(), d.getUTCMonth() + 1) }]; }
        out.push(...got);
      });
      return out.map(o => Object.assign(o, { f: frac(o.t) }));
    }
    return { mode, pauseMonths: pm, segs, breaks, pauses, frac, segOf, ticks, isPause };
  }

  // ------------------------------------------------------------------ chart frame
  function geom(host, A, o) {
    const W = Math.max(240, Math.floor(host.clientWidth || 640)), H = o.height;
    const lw = Math.max.apply(null, o.yt.map(v => o.tf(v).length).concat([o.yUnit.length])) * 6.6 + 12;
    const pad = { l: Math.max(30, Math.round(lw)), r: o.rightPad || 14, t: 24 + (o.topExtra || 0), b: 26 };
    const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
    const y0 = o.yt[0], y1 = o.yt[o.yt.length - 1];
    return {
      W, H, pad, pw, ph, yt: o.yt, tf: o.tf, yUnit: o.yUnit,
      X: t => pad.l + A.frac(t) * pw,
      Y: v => pad.t + (1 - (v - y0) / (y1 - y0 || 1)) * ph
    };
  }

  function drawFrame(svg, G, A, S, o) {
    const base = G.pad.t + G.ph;
    // unit of the y-axis, above the top tick label, aligned with the tick labels' left edge
    const tickW = Math.max.apply(null, G.yt.map(v => G.tf(v).length)) * 6.6;
    svg.append(sv("text", { class: "ibc-yunit", x: Math.max(2, G.pad.l - 8 - Math.max(tickW, G.yUnit.length * 6.6)), y: 12 }, G.yUnit));
    G.yt.forEach(v => {
      svg.append(sv("line", { class: "ibc-grid", x1: G.pad.l, x2: G.pad.l + G.pw, y1: G.Y(v), y2: G.Y(v) }));
      svg.append(sv("text", { class: "ibc-tick", x: G.pad.l - 8, y: G.Y(v) + 4, "text-anchor": "end" }, G.tf(v)));
    });
    A.breaks.forEach(b => {
      const x0 = G.pad.l + b.f0 * G.pw, x1 = G.pad.l + b.f1 * G.pw, c = (x0 + x1) / 2;
      svg.append(sv("rect", { class: "ibc-brk-band", x: x0, y: G.pad.t, width: Math.max(0, x1 - x0), height: G.ph }));
      if (!o.compact) svg.append(sv("text", { class: "ibc-brk-label", x: c, y: 12, "text-anchor": "middle" }, S.fmt.gap(b.days)));
    });
    svg.append(sv("line", { class: "ibc-axis", x1: G.pad.l, x2: G.pad.l + G.pw, y1: base, y2: base }));
    A.breaks.forEach(b => {
      const c = G.pad.l + (b.f0 + b.f1) / 2 * G.pw, bw = (b.f1 - b.f0) * G.pw;
      const d = Math.max(1.5, Math.min(3.5, bw / 4));
      svg.append(sv("rect", { class: "ibc-brk-gap", x: c - d - 3, y: base - 7, width: 2 * d + 6, height: 14 }));
      svg.append(sv("path", { class: "ibc-brk-mark", d: `M${c - d - 2} ${base + 5}L${c - d + 2} ${base - 5}M${c + d - 2} ${base + 5}L${c + d + 2} ${base - 5}` }));
    });
    let lastR = -Infinity;
    A.ticks(G.pw, S).forEach(tk => {
      const x = G.pad.l + tk.f * G.pw, w = tk.label.length * 6.3;
      const cx = Math.min(Math.max(x, w / 2 + 2), G.W - w / 2 - 2);
      if (cx - w / 2 < lastR + 10) return;
      lastR = cx + w / 2;
      svg.append(sv("line", { class: "ibc-axis", x1: x, x2: x, y1: base, y2: base + 4 }));
      svg.append(sv("text", { class: "ibc-tick", x: cx, y: G.H - 7, "text-anchor": "middle" }, tk.label));
    });
  }

  /** Connecting lines between consecutive points of a series (pts: [{i (row index), t, v}]):
   *  - across a pause (only while "Shrink pauses" is on, i.e. across a break band): DASHED -> pause
   *  - over measurements in between that lack the value:                 DOTTED  -> bridge
   *  - everything else (consecutive measurements inside a segment):      SOLID   -> solid */
  function linePaths(pts, A, G) {
    const P = p => `${G.X(p.t).toFixed(1)} ${G.Y(p.v).toFixed(1)}`;
    let solid = "", bridge = "", pause = "", open = false;
    for (let k = 1; k < pts.length; k++) {
      const a = pts[k - 1], b = pts[k];
      if (A.mode === "auto" && (A.segOf(a.t) !== A.segOf(b.t) || A.isPause(a.t, b.t))) { pause += `M${P(a)}L${P(b)}`; open = false; }
      else if (b.i - a.i > 1) { bridge += `M${P(a)}L${P(b)}`; open = false; }
      else { solid += (open ? "" : `M${P(a)}`) + `L${P(b)}`; open = true; }
    }
    return { solid, bridge, pause };
  }
  function appendLines(svg, lp, cls) {
    if (lp.pause) svg.append(sv("path", { class: `ibc-pause ${cls}`, d: lp.pause }));
    if (lp.bridge) svg.append(sv("path", { class: `ibc-bridge ${cls}`, d: lp.bridge }));
    if (lp.solid) svg.append(sv("path", { class: `ibc-line ${cls}`, d: lp.solid }));
  }
  /** point marker: circle by default; sideways triangles mark body sides: 'right' ▶, 'left' ◀ */
  function marker(kind, x, y, cls, big) {
    if (kind !== "right" && kind !== "left") return sv("circle", { class: `ibc-dot ${cls}${big ? " ibc-dot-hl" : ""}`, cx: x, cy: y, r: big ? 6 : 4 });
    const r = big ? 9 : 7, h = r * 0.87, sgn = kind === "right" ? 1 : -1;
    const d = `M${x + sgn * r} ${y}L${x - sgn * r / 2} ${y - h}L${x - sgn * r / 2} ${y + h}Z`;
    return sv("path", { class: `ibc-dot ibc-tri ${cls}${big ? " ibc-dot-hl" : ""}`, d });
  }

  function lineKey(cls, mk, wide) {
    const w = wide ? 26 : 18, h = mk ? 16 : 8;
    const s = sv("svg", { class: "ibc-key " + cls, width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
    s.append(sv("line", { x1: 1, x2: w - 1, y1: h / 2, y2: h / 2, class: "ibc-key-line" }));
    if (mk) s.append(marker(mk, w / 2, h / 2, "ibc-key-mark", false));
    return s;
  }
  function rectKey(cls) { return el("i", { class: "ibc-swatch " + cls, "aria-hidden": "true" }); }

  // ------------------------------------------------------------------ hover layer
  function attachHover(host, svg, G, rows, h, ctx) {
    const tip = el("div", { class: "ibc-tip" });
    tip.hidden = true;
    host.append(tip);
    const xs = rows.map(r => G.X(r._t));
    let cur = -1, armed = -1;
    const nearest = px => { let b = 0, bd = Infinity; xs.forEach((x, i) => { const d = Math.abs(x - px); if (d < bd) { bd = d; b = i; } }); return b; };
    const toX = e => { const b = svg.getBoundingClientRect(); return (e.clientX - b.left) * G.W / (b.width || G.W); };
    function show(i, withTip) {
      cur = i;
      h.mark(i);
      if (!withTip) { tip.hidden = true; return; }
      tip.replaceChildren(...h.tip(i));
      tip.hidden = false;
      const scale = (svg.getBoundingClientRect().width || G.W) / G.W;
      const tw = tip.offsetWidth, x = xs[i] * scale, W = host.clientWidth || G.W;
      let left = x + 16;
      if (left + tw > W) left = x - 16 - tw;
      tip.style.left = Math.max(0, Math.min(left, W - tw)) + "px";
      tip.style.top = Math.max(0, G.pad.t * scale) + "px";
    }
    function clear() { cur = -1; h.unmark(); tip.hidden = true; }
    svg.addEventListener("pointerdown", e => { armed = cur; const i = nearest(toX(e)); show(i, true); ctx.bus.emit(rows[i].measured_date, host); });
    svg.addEventListener("pointermove", e => {
      if (e.pointerType !== "mouse") return;
      const i = nearest(toX(e));
      if (i !== cur || tip.hidden) { show(i, true); ctx.bus.emit(rows[i].measured_date, host); }
    });
    svg.addEventListener("pointerleave", e => { if (e.pointerType === "mouse") { clear(); ctx.bus.emit(null, host); } });
    svg.addEventListener("click", e => {
      const i = nearest(toX(e));
      if (e.pointerType === "mouse" || e.pointerType === "" || armed === i) { if (ctx.onSelect) ctx.onSelect(rows[i]); }
    });
    return {
      show, clear,
      get cur() { return cur; },
      sync(date) { const i = rows.findIndex(r => r.measured_date === date); if (i < 0) clear(); else show(i, false); },
      text() { return tip.textContent; }
    };
  }

  /** Keyboard + focus handling lives on the persistent plot element; it drives whichever
   *  hover handle the latest draw installed (plot._hv). */
  function keyboardOnce(plot, ctx, rowsRef) {
    if (plot._kbd) return;
    plot._kbd = true;
    plot.addEventListener("keydown", e => {
      const hv = plot._hv, rows = rowsRef();
      if (!hv || !rows.length) return;
      let i = hv.cur < 0 ? rows.length - 1 : hv.cur;
      if (e.key === "ArrowLeft") i = Math.max(0, i - 1);
      else if (e.key === "ArrowRight") i = Math.min(rows.length - 1, i + 1);
      else if (e.key === "Home") i = 0;
      else if (e.key === "End") i = rows.length - 1;
      else if (e.key === "Enter" || e.key === " ") { if (hv.cur >= 0 && ctx.onSelect) ctx.onSelect(rows[hv.cur]); e.preventDefault(); return; }
      else if (e.key === "Escape") { hv.clear(); ctx.bus.emit(null, plot); return; }
      else return;
      e.preventDefault();
      hv.show(i, true);
      ctx.bus.emit(rows[i].measured_date, plot);
      ctx.announce(hv.text());
    });
    plot.addEventListener("focus", () => {
      const hv = plot._hv, rows = rowsRef();
      if (!hv || !rows.length) return;
      const i = hv.cur < 0 ? rows.length - 1 : hv.cur;
      hv.show(i, true);
      ctx.bus.emit(rows[i].measured_date, plot);
      ctx.announce(hv.text());
    });
    plot.addEventListener("blur", () => { if (plot._hv) plot._hv.clear(); ctx.bus.emit(null, plot); });
  }

  function tipHead(r, S) {
    const head = el("div", { class: "ibc-tip-h" });
    head.append(el("b", null, S.fmt.date(r.measured_date)));
    if (r.measured_time) head.append(document.createTextNode(" · " + String(r.measured_time).slice(0, 5)));
    return head;
  }
  function tipFoot(r, S) {
    const f = el("div", { class: "ibc-tip-src" });
    f.append(document.createTextNode(srcText(r, S)));
    return [f];   // click / tap still opens the measurement; no hint text
  }
  function tipRow(keyNode, main, label, extra) {
    const row = el("div", { class: "ibc-tip-r" });
    row.append(keyNode, el("b", null, main), el("span", null, label));
    if (extra) row.append(el("small", null, extra));
    return row;
  }
  function emptyPlot(host, sp, S) {
    const e = el("p", { class: "ibc-empty" }, S.chart.emptyRange);
    e.style.minHeight = sp.height + "px";
    host.append(e);
    return null;
  }

  // ------------------------------------------------------------------ line chart
  /** spec: { rows, axis, S, height, compact, yUnit:'[kg]', series:[{id, cls, label, short,
   *          marker?: 'up'|'down', get(r), tip?(r)}], fmt(v, series) -> string, refs:[{v, label}],
   *          zones:{top, bottom}, symmetric, hidden:Set, bus, onSelect, announce }
   *  rows must carry _t (UTC ms of measured_date). */
  function lineChart(host, sp) {
    host.querySelectorAll("svg.ibc-svg, .ibc-tip, .ibc-empty").forEach(n => n.remove());
    const { rows, axis: A, S } = sp;
    const vis = sp.series.filter(s => !(sp.hidden && sp.hidden.has(s.id)));
    const vals = [];
    vis.forEach(s => rows.forEach(r => { const v = s.get(r); if (v != null) vals.push(v); }));
    if (!vals.length) return emptyPlot(host, sp, S);
    (sp.refs || []).forEach(r => vals.push(r.v));
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (sp.symmetric) { const mm = Math.max(Math.abs(lo), Math.abs(hi)) || 1; lo = -mm; hi = mm; }
    if (lo === hi) { const d = Math.abs(lo) * 0.05 || 1; lo -= d; hi += d; }
    // headroom: a label above the top point / below the bottom point must stay inside the plot
    const room = Math.min(0.3, 26 / Math.max(60, sp.height - 50));
    const padv = (hi - lo) * Math.max(0.12, room / (1 - 2 * room));
    const yt = niceTicks(lo - padv, hi + padv, sp.compact ? 3 : 4);
    const tdp = stepDp(yt);
    const tf = v => fmtNum(v, tdp, S, !!sp.symmetric);
    // no right gutter: every value (the latest included) is labelled inline at its point
    const G = geom(host, A, { height: sp.height, compact: sp.compact, yt, tf, rightPad: 18, yUnit: sp.yUnit || "[–]" });
    const svg = sv("svg", { class: "ibc-svg", viewBox: `0 0 ${G.W} ${G.H}`, "aria-hidden": "true" });
    drawFrame(svg, G, A, S, { compact: sp.compact });

    (sp.refs || []).forEach(r => {
      svg.append(sv("line", { class: "ibc-ref", x1: G.pad.l, x2: G.pad.l + G.pw, y1: G.Y(r.v), y2: G.Y(r.v) }));
      if (r.label) svg.append(sv("text", { class: "ibc-ref-label", x: G.pad.l + 6, y: G.Y(r.v) - 5 }, r.label));
    });
    if (sp.zones) {
      svg.append(sv("text", { class: "ibc-zone", x: G.pad.l + 6, y: G.pad.t + 13 }, sp.zones.top));
      svg.append(sv("text", { class: "ibc-zone", x: G.pad.l + 6, y: G.pad.t + G.ph - 7 }, sp.zones.bottom));
    }

    vis.forEach(s => {
      const pts = [];
      rows.forEach((r, i) => { const v = s.get(r); if (v != null) pts.push({ i, t: r._t, v }); });
      appendLines(svg, linePaths(pts, A, G), s.cls);
      pts.forEach(p => svg.append(marker(s.marker, G.X(p.t), G.Y(p.v), s.cls, false)));
      s._pts = pts;
    });

    // value labels at the points, wherever they fit: the latest first (top priority), then the
    // extremes, then the rest; a label that would collide or overflow is skipped. Series identity
    // stays readable without colour through the legend (names + ◀ ▶ marker shapes).
    const boxes = [];
    const free = b => b.x0 >= G.pad.l - 2 && b.x1 <= G.pad.l + G.pw + 14 && b.y0 >= G.pad.t - 2 && b.y1 <= G.pad.t + G.ph - 2 &&
      !boxes.some(o => b.x0 < o.x1 + 2 && b.x1 > o.x0 - 2 && b.y0 < o.y1 + 1 && b.y1 > o.y0 - 1);
    vis.forEach(s => s._pts.forEach(p => { const x = G.X(p.t), y = G.Y(p.v); boxes.push({ x0: x - 6, x1: x + 6, y0: y - 6, y1: y + 6 }); }));
    if (sp.zones) [[sp.zones.top, G.pad.t + 13], [sp.zones.bottom, G.pad.t + G.ph - 7]].forEach(([tx, y]) =>
      boxes.push({ x0: G.pad.l + 6, x1: G.pad.l + 6 + tx.length * 6, y0: y - 10, y1: y + 2 }));
    (sp.refs || []).forEach(r => { if (r.label) boxes.push({ x0: G.pad.l + 6, x1: G.pad.l + 6 + r.label.length * 6, y0: G.Y(r.v) - 15, y1: G.Y(r.v) - 3 }); });
    // Side per x: at every x the HIGHER series' label goes above its marker and the LOWER one's
    // below (equal values: the first above, the second below), so a label never sits between two
    // lines; a lone series prefers above. The latest point of every series is ALWAYS labelled
    // (placed first, never thinned, kept inside the plot); the rest follow by priority (extremes,
    // then newest first, alternating between the series) and are skipped when they would collide.
    const side = new Map();
    rows.forEach((r, i) => {
      const here = vis.map((s, si) => ({ si, v: s.get(r) })).filter(o => o.v != null).sort((a, b) => b.v - a.v);
      here.forEach((o, k) => side.set(o.si + "|" + i, here.length < 2 ? "any" : k === 0 ? "above" : k === here.length - 1 ? "below" : "any"));
    });
    const place = (s, si, p, must) => {
      const x = G.X(p.t), y = G.Y(p.v);
      // the number only (the axis tag has the unit); a cs thousands separator is also a nbsp, so
      // strip only a trailing non-numeric unit ("1 786 kcal" -> "1 786")
      const text = sp.fmt(p.v, s).replace(/\u00a0[^\u00a0\d]+$/, "");
      const w = text.length * 5.9;
      const pref = side.get(si + "|" + p.i);
      const spots = [];
      (pref === "above" ? [-10] : pref === "below" ? [17] : [-10, 17]).forEach(dy => spots.push([dy, "middle"], [dy, "end"], [dy, "start"]));
      const boxOf = ([dy, anchor]) => {
        const x0 = anchor === "end" ? x + 4 - w : anchor === "start" ? x - 4 : x - w / 2;
        return { x0, x1: x0 + w, y0: y + dy - 9, y1: y + dy + 2, dy, anchor };
      };
      let b = spots.map(boxOf).find(free);
      if (!b && must) {                                     // the latest value is always shown
        b = boxOf(spots[0]);
        const right = G.pad.l + G.pw + 14, top = G.pad.t, bottom = G.pad.t + G.ph;
        if (b.x1 > right) { b.x0 -= b.x1 - right; b.x1 = right; }
        if (b.y0 < top) { const d = top - b.y0; b.y0 += d; b.y1 += d; b.dy += d; }
        if (b.y1 > bottom) { const d = b.y1 - bottom; b.y0 -= d; b.y1 -= d; b.dy -= d; }
      }
      if (!b) return;
      boxes.push(b);
      const tx = b.anchor === "end" ? b.x1 : b.anchor === "start" ? b.x0 : (b.x0 + b.x1) / 2;
      svg.append(sv("text", { class: "ibc-plabel", x: tx, y: y + b.dy, "text-anchor": b.anchor }, text));
    };
    vis.forEach((s, si) => { if (s._pts.length) place(s, si, s._pts[s._pts.length - 1], true); });
    const orders = vis.map(s => {
      const pts = s._pts, last = pts.length - 1, order = [];
      if (!pts.length) return order;
      let iMax = 0, iMin = 0;
      pts.forEach((p, k) => { if (p.v > pts[iMax].v) iMax = k; if (p.v < pts[iMin].v) iMin = k; });
      [iMax, iMin, ...pts.map((p, k) => k).reverse()].forEach(k => { if (k !== last && !order.includes(k)) order.push(k); });
      return order;
    });
    for (let r = 0; r < Math.max(0, ...orders.map(o => o.length)); r++) {
      vis.forEach((s, si) => { const k = orders[si][r]; if (k != null) place(s, si, s._pts[k], false); });
    }

    const cross = sv("line", { class: "ibc-cross", y1: G.pad.t, y2: G.pad.t + G.ph, visibility: "hidden" });
    const hl = sv("g");
    svg.append(cross, hl);
    host.append(svg);

    return attachHover(host, svg, G, rows, {
      mark(i) {
        const cx = G.X(rows[i]._t);
        cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
        hl.replaceChildren();
        vis.forEach(s => { const v = s.get(rows[i]); if (v != null) hl.append(marker(s.marker, cx, G.Y(v), s.cls, true)); });
      },
      unmark() { cross.setAttribute("visibility", "hidden"); hl.replaceChildren(); },
      tip(i) {
        const r = rows[i], out = [tipHead(r, S)];
        vis.forEach(s => {
          const v = s.get(r), t = s.tip ? s.tip(r) : null;
          out.push(tipRow(lineKey(s.cls, s.marker), v == null ? "—" : (t ? t.main : sp.fmt(v, s)), s.label, v == null ? null : (t ? t.extra : null)));
        });
        return out.concat(tipFoot(r, S));
      }
    }, sp);
  }

  // ------------------------------------------------------------------ stacked bar chart
  /** spec: { rows, axis, S, height, mode:'kg'|'pct', parts:[{id, cls, label, get(r)}] bottom->top,
   *          total(r) -> weight, bus, onSelect }. A row missing any part draws the total as one
   *          neutral bar (and is 100 % neutral in 'pct' mode). Each part also gets a 2 px line
   *          through the MIDPOINT of its segment on consecutive bars (same gap rules as lines). */
  function stackChart(host, sp) {
    host.querySelectorAll("svg.ibc-svg, .ibc-tip, .ibc-empty").forEach(n => n.remove());
    const { rows, axis: A, S } = sp;
    const pct = sp.mode === "pct";
    const stacks = rows.map(r => {
      const W = sp.total(r), vals = sp.parts.map(p => p.get(r));
      const ok = W != null && vals.every(v => v != null && v >= 0);
      return { W, vals, ok };
    });
    const Ws = stacks.map(s => s.W).filter(v => v != null);
    if (!Ws.length) return emptyPlot(host, sp, S);
    const yt = pct ? [0, 25, 50, 75, 100] : niceTicks(0, Math.max.apply(null, Ws) * 1.04, 4);
    const tf = v => fmtNum(v, pct ? 0 : stepDp(yt), S);
    const topExtra = pct ? 0 : 12;
    const G = geom(host, A, { height: sp.height, yt, tf, rightPad: 14, topExtra, yUnit: axisUnit(pct ? "%" : "kg", S) });
    const svg = sv("svg", { class: "ibc-svg", viewBox: `0 0 ${G.W} ${G.H}`, "aria-hidden": "true" });
    drawFrame(svg, G, A, S, { compact: false });

    const xs = rows.map(r => G.X(r._t));
    let minGap = Infinity;
    for (let i = 1; i < rows.length; i++) if (A.segOf(rows[i]._t) === A.segOf(rows[i - 1]._t)) minGap = Math.min(minGap, xs[i] - xs[i - 1]);
    const bw = Math.max(3, Math.min(BAR_MAX, Math.floor(minGap - 3)));
    const base = G.pad.t + G.ph;
    const bars = [];
    const mids = sp.parts.map(() => []);    // per part: [{i, t, v=midpoint}]
    const segBoxes = [];                     // for the in-segment value labels
    rows.forEach((r, i) => {
      const s = stacks[i], gEl = sv("g", { class: "ibc-bar" }), x = xs[i] - bw / 2;
      bars.push(gEl);
      svg.append(gEl);
      if (s.W == null) return;
      const segs = s.ok ? s.vals.map((v, j) => ({ v: pct ? v / s.W * 100 : v, cls: sp.parts[j].cls, j })) : [{ v: pct ? 100 : s.W, cls: "ctx", j: -1 }];
      let cum = 0;
      const drawn = segs.filter(p => p.v > 0);
      const barTop = G.Y(drawn.reduce((a, p) => a + p.v, 0));
      drawn.forEach((p, k) => {
        const yTop = G.Y(cum + p.v), yBot = G.Y(cum) - (k > 0 ? SURFACE_GAP : 0);
        if (p.j >= 0) mids[p.j].push({ i, t: r._t, v: cum + p.v / 2 });
        cum += p.v;
        const h = yBot - yTop;
        if (p.j >= 0) segBoxes.push({ i, x: xs[i], yTop, h, barTop, first: k === 0, last: k === drawn.length - 1, v: pct ? p.v : s.vals[p.j] });
        if (h <= 0.5) return;
        if (k === drawn.length - 1) {
          const rr = Math.min(4, bw / 2, h);
          gEl.append(sv("path", { class: "ibc-seg " + p.cls, d: `M${x} ${yBot}V${yTop + rr}Q${x} ${yTop} ${x + rr} ${yTop}H${x + bw - rr}Q${x + bw} ${yTop} ${x + bw} ${yTop + rr}V${yBot}Z` }));
        } else gEl.append(sv("rect", { class: "ibc-seg " + p.cls, x, y: yTop, width: bw, height: h }));
      });
    });
    // category lines: one per layer, through each segment's midpoint, drawn over the bars
    sp.parts.forEach((p, j) => appendLines(svg, linePaths(mids[j], A, G), `ibc-layer ${p.cls}`));
    // one value per segment, inside it when it fits (horizontal, else rotated); white on the
    // categorical fills in both themes (text never takes the series colour). A rotated label may
    // overflow its segment a little: up to ~30 % longer than the segment it stays centred on it; a
    // segment of at least 40 % of the label starts the label at its OUTER edge (the bar top / the axis)
    // and lets it run into the neighbouring segment (a middle segment: centred, spilling both ways).
    // Never above the bar top, below the axis, or onto another label of the same bar; a segment far
    // too thin keeps no label (the tooltip has the value).
    const placed = new Map();   // bar index -> occupied [y0, y1] spans
    const isFree = (i, y0, y1) => (placed.get(i) || []).every(([a, c]) => y1 + 2 <= a || y0 - 2 >= c);
    const take = (i, y0, y1) => { if (!placed.has(i)) placed.set(i, []); placed.get(i).push([y0, y1]); };
    const rot = (x, cy, text, cls) => svg.append(sv("text", { class: cls, x, y: cy, "text-anchor": "middle", "dominant-baseline": "central", transform: `rotate(-90 ${x} ${cy})` }, text));
    const inBar = (b, y0, y1) => y0 >= b.barTop - 0.5 && y1 <= base + 0.5;
    const late = [];
    segBoxes.forEach(b => {
      const text = fmtNum(b.v, 1, S), tw = text.length * 5.6, cy = b.yTop + b.h / 2;   // kg or % with 1 decimal
      if (bw >= tw + 4 && b.h >= 13) {
        svg.append(sv("text", { class: "ibc-seglabel", x: b.x, y: cy + 3.5, "text-anchor": "middle" }, text));
        take(b.i, cy - 6, cy + 6);
      } else if (bw >= 11) {
        if (b.h >= tw + 6) { rot(b.x, cy, text, "ibc-seglabel"); take(b.i, cy - tw / 2, cy + tw / 2); }
        else if (tw <= b.h * 1.3 && inBar(b, cy - tw / 2, cy + tw / 2) && isFree(b.i, cy - tw / 2, cy + tw / 2)) {
          rot(b.x, cy, text, "ibc-seglabel ibc-spill"); take(b.i, cy - tw / 2, cy + tw / 2);
        } else if (b.h >= tw * 0.4) late.push({ b, text, tw, cy });
      }
    });
    late.forEach(({ b, text, tw, cy }) => {
      let y0 = cy - tw / 2;                                  // a middle segment: centred, spilling both ways
      if (b.last && !b.first) y0 = b.yTop + 2;               // the top segment: from the bar top down into the neighbour
      else if (b.first && !b.last) y0 = b.yTop + b.h - 2 - tw;   // the bottom one: from the axis up into the neighbour
      const y1 = y0 + tw;
      if (!inBar(b, y0, y1) || !isFree(b.i, y0, y1)) return;
      rot(b.x, (y0 + y1) / 2, text, "ibc-seglabel ibc-spill"); take(b.i, y0, y1);
    });
    // weight on top: greedy from the latest bar leftwards so the newest is always labelled
    if (!pct) {
      let leftEdge = Infinity;
      for (let i = rows.length - 1; i >= 0; i--) {
        const s = stacks[i];
        if (s.W == null) continue;
        const t = fmtNum(s.W, 1, S), w = t.length * 6.2;
        if (xs[i] + w / 2 > leftEdge - 4) continue;
        leftEdge = xs[i] - w / 2;
        svg.append(sv("text", { class: "ibc-total", x: xs[i], y: G.Y(s.W) - 5, "text-anchor": "middle" }, t));
      }
    }
    svg.append(sv("line", { class: "ibc-axis", x1: G.pad.l, x2: G.pad.l + G.pw, y1: base, y2: base }));
    host.append(svg);

    const fmtKgPct = (kg, W) => withUnit(fmtNum(kg, 1, S), "kg", S) + " · " + withUnit(fmtNum(kg / W * 100, 1, S), "%", S);
    return attachHover(host, svg, G, rows, {
      mark(i) { bars.forEach((b, j) => b.classList.toggle("dim", j !== i)); },
      unmark() { bars.forEach(b => b.classList.remove("dim")); },
      tip(i) {
        const r = rows[i], s = stacks[i], out = [tipHead(r, S)];
        if (s.W != null) {
          if (s.ok) sp.parts.slice().reverse().forEach((p, k) => {
            const v = s.vals[sp.parts.length - 1 - k];
            out.push(tipRow(rectKey(p.cls), fmtKgPct(v, s.W), p.label));
          });
          else out.push(el("div", { class: "ibc-tip-note" }, S.chart.comp.weightOnly));
          out.push(tipRow(rectKey("total"), withUnit(fmtNum(s.W, 1, S), "kg", S) + " · " + withUnit(fmtNum(100, 0, S), "%", S), S.chart.comp.total));
        }
        return out.concat(tipFoot(r, S));
      }
    }, sp);
  }

  // ------------------------------------------------------------------ UI pieces
  function segCtl(label, options, value, onPick, extraCls) {
    const wrap = el("div", { class: "ibc-ctl" + (extraCls ? " " + extraCls : ""), role: "group", "aria-label": label });
    options.forEach(([val, text, disabled, title]) => {
      const b = el("button", { type: "button", "aria-pressed": String(val === value), title: title || null }, text);
      if (disabled) b.disabled = true;
      b.addEventListener("click", () => onPick(val));
      wrap.append(b);
    });
    return wrap;
  }
  /** "gym · source kind", e.g. "City Gym · result sheet"; missing gym -> "Gym not entered" */
  const srcText = (r, S) => (r.gym || S.source.gymMissing) + " · " + (S.source.kind[r.origin] || r.origin || "");
  /** Table twin of a chart. Columns: Date | values (the series shown, legend order, current unit,
   *  unit in [brackets] in the header) | Source. Newest first; "—" for missing values.
   *  cols: [{head, get(r) -> number|null, dp, signed}] */
  function tableView(S, rows, cols) {
    const det = el("details", { class: "ibc-table" });
    det.append(el("summary", null, S.chart.table));
    const wrap = el("div", { class: "ibc-tablewrap" }), t = el("table");
    const hr = el("tr");
    hr.append(el("th", { scope: "col" }, S.chart.date));
    cols.forEach(c => hr.append(el("th", { scope: "col", class: "num" }, c.head)));
    hr.append(el("th", { scope: "col" }, S.chart.source));
    t.append(el("thead"));
    t.tHead.append(hr);
    const tb = el("tbody");
    rows.slice().reverse().forEach(r => {
      const tr = el("tr");
      tr.append(el("th", { scope: "row" }, S.fmt.date(r.measured_date)));
      cols.forEach(c => { const v = c.get(r); tr.append(el("td", { class: "num" }, v == null ? "—" : fmtNum(v, c.dp, S, c.signed))); });
      tr.append(el("td", null, srcText(r, S)));
      tb.append(tr);
    });
    t.append(tb);
    wrap.append(t);
    det.append(wrap);
    return det;
  }
  function makeBus() {
    const subs = new Set();
    return { on: f => subs.add(f), emit: (d, src) => subs.forEach(f => f(d, src)), clear: () => subs.clear() };
  }

  // ⓘ explainer -- ONE component for History and Report (the server renders the same markup in
  // templates/_ui.html): a <details>, so it works without JS; only one is open at a time and Esc or
  // an outside click closes it. Content: term definitions from the SHARED def.* strings (S.def),
  // then a short note on what the chart shows.
  const INFO_OPEN = "details.ibc-info[open]";
  function closeInfo(except, focus) {
    document.querySelectorAll(INFO_OPEN).forEach(d => {
      if (d === except) return;
      d.removeAttribute("open");
      if (focus) { const s = d.querySelector("summary"); if (s) s.focus(); }
    });
  }
  if (typeof document !== "undefined") {
    document.addEventListener("pointerdown", e => closeInfo(e.target.closest && e.target.closest("details.ibc-info"), false));
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeInfo(null, true); });
    document.addEventListener("toggle", e => {
      if (e.target.matches && e.target.matches("details.ibc-info") && e.target.open) closeInfo(e.target, false);
    }, true);
  }
  // term names that are not a canonical column (same keys as views.TERM_NAME on the server)
  const TERM = { seg_lean: S => S.chart.seg.lean, seg_fat: S => S.chart.seg.fat, seg_pct: S => S.chart.seg.pct,
    other_lean: S => S.chart.comp.partOther, pct_of_weight: S => S.chart.comp.pctOfWeight };
  const defs = (S, keys) => keys.map(k => ({ term: TERM[k] ? TERM[k](S) : S.m[k], text: S.def[k] }));
  // every term a History chart can define (cards pick the ones that apply to the current view);
  // the small multiples define their own metric (OTHER). tests/web/test_ui.py checks def.* exists.
  const INFO_TERMS = {
    composition: ["skeletal_muscle_mass_kg", "other_lean", "body_fat_mass_kg", "weight_kg", "pct_of_weight"],
    parts: ["seg_lean", "seg_fat", "seg_pct", "pct_of_weight"]
  };
  function infoButton(S, items, notes) {
    const d = el("details", { class: "ibc-info" });
    const sum = el("summary", { class: "ibc-info-btn", "aria-label": S.chart.info, title: S.chart.info });
    const ic = sv("svg", { width: 18, height: 18, viewBox: "0 0 18 18", "aria-hidden": "true" });
    ic.append(sv("circle", { cx: 9, cy: 9, r: 7.5, class: "ibc-info-ring" }), sv("path", { d: "M9 8v4.6M9 5.4v.2", class: "ibc-info-i" }));
    sum.append(ic);
    const box = el("div", { class: "ibc-info-box", role: "note" });
    items.forEach(it => { const p = el("p", { class: "ibc-def" }); p.append(el("b", null, it.term), document.createTextNode(": " + it.text)); box.append(p); });
    notes.filter(Boolean).forEach(t => box.append(el("p", { class: "ibc-info-note" }, t)));
    d.append(sum, box);
    return d;
  }

  /** A chart card: header (title + ⓘ) + controls + plot(s) + table. `build(card, plot, refill)`
   *  fills it and returns { draws: [[plotElement, drawFn], ...] }; plot(i) hands out persistent
   *  plot elements (keyboard + sync wiring survives control changes). */
  function card(ctx, cls, build) {
    const c = el("section", { class: "ibc-card " + cls });
    const plots = [];
    const plot = i => {
      i = i || 0;
      if (!plots[i]) {
        const p = el("div", { class: "ibc-plot", tabindex: "0", role: "group", "aria-roledescription": ctx.S.chart.chartRole });
        keyboardOnce(p, ctx, () => ctx.rows);
        ctx.bus.on((d, src) => { if (src === p || !p._hv || !p.isConnected) return; if (d) p._hv.sync(d); else p._hv.clear(); });
        plots[i] = p;
      }
      return plots[i];
    };
    let ros = [];
    function fill() {
      ros.forEach(r => r.disconnect());
      ros = [];
      c.replaceChildren();
      plots.forEach(p => { p._hv = null; });
      const cur = build(c, plot, fill);
      cur.draws.forEach(([p, draw]) => {
        let lastW = -1;
        // draw synchronously (no blank first frame); redraw only when the width changed
        const ro = new ResizeObserver(() => {
          const w = p.clientWidth;
          if (w !== lastW && p.isConnected) { lastW = w; p._hv = draw(p); }
        });
        ro.observe(p);
        ros.push(ro);
        ctx.observers.push(ro);
      });
    }
    fill();
    return c;
  }
  function head(c, S, title, info, unit) {   // info: {items, notes}
    const h = el("div", { class: "ibc-head" });
    const h3 = el("h3", null, title);
    if (unit) h3.append(el("span", { class: "ibc-unit" }, unit));
    h.append(h3);
    if (info) h.append(infoButton(S, info.items, info.notes));
    c.append(h);
  }
  /** control row: metric pickers left; unit/mode toggles right (wraps under, still right-aligned).
   *  The ⓘ button lives on the title row (head), right-aligned. */
  function ctlRow() {
    const row = el("div", { class: "ibc-ctls" }), left = el("div", { class: "ibc-ctl-left" }), right = el("div", { class: "ibc-ctl-right" });
    row.append(left, right);
    return [row, left, right];
  }
  function legend(items) {
    const lg = el("div", { class: "ibc-legend" });
    items.forEach(it => {
      const node = it.toggle ? el("button", { type: "button", "aria-pressed": String(!it.hidden) }) : el("span");
      node.append(it.key, document.createTextNode(it.label));
      if (it.toggle) node.addEventListener("click", it.toggle);
      lg.append(node);
    });
    return lg;
  }
  const covered = (rows, f) => rows.filter(r => f(r)).length;

  // ------------------------------------------------------------------ the three history blocks
  function compositionCard(ctx) {
    const { S } = ctx, C = S.chart.comp, st = ctx.st.comp;
    const u = unitLabel("kg", S);
    const W = r => num(r.weight_kg), SMM = r => num(r.skeletal_muscle_mass_kg), FAT = r => num(r.body_fat_mass_kg);
    const OTHER = r => (W(r) != null && SMM(r) != null && FAT(r) != null ? W(r) - SMM(r) - FAT(r) : null);
    const kgPct = (kg, w) => withUnit(fmtNum(kg, 1, S), "kg", S) + " · " + withUnit(fmtNum(kg / w * 100, 1, S), "%", S);
    return card(ctx, "wide", (c, plot, refill) => {
      const rows = ctx.rows, p0 = plot(0);
      const set = patch => { Object.assign(st, patch); ctx.changed(); refill(); };
      const parts = [
        { id: "smm", cls: "s1", label: C.partSmm, get: SMM },
        { id: "other", cls: "s3", label: C.partOther, get: OTHER },
        { id: "fat", cls: "s2", label: C.partFat, get: FAT }
      ];
      const key = { weight: "weight_kg", smm: "skeletal_muscle_mass_kg", fat: "body_fat_mass_kg" }[st.pick];
      const name = key ? S.m[key] : "";
      const asPct = (st.pick === "smm" || st.pick === "fat") && st.unit === "pct";
      const info = st.pick === "all"
        ? { items: defs(S, INFO_TERMS.composition.filter(k => k !== "pct_of_weight" || st.mode === "pct")),
            notes: [st.mode === "pct" ? C.infoRel : C.infoAbs(u), C.infoLines] }
        : st.pick === "weight" ? { items: defs(S, ["weight_kg"]), notes: [C.infoWeight(u)] }
        : { items: defs(S, [key, "pct_of_weight"]), notes: [asPct ? C.infoPct(name) : C.infoKg(name, u)] };
      head(c, S, C.title, info);
      const [ctl, left, right] = ctlRow();
      left.append(segCtl(C.pick, [["all", C.all], ["weight", C.weight], ["smm", C.smm], ["fat", C.fat]], st.pick, v => set({ pick: v })));
      if (st.pick === "all") right.append(segCtl(C.modeLabel, [["kg", C.abs(u)], ["pct", C.rel]], st.mode, v => set({ mode: v })));
      if (st.pick === "smm" || st.pick === "fat") right.append(segCtl(C.unitLabel, [["kg", u], ["pct", C.pctOfWeight]], st.unit, v => set({ unit: v })));
      c.append(ctl);

      let draw, cols;
      if (st.pick === "all") {
        c.append(legend(parts.slice().reverse().map(p => ({ key: rectKey(p.cls), label: p.label }))));
        const full = covered(rows, r => OTHER(r) != null);
        ctx.setNote("comp", full < rows.length ? C.title + ": " + C.partial(full, rows.length) : null);
        draw = p => stackChart(p, { rows, axis: ctx.axis, S, height: 280, mode: st.mode, parts, total: W, bus: ctx.bus, onSelect: ctx.onSelect, announce: ctx.announce });
        const pu = axisUnit(st.mode === "pct" ? "%" : "kg", S);
        cols = parts.slice().reverse().map(p => ({
          head: `${p.label} ${pu}`, dp: 1,
          get: r => (p.get(r) == null ? null : st.mode === "pct" ? p.get(r) / W(r) * 100 : p.get(r))
        })).concat([{ head: `${C.total} ${axisUnit("kg", S)}`, dp: 1, get: W }]);
      } else {
        const cls = { weight: "ink", smm: "s1", fat: "s2" }[st.pick];
        const raw = r => num(r[key]);
        const get = asPct ? r => (raw(r) != null && W(r) ? raw(r) / W(r) * 100 : null) : raw;
        const n = covered(rows, r => raw(r) != null);
        ctx.setNote("comp", n < rows.length ? S.chart.notes.partial(name, n, rows.length) : null);
        const series = [{
          id: key, cls, label: name, short: "", get,
          tip: st.pick === "weight" ? null : r => ({ main: kgPct(raw(r), W(r)), extra: null })
        }];
        draw = p => lineChart(p, {
          rows, axis: ctx.axis, S, height: 280, series, bus: ctx.bus, onSelect: ctx.onSelect, announce: ctx.announce,
          yUnit: axisUnit(asPct ? "%" : "kg", S),
          fmt: v => asPct ? withUnit(fmtNum(v, 1, S), "%", S) : withUnit(fmtNum(v, 1, S), "kg", S)
        });
        cols = [{ head: `${name} ${axisUnit(asPct ? "%" : "kg", S)}`, dp: 1, get }];
      }
      p0.setAttribute("aria-label", C.title + ". " + S.chart.kbdHelp);
      c.append(p0);
      c.append(tableView(S, rows, cols));
      return { draws: [[p0, draw]] };
    });
  }

  const SEG_PICKS = {
    arms: ["right_arm", "left_arm"], legs: ["right_leg", "left_leg"], trunk: ["trunk"],
    right_arm: ["right_arm"], left_arm: ["left_arm"], right_leg: ["right_leg"], left_leg: ["left_leg"]
  };

  /** Lean and fat together: two mini-plots sharing the x-axis (lean on top in the muscle hue,
   *  fat below in the fat hue), each with its own y-scale. No second y-axis. */
  function segmentCard(ctx) {
    const { S } = ctx, C = S.chart.seg, st = ctx.st.seg;
    if (!SEG_PICKS[st.pick]) st.pick = "arms";
    return card(ctx, "wide", (c, plot, refill) => {
      const rows = ctx.rows, u = unitLabel("kg", S);
      const pair = st.pick === "arms" || st.pick === "legs";
      if (st.mode === "balance" && !pair) st.mode = "kg";
      const set = patch => { Object.assign(st, patch); if (st.mode === "balance" && !(st.pick === "arms" || st.pick === "legs")) st.mode = "kg"; ctx.changed(); refill(); };
      const hidden = ctx.hidden.seg;
      const note = st.mode === "balance" ? C.infoBal(u) : st.mode === "pct" ? C.infoPct : st.mode === "pctw" ? C.infoPctw : C.infoKg(u);
      const terms = INFO_TERMS.parts.filter(k => (k !== "seg_pct" || st.mode === "pct") && (k !== "pct_of_weight" || st.mode === "pctw"));
      head(c, S, C.title, { items: defs(S, terms), notes: [note, C.sides] });
      const [ctl, left, right] = ctlRow();
      left.append(segCtl(C.pick, [["arms", C.arms], ["legs", C.legs], ["trunk", C.trunk]], st.pick, v => set({ pick: v })));
      left.append(segCtl(C.single, ["right_arm", "left_arm", "right_leg", "left_leg"].map(s => [s, S.segShort[s], false, S.seg[s]]), st.pick, v => set({ pick: v }), "ibc-ctl-small"));
      right.append(segCtl(C.mode, [["kg", u], ["pctw", C.pctw], ["pct", C.pct], ["balance", C.balance, !pair, pair ? null : C.balanceNeedsPair]], st.mode, v => set({ mode: v })));
      c.append(ctl);

      const segs = SEG_PICKS[st.pick];
      const sides = segs.length > 1;
      if (sides) {
        c.append(legend(segs.map(s => ({
          key: lineKey("ink", s.startsWith("left") ? "left" : "right", true), label: S.seg[s], hidden: hidden.has(s),
          toggle: () => { hidden.has(s) ? hidden.delete(s) : hidden.add(s); refill(); }
        }))));
      }
      const scaleName = st.mode === "balance" ? C.balance : st.mode === "pct" ? C.pct : st.mode === "pctw" ? C.pctw : u;
      const Wt = r => num(r.weight_kg);
      const pw = (r, mm, s) => (kg(r, mm, s) != null && Wt(r) ? kg(r, mm, s) / Wt(r) * 100 : null);   // % of that day's weight
      const pwS = v => withUnit(fmtNum(v, 2, S), "%", S);       // body parts: 2 decimals
      const kgPw = (r, mm, s) => kg(r, mm, s) == null ? "—" : kgS(kg(r, mm, s), mm) + (pw(r, mm, s) == null ? "" : " · " + pwS(pw(r, mm, s)));
      const kg = (r, mm, s) => num(r[`seg_${mm}_${s}_kg`]), pc = (r, mm, s) => num(r[`seg_${mm}_${s}_pct`]);
      const kgS = v => withUnit(fmtNum(v, 2, S), "kg", S);
      const pcS = v => withUnit(fmtNum(v, 0, S), "%", S);  // % of ideal: whole numbers, as printed
      const cov = {};
      const draws = [];
      const colsAll = [];
      [["lean", "s1", C.lean], ["fat", "s2", C.fat]].forEach(([mm, cls, mName], idx) => {
        let series, extra;
        if (st.mode === "balance") {
          const [R, L] = segs;
          const get = r => (kg(r, mm, R) != null && kg(r, mm, L) != null ? kg(r, mm, R) - kg(r, mm, L) : null);
          series = [{
            id: "balance", cls, label: C.balanceSeries, short: "", get,
            tip: r => ({ main: withUnit(fmtNum(get(r), 2, S, true), "kg", S), extra: `${S.segShort[R]} ${kgS(kg(r, mm, R), mm)} · ${S.segShort[L]} ${kgS(kg(r, mm, L), mm)}` })
          }];
          extra = { symmetric: true, refs: [{ v: 0 }], zones: { top: C.rightMore, bottom: C.leftMore }, fmt: v => withUnit(fmtNum(v, 2, S, true), "kg", S), yUnit: axisUnit("kg", S) };
        } else {
          const mode = st.mode;
          series = segs.map(s => ({
            id: s, cls, label: S.seg[s], short: S.segShort[s],
            marker: s === "trunk" ? null : s.startsWith("left") ? "left" : "right",
            get: r => (mode === "pct" ? pc(r, mm, s) : mode === "pctw" ? pw(r, mm, s) : kg(r, mm, s)),
            // tooltip: always kg · % of weight (as in the composition chart); % of ideal when printed
            tip: r => {
              const b = pc(r, mm, s), ideal = b == null ? null : pcS(b) + " " + S.report.ofIdeal;
              if (mode === "pct") return { main: b == null ? "—" : pcS(b) + " " + S.report.ofIdeal, extra: kg(r, mm, s) == null ? null : kgPw(r, mm, s) };
              if (mode === "pctw") return { main: pw(r, mm, s) == null ? "—" : pwS(pw(r, mm, s)) + " · " + kgS(kg(r, mm, s), mm), extra: ideal };
              return { main: kgPw(r, mm, s), extra: ideal };
            }
          }));
          extra = mode === "pct" ? { refs: [{ v: 100, label: C.ideal }], fmt: v => pcS(v), yUnit: axisUnit("%", S) }
                : mode === "pctw" ? { fmt: v => pwS(v), yUnit: axisUnit("%", S) }
                : { fmt: v => kgS(v, mm), yUnit: axisUnit("kg", S) };
        }
        cov[mm] = covered(rows, r => series.some(s => s.get(r) != null));
        const sub = el("div", { class: "ibc-mini" });
        const lbl = el("div", { class: "ibc-mini-h" });
        lbl.append(rectKey(cls), document.createTextNode(mName));
        sub.append(lbl);
        const p = plot(idx);
        p.setAttribute("aria-label", C.title + ", " + mName + ". " + S.chart.kbdHelp);
        sub.append(p);
        c.append(sub);
        draws.push([p, pp => lineChart(pp, Object.assign({
          rows, axis: ctx.axis, S, height: 190, series, hidden, bus: ctx.bus, onSelect: ctx.onSelect, announce: ctx.announce
        }, extra))]);
        series.filter(s => !hidden.has(s.id)).forEach(s => colsAll.push({
          head: `${mName} – ${s.label.charAt(0).toLocaleLowerCase(S.locale) + s.label.slice(1)} ${extra.yUnit}`, get: s.get,
          dp: 2, signed: st.mode === "balance"
        }));
      });
      const n = rows.length;
      if (cov.lean < n || cov.fat < n) {
        const why = st.mode === "pct" ? " " + C.whyPct : (cov.fat < n ? " " + C.whyFat : "");
        ctx.setNote("seg", `${C.title} (${scaleName}): ${C.lean} ${S.chart.coverage(cov.lean, n)}, ${C.fat} ${S.chart.coverage(cov.fat, n)}.${why}`);
      } else ctx.setNote("seg", null);
      ctx.setNote("segEnc", st.mode !== "balance" && segs[0] !== "trunk" ? C.title + ": " + C.sides : null);
      c.append(tableView(S, rows, colsAll));
      return { draws };
    });
  }

  const OTHER = ["bmi", "inbody_score", "visceral_fat_level", "bmr_kcal", "total_body_water_l", "protein_kg"];
  const SHEET_ONLY = new Set(["visceral_fat_level", "protein_kg", "minerals_kg", "waist_hip_ratio"]);

  function metricCard(ctx, key) {
    const { S } = ctx, md = METRICS[key];
    return card(ctx, "small", (c, plot) => {
      const rows = ctx.rows, get = r => num(r[key]), p0 = plot(0);
      head(c, S, S.m[key], { items: defs(S, [key]), notes: [S.chart.other.note] });
      p0.setAttribute("aria-label", S.m[key] + ". " + S.chart.kbdHelp);
      c.append(p0);
      c.append(tableView(S, rows, [{ head: `${S.m[key]} ${axisUnit(md.unit, S)}`, dp: md.dp, get }]));
      return {
        draws: [[p0, p => lineChart(p, {
          rows, axis: ctx.axis, S, height: 170, compact: true, bus: ctx.bus, onSelect: ctx.onSelect, announce: ctx.announce,
          series: [{ id: key, cls: "s1", label: S.m[key], short: "", get }],
          yUnit: axisUnit(md.unit, S),
          fmt: v => fmtVal(key, v, S)
        })]]
      };
    });
  }

  // ------------------------------------------------------------------ mount
  // ---- custom range: months as "YYYY-MM" strings, month index = year * 12 + month0
  const YM_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
  const ymIdx = ym => +ym.slice(0, 4) * 12 + (+ym.slice(5, 7) - 1);
  const idxYm = i => String(Math.floor(i / 12)).padStart(4, "0") + "-" + String(i % 12 + 1).padStart(2, "0");
  const RANGES = ["6m", "1y", "all", "custom"];
  /** first and last month index of the data */
  const dataSpan = all => [ymIdx(all[0].measured_date), ymIdx(all[all.length - 1].measured_date)];
  /** custom range -> valid, clamped to the data span; malformed or entirely outside the data -> "all" */
  function normCustom(st, all) {
    if (st.range !== "custom") return;
    if (!all.length || !YM_RE.test(st.from || "") || !YM_RE.test(st.to || "")) { st.range = "all"; return; }
    const [lo, hi] = dataSpan(all), a = ymIdx(st.from), b = ymIdx(st.to);
    if (a > b || b < lo || a > hi) { st.range = "all"; return; }
    st.from = idxYm(Math.max(a, lo));
    st.to = idxYm(Math.min(b, hi));
  }
  /** what "Custom" starts with: the whole data span, or its last 12 months when it is longer */
  function defaultCustom(st, all) {
    const [lo, hi] = dataSpan(all);
    st.from = idxYm(hi - lo > 11 ? hi - 11 : lo);
    st.to = idxYm(hi);
  }

  function filterRange(all, st) {
    const range = st.range;
    if (range === "custom") {
      const a = st.from + "-01", b = st.to + "-31";   // whole To month: "YYYY-MM-DD" compares as a string
      return all.filter(r => r.measured_date >= a && r.measured_date <= b);
    }
    if (range === "all" || !all.length) return all;
    const last = new Date(all[all.length - 1]._t);
    last.setUTCMonth(last.getUTCMonth() - (range === "6m" ? 6 : 12));
    return all.filter(r => r._t >= last.getTime());
  }

  /** opts: { measurements: canonical rows in SI [{id, measured_date, measured_time, origin, gym, ...}],
   *          lang: 'cs'|'en' (or strings: a dictionary), units: {mass:'metric'|'imperial', energy:'kcal'|'kJ'},
   *          pauseMonths: initial pause length (default 3),
   *          state: {range, breaks: 'auto'|'none', pauseMonths, comp, seg}, onSelect(row), onChange(state) }
   *          The page's "Shrink pauses over [N] mo" control (checkbox default on, months default 3)
   *          is part of `state`, persisted by the caller (per browser). */
  /** pause length in months: an integer 1..24, anything else falls back to 3 */
  function validMonths(v) { const n = Math.round(+v); return n >= 1 && n <= 24 ? n : 3; }

  function mountHistory(root, opts) {
    opts = opts || {};
    const st = {
      range: "all", from: null, to: null, breaks: "auto", pauseMonths: validMonths(opts.pauseMonths),
      comp: { pick: "all", mode: "kg", unit: "kg" },
      seg: { pick: "arms", mode: "kg" }
    };
    const merge = s => { if (!s) return; /* range / from / to are deliberately NOT restored: every visit starts at "All" */ if (s.breaks) st.breaks = s.breaks === "none" ? "none" : "auto"; if ("pauseMonths" in s) st.pauseMonths = validMonths(s.pauseMonths); if (s.comp) Object.assign(st.comp, s.comp); if (s.seg) Object.assign(st.seg, s.seg); };
    merge(opts.state);
    let base = opts.strings || g.InbodyI18n[opts.lang || "en"], units = opts.units || {};
    let S = withUnits(base, units), src = opts.measurements || [], all = [];
    const setRows = () => {
      all = src.filter(r => r && r.measured_date).map(r => Object.assign(toDisplay(r, S.U), { _t: tOf(r.measured_date) }))
        .sort((a, b) => a._t - b._t || String(a.measured_time).localeCompare(String(b.measured_time)));
    };
    setRows();
    const hidden = { seg: new Set() };
    let observers = [];
    // custom month-range picker (own widget: <input type="month"> is missing in desktop Safari)
    let pickerOpen = false, pickerEl = null, customBtn = null, filterEl = null;
    const pv = { from: 0, to: 0 };   // the year each month grid shows
    const monthName = m => S.fmt.monthYear(2000, m).replace("2000", "").trim();
    const monthLong = i => new Intl.DateTimeFormat(S.locale, { month: "long", year: "numeric", timeZone: "UTC" }).format(Date.UTC(Math.floor(i / 12), i % 12, 1));
    const fmtYM = ym => S.fmt.monthYear(+ym.slice(0, 4), +ym.slice(5, 7));
    const focusPk = key => {
      let b = root.querySelector('[data-pk="' + key + '"]');
      if (b && b.disabled) b = root.querySelector('[data-pk="' + (key.indexOf("prev:") === 0 ? "next:" : "prev:") + key.slice(5) + '"]');   // a year arrow that just ran out: its twin
      if (b && !b.disabled) b.focus();
    };
    function closePicker(refocus) {
      if (!pickerOpen) return;
      pickerOpen = false;
      if (pickerEl) pickerEl.remove();
      pickerEl = null;
      if (customBtn) { customBtn.setAttribute("aria-expanded", "false"); if (refocus) customBtn.focus(); }
    }
    function openPicker() {
      pickerOpen = true;
      pv.from = Math.floor(ymIdx(st.from) / 12);
      pv.to = Math.floor(ymIdx(st.to) / 12);
      pickerEl = buildPicker();
      filterEl.append(pickerEl);
      customBtn.setAttribute("aria-expanded", "true");
      focusPk("from:" + st.from);
    }
    function rebuildPicker(key) {
      const fresh = buildPicker();
      pickerEl.replaceWith(fresh);
      pickerEl = fresh;
      focusPk(key);
    }
    function buildPicker() {
      const [lo, hi] = dataSpan(all), y0 = Math.floor(lo / 12), y1 = Math.floor(hi / 12);
      const fromI = ymIdx(st.from), toI = ymIdx(st.to);
      const box = el("div", { class: "ibc-picker", id: "ibc-picker", role: "dialog", "aria-label": S.chart.customTitle });
      const cols = el("div", { class: "ibc-picker-cols" });
      const pick = (which, i) => {
        const oldFrom = fromI, oldTo = toI;
        if (which === "from") { st.from = idxYm(i); if (i > oldTo) st.to = st.from; }
        else { st.to = idxYm(i); if (i < oldFrom) st.from = st.to; }
        pv[which] = Math.floor(i / 12);
        if (which === "from" && i > oldTo) pv.to = pv.from;
        if (which === "to" && i < oldFrom) pv.from = pv.to;
        ctx2.changed();
        ctx2.rerender();
        focusPk(which + ":" + st[which]);
      };
      ["from", "to"].forEach(which => {
        const vy = pv[which], sel = which === "from" ? fromI : toI;
        const grp = el("div", { class: "ibc-pk", role: "group", "aria-label": which === "from" ? S.chart.customFrom : S.chart.customTo });
        grp.append(el("div", { class: "ibc-pk-label" }, which === "from" ? S.chart.customFrom : S.chart.customTo));
        const head = el("div", { class: "ibc-pk-head" });
        const yearBtn = (dir, label, glyph) => {
          const b = el("button", { type: "button", class: "ibc-pk-nav", "aria-label": label, "data-pk": (dir < 0 ? "prev:" : "next:") + which }, glyph);
          if (vy + dir < y0 || vy + dir > y1) b.disabled = true;
          b.addEventListener("click", () => { pv[which] = vy + dir; rebuildPicker((dir < 0 ? "prev:" : "next:") + which); });
          return b;
        };
        head.append(yearBtn(-1, S.chart.customPrevYear, "‹"), el("span", { class: "ibc-pk-year" }, String(vy)), yearBtn(1, S.chart.customNextYear, "›"));
        grp.append(head);
        const grid = el("div", { class: "ibc-pk-grid" });
        let tab = sel >= vy * 12 && sel < vy * 12 + 12 ? sel : -1;
        if (tab < 0) for (let m = 0; m < 12; m++) if (vy * 12 + m >= lo && vy * 12 + m <= hi) { tab = vy * 12 + m; break; }
        for (let m = 0; m < 12; m++) {
          const i = vy * 12 + m, off = i < lo || i > hi;
          const b = el("button", {
            type: "button", class: "ibc-pk-m" + (i > fromI && i < toI ? " in" : ""), "aria-pressed": String(i === sel),
            "aria-label": monthLong(i), "data-pk": which + ":" + idxYm(i), tabindex: i === tab ? "0" : "-1"
          }, monthName(m + 1));
          if (off) b.setAttribute("aria-disabled", "true");
          b.addEventListener("click", () => { if (!off) pick(which, i); });
          grid.append(b);
        }
        grid.addEventListener("keydown", e => {
          const cur = ymIdx((document.activeElement.getAttribute("data-pk") || ":" + st[which]).split(":")[1]);
          const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -3, ArrowDown: 3, PageUp: -12, PageDown: 12 }[e.key];
          if (!step) return;
          e.preventDefault();
          const j = cur + step;
          if (j < lo || j > hi) return;
          pv[which] = Math.floor(j / 12);
          rebuildPicker(which + ":" + idxYm(j));
        });
        grp.append(grid);
        cols.append(grp);
      });
      const foot = el("div", { class: "ibc-picker-foot" });
      const done = el("button", { type: "button", class: "ibc-picker-done" }, S.chart.customDone);
      done.addEventListener("click", () => closePicker(true));
      foot.append(done);
      box.append(cols, foot);
      return box;
    }
    const ctx2 = { changed: () => { if (opts.onChange) opts.onChange(JSON.parse(JSON.stringify(st))); }, rerender: () => render() };
    const onKey = e => { if (pickerOpen && e.key === "Escape") { e.stopPropagation(); closePicker(true); } };
    const onDown = e => {
      if (!pickerOpen || (pickerEl && pickerEl.contains(e.target)) || (customBtn && customBtn.contains(e.target))) return;
      closePicker(false);   // an outside click: focus goes where the user clicked
    };
    if (typeof document !== "undefined") { document.addEventListener("keydown", onKey, true); document.addEventListener("pointerdown", onDown); }
    const bus = makeBus();
    const live = el("div", { class: "ibc-sr", "aria-live": "polite" });

    function render() {
      observers.forEach(o => o.disconnect());
      observers = [];
      bus.clear();
      root.classList.add("ibc");
      root.replaceChildren();
      // n = 0: empty state (+ Upload for the owner); n = 1: no charts, just one centred sentence
      const panel = (text, actions) => {
        const box = el("div", { class: "ibc-empty big ibc-panel" });
        box.append(el("p", null, text));
        const act = el("div", { class: "ibc-panel-actions" });
        actions.filter(Boolean).forEach(a => act.append(a));
        if (act.children.length) box.append(act);
        return box;
      };
      const btn = (cls, text, fn) => { const b = el("button", { type: "button", class: cls }, text); b.addEventListener("click", fn); return b; };
      const uploadBtn = () => {
        if (!opts.onUpload) return null;
        // same icon + label markup as the app bar's Upload button (templates/_ui.html icon("upload"))
        const b = el("button", { type: "button", class: "btn-primary" });
        const ic = sv("svg", { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor",
          "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
        ic.append(sv("path", { d: "M8 10.5V2.5M4.75 5.5 8 2.25l3.25 3.25M2.5 10v2.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V10" }));
        b.append(ic, el("span", null, S.shell.upload));
        b.addEventListener("click", opts.onUpload);
        return b;
      };
      if (!all.length) { root.append(panel(opts.onUpload ? (opts.hasFriends ? S.chart.noDataOwnFriends : S.chart.noDataOwn) : S.chart.noData, [uploadBtn()])); return; }
      if (all.length < 2) {
        root.append(panel(S.chart.needTwo, []));
        return;
      }
      normCustom(st, all);
      if (st.range !== "custom") closePicker(false);
      const rows = filterRange(all, st);
      const axis = buildAxis(rows.map(r => r.measured_date), { breaks: st.breaks, pauseMonths: st.pauseMonths });
      // Notes: every coverage / encoding explanation lives here, so all chart cards share one structure
      const notes = new Map([["comp", null], ["seg", null], ["segEnc", null], ["other", null], ["pause", null], ["lines", null], ["dash", null]]);
      const notesList = el("ul");
      const renderNotes = () => notesList.replaceChildren(...[...notes.values()].flat().filter(Boolean).map(t => el("li", null, t)));
      const ctx = {
        rows, axis, S, bus, st, hidden, observers,
        setNote: (id, text) => { notes.set(id, text); renderNotes(); },
        onSelect: opts.onSelect,
        changed: () => { if (opts.onChange) opts.onChange(JSON.parse(JSON.stringify(st))); },
        announce: t => { live.textContent = t; }
      };

      // filter row: one row, above everything it scopes
      // Range + summary right next to it on the left (wraps under on phones), Shrink pauses right-aligned
      const f = el("div", { class: "ibc-filters" });
      const rangeRow = el("div", { class: "ibc-rangerow" });
      rangeRow.append(el("span", { class: "ibc-flabel" }, S.chart.range));
      const rangeCtl = segCtl(S.chart.range, [["6m", S.chart.r6m], ["1y", S.chart.r1y], ["all", S.chart.rall], ["custom", S.chart.rcustom]], st.range, v => {
        if (v === "custom" && st.range === "custom") { if (pickerOpen) closePicker(true); else openPicker(); return; }   // a second click toggles the picker
        if (v === "custom") {
          st.range = "custom";
          if (!YM_RE.test(st.from || "") || !YM_RE.test(st.to || "")) defaultCustom(st, all);
          normCustom(st, all);
          if (st.range !== "custom") { st.range = "custom"; defaultCustom(st, all); }   // the stored months are outside today's data
          pickerOpen = true;
          pv.from = Math.floor(ymIdx(st.from) / 12);
          pv.to = Math.floor(ymIdx(st.to) / 12);
        } else { closePicker(false); st.range = v; }
        ctx.changed();
        render();
        if (v === "custom") focusPk("from:" + st.from);
      });
      customBtn = rangeCtl.lastElementChild;
      customBtn.setAttribute("aria-haspopup", "dialog");
      customBtn.setAttribute("aria-controls", "ibc-picker");
      customBtn.setAttribute("aria-expanded", String(pickerOpen));
      rangeRow.append(rangeCtl);
      if (st.range === "custom") rangeRow.append(el("span", { class: "ibc-customrange" }, st.from === st.to ? fmtYM(st.from) : fmtYM(st.from) + " – " + fmtYM(st.to)));
      const d0 = rows.length ? rows[0].measured_date : null, d1 = rows.length ? rows[rows.length - 1].measured_date : null;   // a custom range can hold none
      rangeRow.append(el("span", { class: "ibc-summary" },
        S.fmt.count(rows.length) + (d0 ? " · " + (d0 === d1 ? S.fmt.date(d0) : S.fmt.date(d0) + " – " + S.fmt.date(d1)) : "")));
      f.append(rangeRow);
      filterEl = f;
      // one compact control: [x] Shrink pauses over [3] mo -- the checkbox (default on) and the
      // months (default 3, 1..24) apply live and persist per browser in the chart state; the
      // number is disabled (greyed) while the checkbox is off
      const lab = el("label", { class: "ibc-pausefield", title: S.chart.pauseHint });
      const cb = el("input", { type: "checkbox", id: "ibc-shrink-pauses" });
      cb.checked = st.breaks === "auto";
      const inp = el("input", { type: "number", id: "ibc-pause-months", min: "1", max: "24", step: "1", inputmode: "numeric", value: String(st.pauseMonths) });
      inp.disabled = !cb.checked;
      const refocus = id => { const e = document.getElementById(id); if (e) e.focus(); };
      cb.addEventListener("change", () => { st.breaks = cb.checked ? "auto" : "none"; ctx.changed(); render(); refocus("ibc-shrink-pauses"); });
      inp.addEventListener("change", () => {
        const n = Math.round(+inp.value);
        if (!(n >= 1 && n <= 24)) { inp.value = String(st.pauseMonths); return; }
        if (n === st.pauseMonths) return;
        st.pauseMonths = n;
        ctx.changed();
        render();                                   // live, no page reload
        refocus("ibc-pause-months");
      });
      lab.append(cb, el("span", null, S.chart.pauseOver), inp, el("span", null, S.chart.pauseMo));
      f.append(lab);
      root.append(f);
      if (pickerOpen) {   // re-rendered while open (a month was picked): the picker is rebuilt in place
        pickerEl = buildPicker();
        f.append(pickerEl);
      }
      if (rows.length < 2) {   // the range holds < 2 measurements while the whole history has >= 2
        root.append(panel(S.chart.fewInRange, [btn("btn-primary", S.chart.showAll, () => { closePicker(false); st.range = "all"; ctx.changed(); render(); })]));
        root.append(live);
        return;
      }

      root.append(compositionCard(ctx));
      root.append(segmentCard(ctx));
      const grid = el("div", { class: "ibc-grid-small" });
      OTHER.forEach(k => grid.append(metricCard(ctx, k)));
      root.append(grid);

      // small multiples: group metrics by the reason and the count they share
      const groups = new Map();
      OTHER.forEach(k => {
        const c = covered(rows, r => num(r[k]) != null);
        if (c === rows.length) return;
        const gk = (SHEET_ONLY.has(k) ? "sheet" : "part") + c;
        if (!groups.has(gk)) groups.set(gk, { sheet: SHEET_ONLY.has(k), c, names: [] });
        groups.get(gk).names.push(S.m[k]);
      });
      ctx.setNote("other", [...groups.values()].map(gr => gr.sheet
        ? S.chart.notes.sheetOnly(gr.names.join(", "), gr.names.length, gr.c, rows.length)
        : S.chart.notes.partial(gr.names.join(", "), gr.c, rows.length)));
      // with shrinking off there are no pauses: no pause note, no dashed lines
      ctx.setNote("pause", st.breaks === "none" ? null : S.chart.pauseTitle(st.pauseMonths));
      ctx.setNote("lines", st.breaks === "none" ? S.chart.notes.linesPlain : S.chart.notes.lines(st.pauseMonths));
      ctx.setNote("dash", S.chart.notes.dash);
      const notesSec = el("section", { class: "ibc-notes" });
      notesSec.append(el("h2", { class: "ibc-section" }, S.chart.notes.title), notesList);
      root.append(notesSec);
      root.append(live);
    }
    render();
    return {
      /** o: { lang | strings, units, pauseMonths, measurements, state } */
      update(o) {
        if (!o) return;
        if (o.strings) base = o.strings; else if (o.lang) base = g.InbodyI18n[o.lang];
        if (o.units) units = o.units;
        if (o.pauseMonths) st.pauseMonths = validMonths(o.pauseMonths);
        if (o.measurements) src = o.measurements;
        if ("onUpload" in o) opts.onUpload = o.onUpload;
        if ("hasFriends" in o) opts.hasFriends = !!o.hasFriends;
        S = withUnits(base, units);
        setRows();
        merge(o.state);
        render();
      },
      get state() { return JSON.parse(JSON.stringify(st)); },
      destroy() {
        observers.forEach(o => o.disconnect()); bus.clear(); root.replaceChildren();
        if (typeof document !== "undefined") { document.removeEventListener("keydown", onKey, true); document.removeEventListener("pointerdown", onDown); }
      }
    };
  }

  /** Server-rendered pages: <div data-inbody-history data-source="history-json" data-lang="cs"
   *  data-mass="metric" data-energy="kcal" data-pause-months="3" data-report-url="/report/{id}"></div>
   *  + <script type="application/json" id="history-json">. Runs on DOMContentLoaded and after every htmx swap. */
  function autoMount(scope) {
    (scope || document).querySelectorAll("[data-inbody-history]:not([data-mounted])").forEach(node => {
      node.setAttribute("data-mounted", "");
      const src = document.getElementById(node.getAttribute("data-source"));
      const data = src ? JSON.parse(src.textContent) : { measurements: [] };
      const key = "inbody-history-state";
      let state = null;
      try { state = JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { state = null; }
      const tpl = node.getAttribute("data-report-url");
      mountHistory(node, {
        measurements: data.measurements, state,
        lang: node.getAttribute("data-lang") || document.documentElement.lang || "en",
        units: { mass: node.getAttribute("data-mass") || "metric", energy: node.getAttribute("data-energy") || "kcal" },
        pauseMonths: +(node.getAttribute("data-pause-months") || 3),
        onChange: s => { try { localStorage.setItem(key, JSON.stringify(s)); } catch (e) { /* storage blocked */ } },
        onSelect: row => { if (tpl) location.href = tpl.replace("{id}", encodeURIComponent(row.id)); }
      });
    });
  }
  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", () => autoMount());
    document.addEventListener("htmx:afterSettle", e => autoMount(e.target));
  }

  g.InbodyCharts = { mountHistory, autoMount, buildAxis, lineChart, stackChart, METRICS, SEGS, fmtNum, fmtVal, withUnit, withUnits, unitLabel, toDisplay, compare, niceTicks, stepDp };
})(typeof window !== "undefined" ? window : globalThis);
