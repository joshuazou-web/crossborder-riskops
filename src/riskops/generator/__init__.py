"""Synthetic cross-border payment data generation."""

from .scenarios import SCENARIO_BY_KEY, SCENARIOS, ScenarioSpec
from .synth import GeneratedWorld, fx_rate, generate, haversine_km

__all__ = [
    "GeneratedWorld",
    "SCENARIOS",
    "SCENARIO_BY_KEY",
    "ScenarioSpec",
    "fx_rate",
    "generate",
    "haversine_km",
]
