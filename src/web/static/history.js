/* History tab: mounts the chart module on the server-rendered JSON (no API call, CSP-safe).
   #history-data: {lang, units, measurements, reportUrl, uploadUrl}; #i18n-data: flat strings.
   Chart controls (range, "shrink pauses over N months", picks, modes) persist per browser in
   localStorage; the module's defaults are: shrink on, 3 months. */
(function () {
  "use strict";
  const root = document.getElementById("history-root");
  const dataEl = document.getElementById("history-data");
  const i18nEl = document.getElementById("i18n-data");
  if (!root || !dataEl || !i18nEl || !window.InbodyCharts || !window.InbodyStrings) return;
  const data = JSON.parse(dataEl.textContent);
  const strings = window.InbodyStrings.build(JSON.parse(i18nEl.textContent), data.lang);
  const KEY = "inbody-history-state";
  let state = null;
  try { state = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { state = null; }
  window.InbodyCharts.mountHistory(root, {
    measurements: data.measurements,
    strings,
    units: data.units,
    state,
    onChange: s => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* storage blocked */ } },
    onSelect: row => {
      const sep = data.reportUrl.indexOf("?") >= 0 ? "&" : "?";
      window.location.assign(data.reportUrl + sep + "m=" + encodeURIComponent(row.id));
    },
    hasFriends: !!data.hasFriends,
    onUpload: data.uploadUrl ? () => window.location.assign(data.uploadUrl) : null
  });
})();
