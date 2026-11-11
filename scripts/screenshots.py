#!/usr/bin/env python3
"""Regenerate docs/screenshots/01-12 from a running offline demo server (mock PayPal).

    python3 web/server.py --port 8765 &        # offline emulation, no keys
    python3 scripts/screenshots.py --base http://127.0.0.1:8765 --out docs/screenshots

Needs Google Chrome and `websockets`. 13_sandbox_desk_paid.png comes from the real-sandbox video run and is not
regenerated here. The server's demo state is reset several times.
"""
import argparse, asyncio, json, os, sys, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp_shot


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): return None


def api(base, path, body=None, form=False):
    data = None; headers = {}
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 303: return {"location": e.headers.get("Location")}
        raise


def shot(base, url, out, w=1440, h=900, full=False):
    cdp_shot.FULL_PAGE = full
    asyncio.run(cdp_shot.cdp(base + url, out, w, h))


def tickets(base): return api(base, "/api/state")["tickets"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="docs/screenshots")
    a = ap.parse_args(); b, o = a.base.rstrip("/"), a.out
    os.makedirs(o, exist_ok=True)
    p = lambda n: os.path.join(o, n)

    # Dashboard after the four replays (replay=all resets server state at page load).
    shot(b, "/?replay=all", p("01_overview_replay_all.png"), 1440, 1400)
    shot(b, "/?replay=all&panel=trace&trace=red_team", p("02_red_team_trace.png"), 1440, 1100)
    shot(b, "/?demo=laptop-paid&panel=mandate", p("03_approvals_mandate.png"), 1440, 1000)
    shot(b, "/?demo=laptop-paid&panel=paypal", p("04_paypal_order_receiving.png"), 1440, 1000)
    shot(b, "/?replay=all&panel=audit&filter=blocked", p("05_audit_blocked.png"), 1440, 1000)
    api(b, "/api/replay", {"scenario": "mismatch", "reset": True})
    rid = next(t["request_id"] for t in tickets(b) if t["status"] == "disputed")
    shot(b, f"/?panel=dispute#{rid}", p("06_dispute.png"), 1440, 1000)

    # Approval desk: blocked -> approved by both -> released -> buyer approves at checkout -> captured.
    it = api(b, "/api/approvals/demo", {"reset": True}); rid = it["request_id"]
    shot(b, f"/approvals#{rid}", p("09_approval_desk_blocked.png"), 1440, 900, full=True)
    for role in ("manager", "finance"):
        api(b, "/api/approvals/decide", {"request_id": rid, "role": role, "decision": "approve",
                                         "note": "Two seats for the new engineers, budget moved from Q1 onboarding"
                                         if role == "finance" else None})
    api(b, "/api/approvals/release", {"request_id": rid})
    shot(b, f"/approvals#{rid}", p("10_approval_desk_released.png"), 1440, 900, full=True)
    order = next(t for t in tickets(b) if t["request_id"] == rid)["order_id"]
    shot(b, f"/mock-paypal/checkoutnow?token={order}", p("07_paypal_checkout_emulated.png"), 1440, 900)
    loc = api(b, "/mock-paypal/approve", {"token": order}, form=True)["location"]
    shot(b, loc, p("08_paypal_return_captured.png"), 1440, 900)
    shot(b, f"/approvals#{rid}", p("11_approval_desk_paid.png"), 1440, 900, full=True)
    shot(b, "/evaluation", p("12_evaluation_explorer.png"), 1440, 900)


if __name__ == "__main__":
    main()
