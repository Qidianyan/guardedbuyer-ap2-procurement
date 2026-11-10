/* GuardedBuyer dashboard — vanilla JS, no framework, no external resources.
 * All server/user/supplier text is rendered with textContent (via the el() helper);
 * HTML is never built from data-derived strings.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const params = new URLSearchParams(window.location.search);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const XLINK_NS = "http://www.w3.org/1999/xlink";

  const SCENARIOS = {
    agent_loop: { label: "Agent loop", full: "0 · Agent loop", tone: "blue" },
    normal: { label: "Normal purchase", full: "1 · Normal purchase", tone: "ok" },
    over_budget: { label: "Over budget + approvals", full: "2 · Over budget + approvals", tone: "warn" },
    mismatch: { label: "Delivery mismatch → dispute", full: "3 · Delivery mismatch → dispute", tone: "warn" },
    red_team: { label: "Red team (prompt injection)", full: "4 · Red team (prompt injection)", tone: "danger" },
  };

  const STATUS = {
    pending_approval: { label: "Pending approval", tone: "amber" },
    approved: { label: "Approved", tone: "blue" },
    awaiting_buyer_approval: { label: "Awaiting buyer approval", tone: "amber" },
    paid: { label: "Paid", tone: "green" },
    closed: { label: "Closed", tone: "green" },
    rejected: { label: "Rejected", tone: "red" },
    disputed: { label: "Disputed", tone: "red" },
    discrepancy_flagged: { label: "Discrepancy flagged", tone: "red" },
    buyer_cancelled: { label: "Buyer cancelled", tone: "red" },
    blocked_mismatch: { label: "Blocked: mismatch", tone: "red" },
    dispute_pending_buyer_filing: { label: "Dispute draft ready", tone: "amber" },
  };

  const ORDER_STATUS = {
    PAYER_ACTION_REQUIRED: { label: "PAYER_ACTION_REQUIRED", tone: "amber" },
    CREATED: { label: "CREATED", tone: "slate" },
    APPROVED: { label: "APPROVED", tone: "blue" },
    COMPLETED: { label: "COMPLETED", tone: "green" },
  };

  const DISPUTE_STATUS = {
    WAITING_FOR_SELLER_RESPONSE: { label: "WAITING_FOR_SELLER_RESPONSE", tone: "amber" },
    RESOLVED: { label: "RESOLVED", tone: "green" },
    OPEN: { label: "OPEN", tone: "amber" },
    UNDER_REVIEW: { label: "UNDER_REVIEW", tone: "blue" },
  };

  const DISPUTE_REASONS = {
    MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: "Item not as described",
    MERCHANDISE_OR_SERVICE_NOT_RECEIVED: "Item not received",
    UNAUTHORISED: "Unauthorised transaction",
    INCORRECT_AMOUNT: "Incorrect amount",
    DUPLICATE_TRANSACTION: "Duplicate transaction",
    CREDIT_NOT_PROCESSED: "Credit not processed",
  };

  const INJECTION_PATTERNS = [
    [/\bignore\b[\s\S]{0,40}\b(instruction|rule|polic|budget|limit)/i, "override_instructions"],
    [/\b(system|admin|developer)\s*(notice|message|override|prompt|instruction)/i, "fake_system_message"],
    [/\b(ai|llm|assistant|agent|bot)s?\b[\s\S]{0,40}\b(must|should|are instructed|shall)\b/i, "addresses_the_agent"],
    [/\b(pre-?approved|already approved|approval (is )?not (needed|required)|skip (the )?approval)/i, "fake_approval"],
    [/\bapprove_purchase\b|\bexecute_payment\b|\bcharge_amount\b|\bsubmit_purchase\b|\btool\b[\s\S]{0,20}\bcall/i, "tool_manipulation"],
    [/\bgift ?cards?\b|\bwire transfer\b|\bcrypto|\bbitcoin\b/i, "high_risk_payment"],
    [/\b(also|additionally) (buy|order|purchase)\b/i, "extra_purchase"],
  ];

  const STEP_LABELS = [
    "Request", "Quotes", "Policy", "Approvals", "AP2 Mandate",
    "PayPal order", "Buyer approval", "Captured", "Received", "Closed / Disputed",
  ];

  const state = {
    data: null,
    selectedId: null,
    auditFilter: ["blocked", "current"].includes(params.get("filter")) ? params.get("filter") : "all",
    traceFilter: params.get("trace") || null,
    auditResult: null,
    receiveDraft: null,
    busy: false,
    lastJson: null,
    pollTimer: null,
  };

  /* ------------------------------------------------------------------ DOM helpers */

  function appendChildren(node, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) { appendChildren(node, child); continue; }
      if (child instanceof Node) node.appendChild(child);
      else node.appendChild(document.createTextNode(String(child)));
    }
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    const options = attrs || {};
    for (const [key, value] of Object.entries(options)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class" || key === "className") node.className = String(value);
      else if (key === "text") node.textContent = String(value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "value") {
        node.setAttribute("value", String(value));
        try { node.value = String(value); } catch (_) { /* not a form field */ }
      } else if (value === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(value));
      }
    }
    appendChildren(node, children);
    return node;
  }

  function icon(name, cls = "icon") {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", cls);
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#i-" + name);
    use.setAttributeNS(XLINK_NS, "xlink:href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function emptyState(iconName, titleText, text) {
    return el("div", { class: "empty" },
      icon(iconName),
      el("h3", { text: titleText }),
      text ? el("p", { text }) : null
    );
  }

  function pill(text, tone = "slate") {
    return el("span", { class: "pill pill-" + tone },
      el("span", { class: "dot" }),
      el("span", { text })
    );
  }

  function tag(text, tone = "slate") {
    return el("span", { class: "tag tag-" + tone, text });
  }

  function sourceBadge(source) {
    const isLlm = source === "llm_agent";
    return el("span", { class: "source-badge" + (isLlm ? " llm" : "") },
      icon(isLlm ? "sparkles" : "users"),
      isLlm ? "LLM agent" : (source || "employee")
    );
  }

  function statusPill(status) {
    const info = STATUS[status] || { label: humanize(status || "unknown"), tone: "slate" };
    return pill(info.label, info.tone);
  }

  function orderPill(status) {
    const info = ORDER_STATUS[status] || { label: status || "UNKNOWN", tone: "slate" };
    return pill(info.label, info.tone);
  }

  function disputePill(status) {
    const info = DISPUTE_STATUS[status] || { label: status || "UNKNOWN", tone: "slate" };
    return pill(info.label, info.tone);
  }

  function copyButton(text, label = "Copy") {
    return el("button", {
      type: "button", class: "btn btn-secondary btn-xs", title: "Copy to clipboard",
      onclick: async () => {
        const ok = await copyText(text);
        toast(ok ? "Copied" : "Copy failed", ok ? label + " copied to the clipboard." : "Select the text and copy it manually.",
          ok ? "success" : "warn", 3200);
      },
    }, icon("copy"), label);
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(String(text));
        return true;
      }
    } catch (_) { /* fall through */ }
    try {
      const area = document.createElement("textarea");
      area.value = String(text);
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      area.style.top = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch (_) {
      return false;
    }
  }

  /* -------------------------------------------------------------------- formatting */

  function fmtMoney(value, currency = "USD", digits = 2) {
    const n = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(n)) return "—";
    try {
      return new Intl.NumberFormat("en-US", {
        style: "currency", currency: currency || "USD",
        minimumFractionDigits: digits, maximumFractionDigits: digits,
      }).format(n);
    } catch (_) {
      return (currency || "") + " " + n.toFixed(digits);
    }
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return new Intl.DateTimeFormat("en-US", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      hour12: false, timeZone: "UTC",
    }).format(d) + " UTC";
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toISOString().slice(11, 19);
  }

  function shortHash(value, len = 10) {
    const s = String(value || "");
    if (s.length <= len) return s || "—";
    return s.slice(0, len);
  }

  function compactJSON(value) {
    try { return JSON.stringify(value); }
    catch (_) { return String(value); }
  }

  function clip(text, max) {
    const s = String(text === null || text === undefined ? "" : text);
    return s.length > max ? s.slice(0, Math.max(0, max - 1)).trimEnd() + "…" : s;
  }

  function humanize(value) {
    return String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function roleLabel(role) {
    return humanize(role || "approver");
  }

  function stripUntrusted(text) {
    return String(text || "")
      .replace(/<untrusted[^>]*>/gi, "")
      .replace(/<\/untrusted>/gi, "")
      .trim();
  }

  function detectInjection(text) {
    const haystack = String(text || "");
    const found = [];
    for (const [pattern, label] of INJECTION_PATTERNS) {
      if (pattern.test(haystack)) found.push(label);
    }
    return Array.from(new Set(found)).sort();
  }

  function hasInjectionWarning(text) {
    return /possible prompt injection/i.test(String(text || "")) || detectInjection(text).length > 0;
  }

  function scenarioInfo(name) {
    return SCENARIOS[name] || { label: humanize(name || "scenario"), full: humanize(name || "scenario"), tone: "blue" };
  }

  function nextFrame() {
    return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }

  /* ------------------------------------------------------------------------ toasts */

  function toast(title, message, tone = "info", timeout = 6200) {
    const host = $("#toasts");
    if (!host) return;
    const iconName = tone === "success" ? "check-circle"
      : tone === "error" || tone === "blocked" ? "x-circle"
      : tone === "warn" ? "alert" : "info";
    const box = el("div", { class: "toast toast-" + tone, role: tone === "blocked" || tone === "error" ? "alert" : "status" },
      icon(iconName, "icon toast-icon"),
      el("div", { class: "toast-content" },
        el("div", { class: "toast-title", text: title }),
        message ? el("div", { class: "toast-msg", text: String(message) }) : null
      ),
      el("button", { type: "button", class: "toast-close", title: "Dismiss", "aria-label": "Dismiss notification" },
        icon("x-circle"))
    );
    const close = () => {
      if (!box.isConnected) return;
      box.style.opacity = "0";
      box.style.transform = "translateY(-4px)";
      box.style.transition = "opacity .15s ease, transform .15s ease";
      setTimeout(() => box.remove(), 160);
    };
    box.querySelector(".toast-close").addEventListener("click", close);
    host.appendChild(box);
    while (host.children.length > 4) host.firstElementChild.remove();
    setTimeout(close, timeout);
  }

  function cleanError(message) {
    let text = String(message || "Unknown error");
    text = text.replace(/^[A-Za-z_$][\w$]*(?:Error|Exception):\s*/, "");
    return text.length > 520 ? text.slice(0, 517) + "…" : text;
  }

  function handleError(err) {
    console.error(err);
    let message = err && err.message ? err.message : String(err);
    if (err instanceof TypeError && /fetch|network/i.test(message)) {
      message = "Could not reach the demo server. Is web/server.py running?";
    }
    if (err && err.blocked) {
      toast("Blocked by guardrail", cleanError(message), "blocked", 9000);
    } else {
      toast("Request failed", cleanError(message), "error", 8000);
    }
  }

  /* ---------------------------------------------------------------------------- api */

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const init = { method, cache: "no-store", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const res = await fetch(path, init);
    const ctype = res.headers.get("content-type") || "";
    let body = null;
    if (ctype.includes("application/json")) {
      body = await res.json().catch(() => null);
    } else {
      body = await res.text().catch(() => null);
    }
    if (!res.ok) {
      const message = body && body.error ? body.error : "HTTP " + res.status;
      const error = new Error(message);
      error.status = res.status;
      error.blocked = !!(body && body.blocked);
      error.payload = body;
      throw error;
    }
    return body;
  }

  /* ---------------------------------------------------------------------- buttons */

  function setBusy(button, label) {
    if (!button) return;
    button._previousChildren = Array.from(button.childNodes);
    button.disabled = true;
    button.replaceChildren(el("span", { class: "spinner spinner-xs" }), document.createTextNode(label));
  }

  function clearBusy(button) {
    if (!button) return;
    if (button._previousChildren) {
      button.replaceChildren(...button._previousChildren);
      button._previousChildren = null;
    }
    button.disabled = false;
  }

  function setGlobalBusy(value) {
    state.busy = value;
    syncReplayControls();
  }

  function syncReplayControls() {
    $$("#scenario-buttons .scenario-btn, #replay-all, #reset-btn").forEach((button) => {
      button.disabled = state.busy;
    });
  }

  /* ---------------------------------------------------------------------- selection */

  function tickets() {
    return (state.data && state.data.tickets) || [];
  }

  function selectedTicket() {
    return tickets().find((t) => t.request_id === state.selectedId) || null;
  }

  function selectFromToken(token) {
    if (token === null || token === undefined || token === "") return null;
    const list = tickets();
    if (/^\d+$/.test(String(token))) {
      const index = Number(token);
      return list[index] ? list[index].request_id : null;
    }
    const match = list.find((t) => t.request_id === token);
    return match ? match.request_id : null;
  }

  function ensureSelection() {
    const list = tickets();
    if (!list.length) { state.selectedId = null; return; }
    if (!list.some((t) => t.request_id === state.selectedId)) {
      state.selectedId = list[0].request_id;
    }
  }

  function applyInitialSelection() {
    const fromQuery = selectFromToken(params.get("select"));
    if (fromQuery) { state.selectedId = fromQuery; return; }
    const hash = decodeURIComponent((window.location.hash || "").replace(/^#/, ""));
    if (hash) {
      const match = tickets().find((t) => t.request_id === hash);
      if (match) { state.selectedId = match.request_id; return; }
    }
    ensureSelection();
  }

  function selectRequest(id, options = {}) {
    if (!tickets().some((t) => t.request_id === id)) return;
    if (state.selectedId !== id) state.receiveDraft = null;
    state.selectedId = id;
    renderRequests();
    renderDetail();
    if (state.auditFilter === "current") renderAudit();
    if (options.scroll) {
      const target = $("#detail");
      if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
    }
    try {
      history.replaceState(null, "", "#" + encodeURIComponent(id));
    } catch (_) { /* ignore */ }
  }

  /* ----------------------------------------------------------------------- refresh */

  async function refreshState(options = {}) {
    const data = await api("/api/state");
    state.data = data;
    state.lastJson = JSON.stringify(data);
    if (options.select) state.selectedId = options.select;
    ensureSelection();
    render();
    return data;
  }

  async function refreshStateQuietly() {
    try {
      return await refreshState();
    } catch (error) {
      handleError(error);
      return null;
    }
  }

  /* ------------------------------------------------------------------------ render */

  function render() {
    if (!state.data) return;
    renderStatus();
    renderPolicy();
    renderRequests();
    renderDetail();
    renderTrace();
    renderAudit();
    renderReplayState();
    renderAuditResult();
    syncReplayControls();
  }

  function renderStatus() {
    const host = $("#status-chips");
    if (!host) return;
    const data = state.data;
    const chips = [];

    const mode = String(data.paypal_mode || "unknown");
    if (mode.startsWith("MOCK")) {
      chips.push(statusChip("PayPal: offline emulation (Orders v2 / Disputes v1)", "accent",
        { title: "PayPal backend: " + mode + " — no network or real money." }));
    } else {
      chips.push(statusChip("PayPal: " + mode, "accent", { title: "PayPal backend mode: " + mode }));
    }

    if (data.llm) {
      const raw = String(data.llm);
      const match = raw.match(/model\s*[:=]\s*['"]?([^,'")\s]+)/i);
      const model = match ? match[1] : clip(raw, 44);
      chips.push(statusChip("LLM: " + model, "accent", { title: raw }));
    } else {
      chips.push(statusChip("LLM: offline fallback", "muted",
        { title: "No LLM configured — deterministic offline planning and parsing." }));
    }

    const chain = data.chain || {};
    if (chain.valid) {
      chips.push(statusChip("Hash chain valid · " + chain.length + " entries", "ok",
        { title: "Head " + (chain.head || "—"), iconName: "check-circle" }));
    } else {
      chips.push(statusChip("Chain BROKEN at #" + chain.first_bad_seq, "bad",
        { title: "Hash chain verification failed at entry #" + chain.first_bad_seq, iconName: "alert" }));
    }

    const blockedCount = (data.block_events || []).length;
    chips.push(chipButton(
      blockedCount + " blocked / flagged events",
      blockedCount ? "bad" : "muted",
      {
        title: "Show blocked and flagged audit entries",
        iconName: "alert",
        onClick: () => {
          state.auditFilter = "blocked";
          renderAudit();
          const target = $("#audit");
          if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
        },
      }
    ));

    host.replaceChildren(...chips);
  }

  function statusChip(label, tone, options = {}) {
    return el("span", { class: "chip chip-" + tone, title: options.title || "" },
      options.iconName ? icon(options.iconName) : el("span", { class: "dot" }),
      el("span", { text: label })
    );
  }

  function chipButton(label, tone, options = {}) {
    const button = el("button", { type: "button", class: "chip chip-" + tone, title: options.title || "" },
      options.iconName ? icon(options.iconName) : el("span", { class: "dot" }),
      el("span", { text: label })
    );
    if (options.onClick) button.addEventListener("click", options.onClick);
    return button;
  }

  function renderPolicy() {
    const host = $("#policy-body");
    if (!host) return;
    const policy = state.data.policy || {};
    const budgets = Object.entries(policy.department_budgets || {});
    const roster = policy.roster || {};
    const chain = policy.approval_chain || [];
    const blocked = policy.blocked_merchants || [];
    const categories = policy.allowed_categories || [];
    clear(host);

    const budgetGroup = policyGroup("Department budgets");
    if (budgets.length) {
      for (const [dept, amount] of budgets) {
        const remaining = Number(amount);
        budgetGroup.appendChild(el("div", { class: "budget-row" },
          el("span", { class: "budget-dept" }, el("span", { class: "dept-dot" }), dept),
          el("span", { class: "budget-amount" + (Number.isFinite(remaining) && remaining < 0 ? " negative" : "") },
            fmtMoney(amount),
            el("small", { text: "remaining" })
          )
        ));
      }
    } else {
      budgetGroup.appendChild(el("p", { class: "policy-row" }, "No department budgets configured."));
    }
    host.appendChild(budgetGroup);

    const chainGroup = policyGroup("Human approval chain");
    if (chain.length) {
      for (const entry of chain) {
        const threshold = Array.isArray(entry) ? entry[0] : entry.threshold;
        const role = Array.isArray(entry) ? entry[1] : entry.role;
        const person = roster[role] || "—";
        chainGroup.appendChild(el("div", { class: "policy-row" },
          icon("lock"),
          el("span", {},
            el("b", { text: roleLabel(role) + " · " + person }),
            role === "finance" ? " (or over budget)" : ""
          ),
          el("span", { class: "policy-threshold", text: "> " + fmtMoney(threshold, "USD", 0) })
        ));
      }
    } else {
      chainGroup.appendChild(el("p", { class: "policy-row" }, "No approval thresholds configured."));
    }
    chainGroup.appendChild(el("div", { class: "policy-row" },
      icon("shield"),
      el("span", {}, el("b", { text: "Per-order hard cap" })),
      el("span", { class: "policy-threshold", text: fmtMoney(policy.per_order_hard_cap) })
    ));
    host.appendChild(chainGroup);

    const limitsGroup = policyGroup("Blocked merchants");
    if (blocked.length) {
      limitsGroup.appendChild(el("div", { class: "chip-row" },
        blocked.map((merchant) => el("span", { class: "tiny-chip bad", text: merchant }))
      ));
    } else {
      limitsGroup.appendChild(el("p", { class: "policy-row" }, "None."));
    }
    if (categories.length) {
      limitsGroup.appendChild(el("div", { class: "policy-label", style: { marginTop: "8px", marginBottom: "4px" }, text: "Allowed categories" }));
      limitsGroup.appendChild(el("div", { class: "chip-row" },
        categories.map((category) => el("span", { class: "tiny-chip blue", text: category }))
      ));
    }
    host.appendChild(limitsGroup);

    const rosterGroup = policyGroup("Roster");
    const rosterEntries = Object.entries(roster);
    if (rosterEntries.length) {
      rosterGroup.appendChild(el("div", { class: "chip-row" },
        rosterEntries.map(([role, person]) => el("span", { class: "tiny-chip", text: roleLabel(role) + " · " + person }))
      ));
    } else {
      rosterGroup.appendChild(el("p", { class: "policy-row" }, "No approvers configured."));
    }
    rosterGroup.appendChild(el("div", { class: "policy-note", text: "Budgets update when a payment is captured. Only humans can approve spend." }));
    host.appendChild(rosterGroup);
  }

  function policyGroup(label) {
    const group = el("div", { class: "policy-group" });
    group.appendChild(el("div", { class: "policy-label", text: label }));
    return group;
  }

  function renderRequests() {
    const host = $("#requests-list");
    const count = $("#requests-count");
    const meta = $("#requests-meta");
    if (!host) return;
    const list = tickets();
    clear(host);
    if (count) count.textContent = String(list.length);
    if (meta) {
      meta.textContent = list.length
        ? list.length + (list.length === 1 ? " request" : " requests") + " · newest first · click to inspect"
        : "No requests yet";
    }
    if (!list.length) {
      host.appendChild(emptyState("receipt", "No requests yet",
        "Ask the agent for a purchase or replay a scenario to populate the demo."));
      return;
    }
    for (const ticket of list) {
      const request = ticket.request || {};
      const row = el("button", {
        type: "button",
        class: "request-row" + (ticket.request_id === state.selectedId ? " active" : ""),
        title: "Inspect " + ticket.request_id,
        "aria-current": ticket.request_id === state.selectedId ? "true" : null,
      },
        el("span", { class: "request-id", text: ticket.request_id }),
        el("span", { class: "request-item" },
          el("span", { class: "ri-title", text: request.description || "Purchase request" }),
          el("span", { class: "ri-sub", text: (request.quantity || "?") + " × " + (request.sku || "—") + " · " + fmtMoney(request.unit_price, request.currency) + " each" })
        ),
        el("span", { class: "request-merchant", text: request.merchant || "—" }),
        el("span", { class: "request-total", text: fmtMoney(request.total, request.currency) }),
        sourceBadge(ticket.source),
        statusPill(ticket.status),
        icon("chevron")
      );
      row.addEventListener("click", () => selectRequest(ticket.request_id));
      host.appendChild(row);
    }
  }

  /* --------------------------------------------------------------- detail: shared bits */

  function requestMoney(request) {
    return fmtMoney(request && request.total, (request && request.currency) || "USD");
  }

  function setDocumentTitle(ticket) {
    if (ticket && ticket.request) {
      document.title = "GuardedBuyer — " + (ticket.request.description || ticket.request_id);
    } else {
      document.title = "GuardedBuyer — AP2-guarded procurement";
    }
  }

  function renderDetail() {
    const host = $("#detail");
    if (!host) return;
    clear(host);
    const ticket = selectedTicket();
    setDocumentTitle(ticket);
    if (!ticket) {
      host.appendChild(el("div", { class: "card" },
        emptyState("receipt", "No request selected", "Pick a request above or ask the agent to create one.")));
      return;
    }
    const info = getQuoteInfo(ticket);
    const shell = el("div", { class: "detail-shell" });
    shell.appendChild(detailHero(ticket));
    shell.appendChild(stepperCard(ticket));
    shell.appendChild(requestSection(ticket, info));
    shell.appendChild(quotesSection(ticket, info));
    shell.appendChild(policySection(ticket));
    shell.appendChild(approvalsSection(ticket));
    shell.appendChild(mandateSection(ticket));
    shell.appendChild(paypalSection(ticket));
    shell.appendChild(receivingSection(ticket));
    const dispute = disputeSection(ticket);
    if (dispute) shell.appendChild(dispute);
    host.appendChild(shell);
  }

  function detailHero(ticket) {
    const request = ticket.request || {};
    const meta = el("div", { class: "hero-meta" },
      metaItem("Requester", request.requester || "—"),
      metaItem("Dept", request.department || "—"),
      metaItem("Qty", request.quantity || "—"),
      metaItem("Merchant", request.merchant || "—"),
      metaItem("Unit", fmtMoney(request.unit_price, request.currency)),
      metaItem("SKU", request.sku || "—")
    );
    return el("div", { class: "detail-hero" },
      el("div", { class: "hero-main" },
        el("div", { class: "hero-kicker" },
          el("span", { text: "Purchase request" }),
          el("code", { text: ticket.request_id }),
          sourceBadge(ticket.source)
        ),
        el("h2", { text: request.description || "Purchase request" }),
        meta
      ),
      el("div", { class: "hero-total" },
        el("span", { class: "total-label", text: "Request total" }),
        el("span", { class: "amount" },
          fmtMoney(request.total, request.currency).replace(/\s?[A-Z]{3}$/, ""),
          el("small", { text: request.currency || "USD" })
        ),
        statusPill(ticket.status)
      )
    );
  }

  function metaItem(label, value) {
    return el("span", { class: "meta-item" }, label + " ", el("b", { text: String(value) }));
  }

  function stepperCard(ticket) {
    const states = stepStates(ticket);
    const stepper = el("div", { class: "stepper", role: "list", "aria-label": "Request progress" });
    states.forEach((step, index) => {
      const current = step.state === "current";
      stepper.appendChild(el("div", {
        class: "step " + step.state,
        role: "listitem",
        title: STEP_LABELS[index] + ": " + step.state,
        "aria-current": current ? "step" : null,
      },
        el("span", { class: "step-dot", text: step.state === "done" ? "✓" : step.state === "error" ? "!" : String(index + 1) }),
        el("span", { class: "step-label", text: STEP_LABELS[index] })
      ));
    });
    return el("div", { class: "stepper-card" }, stepper);
  }

  function stepStates(ticket) {
    const status = ticket.status;
    const decision = ticket.decision || {};
    const needed = decision.needs_approval_from || [];
    const approvals = ticket.approvals || [];
    const rejected = status === "rejected";
    const cancelled = status === "buyer_cancelled";
    const mismatch = status === "blocked_mismatch";
    const approvalsDone = needed.length === 0 || needed.every((role) => approvals.includes(role));
    const hasMandate = !!ticket.mandate;
    const order = ticket.paypal_order || {};
    const orderStatus = order.status;
    const orderCreated = !!ticket.order_id;
    const captured = !!ticket.capture_id || ["paid", "closed", "disputed", "discrepancy_flagged", "dispute_pending_buyer_filing"].includes(status);
    const buyerDone = !!ticket.capture_id || orderStatus === "APPROVED" || orderStatus === "COMPLETED"
      || ["paid", "closed", "disputed", "discrepancy_flagged", "dispute_pending_buyer_filing"].includes(status);
    const received = !!ticket.receipt || !!ticket.dispute
      || ["closed", "disputed", "discrepancy_flagged", "dispute_pending_buyer_filing"].includes(status);

    const states = [];
    states.push({ state: "done" }); // request
    states.push({ state: "done" }); // quotes / policy always evaluated before a ticket exists
    states.push({ state: rejected ? "error" : "done" }); // policy
    states.push({
      state: rejected ? "pending"
        : needed.length === 0 ? "done"
          : approvalsDone ? "done"
            : status === "pending_approval" ? "current" : "pending",
    });
    states.push({
      state: rejected ? "pending"
        : hasMandate ? "done"
          : status === "approved" ? "current" : "pending",
    });
    states.push({
      state: mismatch ? "error"
        : rejected ? "pending"
          : orderCreated ? "done"
            : hasMandate && status === "approved" ? "current" : "pending",
    });
    states.push({
      state: cancelled ? "error"
        : rejected ? "pending"
          : buyerDone ? "done"
            : status === "awaiting_buyer_approval" ? "current" : "pending",
    });
    states.push({ state: captured ? "done" : "pending" });
    states.push({
      state: received ? "done"
        : rejected || cancelled || mismatch ? "pending"
          : status === "paid" ? "current" : "pending",
    });
    states.push({
      state: status === "closed" ? "done"
        : ["disputed", "discrepancy_flagged", "dispute_pending_buyer_filing"].includes(status) ? "error"
          : "pending",
    });
    return states;
  }

  function sectionCard(options) {
    const section = el("section", { class: "section-card", id: options.id || null });
    const titleWrap = el("div", { class: "section-title-wrap" },
      el("h3", { text: options.title }),
      options.sub ? el("div", { class: "section-sub", text: options.sub }) : null
    );
    const head = el("div", { class: "section-head" },
      el("span", { class: "section-num", text: String(options.num) }),
      titleWrap,
      options.actions ? el("div", { class: "section-actions" }, options.actions) : null
    );
    section.appendChild(head);
    section.appendChild(el("div", { class: "section-body" }, options.body));
    return section;
  }

  /* ------------------------------------------------------------------ quote data */

  function getQuoteInfo(ticket) {
    const nr = ticket.nl_request;
    if (nr && Array.isArray(nr.quotes) && nr.quotes.length) {
      return {
        quotes: nr.quotes,
        recommendation: nr.recommendation || null,
        parse: nr.parse || null,
        text: nr.text || null,
        taskText: null,
        origin: "request",
      };
    }
    return reconstructQuotes(ticket);
  }

  function reconstructQuotes(ticket) {
    const audit = (state.data && state.data.audit) || [];
    if (!audit.length) return null;
    let policyIndex = -1;
    const eventSeqs = new Set(ticket.events || []);
    for (let i = 0; i < audit.length; i += 1) {
      const entry = audit[i];
      if (eventSeqs.has(entry.seq) && entry.event === "policy_evaluated"
          && entry.data && entry.data.request_id === ticket.request_id) {
        policyIndex = i;
        break;
      }
    }
    if (policyIndex < 0) {
      policyIndex = audit.findIndex((entry) => entry.event === "policy_evaluated"
        && entry.data && entry.data.request_id === ticket.request_id);
    }
    if (policyIndex < 0) return null;

    const request = ticket.request || {};
    for (let i = policyIndex - 1; i >= 0 && i >= policyIndex - 40; i -= 1) {
      const entry = audit[i];
      if (!entry || entry.event !== "quotes_compared") continue;
      const chosen = (entry.data && entry.data.chosen) || {};
      if (chosen.merchant !== request.merchant) continue;
      if (Math.abs(Number(chosen.unit_price) - Number(request.unit_price)) > 0.009) continue;
      const quotes = ((entry.data && entry.data.quotes) || []).map((quote) => {
        const flags = detectInjection(quote.listing);
        const rating = Number(quote.rating);
        return {
          ...quote,
          injection_flags: flags,
          eligible: flags.length === 0 && Number.isFinite(rating) && rating >= 4.0,
        };
      });
      return {
        quotes,
        recommendation: {
          merchant: chosen.merchant,
          reason: (entry.data && entry.data.reason) || "",
          source: "audit snapshot",
        },
        parse: {
          sku: request.sku,
          quantity: request.quantity,
          category: request.category,
          item_name: request.description,
          parser: "offline",
        },
        text: null,
        taskText: findAgentTask(audit, policyIndex, ticket),
        origin: "audit",
      };
    }
    return null;
  }

  function findAgentTask(audit, policyIndex, ticket) {
    const request = ticket.request || {};
    for (let i = policyIndex - 1; i >= 0 && i >= policyIndex - 140; i -= 1) {
      const entry = audit[i];
      if (!entry) continue;
      if (entry.event === "policy_evaluated") break;
      if (entry.event !== "agent_task") continue;
      const scope = entry.data && entry.data.scope;
      if (scope && scope.sku === request.sku
          && Number(scope.max_quantity) === Number(request.quantity)) {
        return entry.data.task || null;
      }
    }
    return null;
  }

  /* ------------------------------------------------------------------ 1. request */

  function requestSection(ticket, info) {
    const request = ticket.request || {};
    const parse = (info && info.parse) || (ticket.nl_request && ticket.nl_request.parse) || {
      sku: request.sku, quantity: request.quantity, category: request.category,
      parser: ticket.source === "llm_agent" ? "llm" : "offline",
    };
    const parser = parse.parser || "offline";
    let text = (info && info.text) || (ticket.nl_request && ticket.nl_request.text) || (info && info.taskText) || null;
    let origin = text ? (info && info.text ? "request" : "task") : "description";
    if (!text) text = request.description || "Scenario-generated request";

    const body = [];
    const quote = el("div", { class: "nl-quote", text });
    if (origin === "task") quote.appendChild(el("span", { class: "nl-origin", text: "Recovered from the agent task recorded in the audit log." }));
    if (origin === "description") quote.appendChild(el("span", { class: "nl-origin", text: "Scenario-created request — the original prompt is not stored on the ticket; showing the parsed item." }));
    body.push(quote);

    body.push(el("div", { class: "parse-strip" },
      el("span", { class: "parse-label", text: "Parsed" }),
      el("span", { class: "parse-chip" }, el("span", { class: "k", text: "SKU" }), el("b", { text: parse.sku || request.sku || "—" })),
      el("span", { class: "parse-chip" }, el("span", { class: "k", text: "Qty" }), el("b", { text: String(parse.quantity || request.quantity || "—") })),
      el("span", { class: "parse-chip" }, el("span", { class: "k", text: "Cat" }), el("b", { text: parse.category || request.category || "—" })),
      el("span", { class: "parse-source" },
        tag("parser: " + parser, parser === "llm" ? "blue" : "slate"))
    ));

    body.push(el("div", { class: "kv-grid" },
      kv("Requested by", request.requester || "—"),
      kv("Department", request.department || "—"),
      kv("Merchant", request.merchant || "—"),
      kv("SKU", request.sku || "—", true),
      kv("Quantity", String(request.quantity || "—")),
      kv("Unit price", fmtMoney(request.unit_price, request.currency))
    ));

    return sectionCard({
      num: 1, id: "request", title: "Request",
      sub: "Natural-language input and the parsed purchase line.",
      actions: tag("source: " + (ticket.source === "llm_agent" ? "LLM agent" : (ticket.source || "employee")), "slate"),
      body,
    });
  }

  function kv(label, value, mono = false) {
    return el("div", { class: "kv" },
      el("div", { class: "k", text: label }),
      el("div", { class: "v" + (mono ? " mono" : ""), text: String(value) })
    );
  }

  /* ------------------------------------------------------------------- 2. quotes */

  function quotesSection(ticket, info) {
    if (!info || !info.quotes || !info.quotes.length) {
      return sectionCard({
        num: 2, id: "quotes", title: "Quote comparison",
        sub: "Merchant quotes, eligibility and untrusted supplier text.",
        body: el("div", { class: "callout callout-info" },
          icon("info"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "Quote snapshot not attached to this ticket" }),
            el("div", { class: "callout-text", text: "Scenario-created requests keep their quote comparison in the quotes_compared audit entry. See the Agent trace below for the supplier listings." })
          )
        ),
      });
    }

    const chosen = (info.recommendation && info.recommendation.merchant) || (ticket.request && ticket.request.merchant);
    const table = el("table", { class: "data-table quote-table" },
      el("thead", {},
        el("tr", {},
          el("th", { text: "Merchant" }),
          el("th", { text: "Unit price" }),
          el("th", { text: "Lead time" }),
          el("th", { text: "Rating" }),
          el("th", { text: "Eligibility" })
        )
      )
    );
    const tbody = el("tbody");
    const flaggedQuotes = [];
    for (const quote of info.quotes) {
      const flags = (quote.injection_flags && quote.injection_flags.length)
        ? quote.injection_flags : detectInjection(quote.listing);
      const rating = Number(quote.rating);
      const eligible = quote.eligible !== undefined
        ? !!quote.eligible
        : (flags.length === 0 && Number.isFinite(rating) && rating >= 4.0);
      const isChosen = quote.merchant === chosen;
      const rowClass = [
        isChosen ? "chosen" : "",
        !eligible ? "excluded" : "",
        flags.length ? "flagged" : "",
      ].filter(Boolean).join(" ");
      if (flags.length) flaggedQuotes.push({ quote, flags });

      const tags = [];
      if (isChosen) tags.push(tag("chosen", "blue"));
      if (flags.length) tags.push(tag("⚠ prompt injection", "red"));

      const reasons = [];
      if (flags.length) reasons.push("Possible prompt injection: " + flags.join(", "));
      if (Number.isFinite(rating) && rating < 4.0) reasons.push("Rating " + rating.toFixed(1) + " < 4.0");

      const eligibilityCell = el("td", {},
        flags.length ? tag("⚠ prompt injection", "red")
          : eligible ? tag("Eligible", "green") : tag("Excluded", "slate"),
        reasons.length ? el("span", { class: "exclusion-reason", text: reasons.join("; ") }) : null
      );

      tbody.appendChild(el("tr", { class: rowClass },
        el("td", {},
          el("div", { class: "merchant-cell" },
            el("span", { class: "merchant-name", text: quote.merchant || "—" }),
            tags.length ? el("div", { class: "merchant-tags" }, tags) : null
          )
        ),
        el("td", { class: "price-cell", text: fmtMoney(quote.unit_price, quote.currency || "USD") }),
        el("td", { text: (quote.lead_time_days === null || quote.lead_time_days === undefined) ? "—" : quote.lead_time_days + " days" }),
        el("td", { class: "rating-cell" },
          Number.isFinite(rating) ? rating.toFixed(1) : "—",
          Number.isFinite(rating) ? el("span", { class: "star", text: " ★" }) : null
        ),
        eligibilityCell
      ));
    }
    table.appendChild(tbody);

    const body = [el("div", { class: "table-wrap" }, table)];

    for (const item of flaggedQuotes) {
      body.push(el("div", { class: "untrusted" },
        el("div", { class: "untrusted-head" },
          icon("alert"),
          el("span", { text: "UNTRUSTED supplier text" }),
          el("span", { class: "untrusted-merchant", text: item.quote.merchant || "supplier" })
        ),
        el("div", { class: "untrusted-body" },
          el("pre", { class: "untrusted-text", text: stripUntrusted(item.quote.listing) }),
          el("div", { class: "untrusted-flags" },
            item.flags.map((flag) => tag(flag, "red")),
            el("span", { class: "exclusion-reason", text: "Data only — never followed as instructions." })
          )
        )
      ));
    }

    if (info.recommendation) {
      const source = info.recommendation.source || "offline";
      body.push(el("div", { class: "recommendation" },
        icon("search"),
        el("div", { class: "rec-body" },
          el("b", { text: info.recommendation.merchant || "—" }),
          " recommended — ",
          info.recommendation.reason || "selected by the procurement agent",
          el("span", { class: "rec-source" },
            tag("source: " + source, source === "llm" ? "blue" : "slate"))
        )
      ));
    }

    if (info.origin === "audit") {
      body.push(el("p", { class: "note", text: "Reconstructed from this request's quotes_compared audit entry and its chosen quote. Supplier text is untrusted data." }));
    }

    return sectionCard({
      num: 2, id: "quotes", title: "Quote comparison",
      sub: info.quotes.length + " supplier quotes · eligibility checks applied before recommendation.",
      actions: info.recommendation ? tag("chosen: " + (info.recommendation.merchant || "—"), "blue") : null,
      body,
    });
  }

  /* ------------------------------------------------------------------- 3. policy */

  function policySection(ticket) {
    const decision = ticket.decision || {};
    const needed = decision.needs_approval_from || [];
    const reasons = decision.reasons || [];
    let tone = "ok";
    let iconName = "check-circle";
    let title = "Allowed by policy";
    if (!decision.allowed && needed.length) {
      tone = "warn"; iconName = "lock"; title = "Approval required";
    } else if (!decision.allowed) {
      tone = "bad"; iconName = "x-circle"; title = "Rejected by policy";
    }

    const callout = el("div", { class: "callout callout-" + tone },
      icon(iconName),
      el("div", { class: "callout-body" },
        el("div", { class: "callout-title", text: title }),
        needed.length
          ? el("div", { class: "callout-text", text: "Needs approval from " + needed.map(roleLabel).join(" and ") + "." })
          : null,
        reasons.length ? el("ul", { class: "callout-list" },
          reasons.map((reason) => el("li", { text: String(reason) }))) : null
      )
    );

    const policy = (state.data && state.data.policy) || {};
    const request = ticket.request || {};
    const detailBits = [
      "Total " + fmtMoney(request.total, request.currency),
      "hard cap " + fmtMoney(policy.per_order_hard_cap),
      request.department + " remaining " + fmtMoney((policy.department_budgets || {})[request.department]),
    ];

    return sectionCard({
      num: 3, id: "policy", title: "Policy decision",
      sub: "Deterministic budgets, category and merchant checks.",
      actions: decision.allowed ? tag("allowed", "green") : (needed.length ? tag("needs approval", "amber") : tag("rejected", "red")),
      body: [callout, el("p", { class: "note", text: detailBits.join(" · ") + "." })],
    });
  }

  /* ---------------------------------------------------------------- 4. approvals */

  function approvalsSection(ticket) {
    const decision = ticket.decision || {};
    const needed = decision.needs_approval_from || [];
    const approvals = ticket.approvals || [];
    const policy = (state.data && state.data.policy) || {};
    const roster = policy.roster || {};
    const body = [];

    if (!needed.length) {
      if (ticket.status === "rejected") {
        body.push(el("div", { class: "callout callout-bad" },
          icon("x-circle"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "No approval path" }),
            el("div", { class: "callout-text", text: "Policy rejected this request before any human approval could be requested." })
          )
        ));
      } else {
        body.push(el("div", { class: "callout callout-ok" },
          icon("check-circle"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "No human approval required" }),
            el("div", { class: "callout-text", text: "The total is within policy thresholds. The agent can proceed to the AP2 mandate and the PayPal order." })
          )
        ));
      }
    } else {
      const list = el("div", { class: "approval-list" });
      for (const role of needed) {
        const person = roster[role] || "—";
        const granted = approvals.includes(role);
        const canApprove = ticket.status === "pending_approval" && !granted;
        const threshold = approvalThreshold(role, policy);
        const sub = role === "finance"
          ? "Required over " + fmtMoney(threshold, "USD", 0) + " or when a department is over budget"
          : "Required for purchases over " + fmtMoney(threshold, "USD", 0);

        const side = el("div", { class: "approval-side" });
        if (granted) side.appendChild(tag("Approved", "green"));
        else if (ticket.status === "pending_approval") side.appendChild(tag("Required", "amber"));
        else if (ticket.status === "rejected") side.appendChild(tag("No path", "slate"));
        else side.appendChild(tag("Not granted", "slate"));

        if (canApprove) {
          const button = el("button", { type: "button", class: "btn btn-primary btn-sm" },
            icon("check-circle"),
            "Approve as " + roleLabel(role) + " (" + person + ")"
          );
          button.addEventListener("click", () => handleApprove(ticket, role, button));
          side.appendChild(button);
        }

        list.appendChild(el("div", { class: "approval-row" + (granted ? " granted" : "") },
          el("span", { class: "approval-icon" }, icon(granted ? "check-circle" : "lock")),
          el("div", { class: "approval-main" },
            el("div", { class: "approval-name" },
              roleLabel(role) + " ",
              el("span", { class: "person", text: "· " + person })
            ),
            el("div", { class: "approval-sub", text: sub })
          ),
          side
        ));
      }
      body.push(list);
    }

    body.push(el("div", { class: "human-note" },
      icon("lock"),
      el("span", { text: "Only humans can approve; the agent has no approve tool. Approvals are recorded in the hash-chained audit log." })
    ));

    return sectionCard({
      num: 4, id: "approvals", title: "Approval flow",
      sub: needed.length
        ? needed.length + " required approver" + (needed.length === 1 ? "" : "s") + " · human-only step."
        : "No approval thresholds triggered.",
      actions: tag(needed.length ? (needed.every((role) => approvals.includes(role)) ? "approved" : "waiting") : "not required",
        needed.length ? (needed.every((role) => approvals.includes(role)) ? "green" : "amber") : "green"),
      body,
    });
  }

  function approvalThreshold(role, policy) {
    const chain = policy.approval_chain || [];
    const found = chain.find((entry) => (Array.isArray(entry) ? entry[1] : entry.role) === role);
    if (found) return Array.isArray(found) ? found[0] : found.threshold;
    return 0;
  }

  /* ------------------------------------------------------------------- 5. mandate */

  function mandateSection(ticket) {
    const mandate = ticket.mandate;
    const status = ticket.status;
    if (!mandate) {
      let text = "The signed AP2 intent mandate will appear here once the purchase is ready to execute.";
      if (status === "rejected") text = "No mandate will be issued — policy rejected this request.";
      else if (status === "pending_approval") text = "The mandate is issued only after all required human approvals are complete.";
      else if (status === "approved") text = "The mandate is issued when the agent prepares the PayPal order.";
      return sectionCard({
        num: 5, id: "mandate", title: "AP2 Intent Mandate",
        sub: "Self-issued Ed25519 intent with x_constraints spend limits.",
        actions: tag("not issued", "slate"),
        body: emptyState("lock", "No mandate yet", text),
      });
    }

    const payload = mandate.payload || {};
    const intent = payload.intent || {};
    const constraints = payload.x_constraints || {};
    const maxAmount = constraints.max_amount || {};
    const verified = !!mandate.verified;
    const body = [];

    body.push(el("div", { class: "mandate-top" },
      el("div", { class: "mandate-id" },
        el("span", { text: "Mandate" }),
        el("code", { text: payload.mandate_id || "—" }),
        copyButton(payload.mandate_id || "", "Copy ID")
      ),
      verified ? tag("Verified ✓", "green") : tag("Unverified", "red")
    ));

    body.push(el("div", { class: "intent-grid" },
      fieldLine("natural_language_description", intent.natural_language_description || "—", "span-3"),
      fieldLine("merchants", (intent.merchants || []).join(", ") || "—"),
      fieldLine("skus", (intent.skus || []).join(", ") || "—"),
      fieldLine("intent_expiry", fmtDate(intent.intent_expiry)),
      fieldLine("requires_refundability", yesNo(intent.requires_refundability)),
      fieldLine("user_cart_confirmation_required", yesNo(intent.user_cart_confirmation_required)),
      fieldLine("issued_at", fmtDate(payload.issued_at))
    ));

    body.push(el("div", { class: "constraint-box" },
      el("div", { class: "constraint-title" }, icon("lock"), el("span", { text: "x_constraints" })),
      el("div", { class: "constraint-grid" },
        fieldLine("max_amount", fmtMoney(maxAmount.value, maxAmount.currency || "USD")),
        fieldLine("max_quantity", String(constraints.max_quantity === undefined ? "—" : constraints.max_quantity)),
        fieldLine("category", constraints.category || "—"),
        fieldLine("approvals", (constraints.approvals || []).length ? constraints.approvals.map(roleLabel).join(", ") : "none")
      )
    ));

    body.push(el("div", { class: "kv-grid", style: { marginTop: "10px" } },
      kv("type", payload.type || "ap2.IntentMandate", true),
      kv("kid", payload.kid || "—", true),
      kv("alg", mandate.alg || "—")
    ));

    body.push(el("div", { class: "sig-row" },
      el("span", { class: "sig-label", text: "signature" }),
      el("code", { class: "sig-value", text: truncateMiddle(mandate.signature || "", 48) }),
      copyButton(mandate.signature || "", "Copy")
    ));

    body.push(el("p", { class: "note", text: "self-issued Ed25519; spend limits are our x_constraints extension." }));

    return sectionCard({
      num: 5, id: "mandate", title: "AP2 Intent Mandate",
      sub: "The signed intent the agent must stay inside when paying.",
      actions: verified ? tag("Verified ✓", "green") : tag("Unverified", "red"),
      body,
    });
  }

  function fieldLine(label, value, extraClass) {
    return el("div", { class: "field-line " + (extraClass || "") },
      el("div", { class: "k", text: label }),
      el("div", { class: "v", text: String(value) })
    );
  }

  function yesNo(value) {
    return value === true ? "Yes" : value === false ? "No" : "—";
  }

  function truncateMiddle(text, max) {
    const s = String(text || "");
    if (s.length <= max) return s;
    const head = Math.ceil((max - 1) / 2);
    const tail = Math.floor((max - 1) / 2);
    return s.slice(0, head) + "…" + s.slice(s.length - tail);
  }

  /* ------------------------------------------------------------------- 6. PayPal */

  function paypalSection(ticket) {
    const request = ticket.request || {};
    const body = [];
    const order = ticket.paypal_order && !ticket.paypal_order.error ? ticket.paypal_order : null;
    const orderError = ticket.paypal_order && ticket.paypal_order.error;

    if (!ticket.order_id) {
      if (ticket.status === "rejected") {
        body.push(el("div", { class: "callout callout-bad" },
          icon("x-circle"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "No PayPal order" }),
            el("div", { class: "callout-text", text: "The request was rejected by policy, so the agent will not create or pay an order." })
          )
        ));
      } else if (ticket.status === "pending_approval") {
        body.push(el("div", { class: "callout callout-warn" },
          icon("lock"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "Waiting for human approvals" }),
            el("div", { class: "callout-text", text: "The PayPal order can only be created after all required approvers sign off." })
          )
        ));
      } else if (ticket.status === "approved") {
        body.push(el("div", { class: "callout callout-info" },
          icon("info"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "Approved — ready to create the PayPal order" }),
            el("div", { class: "callout-text", text: "The agent will mint the AP2 mandate, create an Orders v2 order with experience_context, then wait for the buyer to approve." })
          )
        ));
      } else {
        body.push(el("div", { class: "callout callout-info" },
          icon("info"),
          el("div", { class: "callout-body" },
            el("div", { class: "callout-title", text: "No PayPal order yet" }),
            el("div", { class: "callout-text", text: "An order is created when the request reaches the approved state." })
          )
        ));
      }

      const enabled = ticket.status === "approved";
      const actions = el("div", { class: "pay-actions" });
      const payButton = el("button", { type: "button", class: "btn btn-primary", disabled: !enabled,
        title: enabled ? "Create the PayPal order and wait for buyer approval" : "Approvals are required first" },
        icon("card"), "Pay with PayPal");
      if (enabled) payButton.addEventListener("click", () => handlePay(ticket, false, payButton));
      const tamperButton = el("button", { type: "button", class: "btn btn-danger-outline", disabled: !enabled,
        title: enabled ? "Demo: attempt to charge $150 more than the mandate allows" : "Approvals are required first" },
        icon("alert"), "Try tampered charge (+$150)");
      if (enabled) tamperButton.addEventListener("click", () => handlePay(ticket, true, tamperButton));
      actions.appendChild(payButton);
      actions.appendChild(tamperButton);
      actions.appendChild(el("span", { class: "demo-hint" },
        icon("lock"),
        "The tampered amount is rejected by the AP2 mandate verifier. Payment uses the ticket total of " + fmtMoney(request.total, request.currency) + "."
      ));
      body.push(actions);

      return sectionCard({
        num: 6, id: "paypal", title: "PayPal order",
        sub: "Orders v2 · buyer approval in the simulated PayPal checkout.",
        actions: tag("no order", "slate"),
        body,
      });
    }

    const amount = orderAmount(order, ticket);
    const captureId = ticket.capture_id || captureIdFromOrder(order);
    const links = (order && order.links) || [];
    const orderStatus = order && order.status;

    body.push(el("div", { class: "order-banner" },
      el("div", { class: "order-status-line" },
        el("span", { class: "sig-label", text: "Order" }),
        el("code", { text: ticket.order_id }),
        orderStatus ? orderPill(orderStatus) : tag("status unknown", "slate"),
        el("span", { class: "note", style: { margin: "0" }, text: "Orders v2" })
      ),
      el("span", { class: "order-amount", text: amount })
    ));

    body.push(el("div", { class: "order-grid" },
      kv("Order ID", ticket.order_id, true),
      kv("Order status", orderStatus || "—"),
      kv("Amount", amount),
      kv("Capture ID", captureId || "—", true),
      kv("Charged", ticket.charged === null || ticket.charged === undefined ? "—" : fmtMoney(ticket.charged, request.currency)),
      kv("Created", order && order.create_time ? fmtDate(order.create_time) : "—")
    ));

    if (links.length) {
      body.push(el("div", { class: "order-links" },
        links.map((link) => el("span", { class: "link-chip", title: link.href || "" },
          el("b", { text: link.rel || "link" }),
          el("span", { class: "method", text: link.method || "GET" })
        ))
      ));
    }

    if (orderError) {
      body.push(el("div", { class: "callout callout-warn", style: { marginTop: "10px" } },
        icon("alert"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: "Could not read the PayPal order" }),
          el("div", { class: "callout-text", text: cleanError(orderError) })
        )
      ));
    }

    if (ticket.status === "awaiting_buyer_approval" && ticket.approve_url) {
      body.push(el("div", { class: "approve-cta" },
        el("div", { class: "cta-copy" },
          el("b", { text: "Buyer approval required" }),
          "Open the simulated PayPal checkout in this tab. After approval, PayPal redirects to /paypal/return and the agent captures the payment."
        ),
        el("a", { class: "btn btn-paypal", href: ticket.approve_url },
          "Open PayPal approval →",
          icon("external")
        )
      ));
      body.push(el("p", { class: "note", text: "Local simulated checkout — no real PayPal account or money involved." }));
    } else if (captureId) {
      body.push(el("div", { class: "callout callout-ok", style: { marginTop: "10px" } },
        icon("check-circle"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: "Payment captured" }),
          el("div", { class: "callout-text", text: "Capture " + captureId + " · " + amount + " · buyer " + ((order && order.payer && order.payer.email_address) || "sandbox buyer") + "." })
        )
      ));
    } else if (ticket.status === "buyer_cancelled") {
      body.push(el("div", { class: "callout callout-bad", style: { marginTop: "10px" } },
        icon("x-circle"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: "Buyer cancelled the approval" }),
          el("div", { class: "callout-text", text: "No money moved. The order can still be inspected but cannot be captured." })
        )
      ));
    } else if (orderStatus === "APPROVED") {
      body.push(el("div", { class: "callout callout-info", style: { marginTop: "10px" } },
        icon("info"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: "Buyer approved — capture pending" }),
          el("div", { class: "callout-text", text: "PayPal approved the order; the agent will capture it on the return_url callback." })
        )
      ));
    }

    body.push(el("p", { class: "note", text: "PayPal is running in offline emulation. Order and capture IDs start with MOCK; no real network calls or money." }));

    return sectionCard({
      num: 6, id: "paypal", title: "PayPal order",
      sub: "Orders v2 order, HATEOAS links and buyer approval.",
      actions: orderStatus ? orderPill(orderStatus) : tag("order", "slate"),
      body,
    });
  }

  function orderAmount(order, ticket) {
    try {
      const unit = (order.purchase_units || [])[0];
      if (unit && unit.amount && unit.amount.value !== undefined) {
        return fmtMoney(unit.amount.value, unit.amount.currency_code || "USD");
      }
    } catch (_) { /* fall through */ }
    if (ticket.charged !== null && ticket.charged !== undefined) return fmtMoney(ticket.charged, (ticket.request || {}).currency);
    return fmtMoney((ticket.request || {}).total, (ticket.request || {}).currency);
  }

  function captureIdFromOrder(order) {
    try {
      const caps = (((order.purchase_units || [])[0] || {}).payments || {}).captures || [];
      return caps[0] ? caps[0].id : null;
    } catch (_) {
      return null;
    }
  }

  /* ------------------------------------------------------------------ 7. receiving */

  function receivingSection(ticket) {
    const request = ticket.request || {};
    const body = [];
    const canReceive = ticket.status === "paid" && !ticket.receipt;

    if (ticket.receipt) {
      const receipt = ticket.receipt;
      body.push(el("div", { class: "kv-grid" },
        kv("SKU received", receipt.sku || "—", true),
        kv("Quantity received", String(receipt.quantity)),
        kv("Unit price", fmtMoney(receipt.unit_price, request.currency))
      ));
      if (ticket.reconcile) body.push(reconcileBox(ticket));
    } else if (canReceive) {
      const draft = state.receiveDraft && state.receiveDraft.requestId === ticket.request_id ? state.receiveDraft : null;
      const qtyInput = el("input", {
        type: "number", id: "recv-qty", min: "0", max: "1000", step: "1",
        value: draft ? draft.quantity : String(request.quantity || 1),
      });
      const priceInput = el("input", {
        type: "number", id: "recv-price", min: "0", step: "0.01",
        value: draft ? draft.unit_price : Number(request.unit_price || 0).toFixed(2),
      });
      const saveDraft = () => {
        state.receiveDraft = {
          requestId: ticket.request_id,
          quantity: qtyInput.value,
          unit_price: priceInput.value,
        };
      };
      qtyInput.addEventListener("input", saveDraft);
      priceInput.addEventListener("input", saveDraft);

      const button = el("button", { type: "button", class: "btn btn-primary", id: "record-delivery" },
        icon("box"), "Record delivery");
      button.addEventListener("click", () => handleReceive(ticket, qtyInput, priceInput, button));

      body.push(el("div", { class: "receive-form" },
        el("label", { class: "inline-field" }, el("span", { text: "Delivered quantity" }), qtyInput),
        el("label", { class: "inline-field" }, el("span", { text: "Unit price (USD)" }), priceInput),
        button
      ));
      body.push(el("p", { class: "note", text: "Packing-slip check: a short shipment or overcharge opens a PayPal dispute automatically." }));
    } else if (["closed", "disputed", "discrepancy_flagged", "dispute_pending_buyer_filing"].includes(ticket.status)) {
      body.push(el("div", { class: "callout callout-" + (ticket.status === "closed" ? "ok" : "warn") },
        icon(ticket.status === "closed" ? "check-circle" : "alert"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: ticket.status === "closed" ? "Goods received and closed" : "Receipt discrepancy recorded" }),
          el("div", { class: "callout-text", text: "This scenario recorded the goods receipt through the API; the receipt details are in the goods_received audit entry. " + (ticket.status === "closed" ? "" : "A dispute or human review follows.") })
        )
      ));
    } else {
      let text = "Receiving is available after the payment is captured.";
      if (ticket.status === "pending_approval") text = "Waiting for human approvals before any PayPal order or delivery.";
      else if (ticket.status === "approved") text = "Waiting for the PayPal order and buyer approval.";
      else if (ticket.status === "awaiting_buyer_approval") text = "Available after the buyer approves in PayPal and the payment is captured.";
      else if (ticket.status === "rejected") text = "The request was rejected, so there is no delivery to record.";
      else if (ticket.status === "buyer_cancelled") text = "The buyer cancelled the payment, so there is no order to receive.";
      body.push(el("div", { class: "callout callout-info" },
        icon("clock"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: "Receiving not available yet" }),
          el("div", { class: "callout-text", text })
        )
      ));
    }

    return sectionCard({
      num: 7, id: "receiving", title: "Receiving",
      sub: "Reconcile delivered quantity and unit price against the order.",
      actions: ticket.receipt ? tag("received", "green") : tag("pending", "slate"),
      body,
    });
  }

  function reconcileBox(ticket) {
    const result = ticket.reconcile || {};
    const issues = result.issues || [];
    const refund = Number(result.refund_due || 0);
    const tone = result.ok ? "ok" : refund > 0 ? "bad" : "warn";
    const title = result.ok ? "Delivery matches the order"
      : ticket.status === "disputed" ? "Delivery mismatch — dispute opened"
        : "Delivery mismatch flagged for review";
    const box = el("div", { class: "reconcile-box " + tone },
      el("div", { class: "reconcile-title" },
        icon(result.ok ? "check-circle" : "alert"),
        el("span", { text: title })
      )
    );
    if (issues.length) {
      box.appendChild(el("ul", { class: "reconcile-issues" },
        issues.map((issue) => el("li", { text: String(issue) }))));
    }
    if (refund > 0) {
      box.appendChild(el("div", { class: "reconcile-refund" },
        "Refund due: ", el("b", { text: fmtMoney(refund, (ticket.request || {}).currency) })
      ));
    }
    return box;
  }

  /* -------------------------------------------------------------------- 8. dispute */

  function disputeSection(ticket) {
    if (!ticket.dispute && !ticket.dispute_draft) return null;
    if (ticket.dispute_draft && !ticket.dispute) {
      const draft = ticket.dispute_draft;
      return sectionCard({
        num: 8, id: "dispute", title: "Dispute",
        sub: "Draft claim for the PayPal Resolution Center.",
        actions: tag("draft", "amber"),
        body: [
          el("div", { class: "callout callout-warn" },
            icon("alert"),
            el("div", { class: "callout-body" },
              el("div", { class: "callout-title", text: "Dispute prepared — manual filing required" }),
              el("div", { class: "callout-text", text: "PayPal's public API has no buyer-side dispute-creation endpoint. A human must file this in the sandbox Resolution Center." })
            )
          ),
          el("div", { class: "dispute-grid", style: { marginTop: "10px" } },
            fieldLine("reason", draft.reason || "—"),
            fieldLine("amount", fmtMoney(draft.amount, (ticket.request || {}).currency)),
            fieldLine("status", "DRAFT")
          ),
          el("div", { class: "dispute-message" },
            el("div", { class: "dm-head" }, el("span", { text: "Draft message" })),
            el("div", { class: "dm-text", text: draft.note || "—" })
          ),
          el("div", { class: "manual-step" },
            icon("lock"),
            el("span", {}, el("b", { text: "File in PayPal Resolution Center" }), " (sandbox: manual step)"),
            el("button", { type: "button", class: "btn btn-secondary btn-sm", disabled: true, title: "Manual step in the PayPal sandbox UI" },
              icon("external"), "File in PayPal Resolution Center (sandbox: manual step)")
          )
        ],
      });
    }

    const dispute = ticket.dispute || {};
    const status = dispute.status || "UNKNOWN";
    const resolved = status === "RESOLVED";
    const amount = (dispute.dispute_amount || {}).value;
    const currency = (dispute.dispute_amount || {}).currency_code || (ticket.request || {}).currency || "USD";
    const reason = dispute.reason || "—";
    const body = [];

    body.push(el("div", { class: "dispute-grid" },
      kv("Dispute ID", dispute.dispute_id || "—", true),
      fieldLine("status", status),
      fieldLine("reason", DISPUTE_REASONS[reason] || reason),
      fieldLine("dispute_amount", fmtMoney(amount, currency)),
      fieldLine("life cycle stage", dispute.dispute_life_cycle_stage || "—"),
      fieldLine("seller response due", fmtDate(dispute.seller_response_due_date))
    ));

    if (dispute.dispute_outcome) {
      const outcome = dispute.dispute_outcome || {};
      const refunded = outcome.amount_refunded || {};
      body.push(el("div", { class: "callout callout-ok", style: { marginTop: "10px" } },
        icon("check-circle"),
        el("div", { class: "callout-body" },
          el("div", { class: "callout-title", text: "Dispute resolved" }),
          el("div", { class: "callout-text", text: (outcome.outcome_code || "RESOLVED") + (refunded.value ? " · refunded " + fmtMoney(refunded.value, refunded.currency_code || currency) : "") })
        )
      ));
    }

    const messages = dispute.messages || [];
    if (messages.length) {
      body.push(el("div", { class: "dispute-messages" },
        messages.map((message) => el("div", { class: "dispute-message" },
          el("div", { class: "dm-head" },
            el("span", { text: "Posted by " + (message.posted_by || "UNKNOWN") }),
            el("span", { text: fmtDate(message.time_posted) })
          ),
          el("div", { class: "dm-text", text: message.content || "" })
        ))
      ));
    }

    const actions = el("div", { class: "dispute-actions" });
    if (!resolved) {
      const button = el("button", { type: "button", class: "btn btn-primary btn-sm" },
        icon("check-circle"), "Simulate merchant accepts claim");
      button.addEventListener("click", () => handleResolveDispute(ticket, button));
      actions.appendChild(button);
      actions.appendChild(el("span", { class: "text", text: "POST /v1/customer/disputes/{id}/accept-claim on the emulation." }));
    } else {
      actions.appendChild(tag("Resolved in the buyer's favour", "green"));
    }
    body.push(actions);

    return sectionCard({
      num: 8, id: "dispute", title: "Dispute",
      sub: "Disputes v1 · status, messages and seller response.",
      actions: disputePill(status),
      body,
    });
  }

  /* ----------------------------------------------------------------------- trace */

  function renderTrace() {
    const host = $("#trace-body");
    const meta = $("#trace-meta");
    if (!host) return;
    const all = ((state.data && state.data.traces) || []).slice().reverse();
    const filter = state.traceFilter;
    const shown = filter ? all.filter((trace) => trace.scenario === filter) : all;
    if (meta) {
      meta.textContent = filter
        ? "Filtered to " + scenarioInfo(filter).label + " · " + shown.length + " trace" + (shown.length === 1 ? "" : "s")
        : all.length + " trace" + (all.length === 1 ? "" : "s") + " · newest first · guardrail refusals are red";
    }
    clear(host);

    if (filter) {
      host.appendChild(el("div", { class: "trace-filterbar" },
        el("span", {}, "Scenario filter: ", el("b", { text: scenarioInfo(filter).label })),
        el("button", { type: "button", class: "btn btn-ghost btn-xs", onclick: () => {
          state.traceFilter = null;
          try {
            const url = new URL(window.location.href);
            url.searchParams.delete("trace");
            history.replaceState(null, "", url.toString());
          } catch (_) { /* ignore */ }
          renderTrace();
        } }, "Show all traces")
      ));
    }

    if (!shown.length) {
      host.appendChild(emptyState("history", "No traces to show",
        filter ? "Run replay 4 (red team) or clear the scenario filter." : "Replay a scenario to generate an agent trace."));
      return;
    }

    const list = el("div", { class: "trace-list" });
    for (const trace of shown) {
      const info = scenarioInfo(trace.scenario);
      const item = el("div", { class: "trace-item" });
      item.appendChild(el("div", { class: "trace-head" },
        el("span", { class: "trace-scenario " + (info.tone === "blue" ? "" : info.tone), text: info.full }),
        el("span", { class: "trace-model", text: trace.model || "model" }),
        el("span", { class: "trace-step-count", text: (trace.steps || []).length + " steps" })
      ));
      item.appendChild(el("div", { class: "trace-final" },
        el("span", { class: "final-label", text: "Final" }),
        el("span", { class: "final-text", text: trace.final || "(no final message)" })
      ));

      const stepsHost = el("div", { class: "trace-steps" });
      (trace.steps || []).forEach((step, index) => {
        const blocked = isBlockedStep(step && step.result);
        const stepBox = el("div", { class: "trace-step" + (blocked ? " blocked" : "") });
        const toolCell = el("div", { class: "trace-tool-cell" },
          el("span", { class: "trace-step-num", text: String(index + 1).padStart(2, "0") }),
          el("span", { class: "tool-chip", text: step.tool || "tool" }),
          el("div", { class: "trace-args", text: compactJSON(step.args || {}) })
        );
        const resultCell = el("div", { class: "trace-result" });
        if (blocked) {
          resultCell.appendChild(el("div", { class: "trace-blocked-badge" }, tag("guardrail refusal", "red")));
          resultCell.appendChild(el("p", { class: "reason", text: stepReason(step.result) }));
          const extras = stepResultMeta(step.result);
          if (extras) resultCell.appendChild(el("div", { class: "result-meta", text: extras }));
        } else if (step.tool === "get_quotes" && step.result && Array.isArray(step.result.quotes)) {
          resultCell.appendChild(el("div", { class: "result-meta", text: (step.result.sku || "quotes") + " · " + (step.result.name || "") }));
          resultCell.appendChild(traceQuotes(step.result.quotes));
        } else {
          resultCell.appendChild(el("pre", { text: compactJSON(step.result || {}) }));
        }
        stepBox.appendChild(toolCell);
        stepBox.appendChild(resultCell);
        stepsHost.appendChild(stepBox);
      });
      item.appendChild(stepsHost);
      list.appendChild(item);
    }
    host.appendChild(list);
  }

  function isBlockedStep(result) {
    if (!result || typeof result !== "object") return false;
    if (result.blocked === true || result.error) return true;
    return result.status === "rejected" || result.status === "pending_approval";
  }

  function stepReason(result) {
    if (!result) return "Guardrail refused the tool call.";
    return result.reason || result.error
      || (Array.isArray(result.reasons) ? result.reasons.join("; ") : "")
      || result.note || ("status: " + result.status);
  }

  function stepResultMeta(result) {
    if (!result || typeof result !== "object") return "";
    const bits = [];
    if (result.status) bits.push("status: " + result.status);
    if (result.request_id) bits.push("request: " + result.request_id);
    if (result.order_id) bits.push("order: " + result.order_id);
    if (result.note) bits.push(result.note);
    return bits.join(" · ");
  }

  function traceQuotes(quotes) {
    const wrap = el("div", { class: "trace-quotes" });
    for (const quote of quotes) {
      const flagged = hasInjectionWarning(quote.listing);
      const rating = Number(quote.rating);
      const box = el("div", { class: "trace-quote" + (flagged ? " flagged" : "") });
      box.appendChild(el("div", { class: "tq-head" },
        el("span", { class: "tq-merchant", text: quote.merchant || "—" }),
        el("span", { class: "tq-price", text: fmtMoney(quote.unit_price, quote.currency || "USD") }),
        el("span", { class: "tq-meta", text: (Number.isFinite(rating) ? rating.toFixed(1) + " ★" : "—") + " · " + (quote.lead_time_days === undefined ? "—" : quote.lead_time_days + " days") }),
        flagged ? tag("⚠ prompt injection", "red") : null
      ));
      box.appendChild(el("div", { class: "tq-listing", text: stripUntrusted(quote.listing) }));
      wrap.appendChild(box);
    }
    return wrap;
  }

  /* ----------------------------------------------------------------------- audit */

  function renderAudit() {
    const filtersHost = $("#audit-filters");
    const tableHost = $("#audit-table-wrap");
    const countHost = $("#audit-count");
    if (!filtersHost || !tableHost) return;
    const data = state.data || {};
    const all = (data.audit || []).slice().sort((a, b) => b.seq - a.seq);
    const blockSet = new Set(data.block_events || []);
    const ticket = selectedTicket();
    const eventSet = new Set((ticket && ticket.events) || []);

    const filters = [
      { key: "all", label: "All", count: (data.audit || []).length, danger: false },
      { key: "blocked", label: "Blocked & flagged only", count: blockSet.size, danger: true },
      { key: "current", label: "Current request", count: eventSet.size, danger: false },
    ];
    clear(filtersHost);
    for (const filter of filters) {
      filtersHost.appendChild(el("button", {
        type: "button",
        class: "filter-btn" + (state.auditFilter === filter.key ? " active" : "") + (filter.danger ? " danger" : ""),
        "aria-pressed": state.auditFilter === filter.key ? "true" : "false",
        onclick: () => {
          state.auditFilter = filter.key;
          renderAudit();
        },
      }, filter.label, el("span", { class: "n", text: String(filter.count) })));
    }

    let entries = all;
    if (state.auditFilter === "blocked") entries = entries.filter((entry) => blockSet.has(entry.seq));
    else if (state.auditFilter === "current") {
      entries = ticket ? entries.filter((entry) => eventSet.has(entry.seq)
        || (entry.data && entry.data.request_id === ticket.request_id)
        || (ticket.order_id && entry.data && entry.data.order_id === ticket.order_id)) : [];
    }
    const total = entries.length;
    const limited = entries.slice(0, 200);
    if (countHost) {
      countHost.textContent = state.auditFilter === "current" && !ticket
        ? "Select a request"
        : (limited.length === total
          ? total + " of " + (data.audit || []).length + " entries"
          : "Newest " + limited.length + " of " + total + " entries");
    }

    clear(tableHost);
    if (!limited.length) {
      tableHost.appendChild(emptyState("search", "No entries match this filter",
        state.auditFilter === "current"
          ? "Select a request to inspect its slice of the hash chain."
          : "Run a scenario that triggers a blocked or flagged event."));
      return;
    }

    const table = el("table", { class: "audit-table" },
      el("thead", {},
        el("tr", {},
          el("th", { class: "col-seq", text: "Seq" }),
          el("th", { class: "col-time", text: "Time" }),
          el("th", { class: "col-event", text: "Event" }),
          el("th", { text: "Key data" }),
          el("th", { class: "col-hash", text: "Hash / prev" })
        )
      )
    );
    const tbody = el("tbody");
    for (const entry of limited) {
      const isBlock = blockSet.has(entry.seq);
      const row = el("tr", { class: isBlock ? "blocked-row" : "", title: isBlock ? "Blocked / flagged event" : "" });
      row.appendChild(el("td", { class: "col-seq" }, el("span", { class: "audit-seq", text: String(entry.seq) })));
      row.appendChild(el("td", { class: "col-time" }, el("span", { class: "audit-time", text: fmtTime(entry.ts), title: entry.ts || "" })));
      row.appendChild(el("td", { class: "col-event" },
        el("span", { class: "event-name" },
          isBlock ? icon("alert") : null,
          el("span", { text: entry.event || "unknown" })
        )
      ));
      row.appendChild(el("td", {}, el("div", { class: "audit-data", text: summarizeEvent(entry) })));
      row.appendChild(el("td", { class: "col-hash" },
        el("span", { class: "audit-hash", text: shortHash(entry.hash, 10), title: entry.hash || "" }),
        el("span", { class: "audit-prev", text: "prev " + shortHash(entry.prev, 8), title: entry.prev || "" })
      ));
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    tableHost.appendChild(table);
  }

  function summarizeEvent(entry) {
    const data = entry.data || {};
    let text = "";
    switch (entry.event) {
      case "quotes_compared": {
        const chosen = data.chosen || {};
        text = (chosen.merchant || "—") + " · " + fmtMoney(chosen.unit_price, chosen.currency || "USD")
          + " · " + clip(data.reason || "chosen quote", 90);
        break;
      }
      case "policy_evaluated":
        text = (data.request_id || "—") + " · " + (data.merchant || "—") + " · " + fmtMoney(data.total)
          + " · " + (data.allowed ? "allowed" : "needs " + ((data.needs_approval_from || []).join(", ") || "review"));
        break;
      case "purchase_blocked":
        text = (data.request_id || "—") + " · " + (data.status || "blocked") + " · " + clip((data.reasons || []).join("; "), 100);
        break;
      case "approval_granted":
        text = (data.request_id || "—") + " · " + (data.role || "role") + " " + (data.by || "approver");
        break;
      case "mandate_issued":
        text = (data.request_id || "—") + " · " + (data.mandate_id || "—");
        break;
      case "mandate_verified":
        text = (data.mandate_id || "—") + " · " + fmtMoney(data.amount) + " within constraint";
        break;
      case "paypal_order_created":
        text = (data.order_id || "—") + " · " + (data.status || "—") + " · " + (data.mode || "—");
        break;
      case "order_readback_verified":
        text = (data.order_id || "—") + " · readback amount " + ((data.paypal_amount || {}).value || "—") + " " + ((data.paypal_amount || {}).currency_code || "");
        break;
      case "order_readback_mismatch":
        text = (data.order_id || "—") + " · PayPal amount " + ((data.paypal_amount || {}).value || "—") + " does not match mandate";
        break;
      case "paypal_order_captured":
        text = (data.order_id || "—") + " · capture " + (data.capture_id || "—") + " · " + (data.status || "—");
        break;
      case "paypal_buyer_approved":
        text = (data.order_id || "—") + " · approved via " + (data.via || data.mode || "PayPal");
        break;
      case "paypal_return_url_hit":
        text = (data.order_id || "—") + " · PayerID " + (data.payer_id || "—");
        break;
      case "paypal_buyer_cancelled":
        text = (data.order_id || "—") + " · buyer cancelled approval";
        break;
      case "goods_received": {
        const result = data.result || {};
        const receipt = data.receipt || {};
        text = (receipt.quantity === undefined ? "—" : receipt.quantity + " received") + " · "
          + (result.ok ? "matched" : "issues: " + clip((result.issues || []).join("; "), 90));
        break;
      }
      case "dispute_opened":
        text = (data.dispute_id || "—") + " · " + fmtMoney(data.amount) + " · " + clip((data.issues || []).join("; "), 80);
        break;
      case "dispute_status_changed":
        text = (data.dispute_id || "—") + " · " + (data.status || "—") + " · via " + (data.via || "—");
        break;
      case "dispute_prepared_for_buyer":
        text = (data.order_id || "—") + " · draft " + fmtMoney(data.amount) + " · manual filing required";
        break;
      case "untrusted_content_flagged":
        text = (data.merchant || "supplier") + " · " + clip((data.indicators || []).join(", "), 100);
        break;
      case "tool_call_blocked":
      case "quarantined_merchant":
      case "scope_violation":
      case "execution_refused":
      case "payment_blocked_by_mandate":
        text = (data.tool ? data.tool + " · " : "") + clip(data.reason || (data.reasons || []).join("; "), 120);
        break;
      case "agent_task":
        text = clip(data.task || "—", 90) + " · scope " + ((data.scope || {}).sku || "—")
          + " × " + ((data.scope || {}).max_quantity === undefined ? "—" : (data.scope || {}).max_quantity);
        break;
      case "agent_tool_call":
        text = (data.tool || "tool") + " · " + clip(compactJSON(data.args || {}), 80) + (data.blocked ? " · BLOCKED" : "");
        break;
      case "agent_started":
        text = (data.paypal_mode || "—") + " · guards " + Object.entries(data.guards || {}).map(([k, v]) => k + "=" + (v ? "on" : "off")).join(", ");
        break;
      case "agent_finished":
        text = "final: " + clip(data.final || "—", 90);
        break;
      default:
        text = genericEventData(data);
    }
    return text || "—";
  }

  function genericEventData(data) {
    const keys = ["request_id", "order_id", "merchant", "tool", "status", "role", "by", "amount", "reason", "total", "source", "mode", "capture_id", "mandate_id", "dispute_id"];
    const bits = [];
    for (const key of keys) {
      if (data[key] === null || data[key] === undefined || typeof data[key] === "object") continue;
      bits.push(key + "=" + String(data[key]));
    }
    const text = bits.length ? bits.join(" · ") : compactJSON(data);
    return clip(text, 130);
  }

  function renderAuditResult() {
    const host = $("#audit-result");
    if (!host) return;
    clear(host);
    if (!state.auditResult) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    const result = state.auditResult;
    const tone = result.tone || "info";
    const callout = el("div", { class: "callout callout-" + tone },
      icon(tone === "ok" ? "check-circle" : "alert"),
      el("div", { class: "callout-body" },
        el("div", { class: "callout-title", text: result.title }),
        result.text ? el("div", { class: "callout-text", text: result.text }) : null,
        result.code ? el("div", { class: "callout-text" }, el("code", { text: result.code })) : null,
        result.foot ? el("div", { class: "note", text: result.foot }) : null
      )
    );
    host.appendChild(callout);
  }

  /* ---------------------------------------------------------------------- replay */

  function renderReplayState() {
    const host = $("#last-replay");
    if (!host) return;
    clear(host);
    const replays = (state.data && state.data.replays) || [];
    if (!replays.length) {
      host.appendChild(el("p", { class: "hint", text: "No scenario has been run yet." }));
      return;
    }
    const last = replays[replays.length - 1];
    const info = scenarioInfo(last.scenario);
    const from = Number(last.audit_from || 0);
    const to = Math.max(from, Number(last.audit_to || from) - 1);
    host.appendChild(el("div", { class: "replay-last-line" },
      icon("history"),
      el("span", {},
        "Last run: ",
        el("b", { text: info.label }),
        " · audit #" + from + "–#" + to
      )
    ));
    host.appendChild(el("details", { class: "raw-log" },
      el("summary", { text: "Raw log" }),
      el("pre", { text: last.log || "(no output)" })
    ));
  }

  /* --------------------------------------------------------------------- actions */

  async function handleNewRequest(event) {
    event.preventDefault();
    const textarea = $("#req-text");
    const button = $("#ask-btn");
    const text = (textarea && textarea.value || "").trim();
    if (!text) {
      toast("Add a request", "Describe what the company should buy, then ask the agent.", "warn", 4200);
      if (textarea) textarea.focus();
      return;
    }
    const requester = ($("#req-requester") && $("#req-requester").value.trim()) || "dana";
    const department = ($("#req-department") && $("#req-department").value) || "ops";
    setGlobalBusy(true);
    setBusy(button, "Asking the agent…");
    try {
      const ticket = await api("/api/request", {
        method: "POST",
        body: { text, requester, department },
      });
      if (textarea) textarea.value = "";
      state.selectedId = ticket.request_id;
      state.receiveDraft = null;
      await refreshState({ select: ticket.request_id });
      toast("Request created",
        ticket.request_id + " · " + (ticket.request || {}).description + " · " + fmtMoney((ticket.request || {}).total, (ticket.request || {}).currency),
        "success");
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function handleReplay(scenario, button, reset) {
    if (state.busy) return;
    setGlobalBusy(true);
    setBusy(button, "Running…");
    try {
      const result = await api("/api/replay", { method: "POST", body: { scenario, reset: !!reset } });
      state.auditResult = null;
      if (reset) state.selectedId = null;
      state.receiveDraft = null;
      await refreshState();
      const info = scenarioInfo(scenario === "all" ? "all" : scenario);
      const label = scenario === "all" ? "All scenarios" : info.label;
      const count = Array.isArray(result && result.replays) ? result.replays.length : 1;
      toast("Replay complete", label + (reset ? " · fresh state" : "") + " · " + count + " scenario" + (count === 1 ? "" : "s") + " appended.", "success", 5200);
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function handleReset(button) {
    if (state.busy) return;
    setGlobalBusy(true);
    setBusy(button, "Resetting…");
    try {
      await api("/api/reset", { method: "POST" });
      state.selectedId = null;
      state.receiveDraft = null;
      state.auditResult = null;
      await refreshState();
      toast("Demo reset", "All requests, traces and audit entries have been cleared.", "info", 4200);
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function handleApprove(ticket, role, button) {
    if (state.busy) return;
    setGlobalBusy(true);
    setBusy(button, "Approving…");
    try {
      const updated = await api("/api/approve", {
        method: "POST",
        body: { request_id: ticket.request_id, role },
      });
      await refreshState({ select: ticket.request_id });
      const remaining = (updated.decision && updated.decision.needs_approval_from || [])
        .filter((needed) => !(updated.approvals || []).includes(needed));
      if (remaining.length) {
        toast("Approval recorded",
          roleLabel(role) + " approved. Still waiting for " + remaining.map(roleLabel).join(" and ") + ".",
          "success");
      } else {
        toast("Approval complete",
          "All required approvers have approved " + ticket.request_id + ". The agent can create the PayPal order.",
          "success");
      }
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function handlePay(ticket, tampered, button) {
    if (state.busy) return;
    setGlobalBusy(true);
    setBusy(button, tampered ? "Testing guardrail…" : "Creating order…");
    try {
      const body = { request_id: ticket.request_id };
      if (tampered) {
        const total = Number((ticket.request || {}).total || 0);
        body.charge_amount = Math.round((total + 150) * 100) / 100;
      }
      const updated = await api("/api/pay", { method: "POST", body });
      await refreshState({ select: ticket.request_id });
      toast("PayPal order created",
        "Order " + (updated.order_id || "—") + " · awaiting buyer approval. Open the simulated PayPal checkout to approve.",
        "success", 7000);
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function handleReceive(ticket, quantityInput, priceInput, button) {
    if (state.busy) return;
    const quantity = Number(quantityInput.value);
    const unitPrice = Number(priceInput.value);
    if (!Number.isInteger(quantity) || quantity < 0) {
      toast("Check the quantity", "Delivered quantity must be a whole number of zero or more.", "warn");
      quantityInput.focus();
      return;
    }
    if (!Number.isFinite(unitPrice) || unitPrice < 0) {
      toast("Check the unit price", "Unit price must be zero or more.", "warn");
      priceInput.focus();
      return;
    }
    setGlobalBusy(true);
    setBusy(button, "Recording…");
    try {
      const updated = await api("/api/receive", {
        method: "POST",
        body: { request_id: ticket.request_id, quantity, unit_price: unitPrice },
      });
      state.receiveDraft = null;
      await refreshState({ select: ticket.request_id });
      const reconcile = updated.reconcile || {};
      if (reconcile.ok) {
        toast("Delivery recorded", "Goods receipt matches the order; the request is closed.", "success");
      } else if (updated.dispute) {
        toast("Delivery mismatch", "A PayPal dispute was opened automatically. Refund due " + fmtMoney(reconcile.refund_due, (ticket.request || {}).currency) + ".", "warn", 8000);
      } else {
        toast("Delivery mismatch flagged", "No refund is due; the discrepancy was flagged for a human.", "warn", 7000);
      }
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function handleResolveDispute(ticket, button) {
    if (state.busy) return;
    setGlobalBusy(true);
    setBusy(button, "Resolving…");
    try {
      const updated = await api("/api/dispute/resolve", {
        method: "POST",
        body: { request_id: ticket.request_id },
      });
      await refreshState({ select: ticket.request_id });
      const status = (updated.dispute && updated.dispute.status) || "RESOLVED";
      toast("Dispute resolved", "Merchant accepted the claim — dispute status " + status + ".", "success", 6500);
    } catch (error) {
      handleError(error);
      await refreshStateQuietly();
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function verifyChain(button) {
    if (state.busy) return;
    setGlobalBusy(true);
    setBusy(button, "Verifying…");
    try {
      const result = await api("/api/audit/verify");
      state.auditResult = result.valid
        ? {
          tone: "ok",
          title: "Hash chain valid · " + result.length + " entries",
          text: "Every entry's prev-hash link and SHA-256 hash was verified.",
          code: "head " + (result.head || "—"),
          foot: "The live log was read only; nothing was modified.",
        }
        : {
          tone: "bad",
          title: "Chain BROKEN at #" + result.first_bad_seq,
          text: "The entry hash or prev link does not match. " + result.length + " entries scanned.",
          foot: "The live log was read only; nothing was modified.",
        };
      renderAuditResult();
    } catch (error) {
      handleError(error);
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  async function tamperCheck(button) {
    if (state.busy) return;
    const length = (state.data && state.data.audit && state.data.audit.length) || 0;
    if (!length) {
      toast("No audit entries", "Replay a scenario before running the tamper test.", "warn");
      return;
    }
    const seq = Math.floor(length / 2);
    setGlobalBusy(true);
    setBusy(button, "Testing…");
    try {
      const result = await api("/api/audit/tamper-check?seq=" + seq);
      const verified = result.result || {};
      state.auditResult = {
        tone: "bad",
        title: "Edited entry #" + result.tampered_seq + " → chain breaks at #" + verified.first_bad_seq,
        text: "Verification ran against a tampered copy of the log. The detector found the altered entry.",
        foot: "The live audit log is untouched — this was a read-only demonstration.",
      };
      renderAuditResult();
    } catch (error) {
      handleError(error);
    } finally {
      setGlobalBusy(false);
      clearBusy(button);
    }
  }

  /* ------------------------------------------------------------------------ boot */

  function bindStatic() {
    const form = $("#request-form-el");
    if (form) form.addEventListener("submit", handleNewRequest);

    $$("#request-form .example").forEach((button) => {
      button.addEventListener("click", () => {
        const textarea = $("#req-text");
        if (textarea) {
          textarea.value = button.dataset.text || "";
          textarea.focus();
        }
        const department = $("#req-department");
        if (department && button.dataset.dept) department.value = button.dataset.dept;
      });
    });

    $$("#scenario-buttons .scenario-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const scenario = button.dataset.scenario || "normal";
        handleReplay(scenario, button, false);
      });
    });

    const replayAll = $("#replay-all");
    if (replayAll) replayAll.addEventListener("click", () => handleReplay("all", replayAll, true));

    const reset = $("#reset-btn");
    if (reset) reset.addEventListener("click", () => handleReset(reset));

    const verify = $("#verify-btn");
    if (verify) verify.addEventListener("click", () => verifyChain(verify));

    const tamper = $("#tamper-btn");
    if (tamper) tamper.addEventListener("click", () => tamperCheck(tamper));

    window.addEventListener("hashchange", () => {
      const id = decodeURIComponent((window.location.hash || "").replace(/^#/, ""));
      if (!id) return;
      if (tickets().some((ticket) => ticket.request_id === id)) {
        state.selectedId = id;
        state.receiveDraft = null;
        renderRequests();
        renderDetail();
        if (state.auditFilter === "current") renderAudit();
      }
    });
  }

  async function runDemo(paid) {
    await api("/api/reset", { method: "POST" });
    let ticket = await api("/api/request", {
      method: "POST",
      body: { text: "Buy 2 laptops for the new engineers", department: "it", requester: "dana" },
    });
    state.selectedId = ticket.request_id;
    if (paid) {
      for (const role of ["manager", "finance"]) {
        ticket = await api("/api/approve", {
          method: "POST",
          body: { request_id: ticket.request_id, role },
        });
      }
      ticket = await api("/api/pay", {
        method: "POST",
        body: { request_id: ticket.request_id },
      });
    }
    state.selectedId = ticket.request_id;
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      if (document.hidden || state.busy) return;
      const active = document.activeElement;
      if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
      try {
        const data = await api("/api/state");
        const json = JSON.stringify(data);
        if (json === state.lastJson) return;
        state.data = data;
        state.lastJson = json;
        ensureSelection();
        render();
      } catch (_) {
        /* polling is best effort — keep the current view */
      }
    }, 4500);
  }

  function renderBootError(error) {
    const host = $("#status-chips");
    if (host) {
      host.replaceChildren(statusChip("Demo server unavailable", "bad", { title: cleanError(error && error.message) }));
    }
    ["#requests-list", "#trace-body"].forEach((selector) => {
      const node = $(selector);
      if (node) node.replaceChildren(emptyState("alert", "Could not load the demo", cleanError(error && error.message)));
    });
    const detail = $("#detail");
    if (detail) detail.replaceChildren(el("div", { class: "card" },
      emptyState("alert", "Could not load request data", "Start web/server.py and reload this page.")));
    const audit = $("#audit-table-wrap");
    if (audit) audit.replaceChildren(emptyState("alert", "Could not load the audit log", "The demo server may not be running."));
    const policy = $("#policy-body");
    if (policy) policy.replaceChildren(emptyState("lock", "Policy unavailable", "The demo server may not be running."));
  }

  async function boot() {
    delete document.body.dataset.ready;
    bindStatic();
    try {
      const replay = params.get("replay");
      const demo = params.get("demo");
      if (replay) {
        const scenario = ["agent_loop", "normal", "over_budget", "mismatch", "red_team", "all"].includes(replay) ? replay : "all";
        await api("/api/replay", { method: "POST", body: { scenario, reset: true } });
      } else if (demo === "laptop" || demo === "laptop-paid") {
        await runDemo(demo === "laptop-paid");
      }

      await refreshState();
      applyInitialSelection();
      render();

      const panel = params.get("panel");
      await nextFrame();
      if (panel) {
        const target = document.getElementById(panel);
        if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
      } else if (window.location.hash) {
        const detail = $("#detail");
        if (detail) detail.scrollIntoView({ behavior: "instant", block: "start" });
      }
      await nextFrame();
      document.body.dataset.ready = "1";
      startPolling();
    } catch (error) {
      console.error(error);
      handleError(error);
      renderBootError(error);
      document.body.dataset.ready = "1";
    }
  }

  boot();
})();
