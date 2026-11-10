#!/usr/bin/env python3
"""PayPal sandbox smoke test: one command to prove the real Orders v2 / Disputes v1 path.

  python3 scripts/sandbox_smoke.py                    # REAL sandbox, credentials from .secrets (see .secrets.example)
  python3 scripts/sandbox_smoke.py --capture ORDER_ID # after the sandbox buyer approved the order in the browser
  python3 scripts/sandbox_smoke.py --capture-id CAPTURE_ID   # find disputes filed against a capture
  python3 scripts/sandbox_smoke.py --dispute DISPUTE_ID      # show one dispute
  python3 scripts/sandbox_smoke.py --evidence DISPUTE_ID --notes "1 of 2 laptops delivered"
  python3 scripts/sandbox_smoke.py --fake             # same flow against a local HTTP fake (no network, no credentials)

Exit codes: 0 all steps ok, 1 a step failed, 2 no credentials. Secrets and tokens are never printed.
"""
import argparse, json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from procure import secrets as S
from procure.paypal import PayPalREST, BuyerApprovalRequired
from procure.paypal_mock import PayPalHTTPError

class Run:
    def __init__(self): self.steps, self.failed = [], False
    def ok(self, name, **info): self._add("ok", name, info)
    def expected(self, name, **info): self._add("expected", name, info)
    def fail(self, name, **info): self.failed = True; self._add("FAIL", name, info)
    def _add(self, tag, name, info):
        self.steps.append({"result": tag, "step": name, **info})
        extra = "  ".join(f"{k}={v}" for k, v in info.items())
        print(f"[{tag}] {name}" + (f"  {extra}" if extra else ""), flush=True)

def _err(e): return f"{type(e).__name__}: {e}"

def flow_order(c, run):
    """OAuth -> create -> read back -> capture (expect ORDER_NOT_APPROVED) -> list disputes. Returns the order."""
    try:
        c._token(); run.ok("oauth2 client_credentials", token="(hidden)")
    except Exception as e:
        run.fail("oauth2 client_credentials", error=_err(e)); return None
    ref = time.strftime("SMOKE-%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        o = c.create_order(1.00, "USD", "GuardedBuyer sandbox smoke test", ref)
        run.ok("POST /v2/checkout/orders", order_id=o["id"], status=o.get("status"), approve_url=o.get("approve_url"))
    except Exception as e:
        run.fail("POST /v2/checkout/orders", error=_err(e)); return None
    try:
        again = c.create_order(1.00, "USD", "GuardedBuyer sandbox smoke test", ref)
        (run.ok if again["id"] == o["id"] else run.fail)("idempotent re-create (same PayPal-Request-Id)",
                                                          same_order=again["id"] == o["id"])
    except Exception as e:
        run.fail("idempotent re-create", error=_err(e))
    try:
        g = c.get_order(o["id"])
        amt = (g.get("purchase_units") or [{}])[0].get("amount", {})
        good = amt.get("value") == "1.00" and amt.get("currency_code") == "USD"
        (run.ok if good else run.fail)("GET order read-back", amount=f"{amt.get('value')} {amt.get('currency_code')}")
    except Exception as e:
        run.fail("GET order read-back", error=_err(e))
    try:
        c.capture_order(o["id"])
        run.fail("capture before buyer approval", note="PayPal captured without approval?")
    except BuyerApprovalRequired as e:
        run.expected("capture before buyer approval -> 422 ORDER_NOT_APPROVED", detail=str(e))
    except Exception as e:
        run.fail("capture before buyer approval", error=_err(e))
    try:
        d = c.list_disputes(page_size=5)
        run.ok("GET /v1/customer/disputes", count=len(d.get("items") or []))
    except Exception as e:
        run.fail("GET /v1/customer/disputes", error=_err(e))
    return o

def flow_capture(c, run, order_id):
    try:
        cap = c.capture_order(order_id)
        run.ok("POST capture", capture_id=cap.get("capture_id"), status=cap.get("status"))
    except BuyerApprovalRequired as e:
        run.fail("POST capture", error=f"not approved yet: {e}"); return None
    except Exception as e:
        run.fail("POST capture", error=_err(e)); return None
    try:
        cap2 = c.capture_order(order_id)
        same = cap2.get("capture_id") == cap.get("capture_id")
        (run.ok if same else run.fail)("capture again is safe (ORDER_ALREADY_CAPTURED -> same capture)",
                                       capture_id=cap2.get("capture_id"), already_captured=cap2.get("already_captured"))
    except Exception as e:
        run.fail("capture again", error=_err(e))
    return cap

def flow_disputes_for_capture(c, run, capture_id):
    try:
        items = c.find_disputes_for_capture(capture_id)
        run.ok("find disputes for capture", capture_id=capture_id, disputes=[i.get("dispute_id") for i in items])
        return items
    except Exception as e:
        run.fail("find disputes for capture", error=_err(e)); return []

def flow_dispute(c, run, dispute_id):
    try:
        d = c.get_dispute(dispute_id)
        run.ok("GET dispute", dispute_id=dispute_id, status=d.get("status"), stage=d.get("dispute_life_cycle_stage"),
               amount=(d.get("dispute_amount") or {}).get("value"))
        return d
    except Exception as e:
        run.fail("GET dispute", error=_err(e)); return None

def flow_evidence(c, run, dispute_id, notes):
    try:
        c.provide_evidence(dispute_id, notes)
        run.ok("POST provide-evidence", dispute_id=dispute_id)
    except PayPalHTTPError as e:
        run.fail("POST provide-evidence", status=e.status, issue=e.issue)
    except Exception as e:
        run.fail("POST provide-evidence", error=_err(e))

def run_fake(run):
    from procure.paypal_fake_http import FakePayPalHTTP
    with FakePayPalHTTP() as fake:
        print(f"== FAKE local PayPal (no network) at {fake.base_url} ==")
        c = PayPalREST(fake.client_id, fake.client_secret, base=fake.base_url, sleep=lambda s: None)
        o = flow_order(c, run)
        if not o: return
        fake.fail_next("POST", "/v2/checkout/orders", drop=True, after_processing=True)
        try:
            n0 = len(fake.mock.orders)
            o2 = c.create_order(2.00, "USD", "retry test", "SMOKE-RETRY")
            dup = len(fake.mock.orders) - n0
            (run.ok if dup == 1 else run.fail)("network drop after PayPal processed create -> retry, no duplicate",
                                               order_id=o2["id"], orders_created=dup)
        except Exception as e:
            run.fail("network drop retry", error=_err(e))
        fake.mock.sandbox_buyer_approve(o["id"]); run.ok("sandbox buyer approves (simulated browser step)")
        cap = flow_capture(c, run, o["id"])
        if not cap: return
        fake.mock.sandbox_buyer_open_dispute(o["id"], "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "0.50",
                                             "Received 1 of 2 units.")
        run.ok("buyer files the drafted claim in the Resolution Center (simulated)")
        items = flow_disputes_for_capture(c, run, cap["capture_id"])
        if not items: run.fail("dispute visible via API"); return
        did = items[0]["dispute_id"]
        flow_dispute(c, run, did)
        fake.set_dispute_status(did, "UNDER_REVIEW")
        try:
            c.sandbox_require_evidence(did, "BUYER_EVIDENCE"); run.ok("sandbox require-evidence BUYER_EVIDENCE")
        except Exception as e:
            run.fail("sandbox require-evidence", error=_err(e))
        flow_evidence(c, run, did, "Packing slip shows 1 of 2 units; photo attached in the Resolution Center.")
        flow_dispute(c, run, did)

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--capture"); ap.add_argument("--capture-id"); ap.add_argument("--dispute")
    ap.add_argument("--evidence"); ap.add_argument("--notes", default="Delivery did not match the order.")
    ap.add_argument("--test-card", action="store_true",
                    help="sandbox only: after the order checks, approve with PayPal's sandbox test card, then capture twice")
    ap.add_argument("--secrets", help="path to the secrets file (default .secrets or $PAYPAL_SECRETS_FILE)")
    a = ap.parse_args(argv)
    run = Run()
    if a.fake:
        run_fake(run)
        mode = "fake"
    else:
        try:
            creds = S.paypal_credentials(a.secrets)
        except ValueError as e:
            print(f"refused: {e}"); return 2
        if not creds:
            print(f"No sandbox credentials in {S.secrets_path(a.secrets)}.\n"
                  "Create it from .secrets.example (developer.paypal.com -> Apps & Credentials -> Sandbox), then rerun.\n"
                  "Offline check without credentials: python3 scripts/sandbox_smoke.py --fake")
            return 2
        print(f"== REAL PayPal SANDBOX {creds['base_url']}  client_id={S.mask(creds['client_id'])} ==")
        c = PayPalREST(creds["client_id"], creds["client_secret"], base=creds["base_url"])
        mode = "sandbox"
        if a.capture: flow_capture(c, run, a.capture)
        elif a.capture_id: flow_disputes_for_capture(c, run, a.capture_id)
        elif a.dispute: flow_dispute(c, run, a.dispute)
        elif a.evidence: flow_evidence(c, run, a.evidence, a.notes)
        else:
            o = flow_order(c, run)
            if o and a.test_card:
                try:
                    r = c.sandbox_confirm_test_card(o["id"])
                    run.ok("sandbox test card confirms payment source (stands in for the buyer)", order_status=r.get("status"))
                    cap = flow_capture(c, run, o["id"])
                    if cap: flow_disputes_for_capture(c, run, cap.get("capture_id"))
                except Exception as e:
                    run.fail("sandbox test card", error=_err(e))
            elif o:
                print(f"\nNext: open {o.get('approve_url')} with the sandbox PERSONAL (buyer) account, approve, then run:\n"
                      f"  python3 scripts/sandbox_smoke.py --capture {o['id']}")
    out_dir = os.path.join(ROOT, "out"); os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime(f"sandbox_smoke_{mode}_%Y%m%dT%H%M%S.json"))
    with open(path, "w") as f:
        json.dump({"mode": mode, "steps": run.steps, "ok": not run.failed}, f, indent=1)
    print(f"\n{'ALL STEPS OK' if not run.failed else 'SOME STEPS FAILED'}  -> {os.path.relpath(path, ROOT)}")
    return 1 if run.failed else 0

if __name__ == "__main__":
    sys.exit(main())
