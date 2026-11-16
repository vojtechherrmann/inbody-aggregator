// Upload with real progress for phase 1 ("storing raw data"): XHR upload.onprogress drives the
// first segment, then (after >= 1 s on screen) the browser moves to the upload page, where
// progress.js continues with phases 2 and 3. Without JS the form posts normally.
(function () {
  var form = document.getElementById("upload-form");
  var box = document.getElementById("upload-progress");
  var result = document.getElementById("upload-result");
  if (!form || !box || !window.XMLHttpRequest || !window.FormData) return;
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var bar = document.getElementById("pbar");
    var seg = bar.querySelector('.seg[data-seg="0"]');
    var fill = seg.firstElementChild;
    var text = document.getElementById("pbar-text");
    var started = performance.now();
    box.hidden = false;
    result.textContent = "";
    seg.classList.remove("failed");
    seg.classList.add("active");
    text.textContent = bar.getAttribute("data-t-store");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-Upload", "xhr");
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round(e.loaded / e.total * 90); // the last 10 % = blob write on the server
      fill.style.width = pct + "%";
      bar.setAttribute("aria-valuenow", String(Math.round(pct / 3)));
    };
    xhr.onload = function () {
      var location = xhr.getResponseHeader("Location");
      if (xhr.status === 201 && location && location.charAt(0) === "/") {
        fill.style.width = "100%";
        var wait = Math.max(0, 1000 - (performance.now() - started));
        setTimeout(function () { window.location.assign(location); }, wait);
      } else {
        seg.classList.add("failed");
        fill.style.width = "100%";
        result.innerHTML = xhr.responseText; // server-rendered, escaped fragment (_reject.html)
        text.textContent = bar.getAttribute("data-t-failed");
      }
    };
    xhr.onerror = function () {
      seg.classList.add("failed");
      text.textContent = bar.getAttribute("data-t-failed");
    };
    xhr.send(new FormData(form));
  });
})();
