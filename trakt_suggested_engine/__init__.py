"""Credential-free generation of the Trakt Suggested catalogues, per taste profile."""

from .engine import (
    PROFILES, EngineConfig, GenerationError, generate_catalogues, derive_episode_progress,
    evaluate_returning, thematic_signals, rank_and_mix_series,
)

__all__ = [
    "PROFILES", "EngineConfig", "GenerationError", "generate_catalogues", "derive_episode_progress",
    "evaluate_returning", "thematic_signals", "rank_and_mix_series",
]
