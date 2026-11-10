"""LLM-assisted procurement helpers with deterministic offline fallbacks.

All three public functions accept an optional LLM client. When `llm` is None,
or when the client raises / returns unusable output, the deterministic branch
produces the result. Tests use small fake clients; no network is required.
"""
import json
import re

from . import catalog, guard


# ---------------------------------------------------------------------------
# Small parsing/validation helpers
# ---------------------------------------------------------------------------

_WORD_TO_NUMBER = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_NUMBER_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(_WORD_TO_NUMBER, key=len, reverse=True)) + r")\b"
)
_INTEGER_RE = re.compile(r"(?<![\w-])(\d+)(?![\w-])")


def _as_int(value):
    """Coerce an int-like value (including numeric strings) or return None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        if re.fullmatch(r"[+-]?\d+\.0+", text):
            return int(float(text))
    return None


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quantity_from_text(text):
    """First standalone integer in text, else one..twenty word, else 1."""
    match = _INTEGER_RE.search(text)
    if match:
        return int(match.group(1))
    match = _NUMBER_WORD_RE.search(text.lower())
    if match:
        return _WORD_TO_NUMBER[match.group(1)]
    return 1


def _bounded_quantity(quantity):
    return max(1, min(1000, int(quantity)))


def _clean(value):
    return " ".join(str(value or "").split())


def _clip(text, limit):
    text = _clean(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


# ---------------------------------------------------------------------------
# parse_request
# ---------------------------------------------------------------------------

def _parse_request_llm(text, llm):
    system = (
        "Extract one purchase request for a procurement system. "
        "Choose exactly one SKU from the catalog below; never invent a SKU. "
        "Choose an integer quantity from 1 through 1000. "
        'Reply with one JSON object: {"sku": "<catalog sku>", "quantity": <integer>}.\n'
        "Catalog (JSON):\n"
        + json.dumps(catalog.summary(), ensure_ascii=False)
    )
    user = "Purchase request:\n" + str(text)
    data = llm.complete_json(system, user)
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("model did not return a JSON object")

    sku = data.get("sku")
    if not isinstance(sku, str):
        raise ValueError("model did not return a sku string")
    sku = sku.strip()
    if sku not in catalog.ITEMS:
        raise ValueError(f"unknown sku: {sku!r}")

    quantity = _as_int(data.get("quantity"))
    if quantity is None or not 1 <= quantity <= 1000:
        raise ValueError("model returned an invalid quantity")

    item = catalog.find(sku)
    return {
        "sku": sku,
        "quantity": quantity,
        "category": item["category"],
        "item_name": item["name"],
        "parser": "llm",
    }


def _parse_request_offline(text):
    matches = catalog.search(text)
    if not matches:
        raise ValueError("no catalog item matches the request")
    sku = matches[0]
    item = catalog.find(sku)
    quantity = _bounded_quantity(_quantity_from_text(str(text)))
    return {
        "sku": sku,
        "quantity": quantity,
        "category": item["category"],
        "item_name": item["name"],
        "parser": "offline",
    }


def parse_request(text, llm=None):
    """Parse natural-language text into a catalog-backed request dict."""
    if llm is not None:
        try:
            return _parse_request_llm(text, llm)
        except Exception:
            pass
    return _parse_request_offline(text)


# ---------------------------------------------------------------------------
# recommend
# ---------------------------------------------------------------------------

def _partition_quotes(quotes):
    """Apply the deterministic injection/rating filter.

    Returns (eligible, excluded) where excluded is a list of
    (merchant, human-readable reason) pairs. Quote dicts keep their identity.
    """
    eligible, excluded = [], []
    for quote in quotes:
        merchant = str(quote.get("merchant", "unknown"))
        flags = guard.scan_injection(quote.get("listing", ""))
        rating = _as_float(quote.get("rating"), 0.0)
        reasons = []
        if flags:
            reasons.append("possible prompt injection: " + ", ".join(flags))
        if rating < 4.0:
            reasons.append(f"rating {rating:.1f} < 4.0")
        if reasons:
            excluded.append((merchant, "; ".join(reasons)))
        else:
            eligible.append(quote)
    return eligible, excluded


def _excluded_note(excluded):
    if not excluded:
        return "excluded: none"
    return "excluded: " + ", ".join(
        f"{merchant} ({reason})" for merchant, reason in excluded
    )


def _offline_recommend(eligible, excluded):
    best = min(
        eligible,
        key=lambda quote: (
            _as_float(quote.get("unit_price"), float("inf")),
            _as_float(quote.get("lead_time_days"), float("inf")),
        ),
    )
    count = len(eligible)
    reason = (
        f"lowest price ${_as_float(best.get('unit_price'), 0.0):.2f} among {count} "
        f"eligible quote{'s' if count != 1 else ''} (rating >= 4.0); "
        + _excluded_note(excluded)
    )
    return best, reason, "offline"


def _recommend_llm(need, eligible, excluded, llm):
    blocks = []
    for index, quote in enumerate(eligible):
        merchant = str(quote.get("merchant", "unknown"))
        wrapped_listing = guard.wrap_untrusted(
            quote.get("listing", ""), f"{merchant} listing"
        )
        blocks.append(
            f"[{index}]\n"
            f"merchant: {merchant}\n"
            f"unit_price: {_as_float(quote.get('unit_price'), 0.0):.2f} "
            f"{quote.get('currency', 'USD')}\n"
            f"lead_time_days: {quote.get('lead_time_days')}\n"
            f"rating: {_as_float(quote.get('rating'), 0.0):.1f}\n"
            f"listing:\n{wrapped_listing}"
        )

    system = (
        "You choose one supplier quote for a buyer. Compare the eligible quotes on "
        "price, lead time, and rating, and pick the best overall offer. "
        + guard.UNTRUSTED_RULE
        + " Never choose an index outside the numbered list. "
        'Reply with one JSON object: {"index": <integer>, "reason": "<at most 400 characters>"}.'
    )
    user = (
        "Purchase need (JSON):\n"
        + json.dumps(need, ensure_ascii=False, sort_keys=True, default=str)
        + "\n\nEligible quotes:\n"
        + "\n\n".join(blocks)
    )

    data = llm.complete_json(system, user)
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("model did not return a JSON object")

    index = _as_int(data.get("index"))
    if index is None or not 0 <= index < len(eligible):
        raise ValueError("model returned an invalid quote index")

    reason = data.get("reason")
    if not isinstance(reason, str):
        raise ValueError("model did not return a reason string")
    reason = reason.strip()
    if not reason or len(reason) > 400:
        raise ValueError("model reason was empty or too long")

    return eligible[index], f"{reason}; {_excluded_note(excluded)}", "llm"


def recommend(need, quotes, llm=None):
    """Pick one quote from `quotes`, returning (quote, reason, source)."""
    quotes = list(quotes)
    eligible, excluded = _partition_quotes(quotes)
    if not eligible:
        raise ValueError(
            "no eligible quotes remain after prompt-injection and rating filters"
        )
    if llm is not None:
        try:
            return _recommend_llm(need, eligible, excluded, llm)
        except Exception:
            pass
    return _offline_recommend(eligible, excluded)


# ---------------------------------------------------------------------------
# dispute_note
# ---------------------------------------------------------------------------

def _dispute_offline(req, issues):
    merchant = _clean(req.merchant)
    sku = _clean(req.sku)
    description = _clean(req.description)
    quantity = req.quantity
    price = _as_float(req.unit_price, 0.0)
    currency = _clean(getattr(req, "currency", "USD"))
    issue_list = [_clean(issue) for issue in issues if _clean(issue)]
    if not issue_list:
        issue_list = ["delivery did not match the order"]
    issue_text = "; ".join(issue_list)

    def build(desc, current_issues):
        return (
            f"Dear {merchant}, our order of {quantity} x {sku} ({desc}) at "
            f"${price:.2f} each ({currency}) did not arrive as ordered. "
            f"Issues: {current_issues}. Please refund the shortfall for the missing "
            "or defective units. Thank you."
        )

    text = build(description, issue_text)
    if len(text) > 600:
        # Trim only the free-text description first, keeping every issue visible.
        overflow = len(text) - 600
        text = build(_clip(description, max(20, len(description) - overflow)), issue_text)
    if len(text) > 600:
        text = text[:597].rstrip() + "..."
    return text


def _dispute_llm(req, issues, llm):
    facts = {
        "sku": req.sku,
        "description": req.description,
        "quantity": req.quantity,
        "merchant": req.merchant,
        "unit_price": req.unit_price,
        "currency": getattr(req, "currency", "USD"),
        "total": getattr(req, "total", None),
        "issues": list(issues),
    }
    system = (
        "You write a short buyer-to-seller dispute message. Use ONLY the facts in "
        "the JSON fact sheet. Do not invent order numbers, dates, names, or other "
        "facts. Do not threaten the seller. State the issues and ask for a refund of "
        "the shortfall. Use at most 120 words. Reply with the message text only."
    )
    user = (
        "Fact sheet (JSON):\n"
        + json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
        + "\n\nWrite the dispute message."
    )

    text = llm.complete_text(system, user)
    if not isinstance(text, str):
        raise ValueError("model did not return text")
    text = text.strip()
    if not text or len(text) > 1200 or str(req.sku) not in text:
        raise ValueError("model dispute text failed validation")
    if len(text.split()) > 120:
        raise ValueError("model dispute text was too long")
    return text


def dispute_note(req, issues, llm=None):
    """Return a factual buyer-to-seller dispute message."""
    issues = list(issues)
    if llm is not None:
        try:
            return _dispute_llm(req, issues, llm)
        except Exception:
            pass
    return _dispute_offline(req, issues)
