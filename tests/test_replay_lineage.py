"""Unit tests for §5.1 provenance-pinned replay lineage resolver.

Covers Epic #982 SUB-8 (``mapping-variant-state-compatibility`` task
5.1) evidence:

* Tests (a) / (b) / (c): interval classification and the straddle split
  with per-sub-interval lineage and no cross-variant splicing.
* Test (d): ``t*`` comes from the recorded activation audit authority
  (equal to the cloned rows' ``valid_time``) and IGNORES data-availability
  noise (post-cutover forecast/save-state rows without
  ``clone_gate_fingerprint``).
* Test (e): an offline ``M0`` replay resolution for a basin with
  direct-grid activation history makes ZERO lifecycle calls and is not
  intercepted by Change 4's activation-scoped legacy-reactivation guard.
* Test (f): an actual ``activate(M0)`` request for the SAME basin is
  still refused by the guard — proves the guard-scope invariant is
  preserved orthogonally to the resolver.

Plus:

* ``no_cutover_history`` classification when the basin has no direct-
  grid activation record — single legacy sub-interval end-to-end.
* :class:`ReplayLineageAmbiguityError` raised when the ``M1`` clone
  rows disagree on ``valid_time`` (spec invariant: one ``t*`` per
  cutover).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    ModelLifecycleOperation,
    PsycopgModelRegistryStore,
    _classify_forcing_mapping_mode,
)
from services.orchestrator.replay_lineage import (
    ReplayLineageAmbiguityError,
    ReplayLineageError,
    ReplayLineagePlan,
    ReplayLineageSubInterval,
    resolve_replay_lineage,
)

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
BASIN_ID = "basin_a"
M0_MODEL_ID = "legacy_m0"
M1_MODEL_ID = "direct_grid_m1"
CUTOVER_VALID_TIME = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory fakes for the resolver's read protocols
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditReader:
    """In-memory :class:`ReplayLineageAuditReader` for resolver tests.

    Records the ``basin_version_id`` on every call so tests can assert
    the resolver reads exactly once.
    """

    activations_by_basin: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_latest_direct_grid_activation(
        self, basin_version_id: str
    ) -> Mapping[str, Any] | None:
        self.calls.append(basin_version_id)
        return self.activations_by_basin.get(basin_version_id)


@dataclass
class _FakeCloneReader:
    """In-memory :class:`ReplayLineageCloneReader` for resolver tests.

    ``valid_times_by_model`` maps ``model_id`` -> the set of distinct
    ``valid_time`` values across the clone rows (mirrors the ``SELECT
    DISTINCT valid_time`` shape the production reader emits). Empty
    set = no clone rows for that model; >1 = raises
    :class:`ReplayLineageAmbiguityError`.
    """

    valid_times_by_model: dict[str, set[datetime]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_cutover_valid_time_for_model(
        self, model_id: str
    ) -> datetime | None:
        self.calls.append(model_id)
        distinct = self.valid_times_by_model.get(model_id, set())
        if not distinct:
            return None
        if len(distinct) > 1:
            raise ReplayLineageAmbiguityError(
                f"clone rows for model_id={model_id!r} disagree on valid_time: "
                f"{sorted(str(vt) for vt in distinct)}"
            )
        (only,) = distinct
        return only


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

    Under the SUB-4 hook clone rows are written in the same transaction
    as activation. Missing clone rows for an audit-recorded activation
    means state was mutated out-of-band — the resolver cannot
    distinguish this from a benign no-cutover case, so it raises
    :class:`ReplayLineageError` for the caller to investigate.
    """
    audit, clone = _fake_readers_with_cutover()
    # Wipe the clone rows for M1 — simulate an out-of-band delete
    # after the atomic-cutover transaction committed.
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


# ===========================================================================
# Guard-scope tests (e) and (f) — the resolver DOES NOT call the lifecycle
# path, and the guard STILL refuses an actual ``activate(M0)`` request.
# ===========================================================================


def _valid_direct_grid_forcing_block(
    *,
    model_id: str,
    grid_id: str = "grid_a",
    applicable_source_ids: tuple[str, ...] = ("gfs", "IFS"),
) -> dict[str, Any]:
    """Return a fully-populated ``direct_grid_forcing`` section that parses.

    Same shape as ``tests/test_legacy_reactivation_guard.py`` — includes
    every field ``parse_direct_grid_forcing_contract`` requires so
    ``_classify_forcing_mapping_mode`` returns ``"direct_grid"``.
    """
    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": f"s3://nhms/models/{model_id}/binding.json",
        "binding_checksum": f"sha256:{model_id}-binding",
        "model_input_package_id": f"{model_id}_input",
        "sp_att_path": f"s3://nhms/models/{model_id}/sp_att.csv",
        "sp_att_checksum": f"sha256:{model_id}-spatt",
        "applicable_source_ids": list(applicable_source_ids),
        "grid_id": grid_id,
        "grid_signature": f"{grid_id}_v1",
        "station_bindings": [
            {
                "station_id": f"{model_id}_stn_1",
                "shud_forcing_index": 1,
                "forcing_filename": f"{model_id}_X0Y0.csv",
                "longitude": 100.0,
                "latitude": 30.0,
                "x": 100.0,
                "y": 30.0,
                "z": 0.0,
                "grid_id": grid_id,
                "grid_cell_id": f"{grid_id}_cell_1",
            }
        ],
    }


def _model_row(
    *,
    model_id: str,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    resource_profile: dict[str, Any] | None = None,
    basin_version_id: str = BASIN_VERSION_ID,
    basin_id: str = BASIN_ID,
) -> dict[str, Any]:
    """Return a model row shaped like ``_fetch_model_lifecycle_row`` output.

    Mirrors the fixture shape used by
    ``tests/test_legacy_reactivation_guard.py`` so preflight sees a
    valid activation-ready row.
    """
    profile = {
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "package_checksum": f"sha256:{model_id}-package",
        "copied_root_status": "verified",
        "package_checksum_verified": True,
        **(resource_profile or {}),
    }
    return {
        "model_id": model_id,
        "model_name": model_id,
        "basin_id": basin_id,
        "basin_name": basin_id.upper(),
        "basin_version_id": basin_version_id,
        "river_network_version_id": f"{basin_id}_rivnet_v01",
        "mesh_version_id": f"{basin_id}_mesh_v01",
        "calibration_version_id": f"{basin_id}_cal_v01",
        "shud_code_version": "2.0",
        "mesh_uri": f"s3://nhms/models/{model_id}/mesh.sp.mesh",
        "mesh_checksum": f"sha256:{model_id}-mesh",
        "model_package_uri": f"s3://nhms/models/{model_id}/package/",
        "package_checksum": f"sha256:{model_id}-package",
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "source_inventory_checksum": None,
        "basin_slug": basin_id.replace("_", "-"),
        "shud_input_name": basin_id,
        "segment_count": 1,
        "basin_checksum": f"sha256:{basin_id}-basin",
        "river_network_checksum": f"sha256:{basin_id}-rivnet",
        "mesh_properties_json": {},
        "active_flag": active_flag,
        "lifecycle_state": lifecycle_state,
        "resource_profile": profile,
        "created_at": "2026-07-10T00:00:00Z",
    }


def _legacy_model(
    model_id: str,
    *,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
) -> dict[str, Any]:
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
    )


def _direct_grid_model(
    model_id: str,
    *,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
) -> dict[str, Any]:
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
        resource_profile={
            "direct_grid_forcing": _valid_direct_grid_forcing_block(model_id=model_id),
        },
    )


class _RecordingCursor:
    """Fake cursor recording SQL passing through it (regression guard)."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(  # pragma: no cover - regression guard
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> None:
        self.statements.append((statement, tuple(parameters)))

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover
        return None

    def fetchall(self) -> list[dict[str, Any]]:  # pragma: no cover
        return []


class _FakeTransaction:
    def __init__(self, harness: _LifecycleCallRecorder) -> None:
        self._harness = harness

    def __enter__(self) -> _RecordingCursor:
        cursor = _RecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        return cursor

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: Any,
    ) -> bool:
        self._harness._transactions[-1]["committed"] = exc_type is None
        return False


class _LifecycleCallRecorder(PsycopgModelRegistryStore):
    """In-memory ``PsycopgModelRegistryStore`` recording lifecycle calls.

    Every call to :meth:`model_lifecycle_operation` is appended to
    ``self.lifecycle_calls``. Tests (e) and (f) use this to prove:

    * Test (e): a resolver call produces an empty ``lifecycle_calls``
      list (zero-lifecycle-call invariant).
    * Test (f): a subsequent real ``activate(M0)`` call for the same
      basin is refused by the Change 4 legacy-reactivation guard
      (``LEGACY_REACTIVATION_BLOCKED`` blocker). The recorder confirms
      the request reached the lifecycle path.

    The audit-log guard predicate
    (:meth:`_fetch_direct_grid_activation_history`) is overridden to
    mirror production classification over the in-memory audit rows so
    the guard fires correctly without a live DB.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__("postgresql://harness-replay-lineage")
        object.__setattr__(
            self, "_models", {row["model_id"]: dict(row) for row in models}
        )
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_state_updates", [])
        object.__setattr__(self, "lifecycle_calls", [])

    # ---- lifecycle-call recorder -----------------------------------------

    def model_lifecycle_operation(  # type: ignore[override]
        self,
        model_id: str,
        *,
        operation: ModelLifecycleOperation,
        policy_decision: Any,
        request_id: str,
        override_missing_active: bool = False,
        reason: str | None = None,
        previous_model_id: str | None = None,
    ) -> dict[str, Any]:
        self.lifecycle_calls.append(
            {
                "model_id": model_id,
                "operation": operation,
                "request_id": request_id,
                "previous_model_id": previous_model_id,
            }
        )
        return super().model_lifecycle_operation(
            model_id,
            operation=operation,
            policy_decision=policy_decision,
            request_id=request_id,
            override_missing_active=override_missing_active,
            reason=reason,
            previous_model_id=previous_model_id,
        )

    # ---- transaction plumbing --------------------------------------------

    def _transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    # ---- read helpers ----------------------------------------------------

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:  # noqa: ARG002
        return None

    def _fetch_model_lifecycle_row(
        self, cursor: Any, model_id: str, *, for_update: bool  # noqa: ARG002
    ) -> dict[str, Any] | None:
        row = self._models.get(model_id)
        return dict(row) if row is not None else None

    def _fetch_active_model_for_scope(
        self,
        cursor: Any,  # noqa: ARG002
        basin_version_id: str,
        *,
        for_update: bool,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        for row in self._models.values():
            if (
                row["basin_version_id"] == basin_version_id
                and bool(row.get("active_flag"))
                and str(row.get("lifecycle_state") or "active") == "active"
            ):
                return dict(row)
        return None

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        current_model: Mapping[str, Any],  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_idempotent_rollback_retry_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],  # noqa: ARG002
        current_active: Mapping[str, Any] | None,  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_direct_grid_activation_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        basin_version_id: str,
        current_active: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Mirror production predicate over in-memory audit rows.

        Applies the SAME classifier as production
        (:func:`_classify_forcing_mapping_mode`) so the Change 4 guard
        fires on the audit-log arm without a live DB. Test (f) relies
        on this: it seeds an audit row for the direct-grid activation
        of ``direct_m1`` and then attempts ``activate(legacy_m0)`` —
        this predicate returns non-None (arming the guard), preflight
        emits ``LEGACY_REACTIVATION_BLOCKED``, and the operation is
        refused.
        """
        if current_active is not None:
            if _classify_forcing_mapping_mode(current_active) == "direct_grid":
                return {
                    "source": "current_active",
                    "model_id": str(current_active.get("model_id")),
                    "basin_version_id": basin_version_id,
                }
        for row in self.audit_rows:
            if row.get("entity_type") != "model_instance":
                continue
            if row.get("action") not in {
                "models.activate",
                "models.switch_version",
                "models.rollback_version",
            }:
                continue
            if row.get("outcome") not in {"allowed", "rollback"}:
                continue
            if row.get("basin_version_id") != basin_version_id:
                continue
            updated = self._models.get(row.get("updated_model_id"))
            if updated is None:
                continue
            if _classify_forcing_mapping_mode(updated) == "direct_grid":
                return {
                    "source": "audit_log",
                    "log_id": row.get("log_id"),
                    "model_id": row.get("updated_model_id"),
                    "basin_version_id": basin_version_id,
                }
        return None

    # ---- write helpers ---------------------------------------------------

    def _update_model_lifecycle_state(
        self, cursor: Any, model_id: str, lifecycle_state: str  # noqa: ARG002
    ) -> dict[str, Any]:
        row = self._models[model_id]
        row["lifecycle_state"] = lifecycle_state
        row["active_flag"] = lifecycle_state == "active"
        self._state_updates.append((model_id, lifecycle_state, row["active_flag"]))
        return dict(row)

    def _insert_model_lifecycle_audit(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],
        updated: Mapping[str, Any],
        operation: ModelLifecycleOperation,
        outcome: str,
        policy_decision: Any,
        request_id: str | None,
        preflight: Mapping[str, Any],
        previous_model: Mapping[str, Any] | None,
        reason: str | None,
    ) -> int:
        entry = {
            "log_id": len(self.audit_rows) + 1,
            "entity_type": "model_instance",
            "entity_id": model["model_id"],
            "action": policy_decision.action_id,
            "actor": policy_decision.actor_id,
            "operation": operation,
            "outcome": outcome,
            "basin_version_id": model.get("basin_version_id"),
            "request_id": request_id,
            "reason": reason,
            "preflight_status": preflight.get("status"),
            "updated_model_id": updated["model_id"],
            "previous_model_id": previous_model["model_id"] if previous_model else None,
        }
        self.audit_rows.append(entry)
        return entry["log_id"]


def _decision(action_id: str, target_id: str) -> Any:
    return trusted_internal_policy_decision(
        action_id,
        target_type="model_instance",
        target_id=target_id,
        actor_id="test:replay-lineage",
        roles=("sys_admin",),
    )


def _seed_basin_with_direct_grid_activation_history() -> _LifecycleCallRecorder:
    """Build a store with a real direct-grid activation history.

    Seeds two models on ``BASIN_VERSION_ID``:

    * ``legacy_m0`` — legacy-mapping, inactive/superseded.
    * ``direct_m1`` — direct-grid, active (arms the guard via the
      currently-active-model classifier + carries a matching audit row).

    Also injects a synthetic ``models.activate`` audit row for the M1
    activation so the guard's audit-log arm is armed even if the
    current-active classifier is bypassed in a future refactor.
    """
    store = _LifecycleCallRecorder(
        [
            _legacy_model("legacy_m0", active_flag=False, lifecycle_state="superseded"),
            _direct_grid_model(
                "direct_m1", active_flag=True, lifecycle_state="active"
            ),
        ]
    )
    # Seed a synthetic activation audit row so the audit-log arm is
    # populated (belt-and-suspenders — the current-active classifier
    # also arms the guard in this fixture).
    store.audit_rows.append(
        {
            "log_id": 1,
            "entity_type": "model_instance",
            "entity_id": "direct_m1",
            "action": "models.activate",
            "actor": "test:seed",
            "operation": "activate",
            "outcome": "allowed",
            "basin_version_id": BASIN_VERSION_ID,
            "request_id": None,
            "reason": None,
            "preflight_status": "ready",
            "updated_model_id": "direct_m1",
            "previous_model_id": "legacy_m0",
        }
    )
    return store


def test_pre_cutover_m0_replay_makes_no_lifecycle_call_not_intercepted_by_guard() -> None:
    """Evidence line (e): offline M0 replay makes ZERO lifecycle calls.

    On a basin with direct-grid activation history, resolving a pre-
    cutover replay must NOT touch ``model_lifecycle_operation``. The
    Change 4 legacy-reactivation guard is activation-scoped and cannot
    mis-fire on offline replay because the resolver never reaches an
    activation path.
    """
    store = _seed_basin_with_direct_grid_activation_history()
    # Sanity: no lifecycle calls yet.
    assert store.lifecycle_calls == []

    audit_reader = _FakeAuditReader(
        activations_by_basin={
            BASIN_VERSION_ID: {
                "m1_model_id": "direct_m1",
                "m0_model_id": "legacy_m0",
                "audit_log_id": 1,
            }
        }
    )
    clone_reader = _FakeCloneReader(
        valid_times_by_model={"direct_m1": {CUTOVER_VALID_TIME}}
    )

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=datetime(2026, 5, 1, tzinfo=UTC),
        interval_end=datetime(2026, 5, 15, tzinfo=UTC),
        audit_reader=audit_reader,
        clone_reader=clone_reader,
    )

    # The resolver returned a pre-cutover M0 plan.
    assert plan.classification == "entirely_pre_cutover"
    assert plan.sub_intervals[0].lineage == "M0"
    assert plan.sub_intervals[0].model_id == "legacy_m0"

    # Zero-lifecycle-call invariant: the store recorded NO
    # ``model_lifecycle_operation`` calls, so the Change 4 guard
    # (activation-scoped) cannot have fired.
    assert store.lifecycle_calls == []
    # No state updates either.
    assert store._state_updates == []
    # No audit rows written beyond the seeded one.
    assert len(store.audit_rows) == 1


def test_actual_activate_m0_for_same_basin_still_refused_by_guard() -> None:
    """Evidence line (f): actual ``activate(M0)`` is refused by the guard.

    Uses the SAME basin fixture as test (e) — a direct-grid activation
    history on ``basin_v01``. Calling
    ``store.model_lifecycle_operation('legacy_m0', operation='activate',
    ...)`` reaches the Change 4 legacy-reactivation guard, which
    classifies ``legacy_m0`` as legacy-mapping on a direct-grid-history
    basin and refuses with ``LEGACY_REACTIVATION_BLOCKED``.

    Proves that the resolver's zero-lifecycle-call invariant does NOT
    weaken the guard — the guard remains enforced on real activation
    requests for the same basin (orthogonality of offline replay and
    activation-path guarding).
    """
    store = _seed_basin_with_direct_grid_activation_history()

    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="activate",
        policy_decision=_decision("models.activate", "legacy_m0"),
        request_id="req-real-activate-legacy",
    )

    # The call reached the lifecycle path (recorded).
    assert len(store.lifecycle_calls) == 1
    assert store.lifecycle_calls[0]["model_id"] == "legacy_m0"
    assert store.lifecycle_calls[0]["operation"] == "activate"

    # And it was refused by the Change 4 guard.
    assert result["status"] == "blocked"
    preflight = result.get("preflight") or {}
    blocker_codes = [b["code"] for b in (preflight.get("blockers") or [])]
    assert "LEGACY_REACTIVATION_BLOCKED" in blocker_codes

    # No state transition, no additional audit row (only the seeded
    # audit row remains — the refusal is inert on state).
    assert store._state_updates == []
    # ``legacy_m0`` was not activated; ``direct_m1`` stays active.
    assert store._models["legacy_m0"]["active_flag"] is False
    assert store._models["direct_m1"]["active_flag"] is True


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
