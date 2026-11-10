"""SPEC H2: Approval Desk tests — offline, loopback HTTP against web.server.Demo (mock PayPal)."""
import json, os, re, sys, threading, unittest, urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web.server as WS
from procure.policy import PurchaseRequest
from scenarios import run_all

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


class ApprovalDeskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WS.DEMO = WS.Demo(None, "mock")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), WS.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read().decode()

    def jget(self, path):
        st, _, txt = self.get(path)
        return st, json.loads(txt)

    def post(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post_form_303(self, path, form):
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            self.opener.open(req, timeout=5)
            self.fail("expected a 303 redirect")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location")

    def item(self, rid):
        _, body = self.jget("/api/approvals")
        return next(i for i in body["items"] if i["request_id"] == rid)

    def demo(self):
        _, txt = self.post("/api/approvals/demo", body={"reset": True})
        return json.loads(txt)["request_id"]

    def decide(self, rid, role, decision, note=None):
        _, txt = self.post("/api/approvals/decide",
                           body={"request_id": rid, "role": role, "decision": decision, "note": note})
        return json.loads(txt)

    def approve_all(self, rid):
        self.decide(rid, "manager", "approve"); self.decide(rid, "finance", "approve")

    def test_1_page_clean_and_static_clean(self):
        st, ctype, page = self.get("/approvals")
        self.assertEqual(st, 200); self.assertIn("text/html", ctype)
        self.assertIn("Approval desk", page)
        self.assertIn("/static/icons/lucide-sprite.svg", page); self.assertIn("approvals.js", page)
        files = {"approvals.html": page}
        for name in ("approvals.js", "approvals.css"):
            st, _, txt = self.get("/static/" + name)
            self.assertEqual(st, 200); files[name] = txt
        for name, txt in files.items():
            self.assertIsNone(EMOJI_RE.search(txt), f"{name} contains emoji")
            self.assertNotRegex(txt, "(?i)purple|cyan", f"{name} mentions purple/cyan")

    def test_2_demo_request_pending(self):
        rid = self.demo(); it = self.item(rid)
        self.assertEqual(it["status"], "pending_approval")
        reasons = " ".join(it["decision"]["reasons"])
        self.assertIn("exceeds it remaining budget", reasons)
        self.assertGreater(it["budget"]["overage"], 0)
        self.assertTrue(any(b["event"] == "execution_refused" for b in it["blocked_attempts"]))
        self.assertEqual(it["mandate_preview"]["x_constraints"]["max_amount"]["value"], it["request"]["total"])
        self.assertEqual([c["role"] for c in it["approval_chain"]], ["manager", "finance"])
        self.assertTrue(all(c["status"] == "pending" for c in it["approval_chain"]))

    def test_3_release_before_approval_blocked(self):
        rid = self.demo()
        st, txt = self.post("/api/approvals/release", body={"request_id": rid})
        self.assertEqual(st, 409)
        self.assertTrue(json.loads(txt)["blocked"])
        it = self.item(rid)
        refused = [b for b in it["blocked_attempts"] if b["event"] == "execution_refused"]
        self.assertEqual(len(refused), 2)      # desk-demo attempt + this release attempt
        self.assertEqual(it["status"], "pending_approval")

    def test_4_chain_approves_with_note(self):
        rid = self.demo()
        it = self.decide(rid, "manager", "approve", note="ok for onboarding")
        mgr = next(c for c in it["approval_chain"] if c["role"] == "manager")
        self.assertEqual(mgr["status"], "approved")
        self.assertEqual(mgr["note"], "ok for onboarding")
        self.assertEqual(it["status"], "pending_approval")      # finance still pending
        it = self.decide(rid, "finance", "approve")
        self.assertEqual(it["status"], "approved")
        self.assertTrue(it["can_release"])
        self.assertEqual(it["mandate_preview"]["x_constraints"]["approvals"], ["finance", "manager"])

    def test_5_release_creates_mandate_and_order(self):
        rid = self.demo(); self.approve_all(rid)
        st, txt = self.post("/api/approvals/release", body={"request_id": rid})
        self.assertEqual(st, 200)
        it = json.loads(txt)
        self.assertTrue(it["order_id"])
        self.assertEqual(it["status"], "awaiting_buyer_approval")
        self.assertTrue(it["mandate"]["signature"])
        self.assertTrue(it["mandate_check"]["signature_valid"])
        self.assertEqual(it["mandate"]["payload"]["x_constraints"]["approvals"], ["finance", "manager"])
        self.assertTrue(it["approve_url"].startswith("/mock-paypal/checkoutnow"))
        self.assertFalse(it["can_release"])

    def test_6_paypal_approve_and_return(self):
        rid = self.demo(); self.approve_all(rid)
        _, txt = self.post("/api/approvals/release", body={"request_id": rid})
        order = json.loads(txt)["order_id"]
        st, loc = self.post_form_303("/mock-paypal/approve", form={"token": order})
        self.assertEqual(st, 303)
        self.assertTrue(loc.startswith("/paypal/return?token=") and "PayerID=" in loc)
        st, _, page = self.get(loc)
        self.assertEqual(st, 200)
        self.assertIn("/approvals#", page)
        it = self.item(rid)
        self.assertEqual(it["status"], "paid")
        self.assertTrue(it["capture_id"])
        _, state = self.jget("/api/approvals")
        self.assertTrue(state["chain"]["valid"])

    def test_7_reject_path(self):
        rid = self.demo()
        it = self.decide(rid, "manager", "reject", note="too risky")
        self.assertEqual(it["status"], "rejected_by_approver")
        self.assertTrue(any(a["event"] == "approval_denied" for a in it["audit"]))
        st, txt = self.post("/api/approvals/release", body={"request_id": rid})
        self.assertEqual(st, 409)
        self.assertTrue(json.loads(txt)["blocked"])

    def test_8_wrong_person_cannot_approve(self):
        a = run_all.build()
        req = PurchaseRequest("dana", "it", "it_hardware", "laptop", "LAPTOP-14-PRO", 1, 1100.0, "TechMart")
        t = a.submit(req)
        with self.assertRaises(PermissionError):
            a.approve(t, "manager", "eve")
        with self.assertRaises(PermissionError):
            a.reject(t, "finance", "mallory")
        a.approve(t, "manager", "alice")       # the roster approver works
        self.assertIn("manager", a.approvals.granted[t["request_id"]])

    def test_9_existing_routes_still_work(self):
        st, ctype, _ = self.get("/")
        self.assertEqual(st, 200)
        st, body = self.jget("/api/state")
        self.assertEqual(st, 200)
        self.assertIn("tickets", body)


if __name__ == "__main__":
    unittest.main()
