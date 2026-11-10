"""SPEC J2: tests for the evaluation explorer (/evaluation, AG Grid) plus the audit verify / tamper-check endpoints.
Stdlib unittest, offline. (Written by the architect after the dsh queue stayed blocked for more than 40 minutes.)"""
import json, os, re, sys, threading, unittest, urllib.request
from collections import Counter
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import web.server as WS

CFGS = ("no_guardrails", "policy_only", "full")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")
STATIC = os.path.join(ROOT, "web", "static")


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


class EvaluationDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = WS.evaluation_data()

    def test_1_shape(self):
        d = self.d
        self.assertEqual(len(d["rows"]), 200)
        self.assertEqual(len({r["id"] for r in d["rows"]}), 200)
        self.assertEqual(d["n_cases"], 200); self.assertEqual(d["seed"], 20261110); self.assertIs(d["synthetic"], True)

    def test_2_rows_match_summary(self):
        rows = self.d["rows"]
        attack = [r for r in rows if r["type"] in ("injection", "over_authority")]
        for cfg in CFGS:
            s = self.d["summary"][cfg]
            self.assertEqual(sum(r[cfg] == "blocked" for r in attack), s["attack_cases_blocked"], cfg)
            self.assertEqual(sum(r[cfg] == "false_block" for r in rows if r["type"] == "normal"), s["normal_false_blocks"], cfg)
            self.assertEqual(sum(r[cfg] == "dispute_drafted" for r in rows if r["type"] == "mismatch"), s["mismatch_handled"], cfg)
            self.assertEqual(sum(r[f"{cfg}_blocked_attempts"] for r in attack), s["attempts_blocked"], cfg)
            self.assertEqual(len(attack), s["attack_cases"])

    def test_3_summary_matches_results_file(self):
        with open(os.path.join(ROOT, "metrics", "results_200.json"), encoding="utf-8") as f:
            ref = json.load(f)["offline"]["summary"]
        for cfg in CFGS:
            s, r = self.d["summary"][cfg], ref[cfg]
            self.assertEqual(s["unauthorized_attempts"], r["unauthorized_attempts"], cfg)
            self.assertEqual(s["attempts_blocked"], r["blocked"], cfg)
            self.assertEqual(s["mismatch_cases"], r["mismatch_cases"], cfg)
            self.assertEqual(s["mismatch_handled"], r["mismatch_handled"], cfg)

    def test_4_missed_ids(self):
        missed = {r["id"] for r in self.d["rows"] if r["full"] in ("missed", "partial")}
        self.assertEqual(missed, set(self.d["summary"]["full"]["missed_attack_case_ids"]))
        self.assertEqual(len(missed), 5)

    def test_6_hygiene(self):
        for name in ("evaluation.html", "evaluation.js", "evaluation.css"):
            txt = read(STATIC, name)
            self.assertIsNone(EMOJI_RE.search(txt), name)
            self.assertNotIn("purple", txt.lower(), name); self.assertNotIn("cyan", txt.lower(), name)
        self.assertNotRegex(read(STATIC, "evaluation.js"), r"\.innerHTML\s*=")

    def test_8_render_yaml(self):
        y = read(ROOT, "render.yaml")
        self.assertIn("startCommand: python3 web/server.py", y); self.assertIn("HOST", y)
        self.assertNotRegex(y, r"PAYPAL_CLIENT_SECRET\s*\n\s*value:")


class EvaluationHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WS.DEMO = WS.Demo(None, "mock")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), WS.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def test_5_routes(self):
        st, ct, body = self.get("/evaluation")
        self.assertEqual(st, 200); self.assertIn("text/html", ct)
        html = body.decode()
        self.assertIn("Evaluation explorer", html); self.assertIn("/static/vendor/ag-grid/ag-grid-community.min.js", html)
        st, _, body = self.get("/api/evaluation")
        self.assertEqual(json.loads(body), json.loads(json.dumps(WS.evaluation_data())))
        st, _, body = self.get("/static/vendor/ag-grid/LICENSE.txt")
        self.assertEqual(st, 200); self.assertIn("MIT License", body.decode())
        st, _, body = self.get("/static/vendor/ag-grid/ag-grid-community.min.js")
        self.assertEqual(st, 200); self.assertGreater(len(body), 500000)

    def test_7_nav_links(self):
        for path in ("/approvals", "/"):
            _, _, body = self.get(path)
            self.assertIn('href="/evaluation"', body.decode(), path)

    def test_9_audit_verify_and_tamper_check(self):
        WS.DEMO.replay("normal")
        _, _, body = self.get("/api/audit/verify")
        v = json.loads(body)
        self.assertTrue(v["valid"]); self.assertGreater(v["length"], 3); self.assertEqual(len(v["head"]), 64)
        before = json.loads(json.dumps(WS.DEMO.a.audit.entries, default=str))
        seq = v["length"] // 2
        _, _, body = self.get(f"/api/audit/tamper-check?seq={seq}")
        t = json.loads(body)
        self.assertEqual(t["tampered_seq"], seq)
        self.assertFalse(t["result"]["valid"]); self.assertEqual(t["result"]["first_bad_seq"], seq)
        # the live log is untouched and still verifies
        self.assertEqual(json.loads(json.dumps(WS.DEMO.a.audit.entries, default=str)), before)
        self.assertTrue(json.loads(self.get("/api/audit/verify")[2])["valid"])


if __name__ == "__main__":
    unittest.main()
