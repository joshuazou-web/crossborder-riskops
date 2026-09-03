"""Central configuration.

`get_settings()` re-reads the environment on every call. Field defaults
evaluated at import time would freeze whatever environment existed when the
module was first imported, which silently ignores a `.env` loaded later and
lets a test write into the real project database.

No secret has a default. `RISKOPS_LLM_PROVIDER` defaults to `mock`, so the
entire product - demo, dashboard, evaluation - runs with no API key at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser().resolve() if raw else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    db_path: Path
    reports_dir: Path
    model_path: Path
    sql_dir: Path

    # --- synthetic data generation ---
    random_seed: int
    n_transactions: int
    n_merchants: int
    n_wallets: int
    history_days: int
    # A fixed reporting cut-off keeps every metric in the demo reproducible.
    as_of_date: str

    # --- risk policy thresholds ---
    # A case is auto-released only below this score AND with no high-severity signal.
    auto_release_below: float
    # At or above this score the policy holds without waiting for a human.
    auto_hold_at_or_above: float
    # FX/settlement reconciliation tolerance, in basis points.
    fx_tolerance_bps: int
    # Two captures within this window sharing an idempotency key are duplicates.
    duplicate_window_seconds: int
    # Velocity rule: payments per wallet within the window.
    velocity_window_minutes: int
    velocity_threshold: int
    # Case SLA in hours, by risk band.
    sla_hours_critical: int
    sla_hours_high: int
    sla_hours_medium: int
    sla_hours_low: int

    # --- AI copilot ---
    llm_provider: str          # mock | openai_compatible
    llm_model: str
    llm_base_url: str | None
    llm_api_key: str | None
    llm_timeout_seconds: int
    llm_max_output_tokens: int
    llm_temperature: float
    # Below this the copilot's recommendation is downgraded to `abstain`.
    ai_min_confidence: float
    # Redact PII-shaped strings before any text leaves the process.
    ai_redact_pii: bool

    log_level: str
    extra: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = _env_path("RISKOPS_DATA_DIR", PROJECT_ROOT / "data")
        reports_dir = _env_path("RISKOPS_REPORTS_DIR", PROJECT_ROOT / "reports")
        return cls(
            project_root=PROJECT_ROOT,
            data_dir=data_dir,
            db_path=_env_path("RISKOPS_DB_PATH", data_dir / "riskops.duckdb"),
            reports_dir=reports_dir,
            model_path=_env_path("RISKOPS_MODEL_PATH", reports_dir / "risk_model.joblib"),
            sql_dir=PACKAGE_ROOT / "sql",
            random_seed=_env_int("RISKOPS_SEED", 20260815),
            n_transactions=_env_int("RISKOPS_N_TRANSACTIONS", 6000),
            n_merchants=_env_int("RISKOPS_N_MERCHANTS", 120),
            n_wallets=_env_int("RISKOPS_N_WALLETS", 900),
            history_days=_env_int("RISKOPS_HISTORY_DAYS", 90),
            as_of_date=os.getenv("RISKOPS_AS_OF", "2026-08-31"),
            auto_release_below=_env_float("RISKOPS_AUTO_RELEASE_BELOW", 0.25),
            auto_hold_at_or_above=_env_float("RISKOPS_AUTO_HOLD_AT", 0.92),
            fx_tolerance_bps=_env_int("RISKOPS_FX_TOLERANCE_BPS", 75),
            duplicate_window_seconds=_env_int("RISKOPS_DUPLICATE_WINDOW_SECONDS", 900),
            velocity_window_minutes=_env_int("RISKOPS_VELOCITY_WINDOW_MINUTES", 10),
            velocity_threshold=_env_int("RISKOPS_VELOCITY_THRESHOLD", 6),
            sla_hours_critical=_env_int("RISKOPS_SLA_CRITICAL_HOURS", 2),
            sla_hours_high=_env_int("RISKOPS_SLA_HIGH_HOURS", 8),
            sla_hours_medium=_env_int("RISKOPS_SLA_MEDIUM_HOURS", 24),
            sla_hours_low=_env_int("RISKOPS_SLA_LOW_HOURS", 72),
            llm_provider=os.getenv("RISKOPS_LLM_PROVIDER", "mock").strip().lower(),
            llm_model=os.getenv("RISKOPS_LLM_MODEL", "mock-deterministic-v1"),
            llm_base_url=os.getenv("RISKOPS_LLM_BASE_URL") or None,
            llm_api_key=os.getenv("RISKOPS_LLM_API_KEY") or None,
            llm_timeout_seconds=_env_int("RISKOPS_LLM_TIMEOUT", 30),
            llm_max_output_tokens=_env_int("RISKOPS_LLM_MAX_TOKENS", 1200),
            llm_temperature=_env_float("RISKOPS_LLM_TEMPERATURE", 0.0),
            ai_min_confidence=_env_float("RISKOPS_AI_MIN_CONFIDENCE", 0.55),
            ai_redact_pii=_env_bool("RISKOPS_AI_REDACT_PII", True),
            log_level=os.getenv("RISKOPS_LOG_LEVEL", "INFO"),
            extra={
                # The AML generator's default size. The committed demo warehouse
                # is built smaller (see the Makefile) so the repository stays a
                # reasonable size to clone; the default here is what `riskops aml
                # build` produces when nobody says otherwise, and what the
                # evaluation runs against.
                "aml_n_transfers": _env_int("RISKOPS_AML_N_TRANSFERS", 100_000),
                # How many cases the team can actually open. Zero means "derive
                # it from the case count"; a real number is what makes the queue
                # a queue. Deliberately far below the alert count.
                "aml_review_capacity": _env_int("RISKOPS_AML_REVIEW_CAPACITY", 0),
            },
        )

    def sla_hours(self, band: str) -> int:
        return {
            "critical": self.sla_hours_critical,
            "high": self.sla_hours_high,
            "medium": self.sla_hours_medium,
            "low": self.sla_hours_low,
        }.get(band, self.sla_hours_low)

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.data_dir / "raw",
            self.reports_dir,
            self.reports_dir / "eval",
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Build settings fresh from the current environment."""
    return Settings.from_env()


settings = get_settings()
