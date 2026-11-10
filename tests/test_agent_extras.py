import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenarios import run_all
from procure.paypal import MockPayPal, BuyerApprovalRequired
from procure.mandate import MandateError
from procure import llm as llm_mod
from procure.llm import load_config, OpenAICompatLLM, _extract_json, LLMError

def toner(a, qty=4):
    item = run_all.catalog.find("TONER-BK-26A")
    req = a.pick_quote("dana", "ops", item["category"], item["name"], item["sku"], qty, item["quotes"])
    return a.submit(req)

class NoBuyerDispute(MockPayPal):
    def open_dispute(self, *a, **k): raise NotImplementedError("no buyer-side create-dispute endpoint")
class LyingReadback(MockPayPal):
    def get_order(self, order_id):
        o = super().get_order(order_id); o["purchase_units"][0]["amount"]["value"] = "999.00"; return o
class SandboxLike(MockPayPal):
    mode = "SANDBOX/test"

def agent_with(pp):
    a = run_all.build(); a.pp = pp; return a

class TestAgentExtras(unittest.TestCase):
    def test_sandbox_dispute_is_prepared_for_buyer(self):
        a = agent_with(NoBuyerDispute()); t = toner(a); a.execute(t)
        a.receive(t, {"sku": "TONER-BK-26A", "quantity": 3, "unit_price": 55.0})
        self.assertEqual(t["status"], "dispute_pending_buyer_filing")
        self.assertEqual(set(t["dispute_draft"]), {"reason", "amount", "note"}); self.assertEqual(t["dispute_draft"]["amount"], 55.0)
        self.assertIn("dispute_prepared_for_buyer", a.audit.events())

    def test_readback_mismatch_blocks(self):
        a = agent_with(LyingReadback()); t = toner(a)
        with self.assertRaises(MandateError): a.execute(t)
        self.assertIn("order_readback_mismatch", a.audit.events()); self.assertEqual(t["status"], "blocked_mismatch")

    def test_sandbox_style_buyer_approval(self):
        a = agent_with(SandboxLike()); t = toner(a); a.execute(t)
        self.assertEqual(t["status"], "awaiting_buyer_approval"); self.assertIn("checkoutnow?token=", t["approve_url"])
        with self.assertRaises(BuyerApprovalRequired): a.complete_payment(t)
        self.assertIn("capture_waiting_buyer", a.audit.events())
        a.pp.server.sandbox_buyer_approve(t["order_id"]); a.complete_payment(t)
        self.assertEqual(t["status"], "paid"); self.assertTrue(t["capture_id"].startswith("MOCK"))

class TestLLMConfig(unittest.TestCase):
    def setUp(self):
        self._root = llm_mod.ROOT; llm_mod.ROOT = tempfile.mkdtemp()     # ignore any developer .env
    def tearDown(self): llm_mod.ROOT = self._root
    def envfile(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False); f.write(text); f.close(); return f.name
    def test_no_key_means_offline(self):
        self.assertIsNone(load_config({"LLM_ENV_FILE": self.envfile("OPENAI_BASE_URL=https://x/v1\n")}))
        self.assertIsNone(llm_mod.get_llm({}))
    def test_env_file(self):
        c = load_config({"LLM_ENV_FILE": self.envfile("# c\nOPENAI_BASE_URL=https://x/v1/\nOPENAI_API_KEY=sk-secret\nLLM_MODEL=m1\n")})
        self.assertEqual(c, {"base_url": "https://x/v1", "api_key": "sk-secret", "model": "m1"})
        self.assertNotIn("sk-secret", repr(OpenAICompatLLM(c["base_url"], c["api_key"], c["model"])))
    def test_env_vars_override_file(self):
        c = load_config({"LLM_ENV_FILE": self.envfile("OPENAI_BASE_URL=https://x/v1\nOPENAI_API_KEY=a\n"), "OPENAI_API_KEY": "b"})
        self.assertEqual(c["api_key"], "b"); self.assertEqual(c["model"], llm_mod.DEFAULT_MODEL)
    def test_extract_json(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(_extract_json('Sure! {"sku": "X", "n": {"k": 2}} hope this helps'), {"sku": "X", "n": {"k": 2}})
        with self.assertRaises(LLMError): _extract_json("no json here")

if __name__ == "__main__": unittest.main()
