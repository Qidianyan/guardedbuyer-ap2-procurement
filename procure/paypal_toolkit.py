"""PayPal backend built on the official PayPal Agent Toolkit (PyPI `paypal-agent-toolkit`, tested with 1.11.0).

How the toolkit is meant to be used (README of github.com/paypal/agent-toolkit, Python):
    toolkit = PayPalToolkit(client_id, secret, Configuration(actions={...}, context=Context(sandbox=True)))
    tools = toolkit.get_openai_chat_tools();  toolkit.get_paypal_api().run(method, params)
`paypal_agent_toolkit.openai.toolkit` imports the `openai-agents` package at module level, and the wheel pins
langchain / crewai-tools / openai-agents for its framework adapters. We use the shared layer those classes wrap
(`PayPalAPI`, `Configuration`, `is_tool_allowed`, the `tools` registry) with the same action filter, so only
`pip install --no-deps paypal-agent-toolkit pydantic requests httpx python-dotenv` is needed.

Design decision: the toolkit's tools are NEVER handed to the LLM. Giving a model `create_order` / `pay_order`
directly would let it pay without the AP2 mandate. They are called only by ProcurementAgent.execute(), after the
deterministic policy + approval + mandate checks.

Offline: `PayPalToolkitBackend.offline()` swaps the toolkit's HTTP client for MockPayPalClient, so the official
toolkit handlers (order payload builder, capture parsing) run unchanged against the local Orders v2 / Disputes v1
emulation. Known toolkit limitations: create_order sends no reference_id / PayPal-Request-Id (no idempotency key),
currency is limited to USD, and there is no tool for a BUYER to open a dispute (Disputes v1 has no such endpoint).
"""
import json, os
from .paypal_mock import MockPayPalClient, MockPayPalServer, PayPalHTTPError

RETURN_URL = os.environ.get("PAYPAL_RETURN_URL", "http://localhost:8000/paypal/return")
CANCEL_URL = os.environ.get("PAYPAL_CANCEL_URL", "http://localhost:8000/paypal/cancel")
TOOLKIT_ACTIONS = {"orders": {"create": True, "capture": True, "get": True},
                   "disputes": {"list": True, "get": True}}

def toolkit_available():
    try:
        import paypal_agent_toolkit.shared.api  # noqa: F401
        return True
    except Exception:
        return False

def _approve_link(order):
    for l in order.get("links", []):
        if l.get("rel") in ("payer-action", "approve"): return l.get("href")
    return None

def _issue(exc):
    """Extract the PayPal error `issue` from a mock or real (requests) HTTP error."""
    if isinstance(exc, PayPalHTTPError): return exc.issue
    resp = getattr(exc, "response", None)
    try: return (resp.json().get("details") or [{}])[0].get("issue")
    except Exception: return None

class PayPalToolkitBackend:
    def __init__(self, client_id=None, secret=None, api=None, mode="SANDBOX/agent-toolkit", server=None):
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Configuration, Context, is_tool_allowed
        from paypal_agent_toolkit.shared.tools import tools
        self.configuration = Configuration(actions=TOOLKIT_ACTIONS, context=Context(sandbox=True))
        self._tools = [t for t in tools if is_tool_allowed(t, self.configuration)]
        self.allowed_methods = {t["method"] for t in self._tools}
        self.api = api or PayPalAPI(client_id or os.environ["PAYPAL_CLIENT_ID"],
                                    secret or os.environ["PAYPAL_CLIENT_SECRET"], self.configuration.context)
        self.mode, self.server = mode, server

    @classmethod
    def offline(cls, server=None):
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
        server = server or MockPayPalServer()
        api = PayPalAPI("mock-client-id", "mock-secret", Context(sandbox=True))
        api._paypal_client = MockPayPalClient(server)       # no network: official handlers -> local emulation
        return cls(api=api, mode="MOCK/agent-toolkit", server=server)

    def openai_tool_schemas(self):
        """Same structure as PayPalToolkit.get_openai_chat_tools(); for inspection/docs only (not given to the LLM)."""
        return [{"type": "function", "function": {"name": t["method"], "description": t["description"],
                                                  "parameters": t["args_schema"].model_json_schema()}}
                for t in self._tools]

    def _run(self, method, params):
        if method not in self.allowed_methods: raise PermissionError(f"toolkit method {method} not enabled")
        return json.loads(self.api.run(method, params))

    # ---- adapter interface used by ProcurementAgent ----
    def create_order(self, amount, currency, description, reference_id, items=None):
        items = items or [{"name": description, "sku": "ITEM", "unit_price": amount, "quantity": 1}]
        tk_items = [{"name": i["name"][:127], "quantity": int(i["quantity"]), "item_cost": round(i["unit_price"], 2),
                     "item_total": round(i["unit_price"] * int(i["quantity"]), 2),
                     "description": f"SKU {i.get('sku', '')} / {reference_id}"[:127]} for i in items]
        o = self._run("create_order", {"currency_code": currency, "items": tk_items,
                                       "return_url": RETURN_URL, "cancel_url": CANCEL_URL})
        o["approve_url"] = _approve_link(o)
        return o

    def buyer_approve(self, order_id):
        if self.server is None:
            from .paypal import BuyerApprovalRequired
            raise BuyerApprovalRequired("In sandbox the buyer approves in the browser via approve_url")
        return self.server.sandbox_buyer_approve(order_id)

    def capture_order(self, order_id):
        from .paypal import BuyerApprovalRequired
        try:
            r = self._run("pay_order", {"order_id": order_id})
        except Exception as e:
            if _issue(e) in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
                raise BuyerApprovalRequired(f"order {order_id}: buyer must approve first ({_issue(e)})") from e
            raise
        raw = r.get("raw") or {}
        caps = ((raw.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}]
        return {**raw, "status": r.get("status"), "capture_id": caps[0].get("id"), "toolkit_message": r.get("message")}

    def get_order(self, order_id):
        return self._run("get_order_details", {"order_id": order_id}).get("raw") or {}

    def open_dispute(self, order_id, reason, amount, currency, note):
        if self.server is None:
            raise NotImplementedError("Buyer-side dispute creation is not exposed by the toolkit/REST API; open it in "
                                      "the sandbox Resolution Center with the buyer account, then track via get_dispute().")
        return self.server.sandbox_buyer_open_dispute(order_id, reason, f"{amount:.2f}", note)

    def get_dispute(self, dispute_id): return self._run("get_dispute", {"dispute_id": dispute_id})
    def list_disputes(self, **query): return self._run("list_disputes", query)
