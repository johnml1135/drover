(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var content = $("#content"), pill = $("#new-updates"), sheet = $("#sheet"), toasts = $("#toast");
  var dragging = false, dirty = false, sortables = [], lastMove = null, disclosure = {}, treeSearch = null;
  var pending = [];

  function sheetOpen() { return !!(sheet && sheet.open); }
  function hasDraft() {
    var f = content.querySelectorAll("textarea,input,select");
    for (var i = 0; i < f.length; i++) {
      var control = f[i];
      if (control.id === "tree-search") continue;
      if (control.type === "checkbox" || control.type === "radio") {
        if (control.checked !== control.defaultChecked) return true;
      } else if (control.tagName === "SELECT") {
        var defaults = -1, options = control.options;
        for (var j = 0; j < options.length; j++) {
          if (control.multiple && options[j].selected !== options[j].defaultSelected) return true;
          if (options[j].defaultSelected) defaults = j;
        }
        // With no selected attribute, a single select defaults to its first option.
        if (!control.multiple && control.selectedIndex !== (defaults < 0 && options.length ? 0 : defaults)) return true;
      } else if (control.value !== control.defaultValue) return true;
    }
    var s = $("#sheet-body textarea");
    return !!(s && s.value.trim());
  }
  function paused() { return dragging || sheetOpen() || hasDraft() || pending.length > 0; }

  function refresh() {
    dirty = false; pill.hidden = true;
    return htmx.ajax("GET", location.pathname + location.search, { target: "#content", swap: "innerHTML" });
  }
  function maybeRefresh() { if (paused()) { dirty = true; pill.hidden = false; } else refresh(); }

  $("#refresh").addEventListener("click", refresh);
  pill.addEventListener("click", refresh);
  setInterval(maybeRefresh, 30000);
  document.addEventListener("htmx:sseMessage", maybeRefresh);

  /* details open state and Sortable lifecycle across swaps */
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target !== content) return;
    if (e.detail.xhr && e.detail.xhr.status >= 400) return;
    disclosure = Object.create(null);
    content.querySelectorAll("details[data-id]").forEach(function (d) { disclosure[d.dataset.id] = d.open; });
    var search = $("#tree-search", content);
    treeSearch = search ? search.value : null;
    sortables.forEach(function (s) { try { s.destroy(); } catch (x) {} });
    sortables = [];
  });
  document.addEventListener("htmx:afterSettle", function (e) {
    if (e.target !== content) return;
    var search = $("#tree-search", content);
    if (search && treeSearch !== null) { search.value = treeSearch; search.dispatchEvent(new Event("input", { bubbles: true })); }
    content.querySelectorAll("details[data-id]").forEach(function (d) {
      if (Object.prototype.hasOwnProperty.call(disclosure, d.dataset.id)) d.open = disclosure[d.dataset.id];
    });
    initSortable();
    if (lastMove) {
      var b = $('[data-move="' + lastMove + '"]', content);
      if (b) b.focus();
      lastMove = null;
    }
  });
  content.addEventListener("click", function (e) {
    var b = e.target.closest("[data-move]");
    if (b) lastMove = b.getAttribute("data-move");
  });

  /* comment sheet */
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "sheet-body" && !sheetOpen() && sheet.showModal) sheet.showModal();
  });
  $("#sheet-close").addEventListener("click", function () { sheet.close(); });
  sheet.addEventListener("close", function () { if (dirty && !paused()) refresh(); });

  /* errors: inline message beside the form, else a toast; reorder failures re-fetch */
  function showToast(msg, kind) {
    var t = document.createElement("div");
    t.className = "toast" + (kind ? " " + kind : ""); t.setAttribute("role", kind === "err" ? "alert" : "status");
    toasts.appendChild(t);
    return t;
  }
  function errText(xhr) {
    try { var d = JSON.parse(xhr.responseText).detail; if (typeof d === "string") return d; } catch (x) {}
    return "Request failed (" + xhr.status + ")";
  }
  document.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr, msg = errText(xhr);
    var elt = e.detail.requestConfig && e.detail.requestConfig.elt;
    var form = elt && elt.closest ? elt.closest("form") : null;
    var slot = form && form.querySelector(".err");
    if (slot) { slot.textContent = msg; slot.hidden = false; return; }
    var t = showToast(msg, "err"); t.textContent = msg;
    setTimeout(function () { t.remove(); }, 6000);
    if (e.detail.pathInfo && /\/plan\/reorder/.test(e.detail.pathInfo.requestPath)) refresh();
  });
  document.addEventListener("htmx:sendError", function () {
    var t = showToast("", "err"); t.textContent = "Network error"; setTimeout(function () { t.remove(); }, 6000);
  });
  document.addEventListener("htmx:beforeRequest", function (e) {
    var f = e.detail.elt && e.detail.elt.closest && e.detail.elt.closest("form");
    var slot = f && f.querySelector(".err"); if (slot) slot.hidden = true;
  });

  /* undo: each reject gets its own toast, timer and disabled item; pending ones flush on pagehide */
  function addUndo(msg, url, holder) {
    var t = showToast(msg), span = document.createElement("span"), b = document.createElement("button");
    span.textContent = msg; b.type = "button"; b.textContent = "Undo";
    t.appendChild(span); t.appendChild(b);
    var item = holder && (holder.closest(".card") || holder);
    var ctls = item ? item.querySelectorAll("button,input,textarea") : [];
    ctls.forEach(function (c) { c.disabled = true; });
    var rec = { url: url, done: false };
    function finish() { rec.done = true; t.remove(); pending = pending.filter(function (r) { return r !== rec; }); }
    rec.timer = setTimeout(function () {
      finish();
      htmx.ajax("POST", url, { target: "#content", swap: "innerHTML" });
    }, 6000);
    b.addEventListener("click", function () {
      clearTimeout(rec.timer); finish();
      ctls.forEach(function (c) { c.disabled = false; });
    });
    pending.push(rec);
  }
  window.addEventListener("pagehide", function () {
    pending.forEach(function (r) {
      clearTimeout(r.timer);
      if (navigator.sendBeacon) navigator.sendBeacon(r.url, new Blob([""], { type: "application/x-www-form-urlencoded" }));
      else fetch(r.url, { method: "POST", keepalive: true });
    });
    pending = [];
  });
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-undo-btn]");
    if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    addUndo(btn.getAttribute("data-undo-btn"), btn.getAttribute("formaction"), btn);
  }, true);
  document.addEventListener("submit", function (e) {
    var f = e.target.closest("form[data-undo]");
    if (!f) return;
    e.preventDefault(); e.stopPropagation();
    addUndo(f.getAttribute("data-undo"), f.getAttribute("action"), f);
  }, true);

  /* drag and drop: grip handle only, long-press on touch */
  function post(kind, el) {
    var prev = el.previousElementSibling, next = el.nextElementSibling;
    var vals = { kind: kind, id: el.dataset.id };
    if (prev && prev.dataset.id) vals.after = prev.dataset.id;
    else if (next && next.dataset.id) vals.before = next.dataset.id;
    else return;
    htmx.ajax("POST", "/plan/reorder", { target: "#content", swap: "innerHTML", values: vals });
  }
  function initSortable() {
    if (!window.Sortable) return;
    document.querySelectorAll(".sortable").forEach(function (list) {
      if (list.dataset.sortable) return;
      list.dataset.sortable = "1";
      var kind = list.dataset.kind;
      sortables.push(Sortable.create(list, {
        draggable: kind === "epic" ? ".epic" : ".task",
        handle: kind === "epic" ? ".epic-grip" : ".grip",
        delay: 250, delayOnTouchOnly: true, touchStartThreshold: 6,
        animation: 150, scroll: true, scrollSensitivity: 60,
        onStart: function () { dragging = true; },
        onEnd: function (evt) {
          dragging = false;
          if (evt.oldIndex !== evt.newIndex) post(kind, evt.item);
        }
      }));
    });
  }
  initSortable();
})();
