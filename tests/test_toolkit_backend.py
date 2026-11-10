import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from procure.paypal_toolkit import toolkit_available
from procure.paypal import BuyerApprovalRequired

ITEMS = [{"name": "Toner", "sku": "TONER-BK-26A", "unit_price": 55.0, "quantity": 4}]

@unittest.skipUnless(toolkit_available(), "paypal-agent-toolkit not installed (see requirements-toolkit.txt)")
class TestToolkitBackend(unittest.TestCase):
    def setUp(self):
        from procure.paypal_toolkit import PayPalToolkitBackend
        self.p = PayPalToolkitBackend.offline()
    def test_allowed_methods_and_schemas(self):
        m = {"create_order", "pay_order", "get_order_details", "get_dispute", "list_disputes"}
        self.assertEqual(self.p.allowed_methods, m)
        self.assertEqual({t["function"]["name"] for t in self.p.openai_tool_schemas()}, m)
    def test_order_and_dispute_through_official_handlers(self):
        o = self.p.create_order(220.0, "USD", "4 x toner", "REQ-1", ITEMS)
        self.assertEqual(o["status"], "PAYER_ACTION_REQUIRED"); self.assertIn("checkoutnow?token=", o["approve_url"])
        with self.assertRaises(BuyerApprovalRequired): self.p.capture_order(o["id"])
        self.p.buyer_approve(o["id"]); c = self.p.capture_order(o["id"])
        self.assertEqual(c["status"], "COMPLETED"); self.assertTrue(c["capture_id"])
        self.assertEqual(self.p.get_order(o["id"])["purchase_units"][0]["amount"]["value"], "220.00")
        d = self.p.open_dispute(o["id"], "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", 55.0, "USD", "short")
        self.assertEqual(self.p.get_dispute(d["dispute_id"])["dispute_id"], d["dispute_id"])
        self.assertEqual(len(self.p.list_disputes()["items"]), 1)
    def test_full_scenario_on_toolkit(self):
        from scenarios import run_all
        a = run_all.build(backend="toolkit")
        self.assertEqual(a.pp.mode, "MOCK/agent-toolkit")
        self.assertEqual(run_all.scenario_mismatch(a)["status"], "disputed")
        self.assertEqual(run_all.scenario_over_budget(a)["status"], "closed")

if __name__ == "__main__": unittest.main()
