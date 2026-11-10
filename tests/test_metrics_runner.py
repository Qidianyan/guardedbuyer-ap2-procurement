import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from metrics.cases import build_cases
from metrics.run_metrics import run_case, aggregate, CONFIGS

def pick(cases, sub): return next(c for c in cases if c["subtype"] == sub)

class RunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.cases = build_cases()

    def test_normal_paid_everywhere(self):
        c = pick(self.cases, "needs_finance")
        for cfg in CONFIGS:
            r = run_case(c, cfg); self.assertTrue(r["legit_paid"], cfg); self.assertIsNone(r["error"]); self.assertTrue(r["chain_valid"])

    def test_ablation_order(self):
        for sub, expect in (("off_scope_item", (False, False, True)), ("blocked_merchant", (False, True, True)),
                            ("tamper_amount", (False, False, True)), ("backup_stock", (False, False, True))):
            c = pick(self.cases, sub)
            got = tuple(all(a["blocked"] for a in run_case(c, cfg)["attempts"]) for cfg in ("no_guardrails", "policy_only", "full"))
            self.assertEqual(got, expect, sub)

    def test_mismatch_handled_only_with_reconcile(self):
        for sub in ("short", "wrong_sku", "overcharged"):
            c = pick(self.cases, sub)
            self.assertTrue(run_case(c, "full")["dispute_handled"], sub)
            self.assertFalse(run_case(c, "policy_only")["dispute_handled"], sub)

    def test_aggregate(self):
        rows = [run_case(c, cfg) for c in self.cases[:3] + [pick(self.cases, "pay_unapproved")] for cfg in CONFIGS]
        s = aggregate(rows)
        self.assertEqual(set(s), set(CONFIGS))
        self.assertEqual(s["full"]["block_rate"], 1.0); self.assertEqual(s["no_guardrails"]["block_rate"], 0.0)
        self.assertIsNone(s["full"]["dispute_coverage"])

if __name__ == "__main__":
    unittest.main()
