/* Approval desk front end. Vanilla JS; all server text goes through textContent (no innerHTML with data). */
(function () {
  "use strict";
  var SPRITE = "/static/icons/lucide-sprite.svg#lucide-";
  var NS = "http://www.w3.org/2000/svg";
  var state = null, selected = null, pollTimer = null, busy = false;

  function icon(name, cls) {
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("class", "icon" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true");
    var u = document.createElementNS(NS, "use"); u.setAttribute("href", SPRITE + name); s.appendChild(u);
    return s;
  }
  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    });
    (kids || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return el;
  }
  function money(v, cur) {
    var n = Number(v || 0);
    return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (cur ? " " + cur : "");
  }
  function short(x, n) { x = String(x || ""); return x.length > n ? x.slice(0, n) + "…" : x; }
  function time(ts) { return ts ? String(ts).replace("T", " ").replace("Z", " UTC") : ""; }
  function toast(msg, bad) {
    var t = document.getElementById("toast");
    t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.hidden = true; }, 4200);
  }
  function api(method, path, body) {
    return fetch(path, { method: method, headers: { "Content-Type": "application/json" },
                         body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) { var e = new Error(j.error || r.status); e.data = j; throw e; } return j; }); });
  }

  // ---------------------------------------------------------------- status
  function statusInfo(it) {
    var next = (it.approval_chain || []).filter(function (s) { return s.status === "pending"; })[0];
    switch (it.status) {
      case "pending_approval":
        var anyApproved = (it.approval_chain || []).some(function (s) { return s.status === "approved"; });
        return anyApproved ? { cls: "warn", icon: "clock", title: "Awaiting " + (next ? next.role : "approval"),
                               text: "Partly approved. Payment stays blocked until every required approver signs." }
                           : { cls: "bad", icon: "shield-alert", title: "Blocked by policy",
                               text: "The agent may not pay for this request until the required people approve it." };
      case "approved": return { cls: "info", icon: "lock-open", title: "Approved, ready to release",
                                text: "All approvals are in. Releasing signs an AP2 Intent Mandate and creates the PayPal order." };
      case "awaiting_buyer_approval": return { cls: "info", icon: "credit-card", title: "Order created, awaiting the PayPal buyer",
                                               text: "The company's PayPal account approves the order; the agent then captures it." };
      case "paid": case "closed": return { cls: "ok", icon: "circle-check", title: "Paid",
                                           text: "Captured within the signed mandate. Every step is in the audit chain." };
      case "rejected_by_approver": return { cls: "bad", icon: "circle-x", title: "Rejected by approver",
                                            text: "No mandate was signed and no money can move for this request." };
      case "rejected": return { cls: "bad", icon: "circle-x", title: "Rejected by policy", text: "This request cannot be approved." };
      default: return { cls: "info", icon: "receipt", title: it.status, text: "" };
    }
  }

  // ---------------------------------------------------------------- render
  function renderTop() {
    var mode = document.getElementById("mode-chip"), chain = document.getElementById("chain-chip");
    var m = state.paypal_mode || "";
    mode.className = "chip " + (m.indexOf("MOCK") === 0 ? "chip-warn" : "chip-accent");
    mode.lastChild.textContent = m.indexOf("MOCK") === 0 ? "Offline PayPal emulation" : "PayPal " + m;
    var ok = state.chain && state.chain.valid;
    chain.className = "chip " + (ok ? "chip-ok" : "chip-bad");
    chain.lastChild.textContent = ok ? "Audit chain valid · " + state.chain.length + " entries" : "Audit chain BROKEN";
  }

  function renderQueue() {
    var ul = document.getElementById("queue-list"); ul.textContent = "";
    document.getElementById("queue-empty").hidden = state.items.length > 0;
    state.items.forEach(function (it) {
      var s = statusInfo(it);
      var b = h("button", { type: "button", class: "queue-item", "aria-current": it.request_id === selected ? "true" : "false",
                            onclick: function () { select(it.request_id); } }, [
        h("div", { class: "q-top" }, [h("span", { class: "mono", text: it.request_id }),
                                      h("span", { class: "chip chip-" + (s.cls === "info" ? "accent" : s.cls), text: s.title })]),
        h("div", { class: "q-name", text: it.request.quantity + " × " + it.request.description }),
        h("div", { class: "q-top" }, [h("span", { text: it.request.department.toUpperCase() + " · " + it.request.merchant }),
                                      h("span", { class: "q-total", text: money(it.request.total) })])
      ]);
      ul.appendChild(h("li", {}, [b]));
    });
  }

  function panel(step, title, iconName, kids) {
    return h("section", { class: "panel", "aria-label": title }, [
      h("h2", {}, [step ? h("span", { class: "step", text: String(step) }) : null, icon(iconName), title])].concat(kids));
  }
  function kv(pairs) {
    var dl = h("dl", { class: "kv" });
    pairs.forEach(function (p) { if (p) { dl.appendChild(h("dt", { text: p[0] })); dl.appendChild(h("dd", { class: p[2] || "" }, [p[1]])); } });
    return dl;
  }

  function renderDetail() {
    var main = document.getElementById("detail"); main.textContent = "";
    var it = state.items.filter(function (x) { return x.request_id === selected; })[0];
    if (!it) { main.appendChild(h("div", { class: "panel" }, [h("p", { class: "muted", text: "Select a request from the queue." })])); return; }
    var r = it.request, s = statusInfo(it);
    // (a) banner
    main.appendChild(h("div", { class: "banner banner-" + s.cls, role: "status" }, [icon(s.icon), h("div", {}, [
      h("h1", { text: s.title }), h("p", { text: s.text }),
      h("p", { class: "meta", text: it.request_id + " · requested by " + r.requester + " (" + r.department.toUpperCase() + ") · " +
               r.quantity + " × " + r.description + " · " + money(r.total, r.currency) })])]));
    // (b) why blocked
    var b = it.budget, pct = Math.min(100, b.remaining > 0 ? (Math.min(b.total, b.remaining) / Math.max(b.total, b.remaining)) * 100 : 0);
    var overPct = b.overage > 0 ? (b.overage / Math.max(b.total, b.remaining)) * 100 : 0;
    var reasons = h("ul", { class: "reasons" }, (it.decision.reasons || []).map(function (x) { return h("li", {}, [icon("triangle-alert"), x]); }));
    var attempts = (it.blocked_attempts || []).map(function (e) {
      return h("li", {}, [icon("shield-alert"), (e.event === "execution_refused" ? "Agent tried to pay: refused" :
               e.event === "purchase_blocked" ? "Policy blocked the request" : "Payment blocked by mandate") +
               " · " + time(e.ts) + " · audit #" + e.seq]);
    });
    main.appendChild(panel(1, "Why it was blocked", "shield-alert", [
      reasons,
      h("div", { class: "budget", "aria-label": "Budget" }, [
        h("div", { class: "budget-bar" }, [h("div", { class: "budget-fill", style: "width:" + pct.toFixed(1) + "%" }),
          overPct > 0 ? h("div", { class: "budget-over", style: "left:" + (100 - overPct).toFixed(1) + "%;width:" + overPct.toFixed(1) + "%" }) : null]),
        h("div", { class: "budget-legend" }, [
          h("span", {}, ["Request ", h("b", { text: money(b.total) })]),
          h("span", {}, [b.department.toUpperCase() + " budget left at request time ", h("b", { text: money(b.remaining) })]),
          b.overage > 0 ? h("span", {}, ["Over budget by ", h("b", { text: money(b.overage) })]) : null])]),
      attempts.length ? h("ul", { class: "reasons" }, attempts) : null]));
    // (c) request + quote, (d) approval chain
    var nl = it.nl_request || {}, rec = nl.recommendation || {};
    var reqPanel = panel(2, "Request and chosen quote", "receipt", [kv([
      ["Employee text", nl.text || "—"], ["Item", r.quantity + " × " + r.description + " (" + r.sku + ")"],
      ["Category", r.category], ["Supplier", r.merchant + " at " + money(r.unit_price) + " each"],
      ["Why this supplier", rec.reason || "—"], ["Total", money(r.total, r.currency)]])]);
    var steps = h("ol", { class: "chain-steps" }, (it.approval_chain || []).map(function (c) {
      return h("li", { class: "chain-step " + c.status }, [
        h("div", { class: "badge" }, [icon(c.status === "approved" ? "user-check" : c.status === "rejected" ? "user-x" : "clock")]),
        h("div", {}, [h("div", { class: "who", text: c.role.charAt(0).toUpperCase() + c.role.slice(1) + " · " + c.person }),
                      h("div", { class: "when", text: c.status === "pending" ? "Waiting for signature" : c.status + " · " + time(c.ts) }),
                      c.note ? h("div", { class: "note", text: "“" + c.note + "”" }) : null])]);
    }));
    var next = (it.approval_chain || []).filter(function (c) { return c.status === "pending"; })[0];
    var canDecide = next && it.status === "pending_approval";
    var note = h("textarea", { id: "note", placeholder: "Optional note for the audit log, e.g. budget moved from Q1 onboarding" });
    var decide = canDecide ? h("div", { class: "decide" }, [
      h("label", { for: "note", text: "Note (stored in the audit log)" }), note,
      h("div", { class: "row" }, [
        h("button", { type: "button", class: "btn btn-primary", onclick: function () { decideNow(it, next.role, "approve", note.value); } },
          [icon("badge-check"), "Approve as " + next.role + " (" + next.person + ")"]),
        h("button", { type: "button", class: "btn btn-danger-outline", onclick: function () { decideNow(it, next.role, "reject", note.value); } },
          [icon("circle-x"), "Reject"])]),
      h("p", { class: "signin-note", text: "Demo sign-in: you act as the roster approver. In production each approver signs in separately." })])
      : null;
    var chainPanel = panel(3, "Approval chain", "list-checks", [steps, decide]);
    main.appendChild(h("div", { class: "grid-2" }, [reqPanel, chainPanel]));
    // (e) mandate
    var mandateKids;
    if (it.mandate) {
      var p = it.mandate.payload, c = p.x_constraints, chk = it.mandate_check || {};
      mandateKids = [h("p", { class: "lead", text: "Signed AP2 Intent Mandate. PayPal is only called if the payment matches it." }), kv([
        ["Mandate ID", p.mandate_id, "mono"], ["Algorithm", it.mandate.alg + " · key " + p.kid],
        ["Signature", short(it.mandate.signature, 44), "mono"],
        ["Verification", chk.signature_valid ? h("span", { class: "sig-ok" }, [icon("badge-check"), "Signature valid against the company public key"])
                                             : h("span", { class: "sig-bad" }, [icon("circle-x"), "Invalid: " + (chk.error || "")])],
        ["Merchant / SKU", p.intent.merchants.join(", ") + " / " + p.intent.skus.join(", ")],
        ["Max amount", money(c.max_amount.value, c.max_amount.currency)], ["Max quantity", String(c.max_quantity)],
        ["Approvals inside", (c.approvals || []).join(", ") || "none"], ["Expires", time(p.intent.intent_expiry)]])];
    } else {
      var pv = it.mandate_preview, pc = pv.x_constraints;
      mandateKids = [h("p", { class: "lead", text: "Constraints that will be signed on release. The agent cannot change them." }), kv([
        ["Type", pv.type + " · " + pv.alg + " · key " + pv.kid], ["Merchant", pv.intent.merchants.join(", ")],
        ["SKU", pv.intent.skus.join(", ")], ["Max amount", money(pc.max_amount.value, pc.max_amount.currency)],
        ["Max quantity", String(pc.max_quantity)], ["Category", pc.category], ["Requester", pc.requester],
        ["Approvals so far", (pc.approvals || []).join(", ") || "none yet"], ["Valid for", pv.ttl_minutes + " minutes after signing"]])];
    }
    var mandatePanel = panel(4, "AP2 mandate", "signature", mandateKids);
    // (f) release + PayPal
    var ppKids = [];
    if (!it.order_id) {
      ppKids.push(h("p", { class: "lead", text: it.can_release ? "Release signs the mandate, then the agent creates the PayPal order (Orders v2)."
                                                             : "Locked until every required approver has signed." }));
      ppKids.push(h("div", { class: "actions" }, [h("button", { type: "button", class: "btn btn-paypal", disabled: !it.can_release,
        onclick: function () { release(it); } }, [icon(it.can_release ? "lock-open" : "lock"), "Release & place PayPal order"])]));
    } else {
      var o = it.paypal_order || {};
      ppKids.push(kv([["PayPal order", it.order_id, "mono"], ["Order status", o.status || "—"],
                      ["Capture", it.capture_id || "not captured yet", it.capture_id ? "mono" : ""],
                      ["Order amount", it.charged != null ? money(it.charged, r.currency) : "—"]]));
      if (it.status === "awaiting_buyer_approval" && it.approve_url) {
        var sandbox = (state.paypal_mode || "").indexOf("SANDBOX") === 0;
        var acts = [h("a", { class: "btn btn-paypal", href: it.approve_url, target: sandbox ? "_blank" : null, rel: sandbox ? "noopener" : null },
          [icon("external-link"), sandbox ? "Open PayPal sandbox checkout" : "Open PayPal checkout"])];
        if (sandbox) acts.push(h("button", { type: "button", class: "btn btn-secondary", onclick: function () { testCard(it); } },
          [icon("credit-card"), "Approve with sandbox test card"]));
        ppKids.push(h("div", { class: "actions" }, acts));
        if (sandbox) ppKids.push(h("p", { class: "lead", text: "Real PayPal sandbox order. A sandbox buyer can approve it on PayPal, or use PayPal's sandbox test card; capture then runs through the same return_url path." }));
      }
    }
    var ppPanel = panel(5, "PayPal order", "credit-card", ppKids);
    main.appendChild(h("div", { class: "grid-2" }, [mandatePanel, ppPanel]));
    // (g) audit trail
    var rows = (it.audit || []).map(function (e) {
      var ev = e.event, cls = /blocked|refused|denied|mismatch|quarantin/.test(ev) ? "ev-block" : /approval_granted/.test(ev) ? "ev-approve"
              : /mandate/.test(ev) ? "ev-mandate" : /paypal|order|capture/.test(ev) ? "ev-paypal" : "";
      return h("tr", { class: cls }, [h("td", { class: "mono", text: String(e.seq) }), h("td", { text: time(e.ts) }), h("td", { text: ev }),
        h("td", { class: "mono", text: short(e.hash, 10) }), h("td", { class: "mono", text: short(e.prev, 10) })]);
    });
    main.appendChild(panel(6, "Audit trail (hash chain)", "history", [
      h("p", { class: "lead", text: "Each entry commits to the previous entry's hash, so editing any line breaks the chain. " +
               (state.chain.valid ? "Chain verified: valid." : "Chain verification FAILED.") }),
      h("div", { class: "table-wrap" }, [h("table", { class: "audit" }, [
        h("thead", {}, [h("tr", {}, ["Seq", "Time", "Event", "Hash", "Prev"].map(function (x) { return h("th", { scope: "col", text: x }); }))]),
        h("tbody", {}, rows)])])]));
    var poll = it.status === "awaiting_buyer_approval";
    clearTimeout(pollTimer); if (poll) pollTimer = setTimeout(refresh, 3000);
  }

  // ---------------------------------------------------------------- actions
  function select(rid) { selected = rid; if (location.hash !== "#" + rid) history.replaceState(null, "", "#" + rid); render(); }
  function render() { renderTop(); renderQueue(); renderDetail(); }
  function refresh() {
    return api("GET", "/api/approvals").then(function (s) {
      state = s;
      if (!selected || !state.items.some(function (x) { return x.request_id === selected; }))
        selected = state.items.length ? state.items[0].request_id : null;
      render();
    }).catch(function (e) { toast("Could not load: " + e.message, true); });
  }
  function guard(p) { if (busy) return; busy = true; p().finally(function () { busy = false; }); }
  function decideNow(it, role, decision, note) {
    guard(function () {
      return api("POST", "/api/approvals/decide", { request_id: it.request_id, role: role, decision: decision, note: note })
        .then(function () { toast(decision === "approve" ? role + " approved" : role + " rejected the request"); return refresh(); })
        .catch(function (e) { toast(e.message, true); });
    });
  }
  function release(it) {
    guard(function () {
      return api("POST", "/api/approvals/release", { request_id: it.request_id })
        .then(function () { toast("Mandate signed, PayPal order created"); return refresh(); })
        .catch(function (e) { toast((e.data && e.data.blocked ? "Blocked: " : "") + e.message, true); return refresh(); });
    });
  }
  function testCard(it) {
    guard(function () {
      return api("POST", "/api/approvals/sandbox-test-card", { request_id: it.request_id })
        .then(function () { toast("Approved with the sandbox test card and captured"); return refresh(); })
        .catch(function (e) { toast(e.message, true); return refresh(); });
    });
  }
  function loadDemo() {
    guard(function () {
      return api("POST", "/api/approvals/demo", { reset: true })
        .then(function (r) { selected = r.request_id; history.replaceState(null, "", "/approvals#" + r.request_id); return refresh(); })
        .catch(function (e) { toast(e.message, true); });
    });
  }
  document.getElementById("load-demo").addEventListener("click", loadDemo);
  window.addEventListener("hashchange", function () { selected = location.hash.slice(1) || selected; render(); });
  selected = location.hash.slice(1) || null;
  if (/[?&]demo=1\b/.test(location.search) && !sessionStorage.getItem("desk-demo-loaded")) {
    sessionStorage.setItem("desk-demo-loaded", "1"); loadDemo();
  } else { refresh(); }
})();
