/* Evaluation explorer: summary cards + AG Grid Community table of the 200 synthetic cases. DOM via textContent only. */
(function () {
  "use strict";
  var SPRITE = "/static/icons/lucide-sprite.svg#lucide-";
  var OUTCOME = {
    blocked: ["Blocked", "pill-ok", "shield-check"],
    partial: ["Partly blocked", "pill-warn", "triangle-alert"],
    missed: ["Missed", "pill-bad", "shield-alert"],
    paid: ["Paid", "pill-ok", "circle-check"],
    false_block: ["Wrongly blocked", "pill-bad", "circle-x"],
    dispute_drafted: ["Dispute drafted", "pill-info", "scale"],
    not_claimed: ["Not claimed", "pill-bad", "circle-x"]
  };
  var TYPE_LABEL = { normal: "Normal", over_authority: "Over-authority", injection: "Prompt injection", mismatch: "Delivery mismatch" };
  var state = { type: "", data: null, api: null };

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function icon(name) {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("class", "icon"); s.setAttribute("aria-hidden", "true");
    var u = document.createElementNS("http://www.w3.org/2000/svg", "use"); u.setAttribute("href", SPRITE + name); s.appendChild(u); return s;
  }
  function pct(k, n) { return n ? (100 * k / n).toFixed(1) + "%" : "n/a"; }

  function card(cfg, s) {
    var c = el("article", "eval-card" + (cfg === "full" ? " is-ours" : "") + (cfg === "no_guardrails" ? " is-none" : ""));
    var h = el("h3"); h.appendChild(icon(cfg === "full" ? "shield-check" : cfg === "policy_only" ? "list-checks" : "shield-alert"));
    h.appendChild(document.createTextNode(s.label + (cfg === "full" ? " (ours)" : ""))); c.appendChild(h);
    var big = el("div", "big", s.attack_cases_blocked + "/" + s.attack_cases); big.appendChild(el("small", null, pct(s.attack_cases_blocked, s.attack_cases)));
    c.appendChild(big); c.appendChild(el("div", "label", "attack cases fully stopped"));
    var m = el("div", "meter"); var f = el("span"); f.style.width = (s.attack_cases ? 100 * s.attack_cases_blocked / s.attack_cases : 0) + "%"; m.appendChild(f); c.appendChild(m);
    var dl = el("dl");
    [["Unauthorized attempts stopped", s.attempts_blocked + "/" + s.unauthorized_attempts],
     ["Normal requests wrongly blocked", s.normal_false_blocks + "/" + s.normal_cases],
     ["Delivery mismatches claimed", s.mismatch_handled + "/" + s.mismatch_cases]].forEach(function (p) {
      dl.appendChild(el("dt", null, p[0])); dl.appendChild(el("dd", null, p[1])); });
    c.appendChild(dl);
    return c;
  }

  function outcomeRenderer(p) {
    var o = OUTCOME[p.value]; if (!o) return document.createTextNode(p.value || "");
    var s = el("span", "pill " + o[1]); s.appendChild(icon(o[2])); s.appendChild(document.createTextNode(o[0])); return s;
  }

  function passes(row) {
    var t = state.type;
    if (!t) return true;
    if (t === "attack") return row.type === "over_authority" || row.type === "injection";
    if (t === "missed") return row.full === "missed" || row.full === "partial";
    return row.type === t;
  }

  function build(data) {
    state.data = data;
    document.getElementById("seed-chip").textContent = "Seed " + data.seed + " · " + data.n_cases + " synthetic";
    var cards = document.getElementById("cards");
    ["no_guardrails", "policy_only", "full"].forEach(function (k) { cards.appendChild(card(k, data.summary[k])); });
    var outcomeCol = function (field, name) {
      return { field: field, headerName: name, cellRenderer: outcomeRenderer, width: 170, filter: "agTextColumnFilter",
               comparator: function (a, b) { return String(a).localeCompare(String(b)); } };
    };
    var theme = agGrid.themeQuartz.withParams({ accentColor: "#0070e0", headerBackgroundColor: "#f7fafd", fontFamily: "inherit",
      borderColor: "#e1e9f3", foregroundColor: "#122238", rowHoverColor: "#eaf3ff", headerFontWeight: 650 });
    var opts = {
      theme: theme,
      rowData: data.rows,
      getRowId: function (p) { return p.data.id; },
      columnDefs: [
        { field: "id", headerName: "Case", pinned: "left", width: 105 },
        { field: "type", headerName: "Type", width: 160, valueFormatter: function (p) { return TYPE_LABEL[p.value] || p.value; }, filter: true },
        { field: "subtype", headerName: "Subtype", width: 185, filter: true },
        { field: "task", headerName: "Employee request", flex: 1, minWidth: 260, tooltipField: "task" },
        { field: "attempts", headerName: "Attack attempts", width: 135, type: "numericColumn" },
        outcomeCol("no_guardrails", "No guardrails"),
        outcomeCol("policy_only", "Policy only"),
        outcomeCol("full", "Full guardrails"),
        { field: "poisoned_listing", headerName: "Injected listing text", width: 320, tooltipField: "poisoned_listing", hide: false }
      ],
      defaultColDef: { sortable: true, resizable: true },
      isExternalFilterPresent: function () { return !!state.type; },
      doesExternalFilterPass: function (n) { return passes(n.data); },
      pagination: true, paginationPageSize: 50, paginationPageSizeSelector: [25, 50, 100, 200],
      tooltipShowDelay: 300,
      overlayNoRowsTemplate: "<span>No cases match.</span>"
    };
    opts.onFirstDataRendered = function () { document.body.dataset.ready = "1"; };
    state.api = agGrid.createGrid(document.getElementById("grid"), opts);
    document.getElementById("search").addEventListener("input", function (e) { state.api.setGridOption("quickFilterText", e.target.value); });
    Array.prototype.forEach.call(document.querySelectorAll("#type-filter button"), function (b) {
      b.addEventListener("click", function () { setType(b.getAttribute("data-type")); });
    });
    var q = new URLSearchParams(location.search);
    if (q.get("filter")) setType(q.get("filter"));
  }

  function setType(t) {
    state.type = t || "";
    Array.prototype.forEach.call(document.querySelectorAll("#type-filter button"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-type") === state.type)); });
    if (state.api) state.api.onFilterChanged();
  }
  window.GBEval = { setType: setType };

  fetch("/api/evaluation").then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(build)
    .catch(function (e) { var c = document.getElementById("cards"); c.textContent = ""; c.appendChild(el("p", "eval-foot", "Could not load evaluation data: " + e.message)); });
})();
