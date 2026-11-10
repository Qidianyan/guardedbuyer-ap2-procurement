"""Local HTTP fake of the PayPal REST API, for offline tests and `scripts/sandbox_smoke.py --fake`.

It puts a real HTTP server (127.0.0.1, random port) in front of the in-memory Orders v2 / Disputes v1 emulation
(procure/paypal_mock.py), so the *real* REST client (PayPalREST) is exercised end to end: OAuth2 client credentials,
Bearer tokens, JSON and multipart bodies, status codes, PayPal-Request-Id idempotency, and scripted faults
(5xx, 429 + Retry-After, dropped connections, expired tokens). No network beyond loopback. Not PayPal.
"""
import base64, copy, json, secrets, threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
from .paypal_mock import MockPayPalServer, PayPalHTTPError, _utcnow

class FakePayPalHTTP:
    def __init__(self, server=None, client_id="FAKE-ID", client_secret="FAKE-SECRET"):
        self.mock = server or MockPayPalServer()
        self.client_id, self.client_secret = client_id, client_secret
        self.log, self.faults, self.tokens, self.idem = [], [], set(), {}
        self.lock = threading.RLock()
        self.httpd, self.thread, self.base_url = None, None, None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        fake = self
        class H(_Handler): pass
        H.fake = fake
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown(); self.httpd.server_close(); self.httpd = None

    def __enter__(self): return self.start()
    def __exit__(self, *exc): self.stop()

    # ------------------------------------------------------------ test controls
    def fail_next(self, method, path_prefix, *, status=None, body=None, headers=None, drop=False,
                  after_processing=False, times=1):
        with self.lock:
            self.faults.append({"method": method.upper(), "prefix": path_prefix, "status": status, "body": body,
                                "headers": headers or {}, "drop": drop, "after": after_processing, "times": times})

    def expire_tokens(self):
        with self.lock: self.tokens.clear()

    def set_dispute_status(self, dispute_id, status):
        """Test hook standing in for time passing in PayPal (e.g. the case moving to UNDER_REVIEW)."""
        self.mock.disputes[dispute_id]["status"] = status
        self.mock.disputes[dispute_id]["update_time"] = _utcnow()

    def requests_to(self, method, path_prefix):
        return [r for r in self.log if r["method"] == method.upper() and r["path"].startswith(path_prefix)]

    def _take_fault(self, method, path):
        with self.lock:
            for f in self.faults:
                if f["method"] == method and path.startswith(f["prefix"]) and f["times"] > 0:
                    f["times"] -= 1
                    if f["times"] == 0: self.faults.remove(f)
                    return f
        return None

    # ------------------------------------------------------------ routing
    def handle(self, method, path, query, headers, raw):
        """Returns (status, body_dict, extra_headers)."""
        ctype = headers.get("content-type", "")
        if path == "/v1/oauth2/token" and method == "POST":
            auth = headers.get("authorization", "")
            want = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            if auth != want:
                return 401, {"error": "invalid_client", "error_description": "Client Authentication failed"}, {}
            tok = "FAKE-TOKEN-" + secrets.token_hex(12)
            with self.lock: self.tokens.add(tok)
            return 200, {"access_token": tok, "token_type": "Bearer", "expires_in": 32400, "app_id": "APP-FAKE",
                         "scope": "https://uri.paypal.com/services/payments/payment"}, {}
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return 401, {"name": "AUTHENTICATION_FAILURE", "message": "Authentication failed due to invalid "
                         "authentication credentials or a missing Authorization header.",
                         "details": [{"issue": "INVALID_TOKEN"}]}, {}
        rid = headers.get("paypal-request-id")
        key = (method, path, rid) if (rid and method == "POST") else None
        if key and key in self.idem:
            st, body = self.idem[key]
            return st, copy.deepcopy(body), {}
        st, body = self._route(method, path, query, headers, raw, ctype)
        if key and 200 <= st < 300:   # only successful results are replayed
            self.idem[key] = (st, copy.deepcopy(body))
        return st, body, {}

    def _route(self, method, path, query, headers, raw, ctype):
        m = path.rsplit("/", 1)
        try:
            if method == "POST" and path.startswith("/v1/customer/disputes/") and path.endswith("/provide-evidence"):
                return 200, self._provide_evidence(path.split("/")[4], headers, raw, ctype)
            if method == "POST" and path.startswith("/v1/customer/disputes/") and path.endswith("/require-evidence"):
                return 200, self._require_evidence(path.split("/")[4], json.loads(raw or b"{}"))
            body = json.loads(raw) if raw and "json" in ctype else None
            out = self.mock.request(method, path + (f"?{query}" if query else ""), body,
                                    {k: v for k, v in headers.items()})
            created = method == "POST" and (path == "/v2/checkout/orders" or path.endswith("/capture"))
            return (201 if created else 200), out
        except PayPalHTTPError as e:
            return e.status, e.body

    def _dispute(self, did):
        d = self.mock.disputes.get(did)
        if d is None:
            raise self.mock._error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "The specified dispute ID does not exist.")
        return d

    def _provide_evidence(self, did, headers, raw, ctype):
        d = self._dispute(did)
        parts = parse_multipart(raw, ctype)
        inp = next((p for p in parts if p["name"] == "input"), None)
        if inp is None:
            raise self.mock._error(400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER", "The input part is required.")
        if d["status"] not in ("WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE"):
            raise self.mock._error(422, "UNPROCESSABLE_ENTITY", "NOT_AVAILABLE",
                                   f"Evidence can't be provided while the dispute is {d['status']}.")
        payload = json.loads(inp["content"] or b"{}")
        files = [p["filename"] for p in parts if p["name"] == "evidence-file"]
        party = "BUYER" if d["status"] == "WAITING_FOR_BUYER_RESPONSE" else "SELLER"
        for ev in payload.get("evidences") or []:
            d.setdefault("evidences", []).append({**ev, "source": f"SUBMITTED_BY_{party}", "documents":
                                                  [{"name": f} for f in files], "date": _utcnow()})
        d["status"], d["update_time"] = "UNDER_REVIEW", _utcnow()
        return {"links": [self.mock._dispute_link(did)]}

    def _require_evidence(self, did, body):
        d = self._dispute(did)
        action = body.get("action")
        if action not in ("BUYER_EVIDENCE", "SELLER_EVIDENCE"):
            raise self.mock._error(400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE", "action must be BUYER_EVIDENCE or SELLER_EVIDENCE.")
        if d["status"] != "UNDER_REVIEW":
            raise self.mock._error(422, "UNPROCESSABLE_ENTITY", "NOT_AVAILABLE",
                                   "require-evidence needs a dispute in UNDER_REVIEW (sandbox only).")
        d["status"] = "WAITING_FOR_BUYER_RESPONSE" if action == "BUYER_EVIDENCE" else "WAITING_FOR_SELLER_RESPONSE"
        d["update_time"] = _utcnow()
        return {"links": [self.mock._dispute_link(did)]}

def parse_multipart(raw, ctype):
    """multipart/form-data -> [{"name", "filename", "content_type", "content"}] (stdlib email parser)."""
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + (raw or b""))
    out = []
    if not msg.is_multipart(): return out
    for part in msg.iter_parts():
        out.append({"name": part.get_param("name", header="content-disposition"),
                    "filename": part.get_filename(), "content_type": part.get_content_type(),
                    "content": part.get_payload(decode=True) or b""})
    return out

def _summary(headers, raw):
    ctype = headers.get("content-type", "")
    if not raw: return None
    if "json" in ctype:
        try: return json.loads(raw)
        except ValueError: return raw[:200].decode("utf-8", "replace")
    if "x-www-form-urlencoded" in ctype:
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    if "multipart/form-data" in ctype:
        return [{"name": p["name"], "filename": p["filename"], "content_type": p["content_type"],
                 "content": (p["content"].decode("utf-8", "replace") if p["content_type"] == "application/json"
                             else f"<{len(p['content'])} bytes>")} for p in parse_multipart(raw, ctype)]
    return f"<{len(raw)} bytes>"

def _masked(headers):
    h = dict(headers)
    a = h.get("authorization")
    if a: h["authorization"] = a.split(" ", 1)[0] + " …"
    return h

class _Handler(BaseHTTPRequestHandler):
    fake = None
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _do(self, method):
        u = urlsplit(self.path)
        headers = {k.lower(): v for k, v in self.headers.items()}
        n = int(headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        fake = self.fake
        entry = {"method": method, "path": u.path, "query": u.query, "headers": _masked(headers),
                 "body": _summary(headers, raw), "status": None}
        fake.log.append(entry)
        f = fake._take_fault(method, u.path)
        if f and not f["after"]:
            return self._fault(f, entry)
        status, body, extra = fake.handle(method, u.path, u.query, headers, raw)
        if f:   # after_processing: PayPal did the work, but the client never sees the answer
            return self._fault(f, entry)
        entry["status"] = status
        self._reply(status, body, extra)
    def _fault(self, f, entry):
        if f["drop"]:
            entry["status"] = "dropped"
            self.close_connection = True
            try: self.connection.shutdown(2)
            except OSError: pass
            return
        entry["status"] = f["status"]
        self._reply(f["status"], f["body"] or {"name": "INTERNAL_SERVER_ERROR", "message": "fault injected by test"},
                    f["headers"])
    def _reply(self, status, body, extra):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self): self._do("GET")
    def do_POST(self): self._do("POST")
    def do_PATCH(self): self._do("PATCH")
