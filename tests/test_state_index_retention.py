"""#2548: state-index ``prune-retention`` repair and capacity evidence.

Seams: ``repair_state_snapshot_index(operation="prune-retention", ...)``, the
operator CLI ``scripts.scheduler_state_index_repair.main``, the public
``FileStateSnapshotIndexRepository`` readers before/after a prune,
``evaluate_transition_decision`` fed by those readers, and the capacity fields
of ``state_index_evidence()`` / ``publish_state_snapshot_index``.

Expected values come from the retention contract in
``openspec/changes/state-index-retention-prune/design.md`` (K1-K5, the window
floor ``336 + lag + 48``) and from the readers' own pre-prune answers, never
from the planner under test. Every lock parent created here goes through
``tests.provider_mode_helpers`` (node-27 runs umask 0002, #2614).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import ProviderAtomicError
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateManagerError,
    StateSnapshot,
    publish_state_snapshot_index,
    repair_state_snapshot_index,
)
from scripts import scheduler_state_index_repair as repair
from services.orchestrator import scheduler_generation as generation
from services.orchestrator.scheduler_generation import TransitionDecision
from tests.provider_mode_helpers import make_directory_with_explicit_mode
from workers.data_adapters.base import cycle_id_for

PREFIX = "s3://nhms"
NOW = datetime(2026, 7, 21, tzinfo=UTC)
LEAD = 12
GEN_A = "a" * 64
GEN_B = "b" * 64
GEN_C = "c" * 64
GEN_D = "d" * 64
GEN_E = "e" * 64  # never present in any index: a brand-new candidate package
CHECKSUMS = (GEN_A, GEN_B, GEN_C, GEN_D, GEN_E, None)
RETENTION_DAYS = 21
LAG = 16
INJECTED_FIELDS = ("index_generated_at", "object_evidence")
SHAPES = (14, 17, 18, 16, 19, 20)


def _day(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_state_bytes(seed: bytes) -> bytes:
    minute = 27_000_000.0 + (int.from_bytes(seed[:4].ljust(4, b"\x00"), "big") % 1000)
    return (f"2\t1\t{minute:.6f}\n1\t0.1\t0.1\t0.1\t0.1\t0.1\n2\t0.1\t0.1\t0.1\t0.1\t0.1\n1\t0.5\n").encode()


def _entry(
    model_id: str,
    source_id: str,
    valid_time: datetime,
    *,
    generation_checksum: str | None,
    state_uri: str,
    checksum: str,
    usable: bool = True,
    cloned_from_model_id: str | None = None,
    cloned_from_state_id: str | None = None,
    shape: int = 18,
) -> dict[str, Any]:
    cycle_id = cycle_id_for(source_id, valid_time - timedelta(hours=LEAD))
    entry: dict[str, Any] = {
        "state_id": f"state_{source_id}_{model_id}_{valid_time:%Y%m%d%H%M%S}_{cycle_id}_f012",
        "model_id": model_id,
        "run_id": f"fcst_{cycle_id}_{model_id}",
        "source_id": source_id,
        "valid_time": _iso(valid_time),
        "state_uri": state_uri,
        "checksum": checksum,
        "usable_flag": usable,
        "created_at": None,
        "cycle_id": cycle_id,
        "lead_hours": LEAD,
        "model_package_version": f"{PREFIX}/models/{model_id}/package/",
        "model_package_checksum": generation_checksum,
        "original_shud_filename": "X.f012.cfg.ic.update",
    }
    # Production shapes (2026-09-25 copies): shared lane 14 / 17 / 18 keys; the private
    # lane adds the two read-time fields (index_generated_at, object_evidence): 16 / 19 / 20.
    injected = shape in (16, 19, 20)
    base = shape - 2 if injected else shape
    assert base in (14, 17, 18)
    if base >= 17 or cloned_from_model_id or cloned_from_state_id:
        entry.update(
            {
                "cloned_from_state_id": cloned_from_state_id,
                "cloned_from_model_id": cloned_from_model_id,
                "clone_gate_fingerprint": None,
            }
        )
        if base == 18 or cloned_from_model_id:
            entry["clone_gate_kind"] = "recalibration" if cloned_from_model_id else None
    if injected:
        entry["index_generated_at"] = "2026-07-20T00:00:00Z"
        entry["object_evidence"] = None
    return entry


def _stripped(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key not in INJECTED_FIELDS}


class Lanes:
    """Reference (private) + destination (shared) roots with owner-private archive/receipt roots."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reference_root = root / "object-store"
        self.destination_root = root / "shared-object-store"
        self.archive_root = root / "archives"
        self.receipt_root = root / "receipts"
        for private in (self.archive_root, self.receipt_root):
            private.mkdir(parents=True)
            os.chmod(private, 0o700)
        self.reference_index = self.reference_root / "scheduler/state-index/index-last.json"
        self.destination_index = self.destination_root / "scheduler/state-index/index-last.json"
        make_directory_with_explicit_mode(self.reference_index.parent)
        make_directory_with_explicit_mode(self.destination_index.parent)
        content = _valid_state_bytes(b"retention")
        self.state_uri = LocalObjectStore(self.reference_root, PREFIX).write_bytes_atomic(
            "states/gfs/shared/state.cfg.ic", content
        )
        LocalObjectStore(self.destination_root, PREFIX).write_bytes_atomic("states/gfs/shared/state.cfg.ic", content)
        self.content = content
        self.checksum = f"sha256:{sha256_bytes(content)}"

    def entry(self, model_id: str, source_id: str, valid_time: datetime, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("generation_checksum", GEN_A)
        return _entry(model_id, source_id, valid_time, state_uri=self.state_uri, checksum=self.checksum, **kwargs)

    def publish(self, reference: list[dict[str, Any]], destination: list[dict[str, Any]] | None = None) -> None:
        for index_path, root, entries in (
            (self.reference_index, self.reference_root, reference),
            (self.destination_index, self.destination_root, reference if destination is None else destination),
        ):
            publish_state_snapshot_index(
                entries,
                index_path,
                object_store_root=root,
                object_store_prefix=PREFIX,
                generated_at=NOW,
                verify_objects=False,
            )

    def entries(self, lane: str) -> list[dict[str, Any]]:
        path = self.reference_index if lane == "reference" else self.destination_index
        return json.loads(path.read_text(encoding="utf-8"))["entries"]

    def prune(self, *, enforce: bool = False, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("retention_days", RETENTION_DAYS)
        kwargs.setdefault("cycle_lag_hours", LAG)
        return repair_state_snapshot_index(
            reference_root=self.reference_root,
            destination_root=self.destination_root,
            operation="prune-retention",
            object_store_prefix=PREFIX,
            archive_root=self.archive_root if enforce else None,
            enforce=enforce,
            generated_at=NOW,
            **kwargs,
        )

    def repository(self) -> FileStateSnapshotIndexRepository:
        return FileStateSnapshotIndexRepository(
            str(self.reference_index),
            object_store_root=self.reference_root,
            object_store_prefix=PREFIX,
            now=NOW,
        )

    def apply_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(repair.REFERENCE_ROOT_ENV, str(self.reference_root))
        monkeypatch.setenv(repair.DESTINATION_ROOT_ENV, str(self.destination_root))
        monkeypatch.setenv(repair.OBJECT_STORE_PREFIX_ENV, PREFIX)
        monkeypatch.setenv(repair.ARCHIVE_ROOT_ENV, str(self.archive_root))
        monkeypatch.setenv(repair.RECEIPT_ROOT_ENV, str(self.receipt_root))
        monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", str(LAG))


@pytest.fixture(name="lanes")
def lanes_factory(tmp_path: Path) -> Lanes:
    return Lanes(tmp_path)


def _fs_fingerprint(root: Path) -> dict[str, tuple[Any, ...]]:
    fingerprint: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*"), key=str):
        info = path.lstat()
        rel = str(path.relative_to(root))
        if stat.S_ISDIR(info.st_mode):
            fingerprint[rel] = ("dir", stat.S_IMODE(info.st_mode))
        else:
            fingerprint[rel] = ("file", path.read_bytes(), stat.S_IMODE(info.st_mode))
    return fingerprint


def _series(start: datetime, end: datetime, step_hours: int) -> list[datetime]:
    times = []
    current = start
    while current <= end:
        times.append(current)
        current += timedelta(hours=step_hours)
    return times


# ---------------------------------------------------------------------------
# 2.1 / 2.1b governing invariant fixture
# ---------------------------------------------------------------------------

MAIN_END = _day("2026-07-20T12:00:00")
MAIN_WINDOW = MAIN_END - timedelta(days=RETENTION_DAYS)  # 2026-06-29T12:00Z
CLONE_SOURCE_TIME = _day("2026-06-10T00:00:00")
LATE_CLONE_SOURCE_TIME = _day("2026-06-12T12:00:00")
DORMANT_END = _day("2026-05-31T12:00:00")


def _main_generation(valid_time: datetime) -> str | None:
    if valid_time < _day("2026-05-20T00:00:00"):
        return GEN_A
    if valid_time < _day("2026-06-05T00:00:00"):
        return GEN_B
    if valid_time < _day("2026-06-25T00:00:00"):
        return GEN_A  # rollback to A
    if valid_time < _day("2026-06-27T00:00:00"):
        return None  # checksum-less generation ""
    return GEN_C


def _invariant_entries(lanes: Lanes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, valid_time in enumerate(_series(_day("2026-05-01T00:00:00"), MAIN_END, 12)):
        # Unusable runs: one old isolated row, and a run straddling W_g.
        usable = not (
            valid_time == _day("2026-05-10T12:00:00")
            or _day("2026-06-28T00:00:00") <= valid_time <= _day("2026-07-01T00:00:00")
        )
        source = "GFS" if index % 7 == 3 else "gfs"  # mixed spelling, one normalized group
        entries.append(
            lanes.entry(
                "m_main",
                source,
                valid_time,
                generation_checksum=_main_generation(valid_time),
                usable=usable,
                shape=SHAPES[index % len(SHAPES)],
            )
        )
    by_time = {entry["valid_time"]: entry["state_id"] for entry in entries}
    for valid_time in _series(_day("2026-06-10T00:00:00"), MAIN_END, 12):
        clone_kwargs: dict[str, Any] = {}
        if valid_time == _day("2026-06-10T00:00:00"):
            clone_kwargs = {"cloned_from_model_id": "m_main", "cloned_from_state_id": by_time[_iso(CLONE_SOURCE_TIME)]}
        if valid_time == _day("2026-07-15T00:00:00"):
            # Later backdated re-activation row pointing at an old, otherwise prunable source.
            clone_kwargs = {
                "cloned_from_model_id": "m_main",
                "cloned_from_state_id": by_time[_iso(LATE_CLONE_SOURCE_TIME)],
            }
        entries.append(lanes.entry("m_clone", "gfs", valid_time, generation_checksum=GEN_C, **clone_kwargs))
    for valid_time in _series(_day("2026-05-01T00:00:00"), DORMANT_END, 12):
        entries.append(lanes.entry("m_dormant", "IFS", valid_time, generation_checksum=GEN_D))
    return entries


GROUPS = (
    ("m_main", "gfs", MAIN_WINDOW),
    ("m_clone", "gfs", MAIN_WINDOW),
    ("m_dormant", "IFS", DORMANT_END - timedelta(days=RETENTION_DAYS)),
)


def _cutoffs(entries: list[dict[str, Any]], model_id: str) -> list[datetime]:
    times = sorted(
        {datetime.fromisoformat(e["valid_time"].replace("Z", "+00:00")) for e in entries if e["model_id"] == model_id}
    )
    return sorted({t + delta for t in times for delta in (timedelta(seconds=-1), timedelta(0), timedelta(seconds=1))})


def _without(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in keys}


def _generation_signal(
    repository: FileStateSnapshotIndexRepository, model_id: str, source_id: str, cutoff: datetime, checksum: str | None
) -> dict[str, Any]:
    return repository.generation_scoped_history_signal(
        model_id=model_id,
        source_id=source_id,
        before_time=cutoff,
        current_package_checksum=checksum,
        expected_predecessor_cycle_id=cycle_id_for(source_id, cutoff - timedelta(hours=LEAD)),
        expected_predecessor_lead_hours=LEAD,
    )


def _history_signal(signal: dict[str, Any]) -> generation._HistorySignal:
    # Constructed exactly as scheduler_generation_gate.evaluate_transition_decision does.
    return generation._HistorySignal(
        exists_current_generation=bool(signal.get("history_exists_current_generation")),
        exists_any_generation=bool(signal.get("history_exists_any_generation")),
        latest_current_generation_checkpoint=signal.get("latest_current_generation_checkpoint"),
        latest_any_generation_checkpoint=signal.get("latest_any_generation_checkpoint"),
        wrong_generation_predecessor_present=bool(signal.get("wrong_generation_predecessor_present")),
        wrong_generation_predecessor_checksum=str(signal.get("wrong_generation_predecessor_checksum") or ""),
    )


def _cutover_declaration(model_id: str, cutoff: datetime, *, old_checksum: str) -> dict[str, Any]:
    return {
        "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
        "generation": generation.derive_generation(GEN_E),
        "entries": [
            {
                "model_id": model_id,
                "old_checksum": old_checksum,
                "new_checksum": GEN_E,
                "effective_cycle_utc": cutoff,
                "transition_mode": "replace",
            }
        ],
    }


def _decisions(
    signals: dict[str | None, dict[str, Any]], model_id: str, source_id: str, cutoff: datetime
) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for checksum in CHECKSUMS:
        signal = _history_signal(signals[checksum])
        declarations: dict[str, dict[str, Any] | None] = {"none": None}
        if checksum == GEN_E:
            for old in (GEN_A, GEN_B, GEN_C, GEN_D):
                declarations[f"cutover_old_{old[0]}"] = _cutover_declaration(model_id, cutoff, old_checksum=old)
        for label, declaration in declarations.items():
            evaluation = generation.evaluate_transition_decision(
                model_id=model_id,
                package_checksum=checksum,
                source_id=source_id,
                candidate_cycle_time_utc=cutoff,
                required_lead_hours=LEAD,
                history=signal,
                declaration=declaration,
            )
            decisions[f"{checksum and checksum[0]}:{label}"] = evaluation.decision
    return decisions


def _answers(repository: FileStateSnapshotIndexRepository, entries: list[dict[str, Any]]) -> dict[Any, Any]:
    answers: dict[Any, Any] = {}
    for model_id, source_id, _window in GROUPS:
        lineage = repository.clone_lineage_signal(model_id=model_id, source_id=source_id)
        answers[(model_id, "lineage")] = _without(lineage, "state_snapshot_index")
        for cutoff in _cutoffs(entries, model_id):
            history = repository.usable_state_history_evidence(
                model_id=model_id, source_id=source_id, before_time=cutoff
            )
            answers[(model_id, cutoff, "history")] = history
            signals: dict[str | None, dict[str, Any]] = {}
            for checksum in CHECKSUMS:
                signals[checksum] = _generation_signal(repository, model_id, source_id, cutoff, checksum)
                answers[(model_id, cutoff, "signal", checksum)] = signals[checksum]
                answers[(model_id, cutoff, "strict", checksum)] = repository.strict_warm_start_evidence(
                    model_id=model_id,
                    source_id=source_id,
                    valid_time=cutoff,
                    model_package_checksum=checksum,
                    required_lead_hours=LEAD,
                )
            answers[(model_id, cutoff, "decisions")] = _decisions(signals, model_id, source_id, cutoff)
    return answers


@pytest.fixture(name="invariant_run", scope="module")
def invariant_run_factory(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[list[dict[str, Any]], dict[Any, Any], dict[Any, Any], dict[str, Any], Lanes]:
    lanes = Lanes(tmp_path_factory.mktemp("invariant"))
    entries = _invariant_entries(lanes)
    lanes.publish(entries)
    repository = lanes.repository()
    before = _answers(repository, entries)
    summary = lanes.prune(enforce=True)
    repository.refresh()
    after = _answers(repository, entries)
    return entries, before, after, summary, lanes


def test_governing_invariant_all_cutoffs_and_in_window_payloads(invariant_run: Any) -> None:
    entries, before, after, summary, _lanes = invariant_run
    assert summary["lanes"]["reference"]["action"] == "prune-retention"
    assert summary["lanes"]["reference"]["retention"]["removed_count"] > 50  # the fixture really prunes
    for model_id, _source_id, window_start in GROUPS:
        assert after[(model_id, "lineage")] == before[(model_id, "lineage")]
        for cutoff in _cutoffs(entries, model_id):
            history_before = before[(model_id, cutoff, "history")]
            history_after = after[(model_id, cutoff, "history")]
            assert history_after["history_exists"] == history_before["history_exists"], (model_id, cutoff)
            for checksum in CHECKSUMS:
                signal_before = before[(model_id, cutoff, "signal", checksum)]
                signal_after = after[(model_id, cutoff, "signal", checksum)]
                for field in ("history_exists_any_generation", "history_exists_current_generation"):
                    assert signal_after[field] == signal_before[field], (model_id, cutoff, checksum, field)
                latest_before = signal_before["latest_any_generation_checkpoint"] or {}
                latest_after = signal_after["latest_any_generation_checkpoint"] or {}
                assert latest_after.get("model_package_checksum") == latest_before.get("model_package_checksum"), (
                    model_id,
                    cutoff,
                )
            if cutoff < window_start:
                continue
            evidence_only = ("history_entry_count", "state_snapshot_index")
            assert _without(history_after, *evidence_only) == _without(history_before, *evidence_only), (
                model_id,
                cutoff,
            )
            for checksum in CHECKSUMS:
                counts = ("history_entry_count_any", "history_entry_count_current", "state_snapshot_index")
                assert _without(after[(model_id, cutoff, "signal", checksum)], *counts) == _without(
                    before[(model_id, cutoff, "signal", checksum)], *counts
                ), (model_id, cutoff, checksum)
                assert _without(after[(model_id, cutoff, "strict", checksum)], "state_snapshot_index") == _without(
                    before[(model_id, cutoff, "strict", checksum)], "state_snapshot_index"
                ), (model_id, cutoff, checksum)


def test_transition_decisions_move_only_admit_to_block_and_only_before_the_window(invariant_run: Any) -> None:
    entries, before, after, _summary, _lanes = invariant_run
    warm = {TransitionDecision.WARM_CONTINUE}
    cold = TransitionDecision.ADMIT - warm
    seen_before: set[str] = set()
    accepted_changes: list[tuple[str, datetime, str, str, str]] = []
    for model_id, _source_id, window_start in GROUPS:
        for cutoff in _cutoffs(entries, model_id):
            decisions_before = before[(model_id, cutoff, "decisions")]
            decisions_after = after[(model_id, cutoff, "decisions")]
            assert decisions_after.keys() == decisions_before.keys()
            for label, old in decisions_before.items():
                new = decisions_after[label]
                seen_before.add(old)
                if cutoff >= window_start:
                    assert new == old, (model_id, cutoff, label)
                    continue
                if new == old:
                    continue
                # Out of window a prune may only ever turn an answer into a block:
                # admit -> block, or block -> another block (a pruned wrong-generation
                # entry at the expected key reads as predecessor pending).
                assert new in TransitionDecision.BLOCK, (model_id, cutoff, label, old, new)
                assert not (old in warm and new in cold) and not (old in cold and new in warm)
                accepted_changes.append((model_id, cutoff, label, old, new))
    # The fixture exercises the decision families the invariant names.
    assert {
        TransitionDecision.WARM_CONTINUE,
        TransitionDecision.COLD_DECLARED_CUTOVER,
        TransitionDecision.BLOCK_DECLARATION_STALE,
        TransitionDecision.BLOCK_DECLARATION_MISSING,
        TransitionDecision.BLOCK_WRONG_GENERATION,
        TransitionDecision.BLOCK_PREDECESSOR_PENDING,
        TransitionDecision.COLD_NEW_MODEL,
    } <= seen_before
    # And the accepted out-of-window move is really reachable: warm -> predecessor pending.
    assert any(
        old == TransitionDecision.WARM_CONTINUE and new == TransitionDecision.BLOCK_PREDECESSOR_PENDING
        for *_ignored, old, new in accepted_changes
    )


def test_invariant_fixture_second_prune_removes_nothing(invariant_run: Any) -> None:
    *_ignored, lanes = invariant_run
    _assert_second_prune_removes_nothing(lanes)


def test_pruned_old_exact_checkpoint_fails_closed_and_history_stays(invariant_run: Any) -> None:
    _entries, before, after, _summary, _lanes = invariant_run
    cutoff = _day("2026-06-01T00:00:00")  # generation B, mid-run: not an anchor
    strict_before = before[("m_main", cutoff, "strict", GEN_B)]
    strict_after = after[("m_main", cutoff, "strict", GEN_B)]
    assert strict_before["ready"] is True
    assert strict_after["ready"] is False
    assert strict_after["reason"] == "state_snapshot_index_exact_checkpoint_missing"
    assert after[("m_main", cutoff, "history")]["history_exists"] is True
    assert before[("m_main", cutoff, "decisions")][f"{GEN_B[0]}:none"] == TransitionDecision.WARM_CONTINUE
    assert after[("m_main", cutoff, "decisions")][f"{GEN_B[0]}:none"] == TransitionDecision.BLOCK_PREDECESSOR_PENDING


# ---------------------------------------------------------------------------
# 2.2 boundary, anchors, raw shapes
# ---------------------------------------------------------------------------

K_END = _day("2026-07-20T00:00:00")
K_WINDOW = K_END - timedelta(days=RETENTION_DAYS)
K_BASE = _day("2026-05-01T00:00:00")


def _k(day: float) -> datetime:
    return K_BASE + timedelta(days=day)


def _anchor_fixture(lanes: Lanes) -> tuple[list[dict[str, Any]], dict[str, str]]:
    source_entries = [
        lanes.entry("m_src", "gfs", _k(day), generation_checksum=GEN_A, shape=16) for day in range(0, 5)
    ] + [lanes.entry("m_src", "gfs", K_END - timedelta(days=day), generation_checksum=GEN_A) for day in range(0, 3)]
    k5_target = source_entries[2]
    spec: list[tuple[datetime, dict[str, Any]]] = [
        (_k(0), {"generation_checksum": GEN_A}),  # K2 earliest A + K3
        (
            _k(1),
            {
                "generation_checksum": GEN_A,
                "cloned_from_model_id": "m_src",
                "cloned_from_state_id": k5_target["state_id"],
            },
        ),  # K4
        (_k(2), {"generation_checksum": GEN_A}),  # removed
        (_k(3), {"generation_checksum": GEN_B}),  # K2 (B earliest/latest/latest-before) + K3
        (_k(4), {"generation_checksum": GEN_A}),  # K3 run start after B
        (_k(5), {"generation_checksum": GEN_A}),  # removed
        (_k(6), {"generation_checksum": GEN_A, "usable": False}),  # unusable non-anchor: removed
        # Not a run start: K3 runs are over the usable subsequence, so the unusable
        # d6 is skipped and A simply continues from d5 -> removed.
        (_k(7), {"generation_checksum": GEN_A}),
        (_k(8), {"generation_checksum": GEN_A}),  # K2 latest A / latest A before window
        (_k(9), {"generation_checksum": None}),  # K2 earliest "" + K3
        (_k(10), {"generation_checksum": None}),  # removed
        (_k(11), {"generation_checksum": None}),  # K2 latest ""
        (_k(12), {"generation_checksum": GEN_C}),  # K2 earliest C + K3
        (_k(13), {"generation_checksum": GEN_C}),  # removed
        (_k(14), {"generation_checksum": GEN_C}),  # K2 latest C before window
        (K_WINDOW - timedelta(seconds=1), {"generation_checksum": GEN_C, "usable": False}),  # 1 s early: removed
        (K_WINDOW, {"generation_checksum": GEN_C, "usable": False}),  # exactly at W_g: kept
        (K_WINDOW + timedelta(days=1), {"generation_checksum": GEN_C}),
        (K_END, {"generation_checksum": GEN_C}),
    ]
    group = [
        lanes.entry("m_k", "gfs", valid_time, shape=SHAPES[index % len(SHAPES)], **kwargs)
        for index, (valid_time, kwargs) in enumerate(spec)
    ]
    ids = {
        "k_d2": group[2]["state_id"],
        "k_d5": group[5]["state_id"],
        "k_d6": group[6]["state_id"],
        "k_d7": group[7]["state_id"],
        "k_d10": group[10]["state_id"],
        "k_d13": group[13]["state_id"],
        "k_early": group[15]["state_id"],
        "src_d1": source_entries[1]["state_id"],
        "src_d3": source_entries[3]["state_id"],
    }
    # Interleave the two groups so original order is not group order.
    mixed: list[dict[str, Any]] = []
    for index in range(max(len(group), len(source_entries))):
        if index < len(group):
            mixed.append(group[index])
        if index < len(source_entries):
            mixed.append(source_entries[index])
    return mixed, ids


def test_boundary_and_each_anchor_keep_exactly_the_contracted_entries(lanes: Lanes) -> None:
    entries, ids = _anchor_fixture(lanes)
    lanes.publish(entries)

    preview = lanes.prune()

    expected_removed = sorted(
        [
            ids["k_d2"],
            ids["k_d5"],
            ids["k_d6"],
            ids["k_d7"],
            ids["k_d10"],
            ids["k_d13"],
            ids["k_early"],
            ids["src_d1"],
            ids["src_d3"],
        ]
    )
    for lane in ("reference", "destination"):
        block = preview["lanes"][lane]["retention"]
        assert block["removed_state_ids"] == expected_removed
        assert block["removed_count"] == len(expected_removed)
        assert block["removed_state_ids_sha256"] == "sha256:" + sha256_bytes(
            json.dumps(expected_removed, separators=(",", ":")).encode("utf-8")
        )
        assert block["kept_out_of_window_by_anchor"]["clone_provenance"] == 1
        assert block["kept_out_of_window_by_anchor"]["clone_source"] == 1
        groups = {group["model_id"]: group for group in block["groups"]}
        assert groups["m_k"]["removed_count"] == 7
        assert groups["m_k"]["removed_valid_time_min"] == _iso(_k(2))
        assert groups["m_k"]["removed_valid_time_max"] == _iso(K_WINDOW - timedelta(seconds=1))
        assert groups["m_src"]["removed_count"] == 2

    lanes.prune(enforce=True)

    for lane in ("reference", "destination"):
        after = lanes.entries(lane)
        assert after == [_stripped(entry) for entry in entries if entry["state_id"] not in expected_removed]
    assert {len(entry) for entry in entries} == set(SHAPES)
    _assert_second_prune_removes_nothing(lanes)


def _assert_second_prune_removes_nothing(lanes: Lanes) -> None:
    """The planner is a fixed point: re-running on its own output removes nothing."""

    reference_before = lanes.reference_index.read_bytes()
    destination_before = lanes.destination_index.read_bytes()
    preview = lanes.prune()
    for lane in ("reference", "destination"):
        lane_summary = preview["lanes"][lane]
        assert lane_summary["retention"]["removed_count"] == 0, lane_summary["retention"]["removed_state_ids"]
        assert lane_summary["action"] == "skip"
        assert lane_summary["untouched_reason"] == "nothing_to_prune"
    summary = lanes.prune(enforce=True)
    assert summary["status"] == "untouched"
    assert lanes.reference_index.read_bytes() == reference_before
    assert lanes.destination_index.read_bytes() == destination_before


def test_source_spelling_is_normalized_into_one_group(lanes: Lanes) -> None:
    entries = [
        lanes.entry("m_s", "GFS" if day % 2 else "gfs", _k(day), generation_checksum=GEN_A) for day in range(0, 81)
    ]
    lanes.publish(entries)

    block = lanes.prune()["lanes"]["reference"]["retention"]

    assert block["group_count"] == 1
    assert [(group["model_id"], group["source_id"]) for group in block["groups"]] == [("m_s", "gfs")]
    # Newest is day 80 (W = day 59): days 1..58 are one A run except earliest/latest-before-window.
    assert block["removed_count"] == 57


# ---------------------------------------------------------------------------
# 2.3 dry-run is mutation free; enforce archive-first, reference first, read-back
# ---------------------------------------------------------------------------


def _prunable(lanes: Lanes) -> list[dict[str, Any]]:
    return [lanes.entry("m_p", "gfs", _k(day), generation_checksum=GEN_A) for day in range(0, 81)]


def test_dry_run_changes_no_filesystem_bytes_and_lists_removed_ids(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries, destination=entries[10:])
    lanes.apply_env(monkeypatch)
    before = _fs_fingerprint(lanes.root)

    exit_code = repair.main(["prune-retention"])

    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert exit_code == 0
    assert _fs_fingerprint(lanes.root) == before
    assert preview["mode"] == "dry_run"
    reference = preview["lanes"]["reference"]["retention"]
    destination = preview["lanes"]["destination"]["retention"]
    # Each lane is planned from its own entries (the lanes are not mirrors).
    assert reference["removed_state_ids"] == sorted(entry["state_id"] for entry in entries[1:58])
    assert destination["removed_state_ids"] == sorted(entry["state_id"] for entry in entries[11:58])
    assert reference["entry_count_before"] == 81 and reference["entry_count_after"] == 24
    assert destination["entry_count_before"] == 71 and destination["entry_count_after"] == 24
    assert reference["retention_days"] == RETENTION_DAYS and reference["cycle_lag_hours"] == LAG
    assert reference["index_bytes_after"] < reference["index_bytes_before"]
    assert reference["json_nodes_after"] < reference["json_nodes_before"]


def test_enforce_archives_both_preimages_before_first_cas_and_prunes_reference_first(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries, destination=entries[10:])
    reference_before = lanes.reference_index.read_bytes()
    destination_before = lanes.destination_index.read_bytes()
    real_publish = state_manager_module.publish_state_snapshot_index
    order: list[str] = []

    def publish_after_archives(entries_arg: Any, destination_uri: Any, **kwargs: Any) -> Any:
        names = sorted(path.name for path in lanes.archive_root.glob("*.json"))
        assert any(name.endswith("-reference.json") for name in names)
        assert any(name.endswith("-destination.json") for name in names)
        if not order:
            assert lanes.reference_index.read_bytes() == reference_before
            assert lanes.destination_index.read_bytes() == destination_before
        order.append("reference" if Path(destination_uri) == lanes.reference_index else "destination")
        return real_publish(entries_arg, destination_uri, **kwargs)

    monkeypatch.setattr(state_manager_module, "publish_state_snapshot_index", publish_after_archives)
    summary = lanes.prune(enforce=True)

    assert order == ["reference", "destination"]
    assert summary["status"] == "repaired"
    assert lanes.entries("reference") == [_stripped(entry) for entry in entries[:1] + entries[58:]]
    assert lanes.entries("destination") == [_stripped(entry) for entry in entries[10:11] + entries[58:]]
    archives = {path.name.split("-")[-1]: path for path in lanes.archive_root.glob("*.json")}
    assert archives["reference.json"].read_bytes() == reference_before
    assert archives["destination.json"].read_bytes() == destination_before
    for lane in ("reference", "destination"):
        lane_summary = summary["lanes"][lane]
        assert lane_summary["committed"] is True
        assert "removed_state_ids" not in lane_summary["retention"]
        index = lanes.reference_index if lane == "reference" else lanes.destination_index
        assert lane_summary["retention"]["index_bytes_after"] == len(index.read_bytes())


# ---------------------------------------------------------------------------
# 2.4 partial, refusals, nothing to prune
# ---------------------------------------------------------------------------


def test_destination_cas_failure_after_reference_commit_is_partial_exit_3(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries)
    lanes.apply_env(monkeypatch)
    destination_before = lanes.destination_index.read_bytes()
    real_replace = state_manager_module.atomic_replace_provider_bytes

    def fail_destination_cas(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path) == lanes.destination_index:
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(state_manager_module, "atomic_replace_provider_bytes", fail_destination_cas)
    exit_code = repair.main(["prune-retention", "--enforce"])

    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert exit_code == 3
    assert error["status"] == "repair_committed_incomplete"
    assert summary["lanes"]["reference"]["committed"] is True
    assert lanes.entries("reference") == [_stripped(entry) for entry in entries[:1] + entries[58:]]
    assert lanes.destination_index.read_bytes() == destination_before

    # Recovery (runbook 8.12): a plain re-run finishes the destination and finds
    # nothing left to prune in the already-pruned reference.
    monkeypatch.setattr(state_manager_module, "atomic_replace_provider_bytes", real_replace)
    assert repair.main(["prune-retention", "--enforce"]) == 0
    receipt = json.loads((lanes.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["lanes"]["reference"]["untouched_reason"] == "nothing_to_prune"
    assert receipt["lanes"]["destination"]["action"] == "prune-retention"
    assert lanes.entries("destination") == lanes.entries("reference")


@pytest.mark.parametrize(
    ("argv", "env_lag", "reason"),
    [
        (["--retention-days", "16", "--cycle-lag-hours", "16"], None, "repair_retention_window_too_short"),
        # Exact equality is refused: 17 * 24 == 336 + 24 + 48.
        (["--retention-days", "17", "--cycle-lag-hours", "24"], None, "repair_retention_window_too_short"),
        ([], "", "repair_cycle_lag_unset"),
        ([], "sixteen", "repair_cycle_lag_unset"),
        (["--state-id", "x"], None, "repair_selector_not_applicable"),
        (["--lane", "reference"], None, "repair_lane_not_applicable"),
        (["--allow-missing-destination"], None, "repair_missing_lane_flag_invalid"),
    ],
)
def test_refusals_are_exit_2_with_zero_writes(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env_lag: str | None,
    reason: str,
) -> None:
    lanes.publish(_prunable(lanes))
    lanes.apply_env(monkeypatch)
    if env_lag is not None:
        monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", env_lag)
    before = _fs_fingerprint(lanes.root)
    real_lock = state_manager_module.provider_destination_lock

    def no_lock(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("refusal must precede any lock")

    monkeypatch.setattr(state_manager_module, "provider_destination_lock", no_lock)
    exit_code = repair.main(["prune-retention", *argv, "--enforce"])
    monkeypatch.setattr(state_manager_module, "provider_destination_lock", real_lock)

    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["status"] == "refused"
    assert error["reason"] == reason
    assert _fs_fingerprint(lanes.root) == before


def test_window_floor_is_max_lookback_plus_lag_plus_48(lanes: Lanes) -> None:
    lanes.publish(_prunable(lanes))
    # 17 days = 408 h > 336 + 23 + 48 = 407 h: accepted.
    assert lanes.prune(retention_days=17, cycle_lag_hours=23)["status"] == "preview"
    with pytest.raises(StateManagerError) as error_info:
        lanes.prune(retention_days=17, cycle_lag_hours=24)
    assert getattr(error_info.value, "reason", "") == "repair_retention_window_too_short"
    assert error_info.value.evidence["floor_hours"] == 408
    with pytest.raises(StateManagerError) as error_info:
        lanes.prune(cycle_lag_hours=None)
    assert getattr(error_info.value, "reason", "") == "repair_cycle_lag_unset"


def test_nothing_to_prune_leaves_both_lanes_untouched(lanes: Lanes) -> None:
    entries = [
        lanes.entry("m_n", "gfs", K_END - timedelta(days=day), generation_checksum=GEN_A) for day in range(0, 20)
    ]
    lanes.publish(entries)
    reference_before = lanes.reference_index.read_bytes()
    destination_before = lanes.destination_index.read_bytes()

    summary = lanes.prune(enforce=True)

    assert summary["status"] == "untouched"
    for lane in ("reference", "destination"):
        assert summary["lanes"][lane]["action"] == "skip"
        assert summary["lanes"][lane]["untouched_reason"] == "nothing_to_prune"
        assert summary["lanes"][lane]["committed"] is False
        assert summary["lanes"][lane]["retention"]["removed_count"] == 0
    assert lanes.reference_index.read_bytes() == reference_before
    assert lanes.destination_index.read_bytes() == destination_before
    assert list(lanes.archive_root.iterdir()) == []


# ---------------------------------------------------------------------------
# 2.5 concurrency: an upsert between dry-run and enforce survives
# ---------------------------------------------------------------------------


def test_upsert_between_dry_run_and_enforce_is_not_lost(lanes: Lanes) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries)
    preview = lanes.prune()
    new_time = K_END + timedelta(hours=12)
    snapshot = StateSnapshot(
        state_id="state_gfs_m_p_new",
        model_id="m_p",
        run_id="fcst_gfs_new_m_p",
        valid_time=new_time,
        state_uri=lanes.state_uri,
        checksum=lanes.checksum,
        usable_flag=True,
        source_id="gfs",
        cycle_id=cycle_id_for("gfs", new_time - timedelta(hours=LEAD)),
        lead_hours=LEAD,
        model_package_checksum=GEN_A,
    )
    lanes.repository().upsert_state_snapshot(snapshot)

    summary = lanes.prune(enforce=True)

    after_ids = [entry["state_id"] for entry in lanes.entries("reference")]
    assert "state_gfs_m_p_new" in after_ids
    # The newer entry moved the group window 12 h later: enforce re-planned under the lock.
    assert summary["lanes"]["reference"]["retention"]["removed_count"] == (
        preview["lanes"]["reference"]["retention"]["removed_count"] + 1
    )


# ---------------------------------------------------------------------------
# 2.6 receipt bound; 2.7 CLI round trip
# ---------------------------------------------------------------------------


def test_enforce_receipt_for_over_5000_removals_stays_under_limit(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        lanes.entry(model_id, "gfs", _k(0) + timedelta(hours=hour), generation_checksum=GEN_A)
        for model_id in ("m_big1", "m_big2")
        for hour in range(0, 3100)
    ]
    lanes.publish(entries, destination=entries[:10])
    lanes.apply_env(monkeypatch)

    assert repair.main(["prune-retention"]) == 0
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    removed = preview["lanes"]["reference"]["retention"]["removed_state_ids"]
    assert len(removed) >= 5000

    assert repair.main(["prune-retention", "--enforce"]) == 0
    receipt_bytes = (lanes.receipt_root / "latest.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    assert len(receipt_bytes) < repair.MAX_RECEIPT_BYTES
    assert receipt["lanes"]["reference"]["retention"]["removed_count"] == len(removed)
    assert "removed_state_ids" not in receipt["lanes"]["reference"]["retention"]
    assert receipt["lanes"]["reference"]["retention"]["removed_state_ids_sha256"] == "sha256:" + sha256_bytes(
        json.dumps(sorted(removed), separators=(",", ":")).encode("utf-8")
    )
    assert receipt["lanes"]["destination"]["untouched_reason"] == "nothing_to_prune"


def test_enforce_receipt_with_group_summaries_over_budget_in_both_lanes_is_written(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Long model ids and more groups-with-removals per lane than the summary byte
    # budget holds, in BOTH lanes: the receipt must still be written under the cap.
    group_count = 1500
    entries = [
        lanes.entry(f"model_{index:04d}_{'x' * 90}", "gfs", _k(day), generation_checksum=GEN_A, shape=14)
        for index in range(group_count)
        for day in (0, 1, 2, 30)
    ]
    lanes.publish(entries)
    lanes.apply_env(monkeypatch)

    exit_code = repair.main(["prune-retention", "--enforce"])

    receipt_path = lanes.receipt_root / "latest.json"
    assert exit_code == 0
    assert receipt_path.exists()
    assert len(receipt_path.read_bytes()) < repair.MAX_RECEIPT_BYTES
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for lane in ("reference", "destination"):
        block = receipt["lanes"][lane]["retention"]
        assert block["groups_with_removals"] == group_count
        assert block["group_summaries_truncated"] > 0
        assert len(block["groups"]) + block["group_summaries_truncated"] == group_count
        assert block["removed_count"] == group_count  # day 1 of each group; day 2 is latest-before-window


def test_cli_round_trip_with_retention_days(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries)
    lanes.apply_env(monkeypatch)
    monkeypatch.delenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS")

    assert repair.main(["prune-retention", "--retention-days", "30", "--cycle-lag-hours", "16"]) == 0
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # Newest day 80, W = day 50: day 49 is the latest usable before W (K2); days 1..48 go.
    expected = sorted(entry["state_id"] for entry in entries[1:49])
    assert preview["lanes"]["reference"]["retention"]["removed_state_ids"] == expected
    assert preview["lanes"]["reference"]["retention"]["retention_days"] == 30

    assert repair.main(["prune-retention", "--retention-days", "30", "--cycle-lag-hours", "16", "--enforce"]) == 0
    receipt = json.loads((lanes.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["operation"] == "prune-retention"
    assert receipt["status"] == "repaired"
    assert lanes.entries("reference") == [_stripped(entry) for entry in entries[:1] + entries[49:]]


def test_existing_operations_receipts_carry_no_retention_block(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries)
    lanes.apply_env(monkeypatch)

    assert repair.main(["remove-entry", "--state-id", entries[5]["state_id"]]) == 0
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "retention" not in preview["lanes"]["reference"]
    assert preview["lanes"]["reference"]["action"] == "remove-entry"


# ---------------------------------------------------------------------------
# 3.1 capacity evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", ["entries", "nodes", "bytes"])
@pytest.mark.parametrize(("fraction", "warning"), [(0.60, False), (0.70, True), (0.95, True)])
def test_capacity_warning_threshold_never_fails_reads_or_publishes(
    lanes: Lanes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    limit: str,
    fraction: float,
    warning: bool,
) -> None:
    entries = [
        lanes.entry("m_c", "gfs", K_END - timedelta(days=day), generation_checksum=GEN_A) for day in range(0, 20)
    ]
    first = publish_state_snapshot_index(
        entries,
        lanes.reference_index,
        object_store_root=lanes.reference_root,
        object_store_prefix=PREFIX,
        generated_at=NOW,
        verify_objects=False,
    )
    measured = first["capacity"]
    assert measured["entry_count"] == 20
    assert measured["index_bytes"] == len(lanes.reference_index.read_bytes())
    # Independent node count: every JSON value (objects, arrays, scalars), keys excluded.
    payload = json.loads(lanes.reference_index.read_text(encoding="utf-8"))

    def count(value: Any) -> int:
        if isinstance(value, dict):
            return 1 + sum(count(item) for item in value.values())
        if isinstance(value, list):
            return 1 + sum(count(item) for item in value)
        return 1

    assert measured["json_nodes"] == count(payload)
    used = {"entries": 20, "nodes": measured["json_nodes"], "bytes": measured["index_bytes"]}[limit]
    patched = int(used / fraction) if fraction != 0.70 else int(round(used / 0.70))
    if fraction == 0.70:
        # Land exactly on or just above the threshold.
        while used / patched < 0.70:
            patched -= 1
    constant = {
        "entries": "MAX_STATE_SNAPSHOT_INDEX_ENTRIES",
        "nodes": "MAX_STATE_SNAPSHOT_INDEX_JSON_NODES",
        "bytes": "MAX_STATE_SNAPSHOT_INDEX_BYTES",
    }[limit]
    monkeypatch.setattr(state_manager_module, constant, patched)

    with caplog.at_level(logging.WARNING, logger=state_manager_module.__name__):
        published = publish_state_snapshot_index(
            entries,
            lanes.reference_index,
            object_store_root=lanes.reference_root,
            object_store_prefix=PREFIX,
            generated_at=NOW,
            verify_objects=False,
        )
        publish_warnings = len(caplog.records)
        caplog.clear()
        repository = lanes.repository()
        evidence = repository.state_index_evidence()
        repository.usable_state_history_evidence(model_id="m_c", source_id="gfs", before_time=NOW)
        repository.clone_lineage_signal(model_id="m_c", source_id="gfs")
        read_warnings = len(caplog.records)

    for capacity in (published["capacity"], evidence["capacity"]):
        assert capacity["warning"] is warning
        assert capacity["warning_threshold"] == 0.70
        assert capacity["utilization_ratio"] >= (0.70 if warning else 0.0)
        assert capacity["utilization_ratio"] < 0.70 or warning
    assert evidence["status"] == "ready"
    assert publish_warnings == (1 if warning else 0)
    # One warning per load: the cached snapshot serves the later reads.
    assert read_warnings == (1 if warning else 0)


def test_reader_answers_do_not_embed_capacity_but_state_index_evidence_does(lanes: Lanes) -> None:
    # Node-22's largest pass evidence is 4.5 MB of a 5 MB budget with 384 nested
    # state_snapshot_index blocks: capacity must stay out of every reader answer.
    valid_time = K_END
    entries = [
        lanes.entry("m_r", "gfs", valid_time - timedelta(days=day), generation_checksum=GEN_A) for day in range(3)
    ]
    lanes.publish(entries)
    repository = lanes.repository()

    answers = [
        repository.usable_state_history_evidence(model_id="m_r", source_id="gfs", before_time=NOW),
        repository.strict_warm_start_evidence(
            model_id="m_r",
            source_id="gfs",
            valid_time=valid_time,
            model_package_checksum=GEN_A,
            required_lead_hours=LEAD,
        ),
        _generation_signal(repository, "m_r", "gfs", valid_time, GEN_A),
        repository.clone_lineage_signal(model_id="m_r", source_id="gfs"),
    ]
    assert answers[1]["ready"] is True
    for answer in answers:
        assert answer["state_snapshot_index"]["entry_count"] == 3
        assert "capacity" not in answer["state_snapshot_index"]
        assert "capacity" not in answer

    evidence = repository.state_index_evidence()
    assert evidence["capacity"]["entry_count"] == 3
    assert evidence["capacity"]["index_bytes"] == len(lanes.reference_index.read_bytes())


def test_renewal_evidence_carries_capacity(lanes: Lanes) -> None:
    entries = [lanes.entry("m_w", "gfs", K_END - timedelta(days=day), generation_checksum=GEN_A) for day in range(4)]
    lanes.publish(entries)

    renewal_entries, evidence, _preimage = lanes.repository().validated_entries_for_renewal()

    assert len(renewal_entries) == 4
    capacity = evidence["capacity"]
    assert capacity["entry_count"] == 4
    assert capacity["index_bytes"] == len(lanes.reference_index.read_bytes())
    assert capacity["max_entries"] == state_manager_module.MAX_STATE_SNAPSHOT_INDEX_ENTRIES
    assert capacity["warning"] is False
