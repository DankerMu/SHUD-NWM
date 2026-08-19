"""Requirement-driven tests for the canonical run-id shapes (#1405).

``parse_run_cycle`` is the single seam the journal and retention now share, so
a deletion surface admits only names the pipeline actually mints. Its answer is
either the cycle at the canonical position or None; there is no fallback to
another token in the name.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.orchestrator.run_identity import parse_run_cycle

CYCLE = datetime(2026, 5, 16, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "run_id",
    [
        "fcst_gfs_2026051600_model_a",
        "fcst_gfs_2026051600_model_with_underscores",
        "cycle_gfs_2026051600",
        "cycle_gfs_2026051600_model_a",
        "analysis_era5_2026051600_2026052000_model_a",
    ],
    ids=["forecast", "forecast-underscored-model", "cohort", "cohort-with-suffix", "analysis"],
)
def test_canonical_shapes_resolve_to_the_cycle_at_the_canonical_position(run_id: str) -> None:
    """The analysis shape binds to its START segment (chain_analysis cycle_time)."""
    assert parse_run_cycle(run_id) == CYCLE


def test_parsed_cycle_is_utc_aware() -> None:
    parsed = parse_run_cycle("fcst_gfs_2026051600_model_a")

    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()


def test_leading_timestamp_token_does_not_outrank_the_cycle_position() -> None:
    """[A class] The previous token scan bound this run to 2020-01-01."""
    assert parse_run_cycle("fcst_2020010100_2026081400_model_a") == datetime(
        2026, 8, 14, 0, tzinfo=UTC
    )


def test_trailing_timestamp_like_model_id_does_not_shift_the_cycle() -> None:
    assert parse_run_cycle("fcst_gfs_2026051600_model_2026010100") == CYCLE


@pytest.mark.parametrize(
    "run_id",
    [
        "manual_salvage_2020010100_keepme",
        "debug_snapshot_2026051600",
        "runs_2026051600_scratch",
        "FCST_GFS_2026051600_MODEL_A",
        "fcst_gfs_2026051600",
        "cycle_gfs_202605160",
        "analysis_era5_2026051600_model_a",
        "",
    ],
    ids=[
        "salvage-capture",
        "no-canonical-prefix",
        "foreign-writer",
        "uppercase",
        "forecast-missing-model",
        "cohort-short-cycle",
        "analysis-missing-end",
        "empty",
    ],
)
def test_non_canonical_shapes_resolve_to_none(run_id: str) -> None:
    """[B class] Nothing outside the canonical shapes is a run workspace."""
    assert parse_run_cycle(run_id) is None


@pytest.mark.parametrize(
    "run_id",
    [
        "fcst_gfs_2026139999_model_2026010100",
        "cycle_gfs_2026139999",
        "analysis_era5_2026139999_2026052000_model_a",
        "fcst_x_2026139999_y",
    ],
    ids=["forecast", "cohort", "analysis", "loose-shape-illegal-date"],
)
def test_illegal_date_at_the_canonical_position_never_falls_back(run_id: str) -> None:
    """A canonical shape whose cycle token is not a real timestamp is not a run.

    The trailing `2026010100` of the forecast row is a parseable timestamp; the
    previous token scan would have deleted against it.
    """
    assert parse_run_cycle(run_id) is None
