import json, os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from metrics.cases import build_cases, BASE_ITEMS, INJ_TEMPLATES, OUT
from procure.guard import scan_injection
from procure.policy import PurchaseRequest
from procure.receiving import reconcile

TOOLS = {"search_catalog", "get_quotes", "submit_purchase", "pay_order", "check_request", "approve_purchase"}
SIZES = {"normal": {"small": 20, "needs_manager": 10, "needs_finance": 10},
         "over_authority": {s: 5 for s in ("inflate_quantity", "disallowed_category", "blocked_merchant", "off_scope_item",
                                           "over_hard_cap", "approve_self", "pay_unapproved", "tamper_amount")},
         "injection": {s: 5 for s, _ in INJ_TEMPLATES},
         "mismatch": {"short": 10, "wrong_sku": 10, "overcharged": 10}}

class CaseSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.cases = build_cases()

    def test_counts_ids_determinism(self):
        got = {}
        for c in self.cases: got.setdefault(c["type"], {}).setdefault(c["subtype"], 0); got[c["type"]][c["subtype"]] += 1
        self.assertEqual(got, SIZES)
        self.assertEqual(len({c["id"] for c in self.cases}), len(self.cases))
        self.assertEqual(self.cases, build_cases())
        self.assertTrue(all(c["synthetic"] is True for c in self.cases))

    def test_plans(self):
        for c in self.cases:
            prev_submit = False
            self.assertEqual([s["tool"] for s in c["plan"][:2]], ["search_catalog", "get_quotes"], c["id"])
            for s in c["plan"]:
                self.assertIn(s["tool"], TOOLS)
                if s["tool"] == "submit_purchase": prev_submit = True
                if s["tool"] == "pay_order":
                    self.assertEqual(s["args"]["request_id"], "$prev"); self.assertTrue(prev_submit, c["id"])
            subs = [s for s in c["plan"] if s["tool"] == "submit_purchase"]
            self.assertTrue(any((s["args"]["sku"], s["args"]["quantity"], s["args"]["merchant"]) ==
                                (c["legit"]["sku"], c["legit"]["quantity"], c["legit"]["merchant"]) for s in subs), c["id"])

    def test_legit_purchase_is_clean_and_in_scope(self):
        for c in self.cases:
            L = c["legit"]
            item = next(i for i in c["catalog"] if i["sku"] == L["sku"])
            q = next(q for q in item["quotes"] if q["merchant"] == L["merchant"])
            self.assertAlmostEqual(round(q["unit_price"] * L["quantity"], 2), L["total"])
            self.assertEqual(L["quantity"], c["scope"]["max_quantity"]); self.assertEqual(L["sku"], c["scope"]["sku"])
            self.assertFalse(scan_injection(q["listing"]), c["id"])
            self.assertNotIn(L["merchant"], {"CardsNow", "ShadyDeals"})
            self.assertIn(str(L["quantity"]), c["task"])
            if c["type"] in ("over_authority", "injection"): self.assertTrue(c["unauthorized"], c["id"])
            else: self.assertEqual(c["unauthorized"], [], c["id"])

    def test_detector_truth(self):
        for i in BASE_ITEMS:
            for q in i["quotes"]: self.assertFalse(scan_injection(q["listing"]), q["listing"])
        per_template = {}
        for c in self.cases:
            if c["type"] != "injection": continue
            self.assertEqual(c["detector_should_flag"], bool(scan_injection(c["poisoned_listing"])))
            per_template.setdefault(c["subtype"], set()).add(c["detector_should_flag"])
        self.assertTrue(all(len(v) == 1 for v in per_template.values()))
        self.assertEqual(sorted(v.pop() for v in per_template.values()), [False] * 3 + [True] * 5)

    def test_mismatch_refunds(self):
        for c in self.cases:
            if c["type"] != "mismatch": continue
            L = c["legit"]; item = next(i for i in c["catalog"] if i["sku"] == L["sku"])
            req = PurchaseRequest("dana", c["department"], item["category"], item["name"], L["sku"], L["quantity"],
                                  round(L["total"] / L["quantity"], 2), L["merchant"])
            self.assertAlmostEqual(c["expected_refund"], reconcile(req, c["delivery"])["refund_due"])
            if c["subtype"] == "overcharged": self.assertEqual(c["expected_refund"], 0.0)
            else: self.assertGreater(c["expected_refund"], 0)

    def test_json_file_matches(self):
        with open(OUT) as f: self.assertEqual(json.load(f), self.cases)

if __name__ == "__main__":
    unittest.main()
