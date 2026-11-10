"""AP2 Intent Mandate issuance and verification with a local Ed25519 key.

The `intent` object mirrors the field names of AP2's `IntentMandate`
(google-agentic-commerce/AP2, code/sdk/python/ap2/models/mandate.py, commit e1ea56d):
  user_cart_confirmation_required, natural_language_description, merchants, skus,
  requires_refundability, intent_expiry.
AP2's IntentMandate has no amount/budget field, so spend limits live in a clearly
separated `x_constraints` extension that WE define and enforce. The signature is a
local detached Ed25519 signature over canonical JSON (not an official AP2 credential format).
"""
import base64, json, uuid
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

def _canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()
def _now(): return datetime.now(timezone.utc)

class MandateError(Exception): pass

class MandateIssuer:
    def __init__(self, key: Ed25519PrivateKey | None = None, key_id="company-procurement-key-1"):
        self.key, self.key_id = key or Ed25519PrivateKey.generate(), key_id
    def public_key_b64(self):
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def build_payload(self, req, approvals, ttl_minutes=60, refundable=True, mandate_id=None):
        """The exact payload `issue()` signs (also used to preview a mandate before release)."""
        intent = {
            "user_cart_confirmation_required": False,  # allowed: mandate is signed on the user's behalf
            "natural_language_description": f"{req.quantity} x {req.description} for {req.department}",
            "merchants": [req.merchant],
            "skus": [req.sku],
            "requires_refundability": refundable,
            "intent_expiry": (_now() + timedelta(minutes=ttl_minutes)).isoformat(),
        }
        return {"mandate_id": mandate_id or str(uuid.uuid4()), "type": "ap2.IntentMandate", "intent": intent,
                "x_constraints": {"max_amount": {"currency": req.currency, "value": req.total},
                                  "category": req.category, "max_quantity": req.quantity,
                                  "requester": req.requester, "approvals": sorted(approvals)},
                "issued_at": _now().isoformat(), "kid": self.key_id}

    def issue(self, req, approvals, ttl_minutes=60, refundable=True):
        payload = self.build_payload(req, approvals, ttl_minutes, refundable)
        sig = base64.b64encode(self.key.sign(_canon(payload))).decode()
        return {"payload": payload, "alg": "Ed25519", "signature": sig}

class MandateVerifier:
    def __init__(self, public_key_b64):
        self.pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    def verify(self, signed, *, merchant, sku, amount, currency, quantity, now=None):
        try:
            self.pub.verify(base64.b64decode(signed["signature"]), _canon(signed["payload"]))
        except Exception as e:
            raise MandateError("invalid signature") from e
        p = signed["payload"]; it, c = p["intent"], p["x_constraints"]
        if (now or _now()) > datetime.fromisoformat(it["intent_expiry"]): raise MandateError("mandate expired")
        if it["merchants"] and merchant not in it["merchants"]: raise MandateError(f"merchant {merchant} not in mandate")
        if it["skus"] and sku not in it["skus"]: raise MandateError(f"sku {sku} not in mandate")
        if currency != c["max_amount"]["currency"]: raise MandateError("currency mismatch")
        if amount > c["max_amount"]["value"] + 1e-9: raise MandateError(f"amount {amount} exceeds mandate {c['max_amount']['value']}")
        if quantity > c["max_quantity"]: raise MandateError("quantity exceeds mandate")
        return True
