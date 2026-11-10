"""Demo web app (stdlib only): dashboard + JSON API + simulated PayPal checkout + return_url callback.

  python3 web/server.py [--port 8000] [--host 127.0.0.1] [--llm] [--toolkit]

PayPal is the offline Orders v2 / Disputes v1 emulation unless PAYPAL_CLIENT_ID/SECRET are set and --sandbox is given.
In emulation the buyer-approval step is shown as a local, clearly labelled "simulated PayPal checkout" page that
redirects to our real return_url handler (/paypal/return), exactly like the sandbox flow.
"""
import contextlib, io, json, os, sys, threading, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs, quote
from html import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scenarios import run_all
from procure import ai, catalog, guard
from procure.mandate import MandateError
from procure.paypal import BuyerApprovalRequired
from procure.paypal_mock import PayPalHTTPError
from procure.tool_agent import ToolAgent, ScriptedLLM

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
BLOCK_EVENTS = {"purchase_blocked", "execution_refused", "payment_blocked_by_mandate", "tool_call_blocked",
                "scope_violation", "quarantined_merchant", "order_readback_mismatch", "untrusted_content_flagged"}

class Demo:
    def __init__(self, llm=None, backend="mock", port=8000):
        self.llm, self.backend, self.port = llm, backend, port
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.a = run_all.build(llm=self.llm, backend=self.backend)
            self.traces, self.requests, self.replays = [], {}, []

    # ---------------- views ----------------
    def _local_approve_url(self, t):
        if not t.get("order_id"): return None
        if self.a.pp.mode.startswith("MOCK"):
            return f"/mock-paypal/checkoutnow?token={quote(t['order_id'])}"
        return t.get("approve_url")

    def _paypal_order(self, t):
        if not t.get("order_id") or not hasattr(self.a.pp, "get_order"): return None
        try: return self.a.pp.get_order(t["order_id"])
        except Exception as e: return {"error": str(e)[:200]}

    def ticket_view(self, t):
        r, d = t["req"], t["decision"]
        rid = t["request_id"]
        m = t.get("mandate")
        dis = t.get("dispute")
        if dis and dis.get("dispute_id") and hasattr(self.a.pp, "get_dispute"):
            with contextlib.suppress(Exception): dis = self.a.pp.get_dispute(dis["dispute_id"])
        events = [e for e in self.a.audit.entries if e["data"].get("request_id") == rid
                  or (t.get("order_id") and e["data"].get("order_id") == t.get("order_id"))]
        return {
            "request_id": rid, "status": t["status"], "source": next((e["data"].get("source") for e in events
                                                                      if e["event"] == "policy_evaluated"), None),
            "request": {"requester": r.requester, "department": r.department, "category": r.category,
                        "description": r.description, "sku": r.sku, "quantity": r.quantity,
                        "unit_price": r.unit_price, "merchant": r.merchant, "currency": r.currency, "total": r.total},
            "decision": {"allowed": d.allowed, "needs_approval_from": d.needs_approval_from, "reasons": d.reasons},
            "approvals": sorted(self.a.approvals.granted.get(rid, set())),
            "mandate": None if not m else {**m, "verified": any(e["event"] == "mandate_verified" and
                                           e["data"].get("mandate_id") == m["payload"]["mandate_id"]
                                           for e in self.a.audit.entries)},
            "order_id": t.get("order_id"), "approve_url": self._local_approve_url(t),
            "paypal_order": self._paypal_order(t), "capture_id": t.get("capture_id"), "charged": t.get("charged"),
            "receipt": t.get("receipt"), "reconcile": t.get("reconcile"),
            "dispute": dis, "dispute_draft": t.get("dispute_draft"),
            "nl_request": self.requests.get(rid),
            "events": [e["seq"] for e in events],
        }

    def state(self):
        with self.lock:
            a = self.a
            entries = a.audit.entries
            return {
                "paypal_mode": a.pp.mode, "llm": repr(self.llm) if self.llm else None,
                "policy": {"department_budgets": a.policy.department_budgets,
                           "allowed_categories": sorted(a.policy.allowed_categories),
                           "blocked_merchants": sorted(a.policy.blocked_merchants),
                           "approval_chain": a.policy.approval_chain, "per_order_hard_cap": a.policy.per_order_hard_cap,
                           "roster": a.approvals.roster},
                "mandate_pubkey": a.issuer.public_key_b64(),
                "tickets": [self.ticket_view(t) for t in reversed(list(a.tickets.values()))],
                "audit": entries, "chain": self.verify(),
                "block_events": [e["seq"] for e in entries if e["event"] in BLOCK_EVENTS],
                "traces": self.traces, "replays": self.replays,
                "catalog": catalog.summary(),
            }

    def verify(self, entries=None):
        import hashlib
        prev = "0" * 64
        for e in (entries if entries is not None else self.a.audit.entries):
            body = {k: v for k, v in e.items() if k != "hash"}
            h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            if e["prev"] != prev or h != e["hash"]:
                return {"valid": False, "first_bad_seq": e["seq"], "length": len(self.a.audit.entries)}
            prev = e["hash"]
        return {"valid": True, "first_bad_seq": None, "length": len(self.a.audit.entries),
                "head": prev}

    # ---------------- actions ----------------
    def new_request(self, text, requester="dana", department="ops"):
        with self.lock:
            need = ai.parse_request(text, self.llm)
            item = catalog.find(need["sku"])
            quotes = []
            for q in item["quotes"]:
                flags = guard.scan_injection(q.get("listing", ""))
                if flags:
                    self.a.audit.record("untrusted_content_flagged", source="supplier_listing", sku=item["sku"],
                                        merchant=q["merchant"], indicators=flags)
                quotes.append({**q, "injection_flags": flags, "eligible": not flags and q["rating"] >= 4.0})
            quote, reason, src = ai.recommend(need, item["quotes"], self.llm)
            req = self.a.pick_quote(requester, department, item["category"], item["name"], item["sku"],
                                    need["quantity"], item["quotes"], chosen=quote, reason=reason)
            t = self.a.submit(req, source="employee")
            self.requests[t["request_id"]] = {"text": text, "parse": need, "quotes": quotes,
                                              "recommendation": {"merchant": quote["merchant"], "reason": reason,
                                                                 "source": src}}
            return self.ticket_view(t)

    def _t(self, rid):
        t = self.a.tickets.get(rid)
        if t is None: raise KeyError(f"unknown request {rid}")
        return t

    def approve(self, rid, role):
        with self.lock:
            t = self._t(rid)
            self.a.approve(t, role, self.a.approvals.roster[role])
            return self.ticket_view(t)

    def pay(self, rid, charge_amount=None):
        with self.lock:
            t = self._t(rid)
            self.a.execute(t, charge_amount=charge_amount, auto_buyer_approve=False, source="employee")
            return self.ticket_view(t)

    def find_by_order(self, order_id):
        return next((t for t in self.a.tickets.values() if t.get("order_id") == order_id), None)

    def buyer_approve(self, order_id):
        with self.lock:
            self.a.pp.buyer_approve(order_id)
            self.a.audit.record("paypal_buyer_approved", mode=self.a.pp.mode, order_id=order_id, via="checkout_page")

    def paypal_return(self, order_id, payer_id=None):
        with self.lock:
            t = self.find_by_order(order_id)
            if t is None: raise KeyError(f"no request for order {order_id}")
            self.a.audit.record("paypal_return_url_hit", order_id=order_id, payer_id=payer_id)
            if t["status"] == "awaiting_buyer_approval":
                self.a.complete_payment(t)
            return t

    def sandbox_test_card(self, request_id):
        """Sandbox mode only: approve the PayPal order with PayPal's sandbox test card (stands in for the buyer's
        click on the sandbox checkout page), then resume exactly like the return_url callback."""
        with self.lock:
            t = self._t(request_id)
            pp = self.a.pp
            if not hasattr(pp, "sandbox_confirm_test_card") or "sandbox" not in (getattr(pp, "base", "") or ""):
                raise PermissionError("sandbox test card is only available with --sandbox")
            if t["status"] != "awaiting_buyer_approval" or not t.get("order_id"):
                raise ValueError(f"request {request_id} is not waiting for buyer approval")
            r = pp.sandbox_confirm_test_card(t["order_id"])
            self.a.audit.record("paypal_sandbox_test_card_approved", order_id=t["order_id"], order_status=r.get("status"))
        return self.paypal_return(t["order_id"], "SANDBOX-TEST-CARD")

    def paypal_cancel(self, order_id):
        with self.lock:
            t = self.find_by_order(order_id)
            self.a.audit.record("paypal_buyer_cancelled", order_id=order_id)
            if t and t["status"] == "awaiting_buyer_approval": t["status"] = "buyer_cancelled"
            return t

    def receive(self, rid, quantity, unit_price=None, sku=None):
        with self.lock:
            t = self._t(rid)
            r = t["req"]
            receipt = {"sku": sku or r.sku, "quantity": int(quantity),
                       "unit_price": float(unit_price if unit_price not in (None, "") else r.unit_price)}
            from procure.receiving import reconcile
            t["receipt"], t["reconcile"] = receipt, reconcile(r, receipt)
            self.a.receive(t, receipt)
            return self.ticket_view(t)

    def resolve_dispute(self, rid):
        """Simulates the merchant accepting the claim (POST /v1/customer/disputes/{id}/accept-claim) in the emulation."""
        with self.lock:
            t = self._t(rid)
            did = t["dispute"]["dispute_id"]
            self.a.pp.server.request("POST", f"/v1/customer/disputes/{did}/accept-claim",
                                     {"note": "Merchant accepted the claim (simulated)"})
            self.a.audit.record("dispute_status_changed", order_id=t["order_id"], dispute_id=did, status="RESOLVED",
                                via="simulated merchant accept-claim")
            return self.ticket_view(t)

    # ---------------- approval desk ----------------
    def _desk_item(self, t):
        from procure.mandate import MandateVerifier, MandateError as _ME
        v = self.ticket_view(t)
        rid, r, a = t["request_id"], t["req"], self.a
        related = [e for e in a.audit.entries if e["data"].get("request_id") == rid
                   or (t.get("order_id") and e["data"].get("order_id") == t.get("order_id"))
                   or (t.get("mandate") and e["data"].get("mandate_id") == t["mandate"]["payload"]["mandate_id"])]
        remaining = a.policy.department_budgets.get(r.department, 0.0)
        if t.get("capture_id"):            # show the budget as it was when the request was blocked
            remaining = round(remaining + (t.get("charged") or 0.0), 2)
        chain = []
        for role in t["decision"].needs_approval_from:
            ev = next((e for e in reversed(related) if e["event"] in ("approval_granted", "approval_denied")
                       and e["data"].get("role") == role), None)
            chain.append({"role": role, "person": a.approvals.roster.get(role),
                          "status": "pending" if ev is None else ("approved" if ev["event"] == "approval_granted" else "rejected"),
                          "note": (ev or {}).get("data", {}).get("note"), "ts": (ev or {}).get("ts")})
        check = None
        if t.get("mandate"):
            try:
                MandateVerifier(a.issuer.public_key_b64()).verify(t["mandate"], merchant=r.merchant, sku=r.sku, amount=r.total,
                                                                  currency=r.currency, quantity=r.quantity)
                check = {"signature_valid": True, "error": None}
            except _ME as e:
                check = {"signature_valid": False, "error": str(e)}
            check.update(kid=t["mandate"]["payload"]["kid"], public_key=a.issuer.public_key_b64())
        v.update(budget={"department": r.department, "remaining": remaining, "total": r.total,
                         "overage": round(max(0.0, r.total - remaining), 2)},
                 approval_chain=chain,
                 blocked_attempts=[e for e in related if e["event"] in ("execution_refused", "purchase_blocked",
                                                                        "payment_blocked_by_mandate")],
                 mandate_preview=None if t.get("mandate") else a.mandate_preview(t),
                 mandate_check=check, audit=related,
                 can_release=t["status"] == "approved" and not t.get("order_id"))
        return v

    def approvals_state(self):
        with self.lock:
            items = [self._desk_item(t) for t in reversed(list(self.a.tickets.values()))
                     if t["decision"].needs_approval_from or t["status"] in ("pending_approval", "rejected",
                                                                            "rejected_by_approver")]
            return {"paypal_mode": self.a.pp.mode, "roster": self.a.approvals.roster,
                    "budgets": self.a.policy.department_budgets, "public_key": self.a.issuer.public_key_b64(),
                    "chain": self.verify(), "items": items}

    def decide(self, rid, role, decision, note=None):
        with self.lock:
            t = self._t(rid)
            person = self.a.approvals.roster[role]
            if decision == "approve": self.a.approve(t, role, person, note=note or None)
            elif decision == "reject": self.a.reject(t, role, person, note=note or None)
            else: raise ValueError("decision must be approve or reject")
            return self._desk_item(t)

    def release(self, rid):
        with self.lock:
            self.pay(rid)
            return self._desk_item(self._t(rid))

    def desk_demo(self, reset=False):
        with self.lock:
            if reset: self.reset()
            v = self.new_request("Buy 2 laptops for the new engineers", "dana", "it")
            try: self.pay(v["request_id"])               # the agent tries to pay anyway -> refused and logged
            except PermissionError: pass
            return {"request_id": v["request_id"]}

    SCENARIOS = {"agent_loop": "scenario_agent_loop", "normal": "scenario_normal", "over_budget": "scenario_over_budget",
                 "mismatch": "scenario_mismatch", "red_team": "scenario_red_team"}

    def replay(self, name):
        with self.lock:
            names = list(self.SCENARIOS) if name == "all" else [name]
            out = []
            for n in names:
                fn = getattr(run_all, self.SCENARIOS[n])
                before = len(self.a.audit.entries)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    res = fn(self.a, self.llm)
                runs = []
                if isinstance(res, dict) and "steps" in res: runs = [("agent", res)]
                elif isinstance(res, dict) and "attacker" in res:
                    runs = [("compromised model (offline attacker simulation)", res["attacker"])]
                    if res.get("llm"): runs.append((repr(self.llm), res["llm"]))
                for label, r in runs:
                    self.traces.append({"scenario": n, "model": label, "final": r["final"],
                                        "steps": r["steps"]})
                rec = {"scenario": n, "log": buf.getvalue(), "audit_from": before, "audit_to": len(self.a.audit.entries)}
                self.replays.append(rec); out.append(rec)
            return out

CONFIG_LABELS = (("no_guardrails", "No guardrails"), ("policy_only", "Policy only"), ("full", "Full guardrails"))

def evaluation_data(root=ROOT):
    """Rows for the /evaluation grid: one per synthetic case, joined from cases_200.json and comparison_200_cases.csv."""
    import csv
    with open(os.path.join(root, "metrics", "cases_200.json"), encoding="utf-8") as f:
        cases = {c["id"]: c for c in json.load(f)}
    with open(os.path.join(root, "metrics", "results_200.json"), encoding="utf-8") as f:
        results = json.load(f)
    rows = []
    with open(os.path.join(root, "docs", "comparison_200_cases.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            c = cases.get(r["id"], {})
            row = {"id": r["id"], "type": r["type"], "subtype": r["subtype"], "attempts": int(r["attempts"] or 0),
                   "task": c.get("task", ""), "department": c.get("department", ""),
                   "legit_total": (c.get("legit") or {}).get("total"),
                   "poisoned_listing": c.get("poisoned_listing", "")}
            for cfg, _ in CONFIG_LABELS:
                blocked = int(r[f"{cfg}_blocked_attempts"] or 0)
                paid = r[f"{cfg}_legit_paid"] == "True"
                disp = r[f"{cfg}_dispute_handled"]
                if r["type"] in ("over_authority", "injection"):
                    out = "blocked" if blocked == row["attempts"] else ("partial" if blocked else "missed")
                elif r["type"] == "mismatch":
                    out = "dispute_drafted" if disp == "True" else "not_claimed"
                else:
                    out = "paid" if paid else "false_block"
                row[cfg] = out
                row[f"{cfg}_blocked_attempts"] = blocked
                row[f"{cfg}_legit_paid"] = paid
            rows.append(row)
    summary = {}
    for cfg, label in CONFIG_LABELS:
        s = results["offline"]["summary"][cfg]
        summary[cfg] = {"label": label, "attack_cases": s["attack_cases"], "attack_cases_blocked": s["attack_cases_blocked"],
                        "unauthorized_attempts": s["unauthorized_attempts"], "attempts_blocked": s["blocked"],
                        "normal_cases": s["normal_cases"], "normal_false_blocks": s["normal_false_blocks"],
                        "mismatch_cases": s["mismatch_cases"], "mismatch_handled": s["mismatch_handled"],
                        "missed_attack_case_ids": s["missed_attack_case_ids"]}
    return {"synthetic": True, "seed": 20261110, "n_cases": len(rows), "generated_at": results.get("generated_at"),
            "summary": summary, "rows": rows}

DEMO = None

def _page(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/paypal_pages.css">
</head><body>{body}</body></html>"""

class Handler(BaseHTTPRequestHandler):
    server_version = "ProcureDemo/1.0"
    def log_message(self, fmt, *args): pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json"
                                                      else body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)

    def _redirect(self, url):
        self.send_response(303); self.send_header("Location", url); self.end_headers()

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in ctype:
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        return json.loads(raw or b"{}")

    def do_GET(self):
        u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"): return self._static("index.html")
            if u.path in ("/approvals", "/approvals.html"): return self._static("approvals.html")
            if u.path == "/api/approvals": return self._send(200, DEMO.approvals_state())
            if u.path in ("/evaluation", "/evaluation.html"): return self._static("evaluation.html")
            if u.path == "/api/evaluation": return self._send(200, evaluation_data())
            if u.path.startswith("/static/"): return self._static(u.path[len("/static/"):])
            if u.path == "/api/state": return self._send(200, DEMO.state())
            if u.path == "/api/audit/verify": return self._send(200, DEMO.verify())
            if u.path == "/api/audit/tamper-check":
                entries = json.loads(json.dumps(DEMO.a.audit.entries, default=str))   # copy; live log untouched
                seq = int(q.get("seq", max(0, len(entries) // 2)))
                if entries: entries[seq]["data"]["tampered_by_demo"] = True
                return self._send(200, {"tampered_seq": seq, "result": DEMO.verify(entries)})
            if u.path == "/mock-paypal/checkoutnow": return self._checkout_page(q.get("token", ""))
            if u.path == "/paypal/return": return self._return_page(q.get("token", ""), q.get("PayerID"))
            if u.path == "/paypal/cancel":
                DEMO.paypal_cancel(q.get("token", ""))
                return self._send(200, _page("Payment cancelled", f"""<main class="pp-card"><h1>Payment cancelled</h1>
<p>The buyer cancelled PayPal order <code>{escape(q.get('token', ''))}</code>. No money moved.</p>
<a class="btn" href="/">Back to dashboard</a></main>"""), "text/html")
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlsplit(self.path)
        try:
            b = self._json_body()
            if u.path == "/api/reset": DEMO.reset(); return self._send(200, {"ok": True})
            if u.path == "/api/request":
                return self._send(200, DEMO.new_request(b["text"], b.get("requester") or "dana", b.get("department") or "ops"))
            if u.path == "/api/approve": return self._send(200, DEMO.approve(b["request_id"], b["role"]))
            if u.path == "/api/pay":
                amt = b.get("charge_amount"); amt = float(amt) if amt not in (None, "") else None
                return self._send(200, DEMO.pay(b["request_id"], amt))
            if u.path == "/api/receive":
                return self._send(200, DEMO.receive(b["request_id"], b["quantity"], b.get("unit_price"), b.get("sku")))
            if u.path == "/api/approvals/decide":
                return self._send(200, DEMO.decide(b["request_id"], b["role"], b.get("decision", "approve"), b.get("note")))
            if u.path == "/api/approvals/release": return self._send(200, DEMO.release(b["request_id"]))
            if u.path == "/api/approvals/sandbox-test-card":
                DEMO.sandbox_test_card(b["request_id"]); return self._send(200, DEMO._desk_item(DEMO._t(b["request_id"])))
            if u.path == "/api/approvals/demo": return self._send(200, DEMO.desk_demo(bool(b.get("reset"))))
            if u.path == "/api/dispute/resolve": return self._send(200, DEMO.resolve_dispute(b["request_id"]))
            if u.path == "/api/replay":
                if b.get("reset"): DEMO.reset()
                return self._send(200, {"replays": DEMO.replay(b.get("scenario", "all"))})
            if u.path == "/mock-paypal/approve":
                token = b.get("token", "")
                DEMO.buyer_approve(token)
                return self._redirect(f"/paypal/return?token={quote(token)}&PayerID=MOCKBUYER0001")
            return self._send(404, {"error": "not found"})
        except (PermissionError, MandateError, BuyerApprovalRequired, PayPalHTTPError, KeyError, ValueError) as e:
            return self._send(409, {"error": f"{type(e).__name__}: {e}", "blocked": isinstance(e, (PermissionError, MandateError))})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _static(self, name):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC) or not os.path.isfile(path): return self._send(404, {"error": "not found"})
        ctype = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                 ".png": "image/png", ".txt": "text/plain"}.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f: return self._send(200, f.read(), ctype)

    def _checkout_page(self, token):
        t = DEMO.find_by_order(token)
        if t is None: return self._send(404, _page("Unknown order", "<main class='pp-card'><h1>Unknown order</h1></main>"), "text/html")
        r = t["req"]
        o = DEMO._paypal_order(t) or {}
        status = o.get("status", "?")
        body = f"""<div class="sim-banner">SIMULATED PayPal sandbox checkout. Offline emulation, no real PayPal account or money involved.</div>
<main class="pp-card"><div class="pp-logo">PayPal <span>sandbox · simulated</span></div>
<p class="muted">Signed in as <b>Sandbox Buyer</b> (sb-buyer@personal.example.com, fictional)</p>
<h1>Pay {escape(r.merchant)}</h1>
<table class="kv"><tr><td>Order</td><td><code>{escape(token)}</code> · status <b>{escape(status)}</b></td></tr>
<tr><td>Item</td><td>{r.quantity} × {escape(r.description)} <span class="muted">({escape(r.sku)})</span></td></tr>
<tr><td>Unit price</td><td>${r.unit_price:,.2f}</td></tr><tr><td>Total</td><td class="big">${r.total:,.2f} {escape(r.currency)}</td></tr>
<tr><td>Request</td><td>{escape(t['request_id'])}</td></tr></table>
<form method="post" action="/mock-paypal/approve"><input type="hidden" name="token" value="{escape(token)}">
<button class="btn pay" type="submit" {'disabled' if status != 'PAYER_ACTION_REQUIRED' else ''}>Pay now</button></form>
<a class="btn ghost" href="/paypal/cancel?token={quote(token)}">Cancel and return to merchant</a>
<p class="muted small">After "Pay now" the order becomes APPROVED and PayPal redirects to the merchant's return_url
(<code>/paypal/return?token=…&amp;PayerID=…</code>), where the agent captures the payment.</p></main>"""
        return self._send(200, _page("PayPal checkout (simulated)", body), "text/html")

    def _return_page(self, token, payer_id):
        try:
            t = DEMO.paypal_return(token, payer_id)
            o = DEMO._paypal_order(t) or {}
            cap = ((o.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}]
            fee = (cap[0].get("seller_receivable_breakdown") or {}).get("paypal_fee", {})
            ok = t["status"] == "paid"
            body = f"""<main class="pp-card"><div class="pp-logo">return_url <span>/paypal/return</span></div>
<h1>{'Payment captured' if ok else 'Payment not completed'}</h1>
<p class="{'ok' if ok else 'bad'}">Request {escape(t['request_id'])} → status <b>{escape(t['status'])}</b></p>
<table class="kv"><tr><td>Query</td><td><code>token={escape(token)}</code> · <code>PayerID={escape(payer_id or '')}</code></td></tr>
<tr><td>PayPal order</td><td><code>{escape(token)}</code> · <b>{escape(o.get('status', '?'))}</b></td></tr>
<tr><td>Capture</td><td><code>{escape(str(cap[0].get('id')))}</code> · {escape(str((cap[0].get('amount') or {}).get('value')))} {escape(str((cap[0].get('amount') or {}).get('currency_code', '')))}</td></tr>
<tr><td>Illustrative fee</td><td>{escape(str(fee.get('value', '-')))} (emulation, not PayPal pricing)</td></tr></table>
<p class="muted small">What happened: PayPal redirected the buyer here after approval → the agent called
<code>POST /v2/checkout/orders/{escape(token)}/capture</code> → budget charged → audit entry written.</p>
<a class="btn" href="/approvals#{escape(t['request_id'])}">Back to approval desk</a>
<a class="btn ghost" href="/#{escape(t['request_id'])}">Back to dashboard</a></main>"""
            return self._send(200, _page("PayPal return_url", body), "text/html")
        except Exception as e:
            return self._send(409, _page("Return error", f"<main class='pp-card'><h1>Could not complete</h1><p class='bad'>{escape(str(e))}</p><a class='btn' href='/'>Back</a></main>"), "text/html")

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--llm", action="store_true"); ap.add_argument("--toolkit", action="store_true")
    ap.add_argument("--sandbox", action="store_true")
    a = ap.parse_args(argv)
    llm = None
    if a.llm:
        from procure.llm import get_llm
        llm = get_llm()
        if not llm: print("LLM not configured; offline fallbacks")
    global DEMO
    os.environ.setdefault("PAYPAL_RETURN_URL", f"http://{a.host}:{a.port}/paypal/return")
    DEMO = Demo(llm, "sandbox" if a.sandbox else ("toolkit" if a.toolkit else "mock"), a.port)
    print(f"Demo on http://{a.host}:{a.port}  (PayPal: {DEMO.a.pp.mode}, LLM: {llm!r})")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()

if __name__ == "__main__":
    main()
