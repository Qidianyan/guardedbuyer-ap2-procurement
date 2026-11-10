import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procure import ai, catalog, guard
from procure.llm import LLMError
from procure.policy import PurchaseRequest


class FakeJSONLLM:
    """Minimal stand-in for llm.complete_json; records every prompt."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.payload

    def complete_text(self, system, user):
        raise AssertionError("complete_text should not be used here")


class FakeTextLLM:
    """Minimal stand-in for llm.complete_text; records every prompt."""

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []

    def complete_text(self, system, user):
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.text

    def complete_json(self, system, user):
        raise AssertionError("complete_json should not be used here")


class TestCatalog(unittest.TestCase):
    def test_required_items_categories_and_prices(self):
        expected = {
            "PAPER-A4-500": (
                "office_supplies",
                {"OfficeHub": 42.00, "PaperDirect": 39.50, "QuickSupply": 45.00},
            ),
            "LAPTOP-14-PRO": (
                "it_hardware",
                {"TechMart": 1100.00, "ByteStore": 1149.00, "BudgetPC": 989.00},
            ),
            "TONER-BK-26A": (
                "office_supplies",
                {"InkWorld": 55.00, "PrintPlus": 58.00},
            ),
            "HUB-USBC-7": (
                "it_hardware",
                {"CableCo": 34.00, "GadgetGuru": 31.00, "HubWorks": 36.00},
            ),
            "GC-100": ("gift_cards", {"CardsNow": 100.00}),
        }
        self.assertEqual(set(catalog.ITEMS), set(expected))
        for sku, (category, prices) in expected.items():
            item = catalog.ITEMS[sku]
            self.assertEqual(item["sku"], sku)
            self.assertEqual(item["category"], category)
            self.assertIsInstance(item["keywords"], list)
            self.assertEqual(
                {quote["merchant"]: quote["unit_price"] for quote in item["quotes"]},
                prices,
            )
            for quote in item["quotes"]:
                self.assertEqual(quote["currency"], "USD")
                self.assertIsInstance(quote["lead_time_days"], int)
                self.assertTrue(0.0 <= quote["rating"] <= 5.0)
                self.assertIsInstance(quote["listing"], str)

    def test_search_paper_first(self):
        self.assertEqual(
            catalog.search("We need 10 boxes of A4 paper")[0], "PAPER-A4-500"
        )

    def test_search_unknown_returns_empty(self):
        self.assertEqual(catalog.search("a moon rocket engine"), [])

    def test_find_returns_a_deep_copy(self):
        item = catalog.find("PAPER-A4-500")
        item["name"] = "mutated"
        item["quotes"][0]["unit_price"] = 1.0
        fresh = catalog.find("PAPER-A4-500")
        self.assertEqual(fresh["name"], "A4 copy paper, box of 5 reams")
        self.assertEqual(fresh["quotes"][0]["unit_price"], 42.00)
        self.assertEqual(catalog.ITEMS["PAPER-A4-500"]["quotes"][0]["unit_price"], 42.00)

    def test_find_unknown_raises_key_error(self):
        with self.assertRaises(KeyError):
            catalog.find("NOPE-404")

    def test_all_items_are_copies_in_stable_order(self):
        items = catalog.all_items()
        self.assertEqual([item["sku"] for item in items], list(catalog.ITEMS))
        items[0]["name"] = "mutated"
        self.assertNotEqual(catalog.ITEMS[items[0]["sku"]]["name"], "mutated")

    def test_summary_has_prompting_fields_only(self):
        rows = catalog.summary()
        self.assertEqual(len(rows), len(catalog.ITEMS))
        for row in rows:
            self.assertEqual(set(row), {"sku", "name", "category", "unit"})
            self.assertNotIn("quotes", row)

    def test_gadgetguru_listing_is_flagged_by_guard(self):
        listing = catalog.find("HUB-USBC-7")["quotes"][1]["listing"]
        self.assertEqual(catalog.find("HUB-USBC-7")["quotes"][1]["merchant"], "GadgetGuru")
        flags = guard.scan_injection(listing)
        self.assertTrue(flags)
        self.assertIn("override_instructions", flags)
        self.assertIn("fake_approval", flags)
        self.assertIn("tool_manipulation", flags)
        self.assertIn("extra_purchase", flags)


class TestParseRequestOffline(unittest.TestCase):
    def test_required_offline_examples(self):
        cases = [
            ("We need 10 boxes of A4 paper for the office", "PAPER-A4-500", 10),
            ("Buy 2 laptops for the new engineers", "LAPTOP-14-PRO", 2),
            ("Order 4 black toner cartridges", "TONER-BK-26A", 4),
            ("Please buy five USB-C hubs", "HUB-USBC-7", 5),
        ]
        for text, sku, quantity in cases:
            with self.subTest(text=text):
                result = ai.parse_request(text)
                self.assertEqual(result["sku"], sku)
                self.assertEqual(result["quantity"], quantity)
                self.assertEqual(result["parser"], "offline")
                self.assertEqual(result["category"], catalog.ITEMS[sku]["category"])
                self.assertEqual(result["item_name"], catalog.ITEMS[sku]["name"])

    def test_default_quantity_is_one(self):
        result = ai.parse_request("We need A4 paper")
        self.assertEqual(result["sku"], "PAPER-A4-500")
        self.assertEqual(result["quantity"], 1)

    def test_digits_inside_model_names_are_not_quantity(self):
        result = ai.parse_request("Please buy two USB-C docks")
        self.assertEqual(result["sku"], "HUB-USBC-7")
        self.assertEqual(result["quantity"], 2)

    def test_no_match_raises_value_error(self):
        with self.assertRaises(ValueError):
            ai.parse_request("Please order a completely unknown product")


class TestParseRequestLLM(unittest.TestCase):
    def test_valid_llm_json_is_used(self):
        fake = FakeJSONLLM({"sku": "TONER-BK-26A", "quantity": "3"})
        result = ai.parse_request("some request text", fake)
        self.assertEqual(result["parser"], "llm")
        self.assertEqual(result["sku"], "TONER-BK-26A")
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(result["category"], "office_supplies")
        self.assertEqual(result["item_name"], "Black toner cartridge 26A")
        self.assertIn("PAPER-A4-500", fake.calls[0][0])
        self.assertIn("Catalog", fake.calls[0][0])

    def test_unknown_sku_falls_back(self):
        fake = FakeJSONLLM({"sku": "NOT-IN-CATALOG", "quantity": 2})
        result = ai.parse_request("Order 4 black toner cartridges", fake)
        self.assertEqual(result["parser"], "offline")
        self.assertEqual(result["sku"], "TONER-BK-26A")
        self.assertEqual(result["quantity"], 4)

    def test_zero_quantity_falls_back(self):
        fake = FakeJSONLLM({"sku": "PAPER-A4-500", "quantity": 0})
        result = ai.parse_request("We need 10 boxes of A4 paper for the office", fake)
        self.assertEqual(result["parser"], "offline")
        self.assertEqual(result["sku"], "PAPER-A4-500")
        self.assertEqual(result["quantity"], 10)

    def test_llm_error_falls_back(self):
        fake = FakeJSONLLM(error=LLMError("boom"))
        result = ai.parse_request("Buy 2 laptops for the new engineers", fake)
        self.assertEqual(result["parser"], "offline")
        self.assertEqual(result["sku"], "LAPTOP-14-PRO")
        self.assertEqual(result["quantity"], 2)

    def test_non_dict_output_falls_back(self):
        fake = FakeJSONLLM(["PAPER-A4-500", 10])
        result = ai.parse_request("Order 4 black toner cartridges", fake)
        self.assertEqual(result["parser"], "offline")
        self.assertEqual(result["sku"], "TONER-BK-26A")


class TestRecommendOffline(unittest.TestCase):
    def test_paper_picks_paperdirect(self):
        quotes = catalog.find("PAPER-A4-500")["quotes"]
        best, reason, source = ai.recommend({"sku": "PAPER-A4-500", "quantity": 10}, quotes)
        self.assertIs(best, quotes[1])
        self.assertEqual(best["merchant"], "PaperDirect")
        self.assertEqual(best["unit_price"], 39.50)
        self.assertEqual(source, "offline")
        self.assertIn("lowest price $39.50", reason)
        self.assertIn("excluded: none", reason)

    def test_laptop_excludes_low_rating_budgetpc(self):
        quotes = catalog.find("LAPTOP-14-PRO")["quotes"]
        best, reason, source = ai.recommend({"sku": "LAPTOP-14-PRO", "quantity": 2}, quotes)
        self.assertIs(best, quotes[0])
        self.assertEqual(best["merchant"], "TechMart")
        self.assertEqual(source, "offline")
        self.assertIn("BudgetPC", reason)
        self.assertIn("3.2", reason)
        self.assertIn("< 4.0", reason)

    def test_hub_excludes_injected_gadgetguru(self):
        quotes = catalog.find("HUB-USBC-7")["quotes"]
        best, reason, source = ai.recommend({"sku": "HUB-USBC-7", "quantity": 5}, quotes)
        self.assertIs(best, quotes[0])
        self.assertEqual(best["merchant"], "CableCo")
        self.assertEqual(source, "offline")
        self.assertIn("GadgetGuru", reason)
        self.assertIn("prompt injection", reason)

    def test_no_eligible_quotes_raises(self):
        gadgetguru = catalog.find("HUB-USBC-7")["quotes"][1]
        with self.assertRaises(ValueError):
            ai.recommend({"sku": "HUB-USBC-7", "quantity": 1}, [gadgetguru])
        budgetpc = catalog.find("LAPTOP-14-PRO")["quotes"][2]
        with self.assertRaises(ValueError):
            ai.recommend({"sku": "LAPTOP-14-PRO", "quantity": 1}, [budgetpc])

    def test_price_tie_uses_shorter_lead_time(self):
        quotes = [
            {
                "merchant": "SlowCo",
                "unit_price": 50.0,
                "currency": "USD",
                "lead_time_days": 9,
                "rating": 4.5,
                "listing": "A plain product description.",
            },
            {
                "merchant": "FastCo",
                "unit_price": 50.0,
                "currency": "USD",
                "lead_time_days": 2,
                "rating": 4.5,
                "listing": "Another plain product description.",
            },
        ]
        best, _, source = ai.recommend({"sku": "X", "quantity": 1}, quotes)
        self.assertIs(best, quotes[1])
        self.assertEqual(best["merchant"], "FastCo")
        self.assertEqual(source, "offline")


class TestRecommendLLM(unittest.TestCase):
    def test_valid_index_selects_that_eligible_quote(self):
        quotes = catalog.find("PAPER-A4-500")["quotes"]
        fake = FakeJSONLLM({"index": 1, "reason": "best balance of price and rating"})
        best, reason, source = ai.recommend({"sku": "PAPER-A4-500", "quantity": 10}, quotes, fake)
        self.assertIs(best, quotes[1])
        self.assertEqual(best["merchant"], "PaperDirect")
        self.assertEqual(source, "llm")
        self.assertIn("best balance of price and rating", reason)
        self.assertIn("excluded: none", reason)

    def test_out_of_range_index_falls_back(self):
        quotes = catalog.find("PAPER-A4-500")["quotes"]
        fake = FakeJSONLLM({"index": 99, "reason": "nope"})
        best, reason, source = ai.recommend({"sku": "PAPER-A4-500", "quantity": 10}, quotes, fake)
        self.assertIs(best, quotes[1])
        self.assertEqual(best["merchant"], "PaperDirect")
        self.assertEqual(source, "offline")
        self.assertIn("lowest price $39.50", reason)

    def test_injected_listing_is_not_sent_to_the_model(self):
        quotes = catalog.find("HUB-USBC-7")["quotes"]
        fake = FakeJSONLLM({"index": 0, "reason": "good value"})
        best, _, source = ai.recommend({"sku": "HUB-USBC-7", "quantity": 5}, quotes, fake)
        self.assertIs(best, quotes[0])
        self.assertEqual(best["merchant"], "CableCo")
        self.assertEqual(source, "llm")
        prompt = fake.calls[0][0] + "\n" + fake.calls[0][1]
        self.assertNotIn("IMPORTANT SYSTEM NOTICE", prompt)
        self.assertNotIn("submit quantity 400", prompt)
        self.assertNotIn("approve_purchase", prompt)
        self.assertNotIn("Also buy 25", prompt)
        self.assertIn("<untrusted", prompt)
        self.assertIn("CableCo listing", prompt)
        self.assertIn(guard.UNTRUSTED_RULE, fake.calls[0][0])

    def test_overlong_reason_falls_back(self):
        quotes = catalog.find("PAPER-A4-500")["quotes"]
        fake = FakeJSONLLM({"index": 1, "reason": "x" * 401})
        best, _, source = ai.recommend({"sku": "PAPER-A4-500", "quantity": 1}, quotes, fake)
        self.assertIs(best, quotes[1])
        self.assertEqual(source, "offline")

    def test_llm_error_falls_back(self):
        quotes = catalog.find("PAPER-A4-500")["quotes"]
        fake = FakeJSONLLM(error=LLMError("network-like failure"))
        best, _, source = ai.recommend({"sku": "PAPER-A4-500", "quantity": 1}, quotes, fake)
        self.assertIs(best, quotes[1])
        self.assertEqual(source, "offline")


class TestDisputeNote(unittest.TestCase):
    def setUp(self):
        self.req = PurchaseRequest(
            requester="dana",
            department="ops",
            category="office_supplies",
            description="Black toner cartridge 26A",
            sku="TONER-BK-26A",
            quantity=4,
            unit_price=55.0,
            merchant="InkWorld",
        )
        self.issues = ["short shipment: got 3 of 4"]

    def test_offline_template_contains_facts_and_issue(self):
        note = ai.dispute_note(self.req, self.issues)
        self.assertIn("TONER-BK-26A", note)
        self.assertIn("Black toner cartridge 26A", note)
        self.assertIn("InkWorld", note)
        self.assertIn("4", note)
        self.assertIn("$55.00", note)
        self.assertIn(self.issues[0], note)
        self.assertIn("refund", note.lower())
        self.assertLessEqual(len(note), 600)

    def test_llm_text_with_sku_is_returned(self):
        reply = (
            "We received 3 of the 4 TONER-BK-26A cartridges ordered from InkWorld. "
            "Please refund the shortfall for the missing unit."
        )
        fake = FakeTextLLM(reply)
        self.assertEqual(ai.dispute_note(self.req, self.issues, fake), reply)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("Fact sheet", fake.calls[0][1])
        self.assertIn("short shipment: got 3 of 4", fake.calls[0][1])

    def test_llm_text_without_sku_falls_back(self):
        fake = FakeTextLLM("We received fewer units than ordered. Please refund the shortfall.")
        note = ai.dispute_note(self.req, self.issues, fake)
        self.assertIn("TONER-BK-26A", note)
        self.assertIn(self.issues[0], note)
        self.assertNotEqual(note, fake.text)

    def test_llm_error_falls_back(self):
        fake = FakeTextLLM(error=LLMError("boom"))
        note = ai.dispute_note(self.req, self.issues, fake)
        self.assertIn("TONER-BK-26A", note)
        self.assertIn(self.issues[0], note)
        self.assertLessEqual(len(note), 600)

    def test_overlong_llm_text_falls_back(self):
        fake = FakeTextLLM("TONER-BK-26A " + ("x" * 1300))
        note = ai.dispute_note(self.req, self.issues, fake)
        self.assertIn("TONER-BK-26A", note)
        self.assertIn(self.issues[0], note)
        self.assertLessEqual(len(note), 600)


if __name__ == "__main__":
    unittest.main()
