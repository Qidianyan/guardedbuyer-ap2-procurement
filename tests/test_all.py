import os, sys, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from procure.policy import Policy, PurchaseRequest, ApprovalBook
from procure.mandate import MandateIssuer, MandateVerifier, MandateError
from procure.receiving import reconcile
from procure.audit import AuditLog
from procure.paypal import make_client, MockPayPal
from scenarios import run_all

def req(**kw):
    d = dict(requester="d", department="ops", category="office_supplies", description="x", sku="S1",
             quantity=2, unit_price=100.0, merchant="M")
    d.update(kw); return PurchaseRequest(**d)

class TestPolicy(unittest.TestCase):
    def setUp(self): self.p = Policy({"ops": 1500.0}, {"office_supplies"})
    def test_within(self): self.assertTrue(self.p.evaluate(req()).allowed)
    def test_category(self):
        d = self.p.evaluate(req(category="travel")); self.assertFalse(d.allowed); self.assertFalse(d.needs_approval_from)
    def test_chain(self):
        self.assertEqual(self.p.evaluate(req(unit_price=400.0)).needs_approval_from, ["manager"])
        self.assertEqual(self.p.evaluate(req(unit_price=900.0)).needs_approval_from, ["manager", "finance"])
    def test_wrong_approver(self):
        with self.assertRaises(PermissionError): ApprovalBook({"manager": "alice"}).approve("r", "manager", "eve")

class TestMandate(unittest.TestCase):
    def setUp(self):
        self.i = MandateIssuer(); self.v = MandateVerifier(self.i.public_key_b64()); self.r = req()
        self.m = self.i.issue(self.r, {"manager"})
        self.kw = dict(merchant="M", sku="S1", amount=200.0, currency="USD", quantity=2)
    def test_ap2_fields(self):
        self.assertEqual(set(self.m["payload"]["intent"]), {"user_cart_confirmation_required",
            "natural_language_description", "merchants", "skus", "requires_refundability", "intent_expiry"})
    def test_ok(self): self.assertTrue(self.v.verify(self.m, **self.kw))
    def test_over_amount(self):
        with self.assertRaises(MandateError): self.v.verify(self.m, **{**self.kw, "amount": 201})
    def test_merchant(self):
        with self.assertRaises(MandateError): self.v.verify(self.m, **{**self.kw, "merchant": "Evil"})
    def test_tamper(self):
        self.m["payload"]["x_constraints"]["max_amount"]["value"] = 9999
        with self.assertRaises(MandateError): self.v.verify(self.m, **{**self.kw, "amount": 9999})
    def test_expired(self):
        with self.assertRaises(MandateError):
            self.v.verify(self.m, now=datetime.now(timezone.utc) + timedelta(hours=2), **self.kw)
    def test_other_key(self):
        with self.assertRaises(MandateError):
            MandateVerifier(MandateIssuer().public_key_b64()).verify(self.m, **self.kw)

class TestReceivingAudit(unittest.TestCase):
    def test_reconcile(self):
        self.assertTrue(reconcile(req(), {"sku": "S1", "quantity": 2, "unit_price": 100})["ok"])
        r = reconcile(req(), {"sku": "S1", "quantity": 1, "unit_price": 100})
        self.assertFalse(r["ok"]); self.assertEqual(r["refund_due"], 100.0)
    def test_chain(self):
        a = AuditLog(); a.record("x", v=1); a.record("y"); self.assertTrue(a.verify_chain())
        a.entries[0]["data"]["v"] = 2; self.assertFalse(a.verify_chain())
    def test_mock_default(self):
        os.environ.pop("PAYPAL_CLIENT_ID", None); self.assertIsInstance(make_client(), MockPayPal)

class TestScenarios(unittest.TestCase):
    def test_normal(self):
        a = run_all.build(); self.assertEqual(run_all.scenario_normal(a)["status"], "closed")
    def test_over_budget(self):
        a = run_all.build(); t = run_all.scenario_over_budget(a)
        self.assertEqual(t["status"], "closed")
        ev = a.audit.events()
        for e in ("purchase_blocked", "execution_refused", "payment_blocked_by_mandate", "paypal_order_captured"):
            self.assertIn(e, ev)
    def test_mismatch(self):
        a = run_all.build(); t = run_all.scenario_mismatch(a)
        self.assertEqual(t["status"], "disputed"); self.assertIn("dispute_opened", a.audit.events())
        self.assertTrue(a.audit.verify_chain())

    def test_red_team(self):
        a = run_all.build(); out = run_all.scenario_red_team(a)
        self.assertIn("attacker", out)
        paid = [t for t in a.tickets.values() if t["status"] == "paid"]
        self.assertEqual([(t["req"].merchant, t["req"].quantity) for t in paid], [("CableCo", 5)])
        self.assertTrue(a.audit.verify_chain())
    def test_agent_loop(self):
        a = run_all.build(); r = run_all.scenario_agent_loop(a)
        self.assertEqual([t["status"] for t in r["tickets"].values()], ["paid"])

if __name__ == "__main__": unittest.main()
