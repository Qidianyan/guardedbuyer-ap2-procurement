"""Independent tests for the 200-request guardrail comparison (SPEC I2)."""
import csv, json, os, re, sys, tempfile, unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from metrics.cases import build_cases
from metrics.comparison import render
from metrics.run_metrics import CONFIGS, aggregate, run_case

SEED_200 = 20261110
CONFIG_ORDER = ("no_guardrails", "policy_only", "full")
ATTACK = ("over_authority", "injection")
LABELS = {"no_guardrails": "No guardrails", "policy_only": "Policy only",
          "full": "Full guardrails (ours)"}
COLUMNS = ["config", "label", "attack_cases", "attack_cases_blocked", "attack_case_block_rate",
           "unauthorized_attempts", "attempts_blocked", "attempt_block_rate", "normal_cases",
           "normal_false_blocks", "normal_false_block_rate", "legit_purchases_all",
           "legit_not_paid_all", "false_block_rate_all", "mismatch_cases", "mismatch_handled",
           "dispute_coverage", "errors", "audit_chain_valid_all"]


def _expected_rows(S):
    """Expected summary CSV rows, computed directly from an aggregate() summary."""
    out = []
    for cfg in CONFIG_ORDER:
        s = S[cfg]
        row = {"config": cfg, "label": LABELS[cfg]}
        for col, key in (("attack_cases", "attack_cases"), ("attack_cases_blocked", "attack_cases_blocked"),
                         ("unauthorized_attempts", "unauthorized_attempts"), ("attempts_blocked", "blocked"),
                         ("normal_cases", "normal_cases"), ("normal_false_blocks", "normal_false_blocks"),
                         ("legit_purchases_all", "legit_purchases"), ("legit_not_paid_all", "legit_not_paid"),
                         ("mismatch_cases", "mismatch_cases"), ("mismatch_handled", "mismatch_handled"),
                         ("errors", "errors")):
            row[col] = str(s[key])
        for col, (k, n) in (("attack_case_block_rate", ("attack_cases_blocked", "attack_cases")),
                            ("attempt_block_rate", ("blocked", "unauthorized_attempts")),
                            ("normal_false_block_rate", ("normal_false_blocks", "normal_cases")),
                            ("false_block_rate_all", ("legit_not_paid", "legit_purchases")),
                            ("dispute_coverage", ("mismatch_handled", "mismatch_cases"))):
            row[col] = str(round(s[k] / s[n], 4)) if s[n] else ""
        row["audit_chain_valid_all"] = str(s["chain_valid_all"])
        out.append({c: row[c] for c in COLUMNS})
    return out


def _subset():
    """Two real cases of each type from profile 200, run through all three configs."""
    cases = build_cases(SEED_200, "200")
    picked = []
    for t in ("normal", "over_authority", "injection", "mismatch"):
        picked.extend([c for c in cases if c["type"] == t][:2])
    rows = [run_case(c, cfg) for c in picked for cfg in CONFIG_ORDER]
    results = {"n_cases": len(picked), "offline": {"rows": rows, "summary": aggregate(rows)}}
    return picked, results


class ComparisonTest(unittest.TestCase):
    def test_1_profile_200_counts_and_determinism(self):
        cases = build_cases(SEED_200, "200")
        expected = Counter({("normal", "small"): 39, ("normal", "needs_manager"): 20,
                            ("normal", "needs_finance"): 15, ("mismatch", "short"): 10,
                            ("mismatch", "wrong_sku"): 10, ("mismatch", "overcharged"): 10})
        for sub in ("inflate_quantity", "disallowed_category", "blocked_merchant", "off_scope_item",
                    "over_hard_cap", "approve_self", "pay_unapproved", "tamper_amount"):
            expected[("over_authority", sub)] = 6
        for sub in ("bulk_quantity", "gift_card_side_purchase", "fake_preapproval", "partner_merchant_switch",
                    "travel_voucher_upsell", "backup_stock", "official_supplier", "invoice_redirect"):
            expected[("injection", sub)] = 6
        self.assertEqual(Counter((c["type"], c["subtype"]) for c in cases), expected)
        self.assertEqual(sum(expected.values()), 200)
        self.assertEqual(len(cases), 200)
        self.assertEqual(len({c["id"] for c in cases}), 200)
        self.assertEqual(cases, build_cases(SEED_200, "200"))
        self.assertNotEqual(cases, build_cases(SEED_200 + 1, "200"))

    def test_2_profile_150_matches_committed_cases_json(self):
        self.assertEqual(build_cases(profile="150"), build_cases())
        with open(ROOT / "metrics" / "cases.json", encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertEqual(json.dumps(build_cases(), indent=1, sort_keys=True) + "\n", text)

    def test_3_committed_cases_200_matches_builder(self):
        with open(ROOT / "metrics" / "cases_200.json", encoding="utf-8") as f:
            committed = json.load(f)
        self.assertEqual(len(committed), 200)
        for c in committed:
            self.assertEqual(c["profile"], "200", c["id"])
            self.assertEqual(c["seed"], SEED_200, c["id"])
        trimmed = [{k: v for k, v in c.items() if k not in ("profile", "seed")} for c in committed]
        self.assertEqual(trimmed, build_cases(SEED_200, "200"))

    def test_4_aggregate_case_level_views_on_subset(self):
        cases, results = _subset()
        self.assertEqual(len(cases), 8)
        rows = results["offline"]["rows"]
        self.assertEqual(len(rows), 8 * len(CONFIG_ORDER))
        S = aggregate(rows)
        self.assertEqual(set(S), set(CONFIG_ORDER))
        attack_ids = {c["id"] for c in cases if c["type"] in ATTACK}
        normal_ids = {c["id"] for c in cases if c["type"] == "normal"}
        self.assertEqual(len(attack_ids), 4)
        self.assertEqual(len(normal_ids), 2)
        for cfg in CONFIG_ORDER:
            s = S[cfg]
            rs = [r for r in rows if r["config"] == cfg]
            self.assertEqual(s["attack_cases"], 4, cfg)
            self.assertEqual(s["normal_cases"], 2, cfg)
            self.assertLessEqual(s["attack_cases_blocked"], s["attack_cases"], cfg)
            missed = sorted(r["id"] for r in rs if r["type"] in ATTACK
                            and not all(a["blocked"] for a in r["attempts"]))
            self.assertEqual(s["missed_attack_case_ids"], missed, cfg)
            self.assertTrue(set(missed) <= attack_ids, cfg)
            self.assertTrue(set(missed).isdisjoint(normal_ids), cfg)
            self.assertEqual(s["attack_cases_blocked"], 4 - len(missed), cfg)
            self.assertEqual(s["attack_case_block_rate"], (4 - len(missed)) / 4, cfg)
            fb = sum(not r["legit_paid"] for r in rs if r["type"] == "normal")
            self.assertEqual(s["normal_false_blocks"], fb, cfg)
            self.assertEqual(s["normal_false_block_rate"], fb / 2, cfg)
        self.assertEqual(set(S["no_guardrails"]["missed_attack_case_ids"]), attack_ids)

    def test_5_render_outputs_on_subset(self):
        cases, results = _subset()
        S = results["offline"]["summary"]
        with tempfile.TemporaryDirectory() as d:
            paths = render(results, cases, d)
            self.assertEqual(len(paths), 4)
            for p in paths:
                self.assertTrue(os.path.isfile(p), p)
            by_name = {os.path.basename(p): p for p in paths}
            self.assertEqual(set(by_name), {"comparison_200.csv", "comparison_200_cases.csv",
                                            "comparison_200.svg", "comparison_200.md"})
            with open(by_name["comparison_200.csv"], newline="") as f:
                got = list(csv.DictReader(f))
            self.assertEqual(got, _expected_rows(S))
            with open(by_name["comparison_200_cases.csv"], newline="") as f:
                crows = list(csv.DictReader(f))
            self.assertEqual(len(crows), len(cases))
            self.assertEqual({r["id"] for r in crows}, {c["id"] for c in cases})
            self.assertEqual(len({r["id"] for r in crows}), len(cases))
            with open(by_name["comparison_200.svg"], encoding="utf-8") as f:
                svg = f.read()
            root = ET.fromstring(svg)
            self.assertTrue(root.tag.endswith("}svg") or root.tag == "svg")
            for label in LABELS.values():
                self.assertIn(label, svg)
            with open(by_name["comparison_200.md"], encoding="utf-8") as f:
                md = f.read()
            for h in ("## Sample", "## Definitions (denominators)", "## Results", "## By subtype",
                      "## What full guardrails missed", "## Reproduce", "## Limitations"):
                self.assertIn(h, md)
            self.assertIn("synthetic", md)
            section = md.split("## Results\n", 1)[1].split("## By subtype", 1)[0]
            for cfg in CONFIG_ORDER:
                s = S[cfg]
                self.assertIn(f'{s["attack_cases_blocked"]}/{s["attack_cases"]}', section, cfg)

    def test_6_committed_outputs_match_results_200(self):
        with open(ROOT / "metrics" / "results_200.json", encoding="utf-8") as f:
            results = json.load(f)
        S = results["offline"]["summary"]
        with open(ROOT / "docs" / "comparison_200.csv", newline="", encoding="utf-8") as f:
            got = list(csv.DictReader(f))
        self.assertEqual(list(got[0]), COLUMNS)
        self.assertEqual(got, _expected_rows(S))
        md = (ROOT / "docs" / "comparison_200.md").read_text(encoding="utf-8")
        section = md.split("## Results\n", 1)[1].split("## By subtype", 1)[0]
        inv = {v: k for k, v in LABELS.items()}
        pairs = (("attack_cases_blocked", "attack_cases"), ("blocked", "unauthorized_attempts"),
                 ("normal_false_blocks", "normal_cases"), ("legit_not_paid", "legit_purchases"),
                 ("mismatch_handled", "mismatch_cases"))
        computed = {f"{s[k]}/{s[n]}" for s in S.values() for k, n in pairs}
        found, seen = [], set()
        for line in section.splitlines():
            if not line.startswith("|") or line.startswith("|---") or "Configuration" in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cfg = inv[cells[0]]
            seen.add(cfg)
            s = S[cfg]
            want = [f"{s[k]}/{s[n]}" for k, n in pairs]
            got_kn = [m.group(1) for c in cells[1:6] for m in [re.match(r"(\d+/\d+)", c)] if m]
            self.assertEqual(got_kn, want, cfg)
            found.extend(got_kn)
        self.assertEqual(seen, set(CONFIG_ORDER))
        self.assertEqual(set(re.findall(r"\b\d+/\d+\b", section)), set(found))
        self.assertTrue(set(found) <= computed)


if __name__ == "__main__":
    unittest.main()
