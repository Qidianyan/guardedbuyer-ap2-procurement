"""PayPal adapters. One interface, three backends:

  create_order(amount, currency, description, reference_id, items=None) -> Orders v2 body + "approve_url"
  buyer_approve(order_id)   -> emulation only; in the sandbox the buyer approves in the browser via approve_url
  capture_order(order_id)   -> Orders v2 capture body + "capture_id"; BuyerApprovalRequired if not yet approved
  get_order(order_id)       -> full Orders v2 order (used for the post-create read-back check)
  open_dispute(...)         -> emulation only (PayPal has no buyer-side create-dispute endpoint, see below)
  get_dispute(dispute_id) / list_disputes(**query)  -> Disputes v1 bodies

* MockPayPal           - *** OFFLINE *** emulation of Orders v2 / Disputes v1 (procure/paypal_mock.py). IDs start with MOCK.
* PayPalToolkitBackend - official `paypal-agent-toolkit` (procure/paypal_toolkit.py); online or offline.
* PayPalREST           - plain REST (OAuth2, Orders v2, Disputes v1) if the toolkit is not installed.

Buyer approval (Orders v2): an order created with payment_source.paypal.experience_context is
PAYER_ACTION_REQUIRED with a 'payer-action' link. The company's sandbox *personal* (buyer) account opens the
link and approves -> PayPal redirects to return_url -> capture -> COMPLETED. Capturing earlier returns
HTTP 422 ORDER_NOT_APPROVED, surfaced as BuyerApprovalRequired.

Disputes: Disputes v1 lists/shows disputes and offers actions (accept-claim, send-message, ...), but has no
endpoint for a BUYER to create one; the buyer files it in the sandbox Resolution Center, then we track it.
"""
import base64, hashlib, http.client, json, os, socket, time, urllib.error, urllib.request, uuid
from . import secrets as _secrets
from .paypal_mock import MockPayPalServer, PayPalHTTPError

SANDBOX = "https://api-m.sandbox.paypal.com"
RETURN_URL = os.environ.get("PAYPAL_RETURN_URL", "http://localhost:8000/paypal/return")
CANCEL_URL = os.environ.get("PAYPAL_CANCEL_URL", "http://localhost:8000/paypal/cancel")

class BuyerApprovalRequired(Exception): pass

def _approve_link(order):
    for l in order.get("links", []):
        if l.get("rel") in ("approve", "payer-action"):
            return l.get("href")
    return None

def _money(currency, value): return {"currency_code": currency, "value": f"{value:.2f}"}

def build_order_body(amount, currency, description, reference_id, items=None):
    """Orders v2 create-order request body (intent CAPTURE, itemised, PayPal wallet with experience_context)."""
    items = items or [{"name": description, "sku": "ITEM", "unit_price": amount, "quantity": 1}]
    pp_items = [{"name": str(i["name"])[:127], "sku": str(i.get("sku", ""))[:127],
                 "unit_amount": _money(currency, round(float(i["unit_price"]), 2)),
                 "quantity": str(int(i["quantity"])), "category": "PHYSICAL_GOODS"} for i in items]
    item_total = round(sum(round(float(i["unit_price"]), 2) * int(i["quantity"]) for i in items), 2)
    if abs(item_total - round(amount, 2)) > 0.004:
        raise ValueError(f"amount {amount} does not equal item total {item_total}")
    return {"intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference_id, "description": str(description)[:127],
                                "amount": {**_money(currency, item_total),
                                           "breakdown": {"item_total": _money(currency, item_total)}},
                                "items": pp_items}],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": RETURN_URL, "cancel_url": CANCEL_URL,
                "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"}}}}

def _capture_id(body):
    caps = ((body.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}]
    return caps[0].get("id")

class MockPayPal:
    """Offline backend: same calls as PayPalREST, answered by the in-memory Orders v2 / Disputes v1 emulation."""
    mode = "MOCK"
    def __init__(self, server=None): self.server = server or MockPayPalServer()
    def create_order(self, amount, currency, description, reference_id, items=None):
        o = self.server.request("POST", "/v2/checkout/orders",
                                build_order_body(amount, currency, description, reference_id, items),
                                {"PayPal-Request-Id": reference_id})
        return {**o, "approve_url": _approve_link(o)}
    def buyer_approve(self, order_id):
        """Stands in for the sandbox buyer clicking Pay on the payer-action page."""
        return self.server.sandbox_buyer_approve(order_id)
    def capture_order(self, order_id):
        try:
            c = self.server.request("POST", f"/v2/checkout/orders/{order_id}/capture", {})
        except PayPalHTTPError as e:
            if e.issue == "ORDER_NOT_APPROVED":
                raise BuyerApprovalRequired(f"order {order_id}: buyer must approve first (ORDER_NOT_APPROVED)") from e
            raise
        return {**c, "capture_id": _capture_id(c)}
    def get_order(self, order_id): return self.server.request("GET", f"/v2/checkout/orders/{order_id}")
    def open_dispute(self, order_id, reason, amount, currency, note):
        """Stands in for the buyer filing a claim in the sandbox Resolution Center (no public REST endpoint)."""
        return self.server.sandbox_buyer_open_dispute(order_id, reason, f"{amount:.2f}", note)
    def get_dispute(self, dispute_id): return self.server.request("GET", f"/v1/customer/disputes/{dispute_id}")
    def list_disputes(self, **query):
        from urllib.parse import urlencode
        q = urlencode({k: v for k, v in query.items() if v is not None})
        return self.server.request("GET", "/v1/customer/disputes" + (f"?{q}" if q else ""))

class _Sandbox:
    """Shared behaviour for real sandbox backends."""
    def buyer_approve(self, order_id):
        raise BuyerApprovalRequired("In sandbox the buyer approves in the browser via approve_url")
    def open_dispute(self, order_id, reason, amount, currency, note):
        raise NotImplementedError("Buyer-side dispute creation is not exposed by the toolkit/REST API; open it in "
                                  "the sandbox Resolution Center with the buyer account, then track via get_dispute().")

class PayPalREST(_Sandbox):
    """Plain REST client for the PayPal sandbox: OAuth2 client credentials, Orders v2, Disputes v1.

    Reliability rules (PayPal idempotency guide): every money-moving POST carries a PayPal-Request-Id that is unique per
    call type (create vs capture vs evidence) and stable across retries (uuid5 of kind + reference). Network errors,
    HTTP 429 and 5xx are retried with the SAME id, so PayPal returns the original result instead of acting twice.
    A POST without a PayPal-Request-Id is never retried. 401 refreshes the token once. Other 4xx raise immediately.
    """
    mode = "SANDBOX/REST"
    NAMESPACE = uuid.UUID("6f1c5a52-2a43-4c1e-9a55-0c1d9a1f6a10")
    RETRYABLE_EXC = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                     http.client.RemoteDisconnected, http.client.IncompleteRead)

    def __init__(self, client_id=None, secret=None, base=None, *, max_retries=3, backoff=0.5, timeout=30,
                 sleep=time.sleep, secrets_path=None):
        creds = None if (client_id and secret) else _secrets.paypal_credentials(secrets_path)
        self.cid = client_id or (creds or {}).get("client_id") or os.environ.get("PAYPAL_CLIENT_ID")
        self.secret = secret or (creds or {}).get("client_secret") or os.environ.get("PAYPAL_CLIENT_SECRET")
        if not self.cid or not self.secret:
            raise KeyError("PayPal sandbox credentials missing: create .secrets (see .secrets.example)")
        self.base = _secrets.check_base_url(base or (creds or {}).get("base_url") or SANDBOX)
        self.max_retries, self.backoff, self.timeout, self.sleep = max_retries, backoff, timeout, sleep
        self._tok, self._tok_exp = None, 0.0
        self.calls = []                      # (method, path, status or exception name); never headers or secrets

    def __repr__(self): return f"PayPalREST(base={self.base!r})"

    @classmethod
    def request_id(cls, kind, ref):
        return str(uuid.uuid5(cls.NAMESPACE, f"{kind}:{ref}"))

    # ---------------------------------------------------------------- transport
    def _once(self, method, path, data, headers):
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                t = resp.read()
                self.calls.append((method, path.split("?")[0], resp.status))
                return json.loads(t) if t else {}
        except urllib.error.HTTPError as e:
            raw = e.read() or b"{}"
            try: err = json.loads(raw)
            except ValueError: err = {"message": raw[:200].decode("utf-8", "replace")}
            self.calls.append((method, path.split("?")[0], e.code))
            exc = PayPalHTTPError(e.code, err if isinstance(err, dict) else {})
            exc.retry_after = e.headers.get("Retry-After") if e.headers else None
            raise exc from None
        except self.RETRYABLE_EXC as e:
            self.calls.append((method, path.split("?")[0], type(e).__name__))
            raise

    def _delay(self, attempt, retry_after=None):
        if retry_after:
            try: return min(30.0, max(0.0, float(retry_after)))
            except ValueError: pass
        return self.backoff * (2 ** attempt)

    def _req(self, method, path, body=None, headers=None, form=False, multipart=None):
        h = {"Accept": "application/json"}
        if multipart is not None:
            data, ctype = multipart
            h["Content-Type"] = ctype
        elif form:
            data = body.encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode() if body is not None else None
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        bearer = "Authorization" not in h
        retryable = method == "GET" or any(k.lower() == "paypal-request-id" for k in h)
        refreshed, attempt = False, 0
        while True:
            if bearer: h["Authorization"] = "Bearer " + self._token()
            try:
                return self._once(method, path, data, h)
            except PayPalHTTPError as e:
                if e.status == 401 and bearer and not refreshed:
                    self._tok, refreshed = None, True
                    continue
                if retryable and (e.status == 429 or e.status >= 500) and attempt < self.max_retries:
                    self.sleep(self._delay(attempt, getattr(e, "retry_after", None))); attempt += 1
                    continue
                raise
            except self.RETRYABLE_EXC:
                if retryable and attempt < self.max_retries:
                    self.sleep(self._delay(attempt)); attempt += 1
                    continue
                raise

    def _token(self):
        if not self._tok or time.time() >= self._tok_exp:
            basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
            t = self._req("POST", "/v1/oauth2/token", "grant_type=client_credentials",
                          {"Authorization": "Basic " + basic, "PayPal-Request-Id": self.request_id("oauth", time.time())},
                          form=True)
            self._tok = t["access_token"]
            self._tok_exp = time.time() + max(0, int(t.get("expires_in", 3600)) - 60)
        return self._tok

    # ---------------------------------------------------------------- Orders v2
    def create_order(self, amount, currency, description, reference_id, items=None):
        o = self._req("POST", "/v2/checkout/orders", build_order_body(amount, currency, description, reference_id, items),
                      {"PayPal-Request-Id": self.request_id("create", reference_id), "Prefer": "return=representation"})
        return {**o, "approve_url": _approve_link(o)}

    def capture_order(self, order_id):
        try:
            # One id per capture CALL, reused by the transport retries inside it. Not per order: a capture refused with
            # 422 ORDER_NOT_APPROVED must be retryable after the buyer approves, and PayPal may replay the stored result
            # of a request id. A second capture of the same order is refused by PayPal (ORDER_ALREADY_CAPTURED).
            c = self._req("POST", f"/v2/checkout/orders/{order_id}/capture", {},
                          {"PayPal-Request-Id": self.request_id("capture", f"{order_id}:{uuid.uuid4().hex}"),
                           "Prefer": "return=representation"})
        except PayPalHTTPError as e:
            if e.issue in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
                raise BuyerApprovalRequired(f"order {order_id}: buyer must approve first ({e.issue})") from e
            if e.issue == "ORDER_ALREADY_CAPTURED":
                o = self.get_order(order_id)
                return {**o, "capture_id": _capture_id(o), "already_captured": True}
            raise
        return {**c, "capture_id": _capture_id(c)}

    def get_order(self, order_id): return self._req("GET", f"/v2/checkout/orders/{order_id}")

    # PayPal's published sandbox test Visa (developer.paypal.com card testing). Never valid outside the sandbox.
    SANDBOX_TEST_CARD = {"number": "4032039317984658", "expiry": "2030-01", "name": "Sandbox Tester", "security_code": "123"}

    def sandbox_confirm_test_card(self, order_id):
        """Sandbox only: stand in for the buyer by confirming a PayPal test card as the order's payment source
        (POST /v2/checkout/orders/{id}/confirm-payment-source). The order becomes APPROVED; capture still goes
        through the normal guarded path. Refused unless the base URL is the sandbox."""
        if "sandbox" not in (self.base or ""):
            raise PermissionError("test-card approval is only allowed against the PayPal sandbox")
        return self._req("POST", f"/v2/checkout/orders/{order_id}/confirm-payment-source",
                         {"payment_source": {"card": dict(self.SANDBOX_TEST_CARD)}},
                         headers={"PayPal-Request-Id": self.request_id("confirm-card", order_id)})

    # ---------------------------------------------------------------- Disputes v1
    def get_dispute(self, dispute_id): return self._req("GET", f"/v1/customer/disputes/{dispute_id}")

    def list_disputes(self, **query):
        from urllib.parse import urlencode
        q = urlencode({k: v for k, v in query.items() if v is not None})
        return self._req("GET", "/v1/customer/disputes" + (f"?{q}" if q else ""))

    def find_disputes_for_capture(self, capture_id):
        return self.list_disputes(disputed_transaction_id=capture_id).get("items") or []

    def provide_evidence(self, dispute_id, notes, evidence_type="OTHER", files=None):
        """POST /v1/customer/disputes/{id}/provide-evidence (multipart: JSON part `input` + optional `evidence-file` parts).
        The customer may do this while the dispute is WAITING_FOR_BUYER_RESPONSE."""
        payload = {"evidences": [{"evidence_type": evidence_type, "notes": notes}]}
        parts = [("input", None, json.dumps(payload).encode(), "application/json")]
        for name, content, ctype in files or []:
            parts.append(("evidence-file", name, content, ctype or "application/octet-stream"))
        rid = self.request_id("evidence", dispute_id + ":" + hashlib.sha256(notes.encode()).hexdigest()[:8])
        return self._req("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                         multipart=_multipart(parts), headers={"PayPal-Request-Id": rid})

    def sandbox_require_evidence(self, dispute_id, action="BUYER_EVIDENCE"):
        """Sandbox-only: move an UNDER_REVIEW dispute to WAITING_FOR_BUYER_RESPONSE / WAITING_FOR_SELLER_RESPONSE."""
        return self._req("POST", f"/v1/customer/disputes/{dispute_id}/require-evidence", {"action": action},
                         {"PayPal-Request-Id": self.request_id("require-evidence", f"{dispute_id}:{action}")})

def _multipart(parts):
    """parts: [(field, filename|None, bytes, content_type)] -> (body, content-type header)."""
    boundary = "----guardedbuyer" + uuid.uuid4().hex
    out = []
    for field, filename, content, ctype in parts:
        disp = f'form-data; name="{field}"' + (f'; filename="{filename}"' if filename else "")
        out += [f"--{boundary}".encode(), f"Content-Disposition: {disp}".encode(), f"Content-Type: {ctype}".encode(), b"",
                content]
    out += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(out), f"multipart/form-data; boundary={boundary}"

def make_client(force_mock=False, prefer_toolkit=True, secrets_path=None):
    """MOCK unless sandbox credentials exist (.secrets first, then PAYPAL_CLIENT_ID/SECRET env); prefers the official
    toolkit when it is installed and `prefer_toolkit` is true."""
    creds = None if force_mock else _secrets.paypal_credentials(secrets_path)
    env_ok = bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))
    if force_mock or not (creds or env_ok):
        return MockPayPal()
    if prefer_toolkit:
        from .paypal_toolkit import toolkit_available, PayPalToolkitBackend
        if toolkit_available() and not creds:
            return PayPalToolkitBackend()
    return PayPalREST(secrets_path=secrets_path)
