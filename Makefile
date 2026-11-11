# GuardedBuyer: common tasks. Everything here runs offline unless noted.
PY ?= python3
PORT ?= 8000

.PHONY: help install demo test test-fast scenarios eval smoke sandbox-smoke

help:
	@echo "make demo           start the web app on http://127.0.0.1:$(PORT) (offline PayPal emulation)"
	@echo "make test           full test suite (~7 min)"
	@echo "make test-fast      guardrail + UI shell tests (seconds)"
	@echo "make scenarios      print the four scripted stories in the terminal"
	@echo "make eval           re-run the 200-case comparison (writes metrics/results_200.json)"
	@echo "make smoke          PayPal client flow against the local HTTP fake"
	@echo "make sandbox-smoke  real PayPal sandbox run; needs PAYPAL_SECRETS_FILE (see docs/sandbox.md)"

install:
	$(PY) -m pip install -r requirements.txt

demo:
	PORT=$(PORT) bash scripts/demo.sh

test:
	$(PY) -m pytest -q

test-fast:
	$(PY) -m pytest -q tests/test_guardrails_core.py tests/test_ui_shell.py tests/test_approval_desk.py

scenarios:
	$(PY) scenarios/run_all.py

eval:
	$(PY) -m metrics.run_metrics --cases metrics/cases_200.json --out metrics/results_200.json

smoke:
	$(PY) scripts/sandbox_smoke.py --fake

sandbox-smoke:
	@test -n "$$PAYPAL_SECRETS_FILE" || (echo "Set PAYPAL_SECRETS_FILE to your sandbox .secrets file (docs/sandbox.md)"; exit 1)
	$(PY) scripts/sandbox_smoke.py --test-card
