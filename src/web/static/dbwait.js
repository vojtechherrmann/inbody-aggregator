/* "Starting the database" page: poll /dbstatus (it never touches the database) and go back to the
   page that was asked for as soon as the database is up. No inline code (strict CSP). */
(function () {
  "use strict";
  const box = document.getElementById("dbwait");
  if (!box) return;
  const target = box.getAttribute("data-target") || "/";
  const status = box.getAttribute("data-status") || "/dbstatus";
  let tries = 0;
  function poll() {
    tries += 1;
    fetch(status, { cache: "no-store", credentials: "same-origin" })
      .then(r => r.json())
      .then(s => { if (s && s.warm) window.location.replace(target); else setTimeout(poll, 3000); })
      .catch(() => setTimeout(poll, Math.min(3000 + tries * 500, 10000)));
  }
  setTimeout(poll, 2000);
})();
