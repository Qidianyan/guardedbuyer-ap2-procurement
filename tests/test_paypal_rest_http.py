"""Part 2 tests: the hardened PayPalREST against the local HTTP fake (fully offline, 127.0.0.1 only).

Covers SPEC G section 4: OAuth, idempotent create/capture, retries (5xx/429/dropped connections),
token refresh on 401, non-retryable 4xx, disputes v1 evidence flow, the end-to-end agent flow,
and the secrets loader. No real PayPal credentials and no network beyond loopback are used.
"""
import base64, json, os, re, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procure import secrets as S
from procure.audit import AuditLog
from procure.agent import ProcurementAgent
from procure.paypal import PayPalREST, BuyerApprovalRequired, make_client
from procure.paypal_fake_http import FakePayPalHTTP
from procure.paypal_mock import PayPalHTTPError
from procure.policy import ApprovalBook, Policy, PurchaseRequest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RestCase(unittest.TestCase):
    """One fresh fake + one fresh REST client per test; sleep is faked and recorded."""

    def setUp(self):
        self.fake = FakePayPalHTTP().start()
        self.sleeps = []
        self.c = PayPalREST(self.fake.client_id, self.fake.client_secret, base=self.fake.base_url,
                            sleep=lambda s: self.sleeps.append(s))

    def tearDown(self):
        self.fake.stop()

    # ---------------------------------------------------------------- helpers
    def create_approved(self, ref="REF-1", amount=1.00):
        o = self.c.create_order(amount, "USD", "test item", ref)
        self.fake.mock.sandbox_buyer_approve(o["id"])
        return o

    def posts(self, prefix):
        return self.fake.requests_to("POST", prefix)

    def captures(self):
        return [r for r in self.posts("/v2/checkout/orders/") if r["path"].endswith("/capture")]

    def rids(self, prefix):
        return [r["headers"].get("paypal-request-id") for r in self.posts(prefix)]


class TestOAuthAndCreate(RestCase):
    def test_1_basic_header_and_token_cached(self):
        o = self.create_approved()
        cap = self.c.capture_order(o["id"])
        self.c.list_disputes(page_size=5)
        toks = self.posts("/v1/oauth2/token")
        self.assertEqual(len(toks), 1, "token must be fetched once and cached")
        self.assertEqual(toks[0]["status"], 200)
        self.assertEqual(toks[0]["headers"]["authorization"], "Basic …")   # secret never logged
        self.assertEqual(toks[0]["headers"]["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(toks[0]["body"], {"grant_type": "client_credentials"})
        self.c.capture_order(cap["id"])                  # further calls reuse the token
        self.assertEqual(len(self.posts("/v1/oauth2/token")), 1)
        # a wrong secret must be rejected by the fake -> proves the Basic header carries exactly
        # base64(client_id:secret)
        bad = PayPalREST(self.fake.client_id, "WRONG-SECRET", base=self.fake.base_url,
                         sleep=lambda s: None)
        with self.assertRaises(PayPalHTTPError) as e:
            bad.create_order(1.00, "USD", "x", "REF-BAD")
        self.assertEqual(e.exception.status, 401)

    def test_2_create_request_id_prefer_and_approve_url(self):
        o = self.c.create_order(1.00, "USD", "test item", "REF-2")
        self.assertEqual(o["status"], "PAYER_ACTION_REQUIRED")
        rid = self.rids("/v2/checkout/orders")[0]
        self.assertTrue(UUID_RE.match(rid), rid)
        self.assertLessEqual(len(rid), 38)
        self.assertEqual(self.posts("/v2/checkout/orders")[0]["headers"]["prefer"], "return=representation")
        self.assertIn("payer-action", [l["rel"] for l in o["links"]])
        self.assertTrue(o["approve_url"])
        self.assertIn(o["id"], o["approve_url"])
        again = self.c.create_order(1.00, "USD", "test item", "REF-2")   # same reference -> same id
        self.assertEqual(again["id"], o["id"])
        self.assertEqual(self.rids("/v2/checkout/orders")[1], rid)

    def test_3_capture_before_approval(self):
        o = self.c.create_order(1.00, "USD", "test item", "REF-3")
        with self.assertRaises(BuyerApprovalRequired) as ctx:
            self.c.capture_order(o["id"])
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, PayPalHTTPError)
        self.assertEqual(cause.status, 422)
        self.assertEqual(cause.issue, "ORDER_NOT_APPROVED")
        caps = self.captures()
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["status"], 422)
        self.assertIn("ORDER_NOT_APPROVED", str(ctx.exception))

    def test_4_capture_then_recapture(self):
        o = self.create_approved("REF-4")
        cap = self.c.capture_order(o["id"])
        self.assertEqual(cap["status"], "COMPLETED")
        self.assertTrue(cap["capture_id"])
        cap2 = self.c.capture_order(o["id"])
        self.assertEqual(cap2["capture_id"], cap["capture_id"])
        self.assertTrue(cap2["already_captured"])
        self.assertEqual(cap2["status"], "COMPLETED")


class TestRetries(RestCase):
    def test_5_503_retried_with_same_request_id(self):
        self.fake.fail_next("POST", "/v2/checkout/orders", status=503)
        o = self.c.create_order(1.00, "USD", "retry", "REF-5")
        self.assertEqual(len(self.fake.mock.orders), 1, "exactly one order must exist after the retry")
        ats = self.posts("/v2/checkout/orders")
        self.assertEqual([a["status"] for a in ats], [503, 201])
        self.assertEqual(len(set(self.rids("/v2/checkout/orders"))), 1, "same PayPal-Request-Id on every retry")
        self.assertEqual(len(self.sleeps), 1)

    def test_6_429_retry_after_is_honoured(self):
        self.fake.fail_next("POST", "/v2/checkout/orders", status=429, headers={"Retry-After": "2"})
        o = self.c.create_order(1.00, "USD", "retry", "REF-6")
        self.assertEqual(o["status"], "PAYER_ACTION_REQUIRED")
        self.assertEqual(self.sleeps, [2.0], "sleep must use the Retry-After header")
        self.assertEqual(len(self.posts("/v2/checkout/orders")), 2)

    def test_7_dropped_after_processing_is_idempotent(self):
        self.fake.fail_next("POST", "/v2/checkout/orders", drop=True, after_processing=True)
        o = self.c.create_order(1.00, "USD", "drop", "REF-7")
        self.assertEqual(len(self.fake.mock.orders), 1)
        ats = self.posts("/v2/checkout/orders")
        self.assertEqual(len(ats), 2)
        self.assertEqual(ats[0]["status"], "dropped")
        self.assertEqual(ats[1]["status"], 201)
        self.assertEqual(len(set(self.rids("/v2/checkout/orders"))), 1)
        self.assertEqual(o["id"], list(self.fake.mock.orders)[0])

    def test_8_expired_token_refreshed_once(self):
        self.c._token()
        self.assertEqual(len(self.posts("/v1/oauth2/token")), 1)
        self.fake.expire_tokens()
        o = self.c.create_order(1.00, "USD", "refresh", "REF-8")
        self.assertEqual(o["status"], "PAYER_ACTION_REQUIRED")
        self.assertEqual(len(self.posts("/v1/oauth2/token")), 2, "exactly one refresh")
        ats = self.posts("/v2/checkout/orders")
        self.assertEqual([a["status"] for a in ats], [401, 201], "one refused attempt, one success after refresh")

    def test_9_non_retryable_status_single_attempt(self):
        with self.assertRaises(PayPalHTTPError) as e:
            self.c._req("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": []})
        self.assertEqual(e.exception.status, 400)
        self.assertEqual(len(self.posts("/v2/checkout/orders")), 1)
        o = self.c.create_order(1.00, "USD", "x", "REF-9")       # 422 ORDER_NOT_APPROVED is not retried
        with self.assertRaises(BuyerApprovalRequired):
            self.c.capture_order(o["id"])
        self.assertEqual(len(self.captures()), 1)

    def test_10_persistent_503_exhausts_retries(self):
        self.fake.fail_next("POST", "/v2/checkout/orders", status=503, times=10)
        with self.assertRaises(PayPalHTTPError) as e:
            self.c.create_order(1.00, "USD", "x", "REF-10")
        self.assertEqual(e.exception.status, 503)
        self.assertEqual(len(self.posts("/v2/checkout/orders")), self.c.max_retries + 1)
        self.assertEqual(len(self.sleeps), self.c.max_retries)
        self.assertEqual(len(self.fake.mock.orders), 0)

    def test_11_post_without_request_id_is_not_retried(self):
        self.fake.fail_next("POST", "/v2/checkout/orders", status=503, times=10)
        with self.assertRaises(PayPalHTTPError) as e:
            self.c._req("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": []})
        self.assertEqual(e.exception.status, 503)
        self.assertEqual(len(self.posts("/v2/checkout/orders")), 1, "no retry without PayPal-Request-Id")
        self.assertEqual(self.sleeps, [])

    def test_12_request_ids_unique_per_call_type(self):
        o = self.c.create_order(1.00, "USD", "x", "REF-12")
        rid_create = self.rids("/v2/checkout/orders")[0]
        self.fake.mock.sandbox_buyer_approve(o["id"])
        self.c.capture_order(o["id"])
        rid_capture = self.rids("/v2/checkout/orders/")[-1]
        self.assertNotEqual(rid_create, rid_capture, "create and capture must use different ids")
        o2 = self.c.create_order(1.00, "USD", "x", "REF-12")
        self.assertEqual(self.rids("/v2/checkout/orders")[0], rid_create)
        self.assertEqual(o2["id"], o["id"])


class TestDisputes(RestCase):
    def test_13_dispute_find_get_evidence(self):
        o = self.create_approved("REF-13")
        cap = self.c.capture_order(o["id"])
        d = self.fake.mock.sandbox_buyer_open_dispute(o["id"], "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                                                      f"{o['purchase_units'][0]['amount']['value']}", "short delivery")
        did = d["dispute_id"]
        found = self.c.find_disputes_for_capture(cap["capture_id"])
        self.assertEqual([i["dispute_id"] for i in found], [did])
        got = self.c.get_dispute(did)
        self.assertEqual(got["dispute_id"], did)

        self.fake.set_dispute_status(did, "UNDER_REVIEW")          # evidence is not allowed yet
        with self.assertRaises(PayPalHTTPError) as e:
            self.c.provide_evidence(did, "too early")
        self.assertEqual(e.exception.status, 422)

        self.c.sandbox_require_evidence(did, "BUYER_EVIDENCE")     # sandbox hook: UNDER_REVIEW -> buyer owes evidence
        self.assertEqual(self.fake.mock.disputes[did]["status"], "WAITING_FOR_BUYER_RESPONSE")
        notes = "Packing slip shows 1 of 2 units delivered; photo attached."
        res = self.c.provide_evidence(did, notes, files=[("slip.pdf", b"%PDF-1.4 fake", "application/pdf")])
        self.assertIn("links", res)
        ev = [r for r in self.posts("/v1/customer/disputes/") if r["path"].endswith("/provide-evidence")]
        self.assertEqual([e["status"] for e in ev], [422, 200], "refused once, then accepted")
        parts = ev[-1]["body"]
        self.assertIsInstance(parts, list)
        inp = next(p for p in parts if p["name"] == "input")
        self.assertEqual(inp["content_type"], "application/json")
        self.assertIn(notes, inp["content"])
        self.assertIn("evidence-file", [p["name"] for p in parts])
        self.assertIn("slip.pdf", [p.get("filename") for p in parts])
        self.assertTrue(ev[0]["headers"].get("paypal-request-id"))
        self.assertEqual(self.fake.mock.disputes[did]["status"], "UNDER_REVIEW")


class TestEndToEnd(unittest.TestCase):
    def test_14_agent_flow_over_rest_fake(self):
        with FakePayPalHTTP() as fake:
            c = PayPalREST(fake.client_id, fake.client_secret, base=fake.base_url, sleep=lambda s: None)
            agent = ProcurementAgent(Policy({"ops": 2000.0}, {"office_supplies"}),
                                     ApprovalBook({"manager": "alice"}), c, AuditLog(None))
            r = PurchaseRequest("dana", "ops", "office_supplies", "Toner", "S1", 2, 55.0, "M")
            t = agent.submit(r)
            agent.approve(t, "manager", "alice")
            agent.execute(t, auto_buyer_approve=False)
            self.assertEqual(t["status"], "awaiting_buyer_approval")
            self.assertTrue(t["approve_url"])
            with self.assertRaises(BuyerApprovalRequired):
                agent.complete_payment(t)
            fake.mock.sandbox_buyer_approve(t["order_id"])       # the buyer clicks Pay in the browser
            agent.complete_payment(t)
            self.assertEqual(t["status"], "paid")
            self.assertTrue(t["capture_id"])

            agent.track_dispute(t)                                # nobody filed yet
            self.assertEqual(t["status"], "paid")
            self.assertIn("dispute_not_yet_filed", agent.audit.events())

            agent.receive(t, {"sku": r.sku, "quantity": r.quantity - 1, "unit_price": r.unit_price})
            self.assertEqual(t["status"], "dispute_pending_buyer_filing")
            self.assertIn("reason", t["dispute_draft"])
            self.assertIn("dispute_prepared_for_buyer", agent.audit.events())

            fake.mock.sandbox_buyer_open_dispute(t["order_id"], "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                                                 f"{t['dispute_draft']['amount']:.2f}", "short delivery")
            agent.track_dispute(t)
            self.assertEqual(t["status"], "disputed")
            self.assertEqual(t["dispute"]["disputed_transactions"][0]["seller_transaction_id"], t["capture_id"])
            self.assertIn("dispute_tracked", agent.audit.events())
            self.assertTrue(agent.audit.verify_chain())


class TestSecretsAndWiring(unittest.TestCase):
    def test_15_secrets_loading_and_make_client(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".secrets")
            with open(p, "w") as f:
                f.write("# comment line\n\n   PAYPAL_CLIENT_ID = \"abc 123\" \n"
                        "PAYPAL_CLIENT_SECRET='xyz'\nexport PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com\n")
            self.assertEqual(S.load_secrets(p), {"PAYPAL_CLIENT_ID": "abc 123", "PAYPAL_CLIENT_SECRET": "xyz",
                                                 "PAYPAL_BASE_URL": "https://api-m.sandbox.paypal.com"})
            creds = S.paypal_credentials(p)
            self.assertEqual(creds["client_id"], "abc 123")
            self.assertEqual(creds["base_url"], "https://api-m.sandbox.paypal.com")
            empty = os.path.join(d, "missing")
            self.assertEqual(S.load_secrets(empty), {})
            self.assertIsNone(S.paypal_credentials(empty))
            half = os.path.join(d, "half")
            with open(half, "w") as f:
                f.write("PAYPAL_CLIENT_ID=only-id\n")
            self.assertIsNone(S.paypal_credentials(half))
            live = os.path.join(d, "live")
            with open(live, "w") as f:
                f.write("PAYPAL_CLIENT_ID=a\nPAYPAL_CLIENT_SECRET=b\nPAYPAL_BASE_URL=https://api-m.paypal.com\n")
            with self.assertRaises(ValueError):
                S.paypal_credentials(live)
        masked = S.mask("supersecret-value-123")
        self.assertNotIn("supersecret-value-123", masked)
        self.assertIn("…", masked)
        self.assertEqual(S.mask(""), "(empty)")

    def test_15b_make_client_with_fake_secrets(self):
        with tempfile.TemporaryDirectory() as d, FakePayPalHTTP() as fake:
            p = os.path.join(d, ".secrets")
            with open(p, "w") as f:
                f.write(f"PAYPAL_CLIENT_ID={fake.client_id}\nPAYPAL_CLIENT_SECRET={fake.client_secret}\n"
                        f"PAYPAL_BASE_URL={fake.base_url}\n")
            cl = make_client(secrets_path=p, prefer_toolkit=False)
            self.assertIsInstance(cl, PayPalREST)
            self.assertEqual(cl.base, fake.base_url)
            o = cl.create_order(1.00, "USD", "x", "REF-15")
            self.assertEqual(o["status"], "PAYER_ACTION_REQUIRED")

    def test_16_sandbox_smoke_script(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_SECRETS_FILE", "PAYPAL_BASE_URL")}
        r = subprocess.run([sys.executable, "scripts/sandbox_smoke.py", "--fake"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("FAKE-SECRET", r.stdout)
        env2 = dict(env, PAYPAL_SECRETS_FILE="/nonexistent/.secrets")
        r2 = subprocess.run([sys.executable, "scripts/sandbox_smoke.py"], cwd=ROOT, env=env2,
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(r2.returncode, 2, r2.stdout + r2.stderr)

    def test_17_secrets_is_gitignored(self):
        in_repo = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, text=True)
        if in_repo.returncode == 0 and os.path.realpath(in_repo.stdout.strip()) == os.path.realpath(ROOT):
            self.assertEqual(subprocess.run(["git", "check-ignore", ".secrets"], cwd=ROOT,
                                            capture_output=True, text=True).returncode, 0)
        else:   # exported copy without .git: check the ignore rule itself
            with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
                self.assertIn(".secrets", [l.strip() for l in f])


if __name__ == "__main__":
    unittest.main()
