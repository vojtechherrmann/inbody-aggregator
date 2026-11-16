/* Builds the nested string dictionary the chart module (charts.js) expects from the FLAT i18n keys
   the server embeds in <script type="application/json" id="i18n-data"> (i18n/{cs,en}.json is the
   single source). Key prefixes map onto the dictionary: hc. -> chart., metric. -> m., src. ->
   source., rep. -> report., segshort. -> segShort., seg. / unit. / fmt. / def. stay (def.* = the
   shared term definitions the Report uses too). Values with
   {placeholders} become functions; plurals use Intl.PluralRules (key_one / _few / _many / _other). */
(function (g) {
  "use strict";
  const PREFIX = [["hc.", "chart."], ["metric.", "m."], ["segshort.", "segShort."], ["seg.", "seg."],
    ["unit.", "unit."], ["src.", "source."], ["rep.", "report."], ["def.", "def."]];
  // templated keys -> positional parameter names (as charts.js calls them)
  const PARAMS = {
    "chart.coverage": ["k", "n"], "chart.comp.abs": ["u"], "chart.comp.infoAbs": ["u"],
    "chart.comp.infoWeight": ["u"], "chart.comp.infoKg": ["name", "u"], "chart.comp.infoPct": ["name"],
    "chart.comp.partial": ["k", "n"], "chart.seg.infoKg": ["u"], "chart.seg.infoBal": ["u"],
    "chart.other.info.bmr_kcal": ["u"], "chart.other.info.protein_kg": ["u"],
    "chart.notes.partial": ["names", "k", "n"], "report.vs": ["d"], "report.normal": ["lo", "hi"]
  };
  const fill = (tpl, vals) => String(tpl).replace(/\{(\w+)\}/g, (m, k) => (k in vals ? String(vals[k]) : m));
  function put(obj, path, v) {
    const parts = path.split(".");
    let o = obj;
    for (let i = 0; i < parts.length - 1; i++) o = o[parts[i]] = o[parts[i]] || {};
    o[parts[parts.length - 1]] = v;
  }
  function build(flat, lang) {
    const S = { lang, locale: flat["fmt.locale"], pctSpace: lang === "cs", shell: { upload: flat["nav.upload"] } };
    for (const k of Object.keys(flat)) {
      const pre = PREFIX.find(([p]) => k.startsWith(p));
      if (!pre) continue;
      const path = pre[1] + k.slice(pre[0].length), params = PARAMS[path], v = flat[k];
      put(S, path, params ? (...a) => fill(v, Object.fromEntries(params.map((p, i) => [p, a[i]]))) : v);
    }
    const plural = new Intl.PluralRules(S.locale);
    const pick = (base, n) => flat[base + "_" + plural.select(n)] || flat[base + "_other"];
    const months = n => fill(pick("fmt.months", n), { n });
    S.chart.pauseTitle = m => fill(flat["hc.pauseTitle"], { months: months(m) });
    S.chart.notes.linesPlain = flat["hc.notes.linesPlain"];
    S.chart.notes.lines = m => fill(flat["hc.notes.lines"], { months: months(m) });
    S.chart.notes.sheetOnly = (names, count, k, n) =>
      fill(flat[count > 1 ? "hc.notes.sheetOnly_other" : "hc.notes.sheetOnly_one"], { names, k, n });
    const mon = flat["fmt.mon"].split(","), wd = flat["fmt.wd"].split(",");
    const parts = iso => {
      const [y, m, d] = iso.split("-").map(Number);
      return { y, m, d, mon: mon[m - 1], wd: wd[new Date(Date.UTC(y, m - 1, d)).getUTCDay()] };
    };
    S.fmt = {
      date: iso => fill(flat["fmt.date"], parts(iso)),
      dateLong: iso => fill(flat["fmt.dateLong"], parts(iso)),
      monthYear: (y, m) => fill(flat["fmt.monthYear"], { y, mon: mon[m - 1] }),
      gap: days => days >= 61 ? fill(flat["fmt.gap_mo"], { n: Math.round(days / 30.44) }) : fill(flat["fmt.gap_wk"], { n: Math.round(days / 7) }),
      count: n => fill(pick("fmt.count", n), { n }),
      months
    };
    return S;
  }
  g.InbodyStrings = { build };
})(window);
