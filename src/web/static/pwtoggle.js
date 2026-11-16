/* Password fields, progressive enhancement only (CSP-safe: no inline code or handlers).
   * Every <input type="password"> gets an "eye" button that shows / hides the password: a real
     <button type="button"> (keyboard: Tab + Enter / Space) with aria-label + aria-pressed. The
     label comes from data-show on this <script> tag, rendered by the server in the page language.
     Before a form submits, every field goes back to type=password.
   * A rules list (<ul data-pw-rules-for="<input id>"> with <li data-rule="length|upper|lower|digit">)
     gets a live ✓ per met rule; <p data-pw-match="<id> <id>"> shows whether both fields match.
     Mirrors security.password_policy_errors -- the server stays authoritative.
   Styling hooks: .pw-wrap, .pw-toggle, .pw-mark, li.pw-ok / [data-pw-ok]. */
(function () {
  "use strict";
  const script = document.currentScript;
  const LABEL = (script && script.dataset.show) || "Show password";
  const NS = "http://www.w3.org/2000/svg";

  function svgEl(name, attrs) {
    const el = document.createElementNS(NS, name);
    for (const k of Object.keys(attrs)) el.setAttribute(k, attrs[k]);
    return el;
  }

  function eyeIcon() {
    const svg = svgEl("svg", {
      width: "18", height: "18", viewBox: "0 0 16 16", fill: "none", stroke: "currentColor",
      "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round",
      "aria-hidden": "true", focusable: "false"
    });
    svg.appendChild(svgEl("path", { d: "M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8Z" }));
    svg.appendChild(svgEl("circle", { cx: "8", cy: "8", r: "2" }));
    const slash = svgEl("path", { d: "M2.5 13.5 13.5 2.5", visibility: "hidden" });
    svg.appendChild(slash);
    return { svg, slash };
  }

  function setShown(input, btn, slash, shown) {
    input.type = shown ? "text" : "password";
    btn.setAttribute("aria-pressed", String(shown));
    slash.setAttribute("visibility", shown ? "visible" : "hidden");
  }

  function enhance(input) {
    if (input.dataset.pwToggle) return;
    input.dataset.pwToggle = "1";
    const wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    // element.style (CSSOM) is allowed under style-src 'self'; style="" attributes are not.
    wrap.style.position = "relative";
    wrap.style.display = "block";
    wrap.style.marginTop = getComputedStyle(input).marginTop;
    input.style.marginTop = "0";
    input.style.paddingRight = "40px";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.setAttribute("aria-label", LABEL);
    btn.title = LABEL;
    if (input.id) btn.setAttribute("aria-controls", input.id);
    Object.assign(btn.style, {
      position: "absolute", top: "0", bottom: "0", right: "2px", display: "flex",
      alignItems: "center", padding: "0 8px", border: "0", background: "transparent",
      color: "inherit", cursor: "pointer"
    });
    const { svg, slash } = eyeIcon();
    btn.appendChild(svg);
    setShown(input, btn, slash, false);
    btn.addEventListener("click", () => setShown(input, btn, slash, input.type === "password"));
    wrap.appendChild(btn);
    if (input.form) {
      input.form.addEventListener("submit", () => setShown(input, btn, slash, false));
    }
  }

  // ----------------------------------------------------------------------------- live rule hints
  const RULES = {
    length: (v, min) => Array.from(v).length >= min,  // code points, like Python's len()
    upper: v => /\p{Lu}/u.test(v),
    lower: v => /\p{Ll}/u.test(v),
    digit: v => /[0-9]/.test(v)
  };

  function mark(el, ok) {
    let m = el.querySelector(".pw-mark");
    if (!m) {
      m = document.createElement("span");
      m.className = "pw-mark";
      m.style.display = "inline-block";
      m.style.width = "1.4em";
      el.insertBefore(m, el.firstChild);
    }
    m.textContent = ok ? "✓" : "·";
    m.setAttribute("aria-hidden", ok ? "false" : "true");
    m.style.color = ok ? "var(--good)" : "inherit";
    el.classList.toggle("pw-ok", ok);
    el.dataset.pwOk = String(ok);
  }

  function rulesList(list) {
    const input = document.getElementById(list.dataset.pwRulesFor);
    if (!input) return;
    const min = parseInt(input.getAttribute("minlength"), 10) || 8;
    list.style.listStyle = "none";
    list.style.paddingLeft = "0";
    const items = list.querySelectorAll("li[data-rule]");
    const update = () => items.forEach(li => {
      const test = RULES[li.dataset.rule];
      if (test) mark(li, test(input.value, min));
    });
    input.addEventListener("input", update);
    update();
  }

  function matchHint(p) {
    const [a, b] = p.dataset.pwMatch.split(/\s+/).map(id => document.getElementById(id));
    if (!a || !b) return;
    const update = () => {
      p.hidden = b.value === "";
      mark(p, b.value !== "" && a.value === b.value);
    };
    a.addEventListener("input", update);
    b.addEventListener("input", update);
    update();
  }

  function init() {
    document.querySelectorAll('input[type="password"]').forEach(enhance);
    document.querySelectorAll("[data-pw-rules-for]").forEach(rulesList);
    document.querySelectorAll("[data-pw-match]").forEach(matchHint);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
