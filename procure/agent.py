"""Orchestrator: request -> quotes -> policy -> approvals -> AP2 mandate -> PayPal -> buyer approve
-> capture -> receipt -> dispute.

This class is the deterministic GUARDRAIL layer. Any LLM (see tool_agent.py) can only call into it;
it cannot approve purchases, change policy, or mint mandates with different limits.
"""
import uuid
from .policy import PurchaseRequest
from .mandate import MandateIssuer, MandateVerifier, MandateError
from .receiving import reconcile
from .paypal import BuyerApprovalRequired

FULL_GUARDS = {"policy": True, "mandate": True, "readback": True, "reconcile": True}

class ProcurementAgent:
    def __init__(self, policy, approvals, paypal, audit, issuer=None, dispute_writer=None, guards=None):
        """`guards` exists only for ablation experiments (metrics/); production runs keep every guard on."""
        self.policy, self.approvals, self.pp, self.audit = policy, approvals, paypal, audit
        self.guards = {**FULL_GUARDS, **(guards or {})}
        self.issuer = issuer or MandateIssuer()
        self.verifier = MandateVerifier(self.issuer.public_key_b64())
        self.dispute_writer = dispute_writer          # optional fn(req, issues) -> str (LLM text)
        self.tickets = {}
        audit.record("agent_started", paypal_mode=paypal.mode, mandate_pubkey=self.issuer.public_key_b64(),
                     guards=self.guards)

    def pick_quote(self, requester, department, category, description, sku, quantity, quotes, chosen=None, reason=None):
        best = chosen or min(quotes, key=lambda q: q["unit_price"])
        self.audit.record("quotes_compared", quotes=quotes, chosen=best, reason=reason)
        return PurchaseRequest(requester, department, category, description, sku, quantity,
                               best["unit_price"], best["merchant"])

    def submit(self, req, source="employee"):
        rid = "REQ-" + uuid.uuid4().hex[:8]
        d = self.policy.evaluate(req)
        if not self.guards["policy"]:
            from .policy import Decision
            d = Decision(True, [], ["policy guard disabled (ablation)"])
        self.audit.record("policy_evaluated", request_id=rid, source=source, total=req.total, merchant=req.merchant,
                          category=req.category, allowed=d.allowed, needs_approval_from=d.needs_approval_from,
                          reasons=d.reasons)
        status = "approved" if d.allowed else ("pending_approval" if d.needs_approval_from else "rejected")
        if status != "approved":
            self.audit.record("purchase_blocked", request_id=rid, status=status, reasons=d.reasons)
        t = {"request_id": rid, "req": req, "decision": d, "status": status}
        self.tickets[rid] = t
        return t

    def approve(self, ticket, role, person, note=None):
        """Human approval. Not exposed to the LLM as a tool."""
        self.approvals.approve(ticket["request_id"], role, person)
        extra = {"note": note} if note else {}
        self.audit.record("approval_granted", request_id=ticket["request_id"], role=role, by=person, **extra)
        if ticket["status"] == "pending_approval" and self.approvals.satisfied(
                ticket["request_id"], ticket["decision"].needs_approval_from):
            ticket["status"] = "approved"

    def reject(self, ticket, role, person, note=None):
        """Human rejection. Same roster check as approve(); execute() then refuses (status != approved)."""
        if self.approvals.roster.get(role) != person:
            raise PermissionError(f"{person} is not the {role} approver")
        if ticket.get("order_id"):
            raise ValueError(f"request {ticket['request_id']} already has a PayPal order; cancel it at PayPal instead")
        self.audit.record("approval_denied", request_id=ticket["request_id"], role=role, by=person,
                          **({"note": note} if note else {}))
        ticket["status"] = "rejected_by_approver"
        return ticket

    def mandate_preview(self, ticket):
        """Intent + constraints that would be signed for this ticket with the approvals granted so far."""
        p = self.issuer.build_payload(ticket["req"], self.approvals.granted.get(ticket["request_id"], set()),
                                      mandate_id="(assigned at signing)")
        return {"type": p["type"], "intent": p["intent"], "x_constraints": p["x_constraints"], "kid": p["kid"],
                "ttl_minutes": 60, "alg": "Ed25519"}

    def execute(self, ticket, charge_amount=None, charge_merchant=None, source="employee", auto_buyer_approve=None):
        if ticket["status"] != "approved":
            self.audit.record("execution_refused", request_id=ticket["request_id"], status=ticket["status"], source=source)
            raise PermissionError(f"request {ticket['request_id']} not approved (status={ticket['status']})")
        req = ticket["req"]
        mandate = ticket.get("mandate")
        if mandate is None:
            mandate = self.issuer.issue(req, self.approvals.granted.get(ticket["request_id"], set()))
            ticket["mandate"] = mandate
            self.audit.record("mandate_issued", request_id=ticket["request_id"], mandate_id=mandate["payload"]["mandate_id"],
                              intent=mandate["payload"]["intent"], constraints=mandate["payload"]["x_constraints"])
        amount = req.total if charge_amount is None else charge_amount
        merchant = charge_merchant or req.merchant
        try:
            if self.guards["mandate"]:
                self.verifier.verify(mandate, merchant=merchant, sku=req.sku, amount=amount,
                                     currency=req.currency, quantity=req.quantity)
        except MandateError as e:
            self.audit.record("payment_blocked_by_mandate", request_id=ticket["request_id"], reason=str(e),
                              amount=amount, merchant=merchant, source=source)
            raise
        self.audit.record("mandate_verified", mandate_id=mandate["payload"]["mandate_id"], amount=amount)
        items = [{"name": req.description, "sku": req.sku, "unit_price": req.unit_price, "quantity": req.quantity}]
        if abs(amount - req.total) > 1e-9:   # (only reachable if a caller overrides the amount AND the mandate allows it)
            items = None
        order = self.pp.create_order(amount, req.currency, mandate["payload"]["intent"]["natural_language_description"],
                                     ticket["request_id"], items=items)
        ticket.update(order_id=order["id"], approve_url=order.get("approve_url"), status="awaiting_buyer_approval",
                      charged=amount)
        if self.guards["readback"]:
            self._postcheck_order(ticket, mandate, amount)
        self.audit.record("paypal_order_created", mode=self.pp.mode, order_id=order["id"], status=order.get("status"),
                          approve_url=order.get("approve_url"))
        if auto_buyer_approve is None:
            auto_buyer_approve = self.pp.mode.startswith("MOCK")
        if auto_buyer_approve:
            self.buyer_approve(ticket)
            self.complete_payment(ticket)
        return ticket

    def _postcheck_order(self, ticket, mandate, amount):
        """Read the order back from PayPal and re-check it against the mandate (defence in depth)."""
        get = getattr(self.pp, "get_order", None)
        if get is None: return
        try:
            o = get(ticket["order_id"])
        except Exception as e:                      # read-back is best effort in sandbox
            self.audit.record("order_readback_failed", order_id=ticket["order_id"], error=str(e)[:200]); return
        units = o.get("purchase_units") or (o.get("raw") or {}).get("purchase_units") or []
        if not units: return
        amt = units[0].get("amount", {})
        value, cur = float(amt.get("value", "nan")), amt.get("currency_code")
        cap = mandate["payload"]["x_constraints"]["max_amount"]
        ok = cur == cap["currency"] and value <= cap["value"] + 1e-9 and abs(value - amount) < 0.005
        self.audit.record("order_readback_verified" if ok else "order_readback_mismatch", order_id=ticket["order_id"],
                          paypal_amount=amt, mandate_max=cap)
        if not ok:
            ticket["status"] = "blocked_mismatch"
            raise MandateError(f"PayPal order {ticket['order_id']} amount {amt} does not match mandate {cap}")

    def buyer_approve(self, ticket):
        self.pp.buyer_approve(ticket["order_id"])
        self.audit.record("paypal_buyer_approved", mode=self.pp.mode, order_id=ticket["order_id"])

    def complete_payment(self, ticket):
        try:
            cap = self.pp.capture_order(ticket["order_id"])
        except BuyerApprovalRequired as e:
            self.audit.record("capture_waiting_buyer", order_id=ticket["order_id"], reason=str(e)); raise
        ticket["capture_id"] = cap.get("capture_id")
        self.audit.record("paypal_order_captured", mode=self.pp.mode, order_id=ticket["order_id"], status=cap.get("status"),
                          capture_id=cap.get("capture_id"))
        self.policy.charge(ticket["req"].department, ticket["charged"])
        ticket["status"] = "paid"
        return ticket

    def receive(self, ticket, receipt):
        if not self.guards["reconcile"]:
            self.audit.record("goods_received", order_id=ticket["order_id"], receipt=receipt, result="not checked")
            ticket["status"] = "closed"; return ticket
        r = reconcile(ticket["req"], receipt)
        self.audit.record("goods_received", order_id=ticket["order_id"], receipt=receipt, result=r)
        if r["ok"]:
            ticket["status"] = "closed"; return ticket
        if r["refund_due"] <= 0:
            # e.g. the invoice shows a higher unit price than ordered, but PayPal only charged the mandated amount:
            # nothing to claim back, so flag it for a human instead of filing a zero-amount dispute.
            self.audit.record("receipt_discrepancy_flagged", order_id=ticket["order_id"], issues=r["issues"])
            ticket["status"] = "discrepancy_flagged"; return ticket
        note = "; ".join(r["issues"])
        if self.dispute_writer:
            try: note = self.dispute_writer(ticket["req"], r["issues"]) or note
            except Exception as e: self.audit.record("dispute_writer_failed", error=str(e))
        reason = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
        try:
            dis = self.pp.open_dispute(ticket["order_id"], reason, r["refund_due"], ticket["req"].currency, note)
        except NotImplementedError as e:
            # Real sandbox: PayPal has no public REST endpoint for a BUYER to create a dispute. The agent prepares the
            # claim (reason, amount, message) and a human files it in the Resolution Center; we then track it by id.
            self.audit.record("dispute_prepared_for_buyer", mode=self.pp.mode, order_id=ticket["order_id"],
                              reason=reason, amount=r["refund_due"], issues=r["issues"], note=note, why=str(e))
            ticket.update(status="dispute_pending_buyer_filing",
                          dispute_draft={"reason": reason, "amount": r["refund_due"], "note": note})
            return ticket
        self.audit.record("dispute_opened", mode=self.pp.mode, order_id=ticket["order_id"],
                          dispute_id=dis["dispute_id"], amount=r["refund_due"], issues=r["issues"], note=note)
        ticket.update(status="disputed", dispute=dis)
        return ticket

    def track_dispute(self, ticket, dispute_id=None):
        """After a human filed the drafted claim in PayPal's Resolution Center, find it (by the capture id) and track it."""
        if dispute_id is None:
            cap = ticket.get("capture_id")
            find = getattr(self.pp, "find_disputes_for_capture", None)
            if find is not None:
                items = find(cap)
            else:
                items = (self.pp.list_disputes(disputed_transaction_id=cap) or {}).get("items") or []
            dispute_id = items[0].get("dispute_id") if items else None
        if not dispute_id:
            self.audit.record("dispute_not_yet_filed", order_id=ticket.get("order_id"), capture_id=ticket.get("capture_id"))
            return ticket
        dis = self.pp.get_dispute(dispute_id)
        self.audit.record("dispute_tracked", order_id=ticket.get("order_id"), dispute_id=dispute_id,
                          status=dis.get("status"), mode=self.pp.mode)
        ticket.update(status="disputed", dispute=dis)
        return ticket
