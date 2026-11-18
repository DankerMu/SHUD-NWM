"""Unit tests for §5.1 provenance-pinned replay lineage resolver.

Covers Epic #982 SUB-8 (``mapping-variant-state-compatibility`` task
5.1) evidence:

* Tests (a) / (b) / (c): interval classification and the straddle split
  with per-sub-interval lineage and no cross-variant splicing.
* Test (d): ``t*`` comes from the recorded activation audit authority
  (equal to the cloned rows' ``valid_time``) and IGNORES data-availability
  noise (post-cutover forecast/save-state rows without
  ``clone_gate_fingerprint``).

Plus:

* ``no_cutover_history`` classification when the basin has no direct-
  grid activation record — single legacy sub-interval end-to-end.
* :class:`ReplayLineageAmbiguityError` raised when the ``M1`` clone
  rows disagree on ``valid_time`` (spec invariant: one ``t*`` per
  cutover).

Tests (e) / (f), the guard-scope pair (an offline ``M0`` replay makes
zero lifecycle calls; an actual ``activate(M0)`` is still refused by the
legacy-reactivation guard), are in
``tests/test_replay_lineage_lifecycle_guard.py``; the fixture constants
and in-memory reader fakes both modules use are in
``tests/replay_lineage_helpers.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.orchestrator.replay_lineage import (
    ReplayLineageAmbiguityError,
    ReplayLineageError,
    ReplayLineagePlan,
    ReplayLineageSubInterval,
    resolve_replay_lineage,
)
from tests.replay_lineage_helpers import (
    BASIN_VERSION_ID,
    CUTOVER_VALID_TIME,
    _FakeAuditReader,
    _FakeCloneReader,
)

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------


M0_MODEL_ID = "legacy_m0"
M1_MODEL_ID = "direct_grid_m1"


def _fake_readers_with_cutover(
    *,
    m1_model_id: str = M1_MODEL_ID,
    m0_model_id: str | None = M0_MODEL_ID,
    cutover: datetime = CUTOVER_VALID_TIME,
    basin_version_id: str = BASIN_VERSION_ID,
) -> tuple[_FakeAuditReader, _FakeCloneReader]:
    audit = _FakeAuditReader(
        activations_by_basin={
            basin_version_id: {
                "m1_model_id": m1_model_id,
                "m0_model_id": m0_model_id,
                "audit_log_id": 42,
            }
        }
    )
    clone = _FakeCloneReader(valid_times_by_model={m1_model_id: {cutover}})
    return audit, clone


# ===========================================================================
# Test (a): interval entirely before ``t*`` resolves to ``M0`` lineage
# ===========================================================================


def test_interval_entirely_pre_cutover_resolves_to_m0_with_m0_lineage() -> None:
    """[start, end] < t* → single sub-interval M0-lineage (offline replay).

    Evidence line (a): "an interval entirely before ``t*`` resolves to
    ``M0`` with ``M0`` lineage".
    """
    audit, clone = _fake_readers_with_cutover()
    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = datetime(2026, 5, 15, tzinfo=UTC)

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "entirely_pre_cutover"
    assert plan.cutover_valid_time == CUTOVER_VALID_TIME
    assert plan.m0_model_id == M0_MODEL_ID
    assert plan.m1_model_id == M1_MODEL_ID
    assert plan.sub_intervals == (
        ReplayLineageSubInterval(
            start=start,
            end=end,
            model_id=M0_MODEL_ID,
            lineage="M0",
        ),
    )
    # Resolver reads exactly one basin_version_id and one model_id.
    assert audit.calls == [BASIN_VERSION_ID]
    assert clone.calls == [M1_MODEL_ID]


def test_interval_ending_exactly_at_cutover_resolves_to_m0_pre_cutover() -> None:
    """``end == t*`` is classified as entirely_pre_cutover (boundary).

    The pre-cutover half is closed at ``t*``; the post-cutover half is
    also closed at ``t*``. A degenerate interval ``[start, t*]`` is
    entirely pre-cutover because the boundary is when M0's data ends
    and M1's data begins — a single-point-at-t* is the last M0 sample.
    """
    audit, clone = _fake_readers_with_cutover()
    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = CUTOVER_VALID_TIME

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "entirely_pre_cutover"
    assert plan.sub_intervals[0].lineage == "M0"


# ===========================================================================
# Test (b): interval at or after ``t*`` resolves to ``M1`` lineage
# ===========================================================================


def test_interval_at_or_after_cutover_resolves_to_m1_with_m1_lineage() -> None:
    """[start, end] >= t* → single sub-interval M1-lineage.

    Evidence line (b): "an interval at or after ``t*`` resolves to
    ``M1`` with ``M1`` lineage".
    """
    audit, clone = _fake_readers_with_cutover()
    start = datetime(2026, 6, 15, tzinfo=UTC)
    end = datetime(2026, 7, 15, tzinfo=UTC)

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "entirely_post_cutover"
    assert plan.cutover_valid_time == CUTOVER_VALID_TIME
    assert plan.m0_model_id == M0_MODEL_ID
    assert plan.m1_model_id == M1_MODEL_ID
    assert plan.sub_intervals == (
        ReplayLineageSubInterval(
            start=start,
            end=end,
            model_id=M1_MODEL_ID,
            lineage="M1",
        ),
    )


def test_interval_starting_exactly_at_cutover_resolves_to_m1_post_cutover() -> None:
    """``start == t*`` is classified as entirely_post_cutover (boundary).

    The post-cutover half is closed at ``t*``; a request ``[t*, end]``
    is M1-only lineage (M1 continues from the cloned checkpoint at
    ``t*``).
    """
    audit, clone = _fake_readers_with_cutover()
    start = CUTOVER_VALID_TIME
    end = datetime(2026, 7, 15, tzinfo=UTC)

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "entirely_post_cutover"
    assert plan.sub_intervals[0].lineage == "M1"


# ===========================================================================
# Test (c): straddle interval splits into two lineage-pinned sub-intervals
# ===========================================================================


def test_interval_straddling_cutover_splits_into_two_sub_intervals_no_splicing() -> None:
    """start < t* < end → two lineage-pinned sub-intervals; NO splicing.

    Evidence line (c): "an interval straddling ``t*`` resolves to the
    two-sub-interval split plan with per-sub-interval lineage and no
    spliced output series".
    """
    audit, clone = _fake_readers_with_cutover()
    start = datetime(2026, 5, 15, tzinfo=UTC)
    end = datetime(2026, 6, 15, tzinfo=UTC)

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "straddling_cutover"
    assert plan.cutover_valid_time == CUTOVER_VALID_TIME
    # Exactly TWO sub-intervals, split at ``t*``.
    assert len(plan.sub_intervals) == 2
    assert plan.sub_intervals == (
        ReplayLineageSubInterval(
            start=start,
            end=CUTOVER_VALID_TIME,
            model_id=M0_MODEL_ID,
            lineage="M0",
        ),
        ReplayLineageSubInterval(
            start=CUTOVER_VALID_TIME,
            end=end,
            model_id=M1_MODEL_ID,
            lineage="M1",
        ),
    )
    # Splicing-forbidden invariant: no sub-interval carries a mixed
    # lineage, and the boundary is exactly ``t*`` on both sides.
    assert plan.sub_intervals[0].end == plan.sub_intervals[1].start == CUTOVER_VALID_TIME
    assert {si.lineage for si in plan.sub_intervals} == {"M0", "M1"}


# ===========================================================================
# Test (d): ``t*`` is audit-authority-anchored, not data-availability
# ===========================================================================


def test_t_star_comes_from_audit_authority_and_equals_cloned_rows_valid_time() -> None:
    """``t*`` is the cloned rows' ``valid_time``, NOT data-availability MAX.

    Evidence line (d): "``t*`` is read from the recorded activation
    audit authority, not derived from data availability, and equals the
    cloned rows' ``valid_time`` in a cutover fixture."

    The fake clone reader mirrors the production
    ``clone_gate_fingerprint IS NOT NULL`` filter: it only reports
    ``valid_time`` values from clone rows and IGNORES post-cutover
    forecast/save-state rows (which carry ``clone_gate_fingerprint IS
    NULL`` in the real DB and are ignored by the production SQL
    ``WHERE`` clause).
    """
    audit, clone = _fake_readers_with_cutover()
    # Data-availability noise: seed the fake with a much later
    # ``valid_time`` in a *different* model_id key ("post-cutover
    # forecast row"). It must NOT influence the resolver — the
    # production ``clone_gate_fingerprint IS NOT NULL`` filter excludes
    # non-clone rows, and the fake mirrors that by only reading the
    # ``M1`` clone key.
    noise_key = f"{M1_MODEL_ID}::forecast-noise"
    clone.valid_times_by_model[noise_key] = {datetime(2026, 9, 1, tzinfo=UTC)}

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=datetime(2026, 5, 15, tzinfo=UTC),
        interval_end=datetime(2026, 6, 15, tzinfo=UTC),
        audit_reader=audit,
        clone_reader=clone,
    )

    # The audit-authority-identified ``t*`` equals the cloned rows'
    # ``valid_time``, NOT the noise ``valid_time``.
    assert plan.cutover_valid_time == CUTOVER_VALID_TIME
    # Straddle midpoint proves ``t*`` was used to split (not the noise
    # value 2026-09-01 which would push the whole interval pre-cutover).
    assert plan.classification == "straddling_cutover"
    # The resolver reads exactly the ``M1`` model_id, never the noise
    # key — proves it does not scan data availability across other
    # models.
    assert clone.calls == [M1_MODEL_ID]


def test_ambiguous_clone_rows_raise_replay_lineage_ambiguity_error() -> None:
    """Two distinct ``valid_time`` across ``M1`` clone rows → raise.

    Spec invariant: one ``t*`` per cutover. If the read side returns
    multiple distinct ``valid_time`` values for the audit-identified
    ``M1``, the resolver refuses to guess and raises
    :class:`ReplayLineageAmbiguityError`.
    """
    audit, clone = _fake_readers_with_cutover()
    # Add a second ``valid_time`` under the same ``M1`` key — simulates
    # a broken invariant (two cutover ``valid_time`` values on the same
    # M1 clone lineage).
    clone.valid_times_by_model[M1_MODEL_ID].add(datetime(2026, 6, 8, tzinfo=UTC))

    with pytest.raises(ReplayLineageAmbiguityError):
        resolve_replay_lineage(
            basin_version_id=BASIN_VERSION_ID,
            interval_start=datetime(2026, 5, 15, tzinfo=UTC),
            interval_end=datetime(2026, 6, 15, tzinfo=UTC),
            audit_reader=audit,
            clone_reader=clone,
        )


# ===========================================================================
# no_cutover_history classification
# ===========================================================================


def test_no_cutover_history_returns_single_legacy_sub_interval() -> None:
    """Basin with no direct-grid activation → single legacy sub-interval.

    ``no_cutover_history`` classification: return a single sub-interval
    spanning the full requested interval with lineage="legacy" and an
    empty ``model_id`` (no audit-identified M0/M1). Consumers key on
    ``lineage`` in this case.
    """
    audit = _FakeAuditReader()  # empty — no activation
    clone = _FakeCloneReader()  # empty
    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = datetime(2026, 7, 1, tzinfo=UTC)

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "no_cutover_history"
    assert plan.cutover_valid_time is None
    assert plan.m0_model_id is None
    assert plan.m1_model_id is None
    assert plan.sub_intervals == (
        ReplayLineageSubInterval(
            start=start,
            end=end,
            model_id="",
            lineage="legacy",
        ),
    )
    # Clone reader is NEVER called when there is no audit-identified M1.
    assert clone.calls == []


def test_audit_without_clone_rows_raises_replay_lineage_error() -> None:
    """Audit says direct-grid activated but no clone rows → resolver refuses.

    Activation does not clone state: clone rows are written by the
    explicit clone tooling (``scripts/node22_clone_direct_grid_cutover_states.py``,
    ``scripts/model_succession/``), not by model activation. An
    audit-recorded activation without clone rows is still refused — the
    resolver cannot distinguish this from a benign no-cutover case, so it
    raises :class:`ReplayLineageError` for the caller to investigate.
    """
    audit, clone = _fake_readers_with_cutover()
    # Wipe the clone rows for M1 — clone rows are absent although the
    # audit records a direct-grid activation.
    clone.valid_times_by_model[M1_MODEL_ID] = set()

    with pytest.raises(ReplayLineageError, match="no clone rows"):
        resolve_replay_lineage(
            basin_version_id=BASIN_VERSION_ID,
            interval_start=datetime(2026, 5, 15, tzinfo=UTC),
            interval_end=datetime(2026, 6, 15, tzinfo=UTC),
            audit_reader=audit,
            clone_reader=clone,
        )


def test_invalid_interval_start_after_end_raises_value_error() -> None:
    """``interval_end < interval_start`` is a caller bug → ValueError."""
    audit, clone = _fake_readers_with_cutover()
    with pytest.raises(ValueError, match="interval_end must be >= interval_start"):
        resolve_replay_lineage(
            basin_version_id=BASIN_VERSION_ID,
            interval_start=datetime(2026, 6, 15, tzinfo=UTC),
            interval_end=datetime(2026, 5, 15, tzinfo=UTC),
            audit_reader=audit,
            clone_reader=clone,
        )


def test_naive_datetime_inputs_are_normalized_to_utc() -> None:
    """Naive datetimes are treated as UTC by the resolver.

    Defense-in-depth: the resolver's public API accepts a
    ``datetime``; naive datetimes are silently normalized to UTC
    (mirrors :func:`packages.common.state_manager._ensure_utc`). This
    keeps caller ergonomics consistent with the rest of the codebase.
    """
    audit, clone = _fake_readers_with_cutover()
    # Naive datetimes (no tzinfo).
    start = datetime(2026, 5, 1)
    end = datetime(2026, 5, 15)

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=start,
        interval_end=end,
        audit_reader=audit,
        clone_reader=clone,
    )

    assert plan.classification == "entirely_pre_cutover"
    assert plan.interval_start.tzinfo is not None
    assert plan.interval_end.tzinfo is not None
    assert plan.interval_start == start.replace(tzinfo=UTC)
    assert plan.interval_end == end.replace(tzinfo=UTC)


def test_plan_is_immutable_frozen_dataclass() -> None:
    """The plan is a frozen dataclass — consumers cannot mutate it in-place."""
    audit, clone = _fake_readers_with_cutover()
    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=datetime(2026, 5, 1, tzinfo=UTC),
        interval_end=datetime(2026, 5, 15, tzinfo=UTC),
        audit_reader=audit,
        clone_reader=clone,
    )
    assert isinstance(plan, ReplayLineagePlan)
    with pytest.raises((AttributeError, TypeError)):
        plan.classification = "straddling_cutover"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        plan.sub_intervals[0].lineage = "M1"  # type: ignore[misc]
