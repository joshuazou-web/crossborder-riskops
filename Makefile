# CrossBorder RiskOps - common tasks.
# Windows users without make: every recipe below is a single command you can copy.

PY ?= python
export PYTHONPATH := src

.PHONY: help install install-dev demo refresh status cases audit eval report dashboard \
        test test-fast lint docs screenshots clean

help:          ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:       ## Install runtime dependencies
	$(PY) -m pip install -r requirements.txt

install-dev:   ## Install runtime + development dependencies
	$(PY) -m pip install -r requirements-dev.txt

demo:          ## Build the entire demo from scratch (~90s, no API key needed)
	$(PY) -m riskops demo

refresh:       ## Run one full refresh
	$(PY) -m riskops refresh

status:        ## Row counts, refresh history and the audit-chain verdict
	$(PY) -m riskops status

cases:         ## Show the case queue
	$(PY) -m riskops cases --limit 20

audit:         ## Verify the audit hash chain
	$(PY) -m riskops audit --tail 15

eval:          ## Run the evaluation harness and regenerate the report
	$(PY) -m riskops eval

export:        ## Export every mart table to CSV
	$(PY) -m riskops export

dashboard:     ## Start the workbench on :8501
	$(PY) -m streamlit run app/Home.py

test:          ## Run the full test suite
	$(PY) -m pytest

test-fast:     ## Run everything except the slower warehouse tests
	$(PY) -m pytest -m "not slow"

lint:          ## Lint with ruff
	$(PY) -m ruff check src app tests scripts

docs:          ## Regenerate the data dictionary and rule catalogue from code
	$(PY) scripts/generate_docs.py

screenshots:   ## Capture the dashboard screenshots (needs the dashboard running)
	$(PY) scripts/capture_screenshots.py

clean:         ## Remove the generated warehouse and model artefacts
	rm -f data/riskops.duckdb data/riskops.duckdb.wal reports/risk_model.joblib
	rm -rf reports/marts_csv .pytest_cache .ruff_cache
