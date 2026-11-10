"""In-memory, offline emulation of the PayPal REST sandbox.

The module implements a small but faithful subset of Orders v2 and Disputes v1:

* ``POST /v1/oauth2/token``
* ``POST /v2/checkout/orders`` (create, idempotent via ``PayPal-Request-Id``)
* ``GET  /v2/checkout/orders/{id}``
* ``POST /v2/checkout/orders/{id}/capture``
* ``GET  /v1/customer/disputes`` (search/filter)
* ``GET  /v1/customer/disputes/{id}``
* ``POST /v1/customer/disputes/{id}/accept-claim``

No network access is performed.  IDs are format-valid but start with ``MOCK``
(dispute IDs use the official-looking ``PP-D-MOCK-00001`` shape).

PayPal does not expose a public REST endpoint for a *buyer* to approve an
order or open a dispute.  ``sandbox_buyer_approve`` and
``sandbox_buyer_open_dispute`` are therefore simulator-only helpers: they
model what the sandbox personal account does in the browser / Resolution
Center.  They are deliberately not routed through ``request``.
"""

from __future__ import annotations

import copy
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qs, urlsplit

_ALNUM = string.ascii_uppercase + string.digits

# Official Disputes v1 reason enum.
DISPUTE_REASONS = frozenset({
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
    "UNAUTHORISED",
    "CREDIT_NOT_PROCESSED",
    "DUPLICATE_TRANSACTION",
    "INCORRECT_AMOUNT",
    "PAYMENT_BY_OTHER_MEANS",
    "CANCELED_RECURRING_BILLING",
    "PROBLEM_WITH_REMITTANCE",
    "OTHER",
})

_ERROR_MESSAGES = {
    "INVALID_REQUEST": "Request is not well-formed, syntactically incorrect, or violates schema.",
    "RESOURCE_NOT_FOUND": "The specified resource does not exist.",
    "UNPROCESSABLE_ENTITY": (
        "The requested action could not be performed, semantically incorrect, "
        "or failed business validation."
    ),
}

_ORDER_NOT_APPROVED_DESCRIPTION = (
    "Payer has not yet approved the Order for payment. Please redirect the payer "
    "to the 'rel':'approve' url returned as part of the HATEOAS links within the "
    "Create Order call or provide a valid payment_source in the request."
)

_DISPUTE_FULL_KEYS = (
    "dispute_id",
    "create_time",
    "update_time",
    "disputed_transactions",
    "reason",
    "status",
    "dispute_amount",
    "dispute_life_cycle_stage",
    "dispute_channel",
    "messages",
    "seller_response_due_date",
    "links",
)


class PayPalHTTPError(Exception):
    """HTTP-style error raised by :class:`MockPayPalServer`.

    ``body`` follows PayPal's official error shape and ``issue`` is the first
    issue in ``details`` (for example ``ORDER_NOT_APPROVED``).
    """

    def __init__(self, status: int, body: dict):
        self.status = int(status)
        self.body = body or {}
        details = self.body.get("details") or []
        self.issue = details[0].get("issue") if details else None
        super().__init__(
            f"PayPal HTTP {self.status}: {self.body.get('name', 'ERROR')} "
            f"({self.issue or self.body.get('message', '')})"
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(value) -> str:
    """Format a money value as a two-decimal string."""
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _dec(value):
    """Parse a money value as a two-decimal Decimal, or return None."""
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(currency: str, value) -> dict:
    return {"currency_code": str(currency), "value": _fmt(value)}


def _slug(text) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "store"


class MockPayPalServer:
    """In-memory emulation of the PayPal REST sandbox. No network.

    IDs are format-valid but start with ``MOCK``.
    """

    API = "https://api-m.sandbox.paypal.com"
    WEB = "https://www.sandbox.paypal.com"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.disputes: dict[str, dict] = {}
        self._idempotency: dict[str, str] = {}
        self._used_ids: set[str] = set()
        self._dispute_seq = 0

    # ------------------------------------------------------------------ ids
    def _random_id(self, prefix: str = "MOCK") -> str:
        while True:
            candidate = prefix + "".join(secrets.choice(_ALNUM) for _ in range(13))
            if candidate not in self._used_ids:
                self._used_ids.add(candidate)
                return candidate

    def _merchant_id(self) -> str:
        while True:
            candidate = "".join(secrets.choice(_ALNUM) for _ in range(13))
            if candidate not in self._used_ids:
                self._used_ids.add(candidate)
                return candidate

    @staticmethod
    def _dispute_link(dispute_id: str) -> dict:
        return {
            "href": f"{MockPayPalServer.API}/v1/customer/disputes/{dispute_id}",
            "rel": "self",
            "method": "GET",
        }

    # --------------------------------------------------------------- errors
    def _error(self, status: int, name: str, issue: str, description: str,
               extra_links: list | None = None) -> PayPalHTTPError:
        links = [{
            "href": (
                "https://developer.paypal.com/api/rest/reference/orders/v2/"
                f"errors/#{issue}"
            ),
            "rel": "information_link",
            "method": "GET",
        }]
        if extra_links:
            links.extend(copy.deepcopy(extra_links))
        body = {
            "name": name,
            "message": _ERROR_MESSAGES.get(name, "The request could not be processed."),
            "debug_id": secrets.token_hex(7)[:13],
            "details": [{"issue": issue, "description": description}],
            "links": links,
        }
        return PayPalHTTPError(status, body)

    # ------------------------------------------------------------- requests
    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None) -> dict:
        """Dispatch a REST-like request and return its JSON body.

        ``path`` may be a path (optionally with a query string) or a full
        PayPal URL.  Errors raise :class:`PayPalHTTPError`.
        """
        method = str(method).upper()
        parsed = urlsplit(str(path))
        route = parsed.path or "/"
        if not route.startswith("/"):
            route = "/" + route
        if len(route) > 1:
            route = route.rstrip("/")
        hdrs = {str(k).lower(): v for k, v in (headers or {}).items()}

        if method == "POST" and route == "/v1/oauth2/token":
            return self._access_token()

        if method == "POST" and route == "/v2/checkout/orders":
            return self._create_order(body if isinstance(body, dict) else {}, hdrs)

        match = re.fullmatch(r"/v2/checkout/orders/([^/]+)", route)
        if method == "GET" and match:
            return self._get_order(match.group(1))

        match = re.fullmatch(r"/v2/checkout/orders/([^/]+)/capture", route)
        if method == "POST" and match:
            return self._capture_order(match.group(1))

        if method == "GET" and route == "/v1/customer/disputes":
            return self._list_disputes(parsed.query)

        match = re.fullmatch(r"/v1/customer/disputes/([^/]+)/accept-claim", route)
        if method == "POST" and match:
            return self._accept_claim(match.group(1), body if isinstance(body, dict) else {})

        match = re.fullmatch(r"/v1/customer/disputes/([^/]+)", route)
        if method == "GET" and match:
            return self._get_dispute(match.group(1))

        raise self._error(
            404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
            "The specified resource does not exist.",
        )

    # ------------------------------------------------------------ oauth2
    def _access_token(self) -> dict:
        return {
            "scope": (
                "https://uri.paypal.com/services/payments/payment "
                "https://uri.paypal.com/services/disputes/read-write"
            ),
            "access_token": f"MOCK-ACCESS-TOKEN-{secrets.token_hex(16)}",
            "token_type": "Bearer",
            "app_id": "APP-MOCK",
            "expires_in": 32400,
            "nonce": secrets.token_hex(8),
        }

    # -------------------------------------------------------------- orders
    @staticmethod
    def _prefer_representation(headers: dict) -> bool:
        return "return=representation" in str(headers.get("prefer", "")).lower()

    @staticmethod
    def _has_experience_context(payment_source) -> bool:
        if not isinstance(payment_source, dict):
            return False
        paypal = payment_source.get("paypal")
        return isinstance(paypal, dict) and "experience_context" in paypal

    def _create_order(self, body: dict, headers: dict) -> dict:
        request_id = headers.get("paypal-request-id")
        if request_id and request_id in self._idempotency:
            order = self.orders[self._idempotency[request_id]]
            if self._prefer_representation(headers):
                return self._full_order(order)
            return self._minimal_order(order)

        if "intent" not in body:
            raise self._error(
                400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER",
                "The intent field is required.",
            )
        intent = body.get("intent")
        if intent not in ("CAPTURE", "AUTHORIZE"):
            raise self._error(
                400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                "Intent must be CAPTURE or AUTHORIZE.",
            )

        purchase_units = body.get("purchase_units")
        if not isinstance(purchase_units, list) or not purchase_units:
            raise self._error(
                400, "INVALID_REQUEST", "INVALID_ARRAY_MIN_ITEMS",
                "At least one purchase unit is required.",
            )

        units = [self._validate_unit(unit) for unit in purchase_units]

        order_id = self._random_id()
        now = _utcnow()
        experience_context = self._has_experience_context(body.get("payment_source"))
        order = {
            "id": order_id,
            "intent": intent,
            "status": "PAYER_ACTION_REQUIRED" if experience_context else "CREATED",
            "purchase_units": units,
            "create_time": now,
            "update_time": now,
            "experience_context": experience_context,
            "payer": None,
            "payment_source": None,
        }
        self.orders[order_id] = order
        if request_id:
            self._idempotency[str(request_id)] = order_id
        if self._prefer_representation(headers):
            return self._full_order(order)
        return self._minimal_order(order)

    def _validate_unit(self, unit) -> dict:
        if not isinstance(unit, dict):
            raise self._error(
                400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER",
                "Each purchase unit must be an object.",
            )
        amount = unit.get("amount")
        if not isinstance(amount, dict):
            raise self._error(
                400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER",
                "Each purchase unit requires an amount object.",
            )
        currency = amount.get("currency_code")
        value = amount.get("value")
        if not currency:
            raise self._error(
                400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER",
                "amount.currency_code is required.",
            )
        if not isinstance(value, str):
            raise self._error(
                400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                "amount.value must be a string with two decimals.",
            )
        amount_value = _dec(value)
        if amount_value is None:
            raise self._error(
                400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                "amount.value must be a decimal string.",
            )

        breakdown = amount.get("breakdown")
        items = unit.get("items")
        if isinstance(breakdown, dict):
            item_total = None
            raw_item_total = breakdown.get("item_total")
            if isinstance(raw_item_total, dict) and "value" in raw_item_total:
                item_total = _dec(raw_item_total.get("value"))
                if item_total is None:
                    raise self._error(
                        400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                        "breakdown.item_total.value must be a decimal string.",
                    )

            if item_total is not None and items:
                item_sum = Decimal("0.00")
                for item in items:
                    if not isinstance(item, dict):
                        raise self._error(
                            400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER",
                            "Each item must be an object.",
                        )
                    unit_amount = item.get("unit_amount")
                    if not isinstance(unit_amount, dict) or "value" not in unit_amount:
                        raise self._error(
                            400, "INVALID_REQUEST", "MISSING_REQUIRED_PARAMETER",
                            "Each item requires unit_amount.value.",
                        )
                    unit_value = _dec(unit_amount.get("value"))
                    if unit_value is None:
                        raise self._error(
                            400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                            "item unit_amount.value must be a decimal string.",
                        )
                    try:
                        quantity = int(item.get("quantity"))
                    except (TypeError, ValueError):
                        raise self._error(
                            400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                            "item quantity must be an integer.",
                        ) from None
                    item_sum += unit_value * quantity
                item_sum = item_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if item_sum != item_total:
                    raise self._error(
                        422, "UNPROCESSABLE_ENTITY", "ITEM_TOTAL_MISMATCH",
                        "The item total does not match the sum of the item amounts.",
                    )

            expected = item_total if item_total is not None else Decimal("0.00")
            for key in ("shipping", "tax_total"):
                component = breakdown.get(key)
                if isinstance(component, dict) and "value" in component:
                    component_value = _dec(component.get("value"))
                    if component_value is None:
                        raise self._error(
                            400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                            f"breakdown.{key}.value must be a decimal string.",
                        )
                    expected += component_value
            discount = breakdown.get("discount")
            if isinstance(discount, dict) and "value" in discount:
                discount_value = _dec(discount.get("value"))
                if discount_value is None:
                    raise self._error(
                        400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                        "breakdown.discount.value must be a decimal string.",
                    )
                expected -= discount_value
            expected = expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount_value != expected:
                raise self._error(
                    422, "UNPROCESSABLE_ENTITY", "AMOUNT_MISMATCH",
                    "The amount does not match the sum of the breakdown.",
                )

        normalized_amount = copy.deepcopy(amount)
        normalized_amount["currency_code"] = str(currency)
        normalized_amount["value"] = f"{amount_value:.2f}"
        normalized_breakdown = normalized_amount.get("breakdown")
        if isinstance(normalized_breakdown, dict):
            for component in normalized_breakdown.values():
                if isinstance(component, dict) and "value" in component:
                    component["value"] = _fmt(component["value"])
                    component.setdefault("currency_code", str(currency))

        normalized_items = []
        for item in items or []:
            normalized_item = copy.deepcopy(item)
            unit_amount = normalized_item.get("unit_amount")
            if isinstance(unit_amount, dict):
                unit_amount["value"] = _fmt(unit_amount.get("value"))
                unit_amount.setdefault("currency_code", str(currency))
            if "quantity" in normalized_item:
                normalized_item["quantity"] = str(int(normalized_item["quantity"]))
            normalized_items.append(normalized_item)

        reference_id = str(unit.get("reference_id", ""))
        normalized_unit = {
            "reference_id": reference_id,
            "description": str(unit.get("description", ""))[:127],
            "amount": normalized_amount,
            "payee": {
                "email_address": f"merchant-{_slug(reference_id)}@example.com",
                "merchant_id": self._merchant_id(),
            },
        }
        if "items" in unit:
            normalized_unit["items"] = normalized_items
        return normalized_unit

    def _order_links(self, order: dict) -> list[dict]:
        order_id = order["id"]
        self_link = {
            "href": f"{self.API}/v2/checkout/orders/{order_id}",
            "rel": "self",
            "method": "GET",
        }
        approve_link = {
            "href": f"{self.WEB}/checkoutnow?token={order_id}",
            "rel": "approve",
            "method": "GET",
        }
        capture_link = {
            "href": f"{self.API}/v2/checkout/orders/{order_id}/capture",
            "rel": "capture",
            "method": "POST",
        }
        status = order["status"]
        if status == "PAYER_ACTION_REQUIRED":
            return [self_link, {**approve_link, "rel": "payer-action"}]
        if status == "CREATED":
            return [
                self_link,
                approve_link,
                {"href": self_link["href"], "rel": "update", "method": "PATCH"},
                capture_link,
            ]
        if status == "APPROVED":
            return [self_link, capture_link]
        return [self_link]

    def _minimal_order(self, order: dict) -> dict:
        response = {"id": order["id"], "status": order["status"]}
        if order["experience_context"]:
            response["payment_source"] = {"paypal": {}}
        response["links"] = self._order_links(order)
        return response

    def _full_unit(self, unit: dict) -> dict:
        response = {
            "reference_id": unit["reference_id"],
            "description": unit.get("description", ""),
            "amount": copy.deepcopy(unit["amount"]),
        }
        if "items" in unit:
            response["items"] = copy.deepcopy(unit["items"])
        response["payee"] = copy.deepcopy(unit["payee"])
        if unit.get("payments"):
            response["payments"] = copy.deepcopy(unit["payments"])
        return response

    def _full_order(self, order: dict) -> dict:
        response = {
            "id": order["id"],
            "intent": order["intent"],
            "status": order["status"],
            "purchase_units": [self._full_unit(unit) for unit in order["purchase_units"]],
        }
        if order.get("payer"):
            response["payer"] = copy.deepcopy(order["payer"])
        if order.get("payment_source"):
            response["payment_source"] = copy.deepcopy(order["payment_source"])
        response["create_time"] = order["create_time"]
        response["update_time"] = order["update_time"]
        response["links"] = self._order_links(order)
        return response

    def _get_order(self, order_id: str) -> dict:
        order = self.orders.get(order_id)
        if order is None:
            raise self._error(
                404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
                "The specified order ID does not exist.",
            )
        return self._full_order(order)

    def _buyer_action_link(self, order: dict):
        for link in self._order_links(order):
            if link["rel"] in ("payer-action", "approve"):
                return copy.deepcopy(link)
        return None

    def _capture_order(self, order_id: str) -> dict:
        order = self.orders.get(order_id)
        if order is None:
            raise self._error(
                404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
                "The specified order ID does not exist.",
            )
        if order["status"] == "COMPLETED":
            raise self._error(
                422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED",
                "Order has already been captured.",
            )
        if order["status"] != "APPROVED":
            action_link = self._buyer_action_link(order)
            raise self._error(
                422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED",
                _ORDER_NOT_APPROVED_DESCRIPTION,
                [action_link] if action_link else None,
            )

        now = _utcnow()
        capture_units = []
        for unit in order["purchase_units"]:
            gross = unit["amount"]
            gross_value = _dec(gross["value"])
            # Illustrative fee, not PayPal's actual pricing.
            fee = (gross_value * Decimal("0.0349") + Decimal("0.49")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            net = (gross_value - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            capture_id = self._random_id()
            capture = {
                "id": capture_id,
                "status": "COMPLETED",
                "amount": copy.deepcopy(gross),
                "final_capture": True,
                "seller_protection": {
                    "status": "ELIGIBLE",
                    "dispute_categories": [
                        "ITEM_NOT_RECEIVED",
                        "UNAUTHORIZED_TRANSACTION",
                    ],
                },
                "seller_receivable_breakdown": {
                    "gross_amount": copy.deepcopy(gross),
                    "paypal_fee": _money(gross["currency_code"], fee),
                    "net_amount": _money(gross["currency_code"], net),
                },
                "create_time": now,
                "update_time": now,
                "links": [
                    {
                        "href": f"{self.API}/v2/payments/captures/{capture_id}",
                        "rel": "self",
                        "method": "GET",
                    },
                    {
                        "href": f"{self.API}/v2/payments/captures/{capture_id}/refund",
                        "rel": "refund",
                        "method": "POST",
                    },
                    {
                        "href": f"{self.API}/v2/checkout/orders/{order_id}",
                        "rel": "up",
                        "method": "GET",
                    },
                ],
            }
            unit.setdefault("payments", {"captures": []})["captures"].append(capture)
            capture_units.append({
                "reference_id": unit["reference_id"],
                "payments": copy.deepcopy(unit["payments"]),
            })

        order["status"] = "COMPLETED"
        order["update_time"] = now
        return {
            "id": order_id,
            "status": "COMPLETED",
            "payment_source": copy.deepcopy(order["payment_source"]),
            "purchase_units": capture_units,
            "payer": copy.deepcopy(order["payer"]),
            "links": [{
                "href": f"{self.API}/v2/checkout/orders/{order_id}",
                "rel": "self",
                "method": "GET",
            }],
        }

    # ------------------------------------------------ simulator helpers
    @staticmethod
    def _default_payer() -> dict:
        return {
            "name": {"given_name": "Sandbox", "surname": "Buyer"},
            "email_address": "sb-buyer@personal.example.com",
            "payer_id": "MOCKBUYER0001",
        }

    def _payer_from_buyer(self, buyer) -> dict:
        buyer = buyer if isinstance(buyer, dict) else {}
        name = buyer.get("name")
        if isinstance(name, dict):
            given_name = name.get("given_name", "Sandbox")
            surname = name.get("surname", "Buyer")
        elif isinstance(name, str) and name.strip():
            parts = name.split()
            given_name = parts[0]
            surname = " ".join(parts[1:]) or "Buyer"
        else:
            given_name = buyer.get("given_name", "Sandbox")
            surname = buyer.get("surname", "Buyer")
        return {
            "name": {"given_name": str(given_name), "surname": str(surname)},
            "email_address": str(
                buyer.get("email_address")
                or buyer.get("email")
                or "sb-buyer@personal.example.com"
            ),
            "payer_id": str(
                buyer.get("payer_id")
                or buyer.get("account_id")
                or "MOCKBUYER0001"
            ),
        }

    def sandbox_buyer_approve(self, order_id: str, buyer: dict | None = None) -> dict:
        """Simulator-only: the sandbox buyer clicks Pay. NOT a REST endpoint.

        Models the personal account opening the payer-action / approve URL.
        Only orders in ``CREATED`` or ``PAYER_ACTION_REQUIRED`` can be
        approved.
        """
        order = self.orders.get(order_id)
        if order is None:
            raise self._error(
                404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
                "The specified order ID does not exist.",
            )
        if order["status"] not in ("CREATED", "PAYER_ACTION_REQUIRED"):
            raise self._error(
                422, "UNPROCESSABLE_ENTITY", "ORDER_CANNOT_BE_APPROVED",
                "Order cannot be approved in its current state.",
            )

        payer = self._payer_from_buyer(buyer)
        order["status"] = "APPROVED"
        order["payer"] = copy.deepcopy(payer)
        order["payment_source"] = {
            "paypal": {
                "name": copy.deepcopy(payer["name"]),
                "email_address": payer["email_address"],
                "account_id": payer["payer_id"],
            }
        }
        order["update_time"] = _utcnow()
        return self._full_order(order)

    def sandbox_buyer_open_dispute(self, order_id: str, reason: str, amount: str,
                                   note: str) -> dict:
        """Simulator-only: the sandbox buyer files a claim. NOT a REST endpoint.

        PayPal has no public REST endpoint for a buyer to create a dispute;
        this models the sandbox Resolution Center UI.  The order must already
        be captured.
        """
        order = self.orders.get(order_id)
        if order is None:
            raise self._error(
                404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
                "The specified order ID does not exist.",
            )
        if order["status"] != "COMPLETED":
            raise self._error(
                422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_CAPTURED",
                "Order has not been captured.",
            )
        if reason not in DISPUTE_REASONS:
            raise self._error(
                400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE",
                f"Invalid dispute reason: {reason!r}.",
            )

        amount_value = _dec(amount)
        captures = []
        for unit in order["purchase_units"]:
            for capture in unit.get("payments", {}).get("captures", []):
                captures.append((unit, capture))
        if not captures:
            raise self._error(
                422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_CAPTURED",
                "Order has not been captured.",
            )
        captured_total = sum(
            (_dec(capture["amount"]["value"]) for _, capture in captures),
            Decimal("0.00"),
        )
        if (amount_value is None or amount_value <= Decimal("0.00")
                or amount_value > captured_total):
            raise self._error(
                422, "UNPROCESSABLE_ENTITY", "INVALID_DISPUTE_AMOUNT",
                "The dispute amount must be greater than zero and no greater "
                "than the captured amount.",
            )

        unit, capture = captures[0]
        currency = capture["amount"]["currency_code"]
        now = _utcnow()
        # Simulated seller-response window, ten days from creation.
        due_date = (
            datetime.now(timezone.utc) + timedelta(days=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        payer = order.get("payer") or {}
        payer_name = payer.get("name") or {}
        buyer_name = " ".join(
            part for part in (payer_name.get("given_name"), payer_name.get("surname"))
            if part
        ) or "Sandbox Buyer"
        payee = unit.get("payee") or {}
        seller_name = f"merchant-{_slug(unit.get('reference_id', ''))}"

        self._dispute_seq += 1
        dispute_id = f"PP-D-MOCK-{self._dispute_seq:05d}"
        dispute = {
            "dispute_id": dispute_id,
            "create_time": now,
            "update_time": now,
            "disputed_transactions": [{
                "buyer_transaction_id": self._random_id(),
                "seller_transaction_id": capture["id"],
                "reference_id": unit["reference_id"],
                "create_time": capture["create_time"],
                "transaction_status": "COMPLETED",
                "gross_amount": copy.deepcopy(capture["amount"]),
                "buyer": {"name": buyer_name},
                "seller": {
                    "email": payee.get("email_address", "merchant@example.com"),
                    "merchant_id": payee.get("merchant_id", ""),
                    "name": seller_name,
                },
            }],
            "reason": reason,
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_amount": _money(currency, amount_value),
            "dispute_life_cycle_stage": "INQUIRY",
            "dispute_channel": "INTERNAL",
            "messages": [{
                "posted_by": "BUYER",
                "time_posted": now,
                "content": str(note),
            }],
            "seller_response_due_date": due_date,
            "links": [self._dispute_link(dispute_id)],
            # Internal state used by the search/list representation.
            "dispute_state": "REQUIRED_OTHER_PARTY_ACTION",
            "_order_id": order_id,
            "_capture_id": capture["id"],
        }
        self.disputes[dispute_id] = dispute
        return self._public_dispute(dispute)

    # ------------------------------------------------------------ disputes
    def _public_dispute(self, dispute: dict) -> dict:
        response = {
            key: copy.deepcopy(dispute[key])
            for key in _DISPUTE_FULL_KEYS
            if key in dispute
        }
        if "dispute_outcome" in dispute:
            response["dispute_outcome"] = copy.deepcopy(dispute["dispute_outcome"])
        return response

    def _dispute_info(self, dispute: dict) -> dict:
        return {
            "dispute_id": dispute["dispute_id"],
            "create_time": dispute["create_time"],
            "update_time": dispute["update_time"],
            "reason": dispute["reason"],
            "status": dispute["status"],
            "dispute_state": dispute["dispute_state"],
            "dispute_amount": copy.deepcopy(dispute["dispute_amount"]),
            "dispute_life_cycle_stage": dispute["dispute_life_cycle_stage"],
            "dispute_channel": dispute["dispute_channel"],
            "seller_response_due_date": dispute["seller_response_due_date"],
            "links": copy.deepcopy(dispute["links"]),
        }

    def _list_disputes(self, query: str) -> dict:
        params = parse_qs(query or "", keep_blank_values=True)
        state = params.get("dispute_state", [None])[0]
        transaction_id = params.get("disputed_transaction_id", [None])[0]
        items = []
        for dispute in self.disputes.values():
            if state is not None and dispute.get("dispute_state") != state:
                continue
            if transaction_id is not None:
                seller_ids = {
                    tx.get("seller_transaction_id")
                    for tx in dispute.get("disputed_transactions", [])
                }
                if transaction_id not in seller_ids:
                    continue
            items.append(self._dispute_info(dispute))

        page_size = params.get("page_size", [None])[0]
        if page_size is not None:
            try:
                limit = int(page_size)
            except (TypeError, ValueError):
                limit = None
            if limit is not None and limit >= 0:
                items = items[:limit]

        href = f"{self.API}/v1/customer/disputes"
        if query:
            href += "?" + query
        return {
            "items": items,
            "links": [{"href": href, "rel": "self", "method": "GET"}],
        }

    def _get_dispute(self, dispute_id: str) -> dict:
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise self._error(
                404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
                "The specified dispute ID does not exist.",
            )
        return self._public_dispute(dispute)

    def _accept_claim(self, dispute_id: str, body: dict) -> dict:
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise self._error(
                404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID",
                "The specified dispute ID does not exist.",
            )
        note = body.get("note")
        now = _utcnow()
        dispute["status"] = "RESOLVED"
        dispute["dispute_state"] = "RESOLVED"
        dispute["dispute_outcome"] = {
            "outcome_code": "RESOLVED_BUYER_FAVOUR",
            "amount_refunded": copy.deepcopy(dispute["dispute_amount"]),
        }
        dispute["messages"].append({
            "posted_by": "SELLER",
            "time_posted": now,
            "content": "" if note is None else str(note),
        })
        dispute["update_time"] = now
        return {"links": [self._dispute_link(dispute_id)]}


class MockPayPalClient:
    """Duck-types paypal_agent_toolkit.shared.paypal_client.PayPalClient
    (get_access_token, build_headers, post(uri, payload), get(uri)) so the
    official toolkit handlers can run offline against MockPayPalServer.
    """

    def __init__(self, server: MockPayPalServer):
        self.server = server
        self._access_token = None

    def get_access_token(self) -> str:
        if not self._access_token:
            token = self.server.request("POST", "/v1/oauth2/token", None)
            self._access_token = token["access_token"]
        return self._access_token

    def build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, uri: str, payload: dict | None = None) -> dict:
        return self.server.request("POST", uri, payload)

    def get(self, uri: str) -> dict:
        return self.server.request("GET", uri)
