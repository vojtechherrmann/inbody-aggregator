/* Page shell behaviour (CSP-safe, no inline handlers). Everything works without it; this only
   makes it quicker:
   * the Viewing selector and the Settings form submit on change;
   * app-bar dropdowns (<details>) close on Esc or an outside click;
   * an in-page confirmation (.confirm) cancels on Esc;
   * the "not optimised for mobile" strip can be dismissed (remembered in localStorage);
   * range-bar tick labels that would overlap are hidden. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  // the "not optimised for mobile" strip (CSS shows it only at <= 760 px): once dismissed it stays hidden
  const NOTICE_KEY = "inbody-mobile-notice-dismissed";
  try { if (localStorage.getItem(NOTICE_KEY)) document.documentElement.classList.add("notice-off"); } catch (e) { /* storage blocked */ }
  document.addEventListener("click", ev => {
    if (!(ev.target.closest && ev.target.closest("[data-dismiss-notice]"))) return;
    document.documentElement.classList.add("notice-off");
    try { localStorage.setItem(NOTICE_KEY, "1"); } catch (e) { /* storage blocked */ }
  });

  document.addEventListener("change", ev => {
    const t = ev.target;
    if (t && t.matches && t.matches("[data-autosubmit], [data-autosubmit] *") && t.form) {
      if (t.type === "number" && !(Number(t.value) >= 1)) return;
      t.form.requestSubmit ? t.form.requestSubmit() : t.form.submit();
    }
  });

  const POPS = "details.menu, details.ibc-info";   // app-bar dropdowns + the ⓘ boxes
  function closeMenus(except) {
    document.querySelectorAll("details.menu[open], details.ibc-info[open]").forEach(d => { if (d !== except) d.removeAttribute("open"); });
  }
  document.addEventListener("pointerdown", ev => {
    const inside = ev.target.closest && ev.target.closest(POPS);
    closeMenus(inside);
  });
  document.addEventListener("toggle", ev => {
    if (ev.target.matches && ev.target.matches(POPS) && ev.target.open) closeMenus(ev.target);
  }, true);
  document.addEventListener("keydown", ev => {
    if (ev.key !== "Escape") return;
    const open = document.querySelector("details.menu[open], details.ibc-info[open]");
    if (open) { open.removeAttribute("open"); const s = open.querySelector("summary"); if (s) s.focus(); return; }
    const cancel = document.querySelector(".confirm [data-cancel]");
    if (cancel) { ev.preventDefault(); window.location.assign(cancel.getAttribute("href")); }
  });

  function fitTicks() {
    document.querySelectorAll("svg.rb-svg").forEach(svg => {
      const box = svg.getBoundingClientRect();
      let lastRight = -Infinity;
      svg.querySelectorAll("text.rb-tl").forEach(tx => {
        tx.classList.remove("hide");
        const r = tx.getBoundingClientRect();
        const hide = r.left < box.left - 2 || r.right > box.right + 2 || r.left < lastRight + 6;
        tx.classList.toggle("hide", hide);
        if (!hide) lastRight = r.right;
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fitTicks);
  else fitTicks();
  window.addEventListener("resize", fitTicks);
})();
