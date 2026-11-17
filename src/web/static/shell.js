/* Page shell behaviour (CSP-safe, no inline handlers). Everything works without it; this only
   makes it quicker:
   * the Viewing selector and the Settings form submit on change;
   * app-bar dropdowns (<details>) close on Esc or an outside click;
   * an in-page confirmation (.confirm) cancels on Esc;
   * a link with data-open-dialog="<id>" opens that <dialog> instead (Change password in the account
     menu; the link itself is the no-JavaScript way), [data-close-dialog] closes it, closing wipes it;
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

  document.addEventListener("click", ev => {
    const opener = ev.target.closest && ev.target.closest("[data-open-dialog]");
    if (opener) {
      const dlg = document.getElementById(opener.getAttribute("data-open-dialog"));
      if (!dlg || typeof dlg.showModal !== "function") return;   // old browser: follow the link
      ev.preventDefault();
      closeMenus(null);
      dlg.showModal();
      const first = dlg.querySelector("input:not([type=hidden]):not([readonly])");
      if (first) first.focus();
      return;
    }
    const closer = ev.target.closest && ev.target.closest("[data-close-dialog]");
    if (closer && closer.closest("dialog")) closer.closest("dialog").close();
  });
  // a closed dialog keeps no password: every field is wiped (the live rule marks follow on input)
  document.addEventListener("close", ev => {
    const dlg = ev.target;
    if (!(dlg instanceof HTMLDialogElement)) return;
    dlg.querySelectorAll('input[type="password"], input[type="text"]:not([readonly])').forEach(i => {
      i.value = "";
      i.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }, true);

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
