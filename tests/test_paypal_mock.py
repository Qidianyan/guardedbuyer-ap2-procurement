import os, re, sys, unittest
from decimal import Decimal
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from procure.paypal import MockPayPal, BuyerApprovalRequired, build_order_body
from procure.paypal_mock import MockPayPalServer, MockPayPalClient, PayPalHTTPError

ITEMS = [{"name": "Toner", "sku": "TONER-BK-26A", "unit_price": 55.0, "quantity": 4}]
def body(**kw): return build_order_body(220.0, "USD", "4 x toner", kw.get("ref", "REQ-1"), ITEMS)

class TestOrders(unittest.TestCase):
    def setUp(self): self.s = MockPayPalServer()
    def create(self, b=None, headers=None): return self.s.request("POST", "/v2/checkout/orders", b or body(), headers or {})
    def err(self, fn):
        with self.assertRaises(PayPalHTTPError) as c: fn()
        return c.exception

    def test_create_minimal_payer_action(self):
        o = self.create()
        self.assertEqual(set(o), {"id", "status", "payment_source", "links"})
        self.assertEqual(o["status"], "PAYER_ACTION_REQUIRED")
        self.assertRegex(o["id"], r"^[A-Z0-9]{17,32}$"); self.assertTrue(o["id"].startswith("MOCK"))
        self.assertIn("payer-action", [l["rel"] for l in o["links"]])
    def test_create_without_payment_source(self):
        b = body(); b.pop("payment_source")
        o = self.create(b)
        self.assertEqual(o["status"], "CREATED")
        self.assertTrue({"approve", "update", "capture"} <= {l["rel"] for l in o["links"]})
    def test_idempotency_and_representation(self):
        a = self.create(headers={"PayPal-Request-Id": "X1"}); b = self.create(headers={"PayPal-Request-Id": "X1"})
        self.assertEqual(a["id"], b["id"])
        full = self.create(headers={"Prefer": "return=representation"})
        for k in ("purchase_units", "intent", "create_time"): self.assertIn(k, full)
    def test_validation_errors(self):
        b = body(); b["purchase_units"] = []
        self.assertEqual(self.err(lambda: self.create(b)).status, 400)
        b = body(); b["purchase_units"][0]["items"][0]["quantity"] = "3"
        e = self.err(lambda: self.create(b)); self.assertEqual((e.status, e.issue), (422, "ITEM_TOTAL_MISMATCH"))
    def test_capture_flow(self):
        o = self.create()
        e = self.err(lambda: self.s.request("POST", f"/v2/checkout/orders/{o['id']}/capture", {}))
        self.assertEqual((e.status, e.issue), (422, "ORDER_NOT_APPROVED"))
        self.s.sandbox_buyer_approve(o["id"])
        c = self.s.request("POST", f"/v2/checkout/orders/{o['id']}/capture", {})
        self.assertEqual(c["status"], "COMPLETED")
        cap = c["purchase_units"][0]["payments"]["captures"][0]
        self.assertTrue({"id", "status", "amount", "final_capture", "seller_protection", "seller_receivable_breakdown",
                         "create_time", "update_time", "links"} <= set(cap))
        b = cap["seller_receivable_breakdown"]
        self.assertEqual(Decimal(b["net_amount"]["value"]), Decimal(b["gross_amount"]["value"]) - Decimal(b["paypal_fee"]["value"]))
        e = self.err(lambda: self.s.request("POST", f"/v2/checkout/orders/{o['id']}/capture", {}))
        self.assertEqual(e.issue, "ORDER_ALREADY_CAPTURED")
    def test_unknown_order_404(self):
        self.assertEqual(self.err(lambda: self.s.request("GET", "/v2/checkout/orders/MOCKXXXXXXXXXXXXX")).status, 404)

class TestDisputes(unittest.TestCase):
    def setUp(self):
        self.s = MockPayPalServer()
        self.oid = self.s.request("POST", "/v2/checkout/orders", body(), {})["id"]
    def captured(self):
        self.s.sandbox_buyer_approve(self.oid); self.s.request("POST", f"/v2/checkout/orders/{self.oid}/capture", {})
    def test_errors(self):
        R = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
        with self.assertRaises(PayPalHTTPError) as c: self.s.sandbox_buyer_open_dispute(self.oid, R, "55.00", "n")
        self.assertEqual(c.exception.issue, "ORDER_NOT_CAPTURED")
        self.captured()
        with self.assertRaises(PayPalHTTPError) as c: self.s.sandbox_buyer_open_dispute(self.oid, "BAD", "55.00", "n")
        self.assertEqual(c.exception.status, 400)
        with self.assertRaises(PayPalHTTPError) as c: self.s.sandbox_buyer_open_dispute(self.oid, R, "999.00", "n")
        self.assertEqual(c.exception.status, 422)
    def test_lifecycle(self):
        self.captured()
        d = self.s.sandbox_buyer_open_dispute(self.oid, "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "55.00", "short by one")
        self.assertTrue({"dispute_id", "create_time", "update_time", "disputed_transactions", "reason", "status",
                         "dispute_amount", "dispute_life_cycle_stage", "dispute_channel", "messages",
                         "seller_response_due_date", "links"} <= set(d))
        self.assertEqual(d["messages"][0]["posted_by"], "BUYER")
        self.assertEqual(d["dispute_amount"], {"currency_code": "USD", "value": "55.00"})
        did = d["dispute_id"]
        self.assertEqual(self.s.request("GET", f"/v1/customer/disputes/{did}")["dispute_id"], did)
        items = self.s.request("GET", "/v1/customer/disputes?dispute_state=REQUIRED_OTHER_PARTY_ACTION")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]), {"dispute_id", "create_time", "update_time", "reason", "status", "dispute_state",
                                         "dispute_amount", "dispute_life_cycle_stage", "dispute_channel",
                                         "seller_response_due_date", "links"})
        self.assertEqual(self.s.request("GET", "/v1/customer/disputes?dispute_state=RESOLVED")["items"], [])
        self.s.request("POST", f"/v1/customer/disputes/{did}/accept-claim", {"note": "refund issued"})
        g = self.s.request("GET", f"/v1/customer/disputes/{did}")
        self.assertEqual(g["status"], "RESOLVED"); self.assertEqual(g["dispute_outcome"]["outcome_code"], "RESOLVED_BUYER_FAVOUR")
        with self.assertRaises(PayPalHTTPError) as c: self.s.request("GET", "/v1/customer/disputes/PP-D-NOPE")
        self.assertEqual(c.exception.status, 404)

class TestAdapter(unittest.TestCase):
    def test_build_order_body(self):
        b = body(); pu = b["purchase_units"][0]
        self.assertEqual(pu["amount"]["value"], "220.00"); self.assertEqual(pu["items"][0]["quantity"], "4")
        self.assertEqual(pu["amount"]["breakdown"]["item_total"]["value"], "220.00")
        with self.assertRaises(ValueError): build_order_body(230.0, "USD", "x", "R", ITEMS)
    def test_mockpaypal_flow(self):
        p = MockPayPal(); o = p.create_order(220.0, "USD", "4 x toner", "REQ-9", ITEMS)
        self.assertTrue(o["approve_url"].startswith("https://www.sandbox.paypal.com/checkoutnow?token="))
        with self.assertRaises(BuyerApprovalRequired): p.capture_order(o["id"])
        p.buyer_approve(o["id"]); c = p.capture_order(o["id"])
        self.assertEqual(c["status"], "COMPLETED"); self.assertTrue(c["capture_id"].startswith("MOCK"))
        self.assertEqual(p.get_order(o["id"])["status"], "COMPLETED")
        d = p.open_dispute(o["id"], "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", 55.0, "USD", "short")
        self.assertEqual(p.get_dispute(d["dispute_id"])["status"], "WAITING_FOR_SELLER_RESPONSE")
        self.assertEqual(len(p.list_disputes(dispute_state="REQUIRED_OTHER_PARTY_ACTION")["items"]), 1)
    def test_client_roundtrip(self):
        s = MockPayPalServer(); cl = MockPayPalClient(s)
        o = cl.post("/v2/checkout/orders", body())
        self.assertEqual(cl.get(f"/v2/checkout/orders/{o['id']}")["id"], o["id"])

if __name__ == "__main__": unittest.main()
