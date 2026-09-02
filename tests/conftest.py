"""Shared fixtures. Every test builds its own warehouse in a tmp_path, so a test
run can never touch data/riskops.duckdb."""

import os
from pathlib import Path

import pytest

from riskops.config import Settings, get_settings


@pytest.fixture
def small_settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("RISKOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RISKOPS_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("RISKOPS_DB_PATH", str(tmp_path / "data" / "test.duckdb"))
    monkeypatch.setenv("RISKOPS_MODEL_PATH", str(tmp_path / "reports" / "model.joblib"))
    monkeypatch.setenv("RISKOPS_N_TRANSACTIONS", "600")
    monkeypatch.setenv("RISKOPS_N_MERCHANTS", "40")
    monkeypatch.setenv("RISKOPS_N_WALLETS", "150")
    monkeypatch.setenv("RISKOPS_HISTORY_DAYS", "45")
    monkeypatch.setenv("RISKOPS_LLM_PROVIDER", "mock")
    settings = get_settings()
    settings.ensure_dirs()
    return settings


@pytest.fixture(autouse=True)
def _no_real_api_key(monkeypatch):
    """No test may reach a network provider, whatever the developer's shell has."""
    monkeypatch.delenv("RISKOPS_LLM_API_KEY", raising=False)
    os.environ.setdefault("RISKOPS_LLM_PROVIDER", "mock")
