/* Stats is loaded on both full navigation and htmx refreshes. */
(function () {
  "use strict";
  var target = document.getElementById("chart-allowance");
  if (!target) return;
  var error = document.getElementById("stats-error");
  document.querySelectorAll("time[data-epoch]").forEach(function (t) {
    if (t.dataset.epoch) t.textContent = new Date(Number(t.dataset.epoch) * 1000).toLocaleString();
  });
  if (!window.Plotly) { error.hidden = false; error.textContent = "Charts could not load. Seat readings are available below."; return; }
  function plot(id, data, layout) {
    return Plotly.newPlot(id, data, Object.assign({ margin: { t: 20, l: 60, r: 30, b: 60 },
      height: 320, legend: { orientation: "h" }, paper_bgcolor: "transparent", plot_bgcolor: "transparent" }, layout),
      { responsive: true, displaylogo: false });
  }
  fetch("/api/v1/stats?series=1", { credentials: "same-origin" }).then(function (r) {
    if (!r.ok) throw new Error("Stats request failed");
    return r.json();
  }).then(function (data) {
    if (!target.isConnected) return;
    var rows = data.daily.concat(data.hourly).sort(function (a, b) { return a.ts.localeCompare(b.ts); });
    var allowance = {}, resets = {}, tokens = {}, resources = {}, days = {};
    function point(group, name, ts, value) {
      var trace = group[name] || (group[name] = { name: name, x: [], y: [], type: "scatter", mode: "lines+markers", connectgaps: false });
      trace.x.push(ts); trace.y.push(value);
    }
    rows.forEach(function (r) {
      Object.keys(r.allowance || {}).forEach(function (seat) {
        Object.keys(r.allowance[seat]).forEach(function (window) {
          var w = r.allowance[seat][window], name = seat + " / " + window;
          point(allowance, name, r.ts, w.used_pct);
          if (w.resets_at) resets[name + w.resets_at] = new Date(w.resets_at * 1000).toISOString();
        });
      });
      Object.keys(r.tokens_by_model || {}).forEach(function (model) {
        var t = r.tokens_by_model[model];
        point(tokens, model, r.ts, t.tokens_total == null ? (t.tokens_in || 0) + (t.tokens_out || 0) : t.tokens_total);
      });
      var f = r.resources || {};
      point(resources, "CPU load (5m)", r.ts, f.load5 == null ? null : f.load5);
      point(resources, "RAM available GB", r.ts, f.ram_available_gb == null ? null : f.ram_available_gb);
      Object.keys(f.disk_free_gb || {}).forEach(function (disk) { point(resources, "Disk " + disk + " free GB", r.ts, f.disk_free_gb[disk]); });
      if (f.vram) point(resources, "VRAM used GB", r.ts, f.vram[0] / 1024);
      if (f.wsl_mem) point(resources, "WSL memory GB", r.ts, f.wsl_mem.used_gb);
      var day = r.ts.slice(0, 10); days[day] = (days[day] || 0) + (r.landings || 0);
    });
    plot("chart-allowance", Object.values(allowance), { yaxis: { range: [0, 100], title: "% used" },
      shapes: Object.values(resets).map(function (ts) { return { type: "line", x0: ts, x1: ts, y0: 0, y1: 100,
        line: { dash: "dot", color: "#888", width: 1 } }; }) });
    plot("chart-tokens", Object.values(tokens), { yaxis: { title: "Tokens" } });
    plot("chart-resources", Object.values(resources), { yaxis: { title: "GB / load" } });
    plot("chart-throughput", [{ type: "bar", name: "Merges", x: Object.keys(days), y: Object.values(days) }],
      { yaxis: { title: "Tasks", dtick: 1 } });
  }).catch(function () { error.hidden = false; error.textContent = "Statistics could not load. Refresh to retry."; });
}());
