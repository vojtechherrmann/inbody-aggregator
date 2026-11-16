/* Applies the chosen theme before first paint (loaded first in <body>, no inline code): dark is the
   DEFAULT; "light" / "dark" set <html data-theme>, "system" removes it (prefers-color-scheme decides).
   Same keys as app.js (localStorage "inbody-proto:settings"). */
(function () {
  var t = "dark";
  try {
    var s = JSON.parse(localStorage.getItem("inbody-proto:settings"));
    if (s && (s.theme === "light" || s.theme === "system")) t = s.theme;
  } catch (e) { /* storage blocked: default */ }
  var root = document.documentElement;
  if (t === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", t);
})();
