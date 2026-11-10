"""Tests for metrics/report.py (synthetic results only, no network)."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from metrics import report

CONFIGS = {
    "no_guardrails": {
        "guards": {"policy": False, "mandate": False, "readback": False, "reconcile": False},
        "scope_guard": False, "quarantine": False,
    },
    "policy_only": {
        "guards": {"policy": True, "mandate": False, "readback": False, "reconcile": False},
        "scope_guard": False, "quarantine": False,
    },
    "full": {"guards": {}, "scope_guard": True, "quarantine": True},
}


def _summary(blocked, false_blocks):
    """Three cases: normal/small, over_authority/off_scope_item, injection/backup_stock.

    There are two unauthorized attempts per config (one over_authority, one injection) and no
    mismatch cases, so dispute_coverage is deliberately null.
    """
    normal_legit = 1 - false_blocks
    return {
        "cases": 3,
        "unauthorized_attempts": 2,
        "blocked": blocked,
        "block_rate": blocked / 2,
        "legit_purchases": 3,
        "legit_not_paid": false_blocks,
        "false_block_rate": false_blocks / 3,
        "mismatch_cases": 0,
        "mismatch_handled": 0,
        "dispute_coverage": None,
        "errors": 0,
        "chain_valid_all": True,
        "by_type": {
            "normal": {"cases": 1, "attempts": 0, "blocked": 0, "legit_paid": normal_legit},
            "over_authority": {"cases": 1, "attempts": 1, "blocked": 1 if blocked else 0, "legit_paid": 1},
            "injection": {"cases": 1, "attempts": 1, "blocked": 1 if blocked > 1 else 0, "legit_paid": 1},
            "mismatch": {"cases": 0, "attempts": 0, "blocked": 0, "legit_paid": 0},
        },
        "by_subtype": {
            "normal/small": {"cases": 1, "attempts": 0, "blocked": 0, "legit_paid": normal_legit},
            "over_authority/off_scope_item": {"cases": 1, "attempts": 1,
                                              "blocked": 1 if blocked else 0, "legit_paid": 1},
            "injection/backup_stock": {"cases": 1, "attempts": 1,
                                       "blocked": 1 if blocked > 1 else 0, "legit_paid": 1},
        },
    }


def _row(case_id, type_, subtype, config, legit_paid, blocked, detector=None):
    attempts = []
    if type_ == "over_authority":
        attempts = [{"kind": "off_scope_item", "blocked": blocked}]
    elif type_ == "injection":
        attempts = [{"kind": "side_purchase", "blocked": blocked}]
    return {
        "id": case_id, "type": type_, "subtype": subtype, "config": config,
        "model": "PlanLLM(scripted)", "legit_paid": legit_paid, "attempts": attempts,
        "dispute_handled": None, "block_events": [], "detector_should_flag": detector,
        "error": None, "chain_valid": True,
    }


def _fake_results():
    summaries = {
        # blocked attempts: 0/2, 1/2, 1/2; false blocks: 0/3, 0/3, 1/3
        "no_guardrails": _summary(0, 0),
        "policy_only": _summary(1, 0),
        "full": _summary(1, 1),
    }
    rows = []
    for config in CONFIGS:
        rows.extend([
            _row("NRM-001", "normal", "small", config, legit_paid=(config != "full"), blocked=False),
            _row("OA-001", "over_authority", "off_scope_item", config, legit_paid=True,
                 blocked=(config != "no_guardrails")),
            _row("INJ-001", "injection", "backup_stock", config, legit_paid=True,
                 blocked=(config == "full"), detector=False),
        ])

    llm_summary = {
        "no_guardrails": {
            "cases": 2, "unauthorized_attempts": 2, "blocked": 0, "block_rate": 0.0,
            "legit_purchases": 2, "legit_not_paid": 0, "false_block_rate": 0.0,
            "mismatch_cases": 0, "mismatch_handled": 0, "dispute_coverage": None,
            "errors": 0, "chain_valid_all": True, "by_type": {}, "by_subtype": {},
        },
        "full": {
            "cases": 2, "unauthorized_attempts": 1, "blocked": 0, "block_rate": 0.0,
            "legit_purchases": 2, "legit_not_paid": 1, "false_block_rate": 0.5,
            "mismatch_cases": 0, "mismatch_handled": 0, "dispute_coverage": None,
            "errors": 0, "chain_valid_all": True, "by_type": {}, "by_subtype": {},
        },
    }
    llm_rows = [
        _row("INJ-001", "injection", "backup_stock", "no_guardrails", legit_paid=True,
             blocked=False, detector=False),
        _row("NRM-001", "normal", "small", "full", legit_paid=False, blocked=False),
    ]
    for row in llm_rows:
        row["model"] = "FakeLLM<&>"

    return {
        "generated_at": "2026-01-02T03:04:05Z",
        "synthetic": True,
        "n_cases": 3,
        "configs": copy.deepcopy(CONFIGS),
        "offline": {"rows": rows, "summary": summaries},
        "llm": {
            "model": "FakeLLM<&>",
            "sample_ids": ["INJ-001", "NRM-001"],
            "rows": llm_rows,
            "summary": llm_summary,
            "llm_calls": 7,
            "seconds": 12,
        },
    }


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = _fake_results()
        self.out = os.path.join(self.tmp.name, "docs", "metrics.md")
        self.charts = os.path.join(self.tmp.name, "docs", "charts")
        self.paths = report.render(self.results, self.out, self.charts)
        with open(self.out, encoding="utf-8") as handle:
            self.md = handle.read()

    def test_markdown_sections_and_numbers(self):
        headings = [
            "# Guardrail evaluation (synthetic cases)",
            "## Setup",
            "## Metric definitions",
            "## Headline results",
            "## By attack type",
            "## By subtype",
            "## Real LLM sample",
            "## Limitations",
            "## Reproduce",
        ]
        for heading in headings:
            self.assertIn(heading, self.md)

        self.assertIn("**All cases are synthetic", self.md)
        self.assertIn("synthetic", self.md.lower())
        self.assertIn("n/a", self.md)
        self.assertIn("n/a (0/0)", self.md)          # null dispute coverage, with k/n
        self.assertIn("0.0% (0/2)", self.md)         # no_guardrails block rate
        self.assertIn("50.0% (1/2)", self.md)        # policy_only / full block rate
        self.assertIn("33.3% (1/3)", self.md)        # full false-block rate
        self.assertIn("1/1 (100.0%)", self.md)       # by-type / by-subtype cells
        self.assertIn("0/1 (0.0%)", self.md)

        self.assertIn("Full guardrails block **less than 100%** on", self.md)
        self.assertIn("injection/backup_stock", self.md)
        self.assertIn("small sample (2 cases), single run, not statistically significant", self.md)
        self.assertIn("**1 of 1** full-config injection rows", self.md)
        self.assertIn("FakeLLM<&>", self.md)
        self.assertIn("**LLM calls:** 7", self.md)
        self.assertIn("12 s", self.md)
        self.assertIn("python3 -m metrics.cases && python3 -m metrics.run_metrics [--llm] && python3 -m metrics.report",
                      self.md)

    def test_charts_parse_and_contain_config_names(self):
        self.assertEqual(len(self.paths), 5)
        returned = {os.path.abspath(path) for path in self.paths}
        self.assertIn(os.path.abspath(self.out), returned)

        expected = {
            "headline.svg": ["No guardrails", "Policy only", "Full guardrails (ours)"],
            "block_by_type.svg": ["No guardrails", "Policy only", "Full guardrails (ours)"],
            "block_by_subtype.svg": ["No guardrails", "Policy only", "Full guardrails (ours)"],
            "llm_sample.svg": ["No guardrails", "Full guardrails (ours)"],
        }
        for filename, names in expected.items():
            path = os.path.join(self.charts, filename)
            self.assertTrue(os.path.exists(path), filename)
            with open(path, encoding="utf-8") as handle:
                svg = handle.read()
            root = ET.parse(path).getroot()
            self.assertTrue(root.tag.endswith("svg"))
            self.assertEqual(root.get("width"), "800")
            self.assertEqual(root.get("height"), "420")
            self.assertIn('fill="#ffffff"', svg)
            self.assertIn("system-ui", svg)
            text = " ".join(root.itertext())
            for name in names:
                self.assertIn(name, text, filename)
            if filename == "llm_sample.svg":
                self.assertNotIn("Policy only", text)
                self.assertIn("FakeLLM<&>", text)
                self.assertIn("FakeLLM&lt;&amp;&gt;", svg)
            else:
                self.assertIn("Policy only", text)

        headline = open(os.path.join(self.charts, "headline.svg"), encoding="utf-8").read()
        self.assertIn("n/a", headline)               # null rate rendered without a bar
        self.assertIn("50.0%", headline)
        self.assertIn("Rate (%)", headline)
        self.assertIn("#9aa0a6", headline)
        self.assertIn("#f5a623", headline)
        self.assertIn("#0070e0", headline)

        subtype = open(os.path.join(self.charts, "block_by_subtype.svg"), encoding="utf-8").read()
        self.assertIn("backup_stock", subtype)
        self.assertIn("Block rate (%)", subtype)
        llm = open(os.path.join(self.charts, "llm_sample.svg"), encoding="utf-8").read()
        self.assertIn("Count (cases)", llm)
        self.assertIn("Unauthorized payments", llm)
        self.assertIn("Legit purchases paid", llm)

    def test_render_without_llm(self):
        results = copy.deepcopy(self.results)
        del results["llm"]
        out = os.path.join(self.tmp.name, "no_llm", "metrics.md")
        # Reuse the charts directory from the LLM render: the stale llm_sample.svg must disappear.
        charts = self.charts
        self.assertTrue(os.path.exists(os.path.join(charts, "llm_sample.svg")))
        paths = report.render(results, out, charts)

        self.assertEqual(len(paths), 4)
        self.assertFalse(os.path.exists(os.path.join(charts, "llm_sample.svg")))
        for name in ("headline.svg", "block_by_type.svg", "block_by_subtype.svg"):
            self.assertTrue(os.path.exists(os.path.join(charts, name)))
        with open(out, encoding="utf-8") as handle:
            md = handle.read()
        self.assertIn("## Real LLM sample", md)
        self.assertIn("not run", md.lower())
        self.assertNotIn("charts/llm_sample.svg", md)

    def test_full_blocked_fallback_sentence(self):
        results = copy.deepcopy(self.results)
        full = results["offline"]["summary"]["full"]
        full["blocked"] = 2
        full["block_rate"] = 1.0
        full["by_type"]["injection"]["blocked"] = 1
        full["by_subtype"]["injection/backup_stock"]["blocked"] = 1
        out = os.path.join(self.tmp.name, "fallback", "metrics.md")
        report.render(results, out, os.path.join(self.tmp.name, "fallback", "charts"))
        with open(out, encoding="utf-8") as handle:
            md = handle.read()
        self.assertIn("Full guardrails blocked every attempt in this set.", md)

    def test_main_cli(self):
        results_path = os.path.join(self.tmp.name, "results.json")
        with open(results_path, "w", encoding="utf-8") as handle:
            json.dump(self.results, handle)
        out = os.path.join(self.tmp.name, "cli", "metrics.md")
        charts = os.path.join(self.tmp.name, "cli", "charts")
        with contextlib.redirect_stdout(io.StringIO()):
            code = report.main(["--results", results_path, "--out", out, "--charts", charts])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(os.path.exists(os.path.join(charts, "headline.svg")))
        self.assertTrue(os.path.exists(os.path.join(charts, "llm_sample.svg")))


if __name__ == "__main__":
    unittest.main()
