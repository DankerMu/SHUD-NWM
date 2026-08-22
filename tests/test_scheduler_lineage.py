"""Lineage cutover resolution for #1735 (`lineage-scoped-cycle-completion`).

The resolver answers exactly one question — "when did this ``model_id`` come
into existence for this source?" — and the scheduler scopes cycles by the
answer.  The traps pinned here are the ones that would silently re-open the
production deadlock:

- ``t*`` is the EARLIEST clone row under the model's own id, so a backdated
  re-activation cannot retroactively disown cycles the identity ran (D4);
- no ancestry walk, so a twice-recalibrated model is bounded by its own
  cutover ``t2`` and never by its parent's ``t1`` (D4);
- resolution is per ``(model_id, source_id)`` (D3);
- absent / unreadable provenance is "no lineage", never an error (task 1.7);
- the db-free plane reads the already-loaded index and issues no extra read;
- the DB plane uses its own ASC read — reusing the publisher's ``DESC``
  reader is the defect D3 names by hand.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    PsycopgStateSnapshotRepository,
    StateSnapshot,
)
from services.orchestrator import scheduler_lineage
from tests.lineage_state_index_fixtures import PACKAGE_CHECKSUM
from tests.lineage_state_index_fixtures import index_entry as _entry
from tests.lineage_state_index_fixtures import index_repository as _repository
from tests.lineage_state_index_fixtures import parse_utc as _dt


# ---------------------------------------------------------------------------
# db-free plane: clone provenance already in the loaded index
# ---------------------------------------------------------------------------
def test_clone_lineage_signal_resolves_predecessor_and_cutover(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
            )
        ],
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")

    assert signal["ready"] is True
    assert signal["has_lineage"] is True
    assert signal["predecessor_model_id"] == "model_a"
    assert signal["cutover_valid_time"] == "2026-08-21T12:00:00Z"
    assert signal["clone_gate_kind"] == "state_compatibility"

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )
    assert cutover is not None
    assert cutover.predecessor_model_id == "model_a"
    assert cutover.cutover_time == _dt("2026-08-21T12:00:00Z")


def test_clone_lineage_signal_without_clone_provenance_is_no_lineage(tmp_path: Path) -> None:
    """Task 1.7 / spec: absent provenance means no lineage, not an error."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [_entry(object_root=object_root, model_id="model_a", valid_time="2026-08-21T12:00:00Z")],
    )

    signal = repo.clone_lineage_signal(model_id="model_a", source_id="gfs")

    assert signal["ready"] is True
    assert signal["has_lineage"] is False
    assert signal["predecessor_model_id"] is None
    assert signal["cutover_valid_time"] is None
    assert (
        scheduler_lineage.resolve_lineage_cutover(repo, model_id="model_a", source_id="gfs") is None
    )


def test_self_referential_clone_row_confers_no_lineage_on_the_db_free_plane(
    tmp_path: Path,
) -> None:
    """A1: a row naming ITSELF as predecessor is corrupt provenance, not a ``t*``.

    Honoring it would mint a cutover out of nothing and scope every earlier
    cycle out of completion and cohort admission on the strength of a row that
    proves no predecessor ever existed — the silent direction: the gap simply
    stops being reported.  The model must be scored exactly as one that never
    cloned.
    """
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a_prime",
            )
        ],
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")

    assert signal["ready"] is True
    assert signal["has_lineage"] is False
    assert signal["predecessor_model_id"] is None
    assert signal["cutover_valid_time"] is None
    assert signal["clone_entry_count"] == 0
    assert (
        scheduler_lineage.resolve_lineage_cutover(
            repo, model_id="model_a_prime", source_id="gfs"
        )
        is None
    )


def test_self_referential_row_earlier_than_a_real_clone_does_not_move_the_boundary(
    tmp_path: Path,
) -> None:
    """A1 corner case: the LEGITIMATE row is the existence-start, so ``t*`` is its own.

    Rejecting the earlier self-row moves the boundary LATER, which superficially
    resembles the silent-hide direction.  It is not: the direction heuristic
    arbitrates genuine ambiguity between real clone rows (D4), and a self-row is
    not a real clone row.  ``model_a_prime`` came into existence when it was
    cloned FROM ``model_a``, not when a corrupt row named itself.
    """
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-20T00:00:00Z",
                cloned_from_model_id="model_a_prime",
                state_id="state_self_referential",
            ),
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
                state_id="state_real_clone",
            ),
        ],
    )

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )

    assert cutover is not None
    assert cutover.predecessor_model_id == "model_a"
    assert cutover.cutover_time == _dt("2026-08-21T12:00:00Z")


def test_unusable_earliest_clone_row_still_resolves_lineage_on_the_db_free_plane(
    tmp_path: Path,
) -> None:
    """A2: ``usable_flag`` is deliberately NOT part of the existence question.

    A clone row is mutable after birth — ``run_qc`` / ``mark_init_state_corrupted``
    flip ``usable_flag`` to ``False`` on any ``state_id``, and the clone row is
    exactly the init state the first post-cutover cycle warm-starts from.  An
    unusable row still proves the identity started then; skipping it would move
    ``t*`` later, which is the silent-hide direction.
    """
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
                usable_flag=False,
            )
        ],
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")
    assert signal["has_lineage"] is True

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )

    assert cutover is not None
    assert cutover.predecessor_model_id == "model_a"
    assert cutover.cutover_time == _dt("2026-08-21T12:00:00Z")


def test_clone_lineage_signal_resolves_from_loaded_index_without_extra_read(tmp_path: Path) -> None:
    """Spec: a pass that has loaded the index issues no additional read.

    The index file is deleted after the cache is warm; a resolver that touched
    the file again would fail closed to "no lineage" instead of resolving.
    """
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
            )
        ],
    )
    # Warm the shared index-snapshot cache the way a scheduling pass does.
    repo.generation_scoped_history_signal(
        model_id="model_a_prime",
        source_id="gfs",
        before_time=_dt("2026-08-21T12:00:00Z"),
        current_package_checksum=PACKAGE_CHECKSUM,
    )
    (tmp_path / "state-index.json").unlink()

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")

    assert signal["has_lineage"] is True
    assert signal["cutover_valid_time"] == "2026-08-21T12:00:00Z"


def test_clone_lineage_signal_unreadable_index_is_no_lineage(tmp_path: Path) -> None:
    index_path = tmp_path / "state-index.json"
    index_path.write_text("{not json", encoding="utf-8")
    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=_dt("2026-08-22T06:00:00Z"),
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")

    assert signal["status"] == "blocked"
    assert signal["has_lineage"] is False
    assert (
        scheduler_lineage.resolve_lineage_cutover(
            repo, model_id="model_a_prime", source_id="gfs"
        )
        is None
    )


def test_clone_lineage_signal_takes_the_earliest_clone_row(tmp_path: Path) -> None:
    """Task 5.8 / D4: a backdated re-activation must not move the boundary later."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
                created_at="2026-08-21T12:30:00Z",
            ),
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-22T00:00:00Z",
                cloned_from_model_id="model_a",
                created_at="2026-08-22T00:30:00Z",
            ),
        ],
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")

    assert signal["clone_entry_count"] == 2
    assert signal["cutover_valid_time"] == "2026-08-21T12:00:00Z"


def test_clone_lineage_signal_does_not_walk_the_ancestry_chain(tmp_path: Path) -> None:
    """Task 5.7 / D4: ``M -> M' @ t1 -> M'' @ t2`` bounds ``M''`` by ``t2``, never ``t1``."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-10T00:00:00Z",
                cloned_from_model_id="model_a",
            ),
            _entry(
                object_root=object_root,
                model_id="model_a_prime_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a_prime",
            ),
        ],
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime_prime", source_id="gfs")

    assert signal["predecessor_model_id"] == "model_a_prime"
    assert signal["cutover_valid_time"] == "2026-08-21T12:00:00Z"
    assert signal["clone_entry_count"] == 1


def test_clone_lineage_signal_is_resolved_per_source(tmp_path: Path) -> None:
    """Task 5.9 / D3: GFS and IFS may cut over at different instants."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                source_id="gfs",
                valid_time="2026-08-21T00:00:00Z",
                cloned_from_model_id="model_a",
            ),
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                source_id="IFS",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
            ),
        ],
    )

    gfs = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")
    ifs = repo.clone_lineage_signal(model_id="model_a_prime", source_id="IFS")

    assert gfs["cutover_valid_time"] == "2026-08-21T00:00:00Z"
    assert ifs["cutover_valid_time"] == "2026-08-21T12:00:00Z"


@pytest.mark.parametrize("gate_kind", ["state_compatibility", "hydrologic_core", "legacy_unknown", None])
def test_every_clone_gate_kind_confers_lineage(tmp_path: Path, gate_kind: str | None) -> None:
    """Task 1.6 / D2: the gate kind refines WHY, not WHETHER, the clone happened."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    repo = _repository(
        tmp_path,
        [
            _entry(
                object_root=object_root,
                model_id="model_a_prime",
                valid_time="2026-08-21T12:00:00Z",
                cloned_from_model_id="model_a",
                clone_gate_kind=gate_kind,
            )
        ],
    )

    signal = repo.clone_lineage_signal(model_id="model_a_prime", source_id="gfs")

    assert signal["has_lineage"] is True
    assert signal["cutover_valid_time"] == "2026-08-21T12:00:00Z"


def test_warm_start_at_the_cutover_resolves_to_the_clone_row(tmp_path: Path) -> None:
    """Task 5.6 (second half): the cycle AT ``t*`` warm-starts from the clone row."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    clone_entry = _entry(
        object_root=object_root,
        model_id="model_a_prime",
        valid_time="2026-08-21T12:00:00Z",
        cloned_from_model_id="model_a",
    )
    repo = _repository(tmp_path, [clone_entry])

    evidence = repo.strict_warm_start_evidence(
        model_id="model_a_prime",
        source_id="gfs",
        valid_time=_dt("2026-08-21T12:00:00Z"),
        model_package_version=clone_entry["model_package_version"],
        model_package_checksum=PACKAGE_CHECKSUM,
        required_lead_hours=12,
    )

    assert evidence["ready"] is True
    assert evidence["candidate_state"]["state_id"] == clone_entry["state_id"]
    assert evidence["state_snapshot_index"]["entry_valid_time"] == "2026-08-21T12:00:00Z"


# ---------------------------------------------------------------------------
# Boundary + DB plane
# ---------------------------------------------------------------------------
def test_is_pre_cutover_boundary_is_strict() -> None:
    cutover = scheduler_lineage.LineageCutover(
        model_id="model_a_prime",
        source_id="gfs",
        predecessor_model_id="model_a",
        cutover_time=_dt("2026-08-21T12:00:00Z"),
    )

    assert scheduler_lineage.is_pre_cutover(cutover, _dt("2026-08-21T06:00:00Z")) is True
    # ``C == t*`` is the model's own first cycle: in scope, scored normally.
    assert scheduler_lineage.is_pre_cutover(cutover, _dt("2026-08-21T12:00:00Z")) is False
    assert scheduler_lineage.is_pre_cutover(cutover, _dt("2026-08-21T18:00:00Z")) is False
    # No lineage is never scoped out.
    assert scheduler_lineage.is_pre_cutover(None, _dt("2020-01-01T00:00:00Z")) is False


class _EarliestCloneRowRepo:
    def __init__(self, row: StateSnapshot | None) -> None:
        self.row = row
        self.calls: list[tuple[str, str]] = []

    def get_earliest_clone_row_for_model_source(
        self, *, model_id: str, source_id: str
    ) -> StateSnapshot | None:
        self.calls.append((model_id, source_id))
        return self.row


class _LatestOnlyCloneRowRepo:
    """A repo exposing ONLY the publisher's ``DESC`` reader (D3: must not be used)."""

    def __init__(self, row: StateSnapshot | None) -> None:
        self.row = row
        self.calls: list[tuple[str, str]] = []

    def get_latest_clone_row_for_model_source(
        self, *, model_id: str, source_id: str
    ) -> StateSnapshot | None:
        self.calls.append((model_id, source_id))
        return self.row


def _clone_snapshot(valid_time: str) -> StateSnapshot:
    return StateSnapshot(
        state_id="state_db_clone",
        model_id="model_a_prime",
        run_id="clone_run",
        valid_time=_dt(valid_time),
        state_uri="s3://nhms/states/gfs/model_a_prime/state.cfg.ic",
        checksum="sha256:" + "e" * 64,
        usable_flag=True,
        source_id="gfs",
        cloned_from_model_id="model_a",
        clone_gate_fingerprint="sha256:" + "d" * 64,
        clone_gate_kind="state_compatibility",
    )


def test_db_plane_resolves_from_the_earliest_clone_row_reader() -> None:
    repo = _EarliestCloneRowRepo(_clone_snapshot("2026-08-21T12:00:00Z"))

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )

    assert repo.calls == [("model_a_prime", "gfs")]
    assert cutover is not None
    assert cutover.predecessor_model_id == "model_a"
    assert cutover.cutover_time == _dt("2026-08-21T12:00:00Z")


def test_db_plane_never_consults_the_publishers_latest_row_reader() -> None:
    repo = _LatestOnlyCloneRowRepo(_clone_snapshot("2026-08-22T00:00:00Z"))

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )

    assert cutover is None
    assert repo.calls == []


def test_db_plane_self_referential_clone_row_confers_no_lineage() -> None:
    """A1, DB plane: the resolver rejects it even if a reader hands it over.

    Both plane readers now filter self-referential rows out, so this pins the
    belt-and-braces guard at the resolver boundary: a stub, a stale deployment
    or a future reader that has not been tightened still cannot mint a ``t*``.
    """
    row = replace(
        _clone_snapshot("2026-08-21T12:00:00Z"), cloned_from_model_id="model_a_prime"
    )
    repo = _EarliestCloneRowRepo(row)

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )

    assert repo.calls == [("model_a_prime", "gfs")]
    assert cutover is None


def test_index_signal_plane_self_referential_predecessor_confers_no_lineage() -> None:
    """A1, index-signal branch: same guard on the other duck-typed accessor."""

    class _SelfReferentialSignalRepo:
        def clone_lineage_signal(self, *, model_id: str, source_id: str) -> dict[str, Any]:
            return {
                "ready": True,
                "has_lineage": True,
                "model_id": model_id,
                "source_id": source_id,
                "predecessor_model_id": model_id,
                "cutover_valid_time": "2026-08-21T12:00:00Z",
                "clone_gate_kind": "state_compatibility",
            }

    assert (
        scheduler_lineage.resolve_lineage_cutover(
            _SelfReferentialSignalRepo(), model_id="model_a_prime", source_id="gfs"
        )
        is None
    )


def test_db_plane_unusable_clone_row_still_resolves_lineage() -> None:
    """A2, DB plane: an unusable earliest clone row is still the existence-start."""
    repo = _EarliestCloneRowRepo(
        replace(_clone_snapshot("2026-08-21T12:00:00Z"), usable_flag=False)
    )

    cutover = scheduler_lineage.resolve_lineage_cutover(
        repo, model_id="model_a_prime", source_id="gfs"
    )

    assert cutover is not None
    assert cutover.predecessor_model_id == "model_a"
    assert cutover.cutover_time == _dt("2026-08-21T12:00:00Z")


def test_db_plane_no_clone_row_is_no_lineage() -> None:
    assert (
        scheduler_lineage.resolve_lineage_cutover(
            _EarliestCloneRowRepo(None), model_id="model_a_prime", source_id="gfs"
        )
        is None
    )
    assert (
        scheduler_lineage.resolve_lineage_cutover(None, model_id="model_a_prime", source_id="gfs")
        is None
    )


def test_earliest_clone_row_query_is_ascending_and_clone_scoped(monkeypatch: Any) -> None:
    """D3/D4: existence-start ordering, with the shadow-proof clone filters kept.

    The NEGATIVE pin is the load-bearing half.  Omitting ``usable_flag`` from
    this statement is a deliberate decision (#1735 A2): an unusable clone row
    still proves the identity started then, and filtering it would move ``t*``
    LATER — the silent-hide direction the design forbids.  Without an explicit
    absence assertion, adding ``AND usable_flag = true`` here keeps every other
    test in this suite green while silently reintroducing that regression.
    """
    captured: dict[str, Any] = {}

    def _fake_fetch_optional(self: Any, statement: str, parameters: Any) -> None:
        captured["statement"] = " ".join(statement.split())
        captured["parameters"] = tuple(parameters)
        return None

    monkeypatch.setattr(
        PsycopgStateSnapshotRepository, "_fetch_optional", _fake_fetch_optional, raising=True
    )
    repo = PsycopgStateSnapshotRepository("postgresql://unused/db")

    assert (
        repo.get_earliest_clone_row_for_model_source(model_id="model_a_prime", source_id="gfs")
        is None
    )
    assert "ORDER BY valid_time ASC, created_at ASC" in captured["statement"]
    assert "clone_gate_fingerprint IS NOT NULL" in captured["statement"]
    assert "cloned_from_model_id IS NOT NULL" in captured["statement"]
    # A1: a row naming itself is not a predecessor and confers no lineage.
    assert "cloned_from_model_id <> model_id" in captured["statement"]
    # A2 negative pin: no usable_flag filter, in any spelling.
    assert "usable_flag" not in captured["statement"]
    assert captured["parameters"] == ("model_a_prime", "gfs")


def test_lineage_scoped_out_record_names_predecessor_and_cutover() -> None:
    cutover = scheduler_lineage.LineageCutover(
        model_id="model_a_prime",
        source_id="gfs",
        predecessor_model_id="model_a",
        cutover_time=_dt("2026-08-21T12:00:00Z"),
        clone_gate_kind="state_compatibility",
    )

    record = scheduler_lineage.lineage_scoped_out_record(
        cutover, cycle_time=_dt("2026-08-07T00:00:00Z"), cycle_id="gfs_2026080700"
    )

    assert record == {
        "reason": "lineage_scoped_out_pre_cutover",
        "model_id": "model_a_prime",
        "source_id": "gfs",
        "predecessor_model_id": "model_a",
        "cutover_valid_time": "2026-08-21T12:00:00Z",
        "cycle_time_utc": "2026-08-07T00:00:00Z",
        "clone_gate_kind": "state_compatibility",
        "cycle_id": "gfs_2026080700",
    }
