// Upload progress bar (docs/upload-pipeline.md section 5). Vanilla JS, CSP-safe (no eval, no
// inline code). The server state lives in #progress-state (data-* attributes), replaced by the
// HTMX poll every 1 s; between polls this script animates the bar locally.
//   phases: 0 storing raw data, 1 parsing data, 2 saving data
//   - every phase stays visible >= 1 s (a client-side queue), so the bar takes >= 3 s;
//   - inside phase 2/3 the fill follows a time curve towards 95 % of the estimate and only
//     reaches 100 % when the server says the phase is over;
//   - failed: red at the failed phase; skipped_budget: amber in phase 2; duplicate_file: ends
//     after phase 1; prefers-reduced-motion: no animation, 1 s steps.
(function () {
  var bar = document.getElementById("pbar");
  if (!bar) return;
  var text = document.getElementById("pbar-text");
  var segs = bar.querySelectorAll(".seg");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var PHASE_INDEX = { store: 0, parse: 1, save: 2, end: 3 };
  var MIN_SHOWN_MS = 1000;

  var state = null, received = 0, shown = 0, shownSince = performance.now();

  function t(key, n) {
    var s = bar.getAttribute("data-t-" + key) || "";
    return n === undefined ? s : s.replace("{n}", String(n));
  }

  function readState() {
    var el = document.getElementById("progress-state");
    if (!el) return;
    state = {
      status: el.getAttribute("data-status"),
      phase: PHASE_INDEX[el.getAttribute("data-phase")] || 1,
      elapsed: parseFloat(el.getAttribute("data-elapsed")) || 0,
      est: [1, parseFloat(el.getAttribute("data-parse-estimate")) || 30,
            parseFloat(el.getAttribute("data-save-estimate")) || 1],
      final: el.getAttribute("data-final") === "1"
    };
    received = performance.now();
  }

  // Where the server is: phases below `target` are finished.
  function target() {
    if (!state) return 1;
    var s = state.status;
    if (s === "done" || s === "partial") return 3;
    if (s === "duplicate_file") return 1;
    return state.phase; // in progress, or failed / skipped_budget at this phase
  }

  function curve(elapsed, est) {
    return 0.95 * (1 - Math.exp(-3 * elapsed / Math.max(est, 0.5)));
  }

  function tick() {
    if (!state) readState();
    var now = performance.now();
    var tgt = target();
    var stopped = state && state.final;
    // Queue: advance one phase at a time, each shown for >= MIN_SHOWN_MS.
    if (shown < tgt && now - shownSince >= MIN_SHOWN_MS) {
      shown += 1;
      shownSince = now;
    }
    var frac = 0, label = "";
    for (var i = 0; i < segs.length; i++) {
      var fill = segs[i].firstElementChild, f = 0;
      segs[i].classList.remove("failed", "amber", "skip", "active");
      if (i < shown) f = 1;
      else if (i === shown && i < tgt) f = 1; // finished on the server, still in its 1 s slot
      else if (i === shown && !stopped) {
        var local = state ? state.elapsed + (now - received) / 1000 : 0;
        f = i === 0 ? 1 : curve(local, state.est[i]);
        var left = Math.max(0, Math.round(state.est[i] - local));
        label = t(["store", "parse", "save"][i]) + " - " +
          (local > state.est[i] ? t("slow") : t("left", left));
        segs[i].classList.add("active");
      }
      if (stopped && i === shown && i === tgt) {
        if (state.status === "failed") { segs[i].classList.add("failed"); f = 1; label = t("failed"); }
        if (state.status === "skipped_budget") { segs[i].classList.add("amber"); f = 1; label = t("budget"); }
      }
      if (state && state.status === "duplicate_file" && i > 0) segs[i].classList.add("skip");
      fill.style.width = Math.round(f * 100) + "%";
      if (i === shown) frac = f;
    }
    if (stopped && shown >= tgt) {
      if (state.status === "done" || state.status === "partial") label = t("done");
      if (state.status === "duplicate_file") label = t("duplicate");
    }
    var overall = Math.min(100, Math.round((Math.min(shown, 3) + (shown < 3 ? frac : 0)) / 3 * 100));
    bar.setAttribute("aria-valuenow", String(overall));
    if (label) bar.setAttribute("aria-valuetext", label);
    if (text && label && text.textContent !== label) text.textContent = label;
  }

  document.body.addEventListener("htmx:afterSwap", readState);
  readState();
  tick();
  setInterval(tick, reduced ? 1000 : 100);
})();
