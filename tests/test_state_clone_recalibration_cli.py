"""Recalibration state carry-over: the dual-index CLI end-to-end.

Covers the change's evidence mapping rows 9 and 10 (§6.7-6.9) — ``--apply`` and
dry-run through ``dispatch``, the copyback replay of the two written entries,
``--pairs`` resolution, and the parser's per-mode required flags (§4.1).

The eight-surface gate half lives in
:file:`tests/test_state_clone_recalibration.py`; the fixtures, fakes and the
independent fingerprint oracle both halves share live in
:file:`tests/state_clone_recalibration_fixtures.py`. The CLI environment
helpers live in :file:`tests/state_clone_recalibration_cli_fixtures.py` and are
shared with the validation module that owns every test after the
``§6.8 --pairs resolution`` marker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateManagerError,
    StateSnapshot,
    merge_state_snapshot_index_copyback,
    publish_state_snapshot_index,
)
from scripts.node22_clone_direct_grid_cutover_states import (
    CutoverCloneError,
    _state_compatibility_category_files,
    dispatch,
)
from tests.state_clone_recalibration_cli_fixtures import (
    _add_recalibration_pair,
    _build_cli_environment,
    _cli_args,
)
from tests.state_clone_recalibration_fixtures import (
    _IC_V1,
    _IC_V2,
    _LAKE_RELATIVE_PATH,
    M1_MODEL_ID,
    M1_PACKAGE_CHECKSUM,
    M1_PACKAGE_URI,
    M1P_MODEL_ID,
    M1P_PACKAGE_CHECKSUM,
    M1P_PACKAGE_URI,
    SOURCE_ID,
    _expected_state_compatibility_fingerprint,
)

# --- Dual-index CLI end-to-end (rows 9, 10; §6.7-6.9) ----------------------


def test_cli_apply_writes_both_indexes_with_byte_identical_entries(
    tmp_path: Path,
) -> None:
    """§6.7 + §6.9: one gate run, the same row into both indexes, one receipt."""

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    receipt = dispatch(_cli_args(env, "--apply", "--receipt", str(receipt_path)))

    assert receipt["schema_version"] == "nhms.recalibration_state_clone.v1"
    assert receipt["transfer_mode"] == "recalibration"
    assert receipt["dry_run"] is False
    assert receipt["declared_pair_count"] == 1
    assert receipt["cloned_pair_count"] == 1
    assert receipt["state_index"] == str(env["canonical_index"])
    assert receipt["mirror_state_index"] == str(env["mirror_index"])
    assert receipt["evidence_fingerprint_cross_check"] == "skipped_no_recorded_value"
    assert receipt["clone_gate_kind"] == "state_compatibility"
    assert "spin-up distortion" in receipt["spin_up_distortion_announcement"]

    pair_record = receipt["pairs"][0]
    assert pair_record["source_model_id"] == M1_MODEL_ID
    assert pair_record["target_model_id"] == M1P_MODEL_ID
    assert pair_record["source_id"] == SOURCE_ID
    assert pair_record["clone_gate_kind"] == "state_compatibility"
    assert pair_record["source_model_package_version"] == M1_PACKAGE_URI
    assert pair_record["source_model_package_checksum"] == M1_PACKAGE_CHECKSUM
    assert pair_record["target_model_package_version"] == M1P_PACKAGE_URI
    assert pair_record["target_model_package_checksum"] == M1P_PACKAGE_CHECKSUM
    assert pair_record["cloned_from_model_id"] == M1_MODEL_ID
    assert pair_record["state_index_outcomes"]["canonical"]["outcome"] == "written"
    assert pair_record["state_index_outcomes"]["mirror"]["outcome"] == "written"
    assert f"lake:{_LAKE_RELATIVE_PATH}" in pair_record["covered_paths"]

    category_files = _state_compatibility_category_files(
        env["source_root"], env["target_root"]
    )
    assert pair_record["state_compatibility_fingerprint"] == (
        _expected_state_compatibility_fingerprint(
            env["source_root"], category_files=category_files, ic_bytes=_IC_V1
        )
    )

    # The persisted receipt JSON is EXACTLY the returned payload (task 3.5:
    # successful apply persists the receipt equal to the returned mapping).
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt

    # Both indexes carry a byte-identical serialization of the SAME row.
    clone_entries = {}
    for label, index_path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        payload = json.loads(Path(index_path).read_text(encoding="utf-8"))
        matches = [
            item for item in payload["entries"] if item["model_id"] == M1P_MODEL_ID
        ]
        assert len(matches) == 1, label
        clone_entries[label] = matches[0]
    assert clone_entries["canonical"] == clone_entries["mirror"]
    assert clone_entries["canonical"]["clone_gate_kind"] == "state_compatibility"
    assert clone_entries["canonical"]["cloned_from_model_id"] == M1_MODEL_ID
    assert clone_entries["canonical"]["state_uri"] == env["state_uri"]
    assert clone_entries["canonical"]["model_package_checksum"] == M1P_PACKAGE_CHECKSUM


def test_dual_index_entries_replay_cleanly_through_the_copyback_merge(
    tmp_path: Path,
) -> None:
    """§6.7 / row 9: identical entries take the ``current == source_entry`` branch.

    The copyback merge raises ``state_snapshot_index_copyback_conflict`` when
    two DIFFERING entries share an equal ``created_at``. Writing the same
    ``StateSnapshot`` object into both indexes keeps the two serializations
    byte-identical, so a later copyback of the mirror into the canonical index
    is a no-conflict carry-through.
    """

    env = _build_cli_environment(tmp_path)
    dispatch(_cli_args(env, "--apply"))

    summary = merge_state_snapshot_index_copyback(
        source_path=Path(env["mirror_index"]),
        destination_path=Path(env["canonical_index"]),
        reference_object_store_root=env["object_root"],
        object_store_prefix="s3://nhms",
        source_containment_root=env["object_root"],
        destination_containment_root=env["object_root"],
    )

    assert summary["merged_entry_count"] == 2
    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    clone_entries = [item for item in payload["entries"] if item["model_id"] == M1P_MODEL_ID]
    assert len(clone_entries) == 1
    assert clone_entries[0]["clone_gate_kind"] == "state_compatibility"


def test_copyback_still_fails_closed_on_two_differing_entries(tmp_path: Path) -> None:
    """The equality branch is reached because the entries ARE equal, not always.

    Perturbing one index's clone entry restores the same-``created_at``
    collision the merge must refuse, proving the clean replay above is a
    property of the dual write and not of a merge that never conflicts.
    """

    env = _build_cli_environment(tmp_path)
    dispatch(_cli_args(env, "--apply"))

    payload = json.loads(Path(env["mirror_index"]).read_text(encoding="utf-8"))
    entries = []
    for entry in payload["entries"]:
        item = dict(entry)
        if item["model_id"] == M1P_MODEL_ID:
            item["run_id"] = "fcst_gfs_2026081500_perturbed"
        entries.append(item)
    publish_state_snapshot_index(
        entries,
        Path(env["mirror_index"]),
        object_store_root=env["object_root"],
        object_store_prefix="s3://nhms",
        verify_objects=False,
    )

    with pytest.raises(StateManagerError) as excinfo:
        merge_state_snapshot_index_copyback(
            source_path=Path(env["mirror_index"]),
            destination_path=Path(env["canonical_index"]),
            reference_object_store_root=env["object_root"],
            object_store_prefix="s3://nhms",
            source_containment_root=env["object_root"],
            destination_containment_root=env["object_root"],
        )
    assert "state_snapshot_index_copyback_conflict" in str(excinfo.value)


def test_cli_dry_run_runs_the_gate_and_writes_nothing(tmp_path: Path) -> None:
    """§4.7 / §6.9: dry run validates and gates, but writes no row anywhere."""

    env = _build_cli_environment(tmp_path)
    before = {
        label: Path(path).read_text(encoding="utf-8")
        for label, path in (
            ("canonical", env["canonical_index"]),
            ("mirror", env["mirror_index"]),
        )
    }
    receipt_path = tmp_path / "dry-run-receipt.json"

    receipt = dispatch(_cli_args(env, "--receipt", str(receipt_path)))

    assert receipt["dry_run"] is True
    assert receipt["cloned_pair_count"] == 0
    pair_record = receipt["pairs"][0]
    assert pair_record["state_id"] is None
    assert pair_record["clone_gate_kind"] == "state_compatibility"
    # The gate DID run: the receipt carries the accepted eight-surface value.
    category_files = _state_compatibility_category_files(
        env["source_root"], env["target_root"]
    )
    assert pair_record["state_compatibility_fingerprint"] == (
        _expected_state_compatibility_fingerprint(
            env["source_root"], category_files=category_files, ic_bytes=_IC_V1
        )
    )
    assert pair_record["state_index_outcomes"]["canonical"]["outcome"] == "dry_run_not_written"
    assert pair_record["state_index_outcomes"]["mirror"]["outcome"] == "dry_run_not_written"

    for label, path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        assert Path(path).read_text(encoding="utf-8") == before[label], label
    assert receipt_path.exists()


def test_cli_refuses_the_pair_when_the_target_ships_a_new_cfg_ic(tmp_path: Path) -> None:
    """End-to-end fail-closed: nothing written into either index, no receipt."""

    env = _build_cli_environment(tmp_path, target_ic=_IC_V2)
    before = {
        label: Path(path).read_text(encoding="utf-8")
        for label, path in (
            ("canonical", env["canonical_index"]),
            ("mirror", env["mirror_index"]),
        )
    }
    receipt_path = tmp_path / "refused-receipt.json"

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(_cli_args(env, "--apply", "--receipt", str(receipt_path)))

    assert "state_compatibility_unequal" in str(excinfo.value)
    assert not receipt_path.exists()
    for label, path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        assert Path(path).read_text(encoding="utf-8") == before[label], label


def test_mirror_write_failure_writes_the_receipt_and_exits_non_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.5 / D7: canonical written + mirror not written is reported, not swallowed."""

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "mirror-failure-receipt.json"
    original_upsert = FileStateSnapshotIndexRepository.upsert_state_snapshot

    def _failing_upsert(
        self: FileStateSnapshotIndexRepository, snapshot: StateSnapshot
    ) -> StateSnapshot:
        if str(self.index_uri) == str(env["mirror_index"]):
            raise OSError("scratch mirror is read-only")
        return original_upsert(self, snapshot)

    monkeypatch.setattr(
        FileStateSnapshotIndexRepository, "upsert_state_snapshot", _failing_upsert
    )

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(_cli_args(env, "--apply", "--receipt", str(receipt_path)))

    assert "repair the mirror before t*" in str(excinfo.value)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    outcomes = receipt["pairs"][0]["state_index_outcomes"]
    assert outcomes["canonical"]["outcome"] == "written"
    assert outcomes["mirror"]["outcome"] == "not_written"
    assert "scratch mirror is read-only" in outcomes["mirror"]["error"]

    # The canonical row really is there; the operator repairs the mirror only.
    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    assert any(item["model_id"] == M1P_MODEL_ID for item in payload["entries"])
    mirror_payload = json.loads(Path(env["mirror_index"]).read_text(encoding="utf-8"))
    assert not any(item["model_id"] == M1P_MODEL_ID for item in mirror_payload["entries"])


def test_receipt_is_written_when_a_later_pair_refuses_after_an_applied_pair(
    tmp_path: Path,
) -> None:
    """Round-1 C1: a mid-loop refusal must not unwind past the receipt write.

    Pair 1 is admitted and its clone row goes live in BOTH indexes; pair 2's
    target ships a drifted ``cfg.ic`` and is refused. Before the fix the
    ``raise`` walked straight out of ``run_recalibration``, leaving pair 1's
    rows persisted in both indexes with NO declaring artifact -- while the
    runbook told the operator a refusal writes nothing anywhere. The receipt is
    now written first (recording pair 1 as written, pair 2 as the failure) and
    only then does the refusal propagate.
    """

    env = _build_cli_environment(tmp_path)
    second = _add_recalibration_pair(
        env,
        source_model_id="huai_dg_gfs_v3",
        target_model_id="huai_dg_gfs_v4",
        target_ic=_IC_V2,
    )
    receipt_path = tmp_path / "partial-apply-receipt.json"

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(
            _cli_args(
                env,
                "--apply",
                "--receipt",
                str(receipt_path),
                "--pairs",
                f"{M1_MODEL_ID}:{M1P_MODEL_ID},"
                f"{second['source_model_id']}:{second['target_model_id']}",
            )
        )

    # The refusal still propagates -- the exit code stays non-zero.
    assert "state_compatibility_unequal" in str(excinfo.value)
    assert second["target_model_id"] in str(excinfo.value)

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "nhms.recalibration_state_clone.v1"
    assert receipt["invocation_outcome"] == "aborted"
    assert receipt["declared_pair_count"] == 2
    assert receipt["cloned_pair_count"] == 1

    # Pair 1 is recorded as written to BOTH indexes; only completed pairs are
    # listed under "pairs".
    assert [record["target_model_id"] for record in receipt["pairs"]] == [M1P_MODEL_ID]
    outcomes = receipt["pairs"][0]["state_index_outcomes"]
    assert outcomes["canonical"]["outcome"] == "written"
    assert outcomes["mirror"]["outcome"] == "written"

    # Pair 2 is named as the pair that did not complete, with its reason.
    failed_pair = receipt["failed_pair"]
    assert failed_pair["source_model_id"] == second["source_model_id"]
    assert failed_pair["target_model_id"] == second["target_model_id"]
    assert failed_pair["source_id"] == SOURCE_ID
    assert failed_pair["failure_kind"] == "pair_not_completed"
    assert "state_compatibility_unequal" in failed_pair["error"]

    # And the receipt tells the truth about the disk: pair 1's clone row really
    # is live in both indexes, pair 2's is in neither.
    for label, index_path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        entries = json.loads(Path(index_path).read_text(encoding="utf-8"))["entries"]
        model_ids = {item["model_id"] for item in entries}
        assert M1P_MODEL_ID in model_ids, label
        assert second["target_model_id"] not in model_ids, label


def test_clean_invocation_with_existing_receipt_path_still_raises(tmp_path: Path) -> None:
    """Row 3: a clean invocation with an existing receipt path fails with FileExistsError.

    The ``O_EXCL`` contract is preserved: no error is silently swallowed, the
    existing file is not overwritten, and the clone rows the invocation DID
    write remain live in the indexes (the operator re-runs with a fresh path).
    """

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "clean-existing-receipt.json"
    receipt_path.write_text('{"pre_existing": true}\n', encoding="utf-8")
    before = receipt_path.read_bytes()

    with pytest.raises(FileExistsError):
        dispatch(_cli_args(env, "--apply", "--receipt", str(receipt_path)))

    assert receipt_path.read_bytes() == before
    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    assert any(item["model_id"] == M1P_MODEL_ID for item in payload["entries"])


def test_pre_existing_receipt_cannot_mask_a_later_refusal(tmp_path: Path) -> None:
    """Row 2 (refusal leg): receipt path exists + pair 2 refuses after pair 1.

    The original refusal ``CutoverCloneError``/message propagates as the primary
    error; a note naming ``FileExistsError`` is attached; the pre-existing
    receipt file is byte-for-byte unchanged.
    """

    env = _build_cli_environment(tmp_path)
    second = _add_recalibration_pair(
        env,
        source_model_id="huai_dg_gfs_v3",
        target_model_id="huai_dg_gfs_v4",
        target_ic=_IC_V2,
    )
    receipt_path = tmp_path / "masked-refusal-receipt.json"
    receipt_path.write_text('{"pre_existing": true}\n', encoding="utf-8")
    before = receipt_path.read_bytes()

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(
            _cli_args(
                env,
                "--apply",
                "--receipt",
                str(receipt_path),
                "--pairs",
                f"{M1_MODEL_ID}:{M1P_MODEL_ID},"
                f"{second['source_model_id']}:{second['target_model_id']}",
            )
        )

    # The primary error is still the refusal, with its original message.
    assert "state_compatibility_unequal" in str(excinfo.value)
    notes = list(getattr(excinfo.value, "__notes__", []))
    assert any("FileExistsError" in note and "receipt persistence failed" in note for note in notes)
    assert receipt_path.read_bytes() == before


def test_pre_existing_receipt_cannot_mask_a_mirror_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 2 (mirror leg): receipt path exists + mirror OSError after canonical.

    The primary mirror ``CutoverCloneError`` propagates; notes name BOTH the
    mirror failure and the receipt ``FileExistsError``; the pre-existing receipt
    file is byte-for-byte unchanged.
    """

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "masked-mirror-receipt.json"
    receipt_path.write_text('{"pre_existing": true}\n', encoding="utf-8")
    before = receipt_path.read_bytes()
    original_upsert = FileStateSnapshotIndexRepository.upsert_state_snapshot

    def _failing_upsert(
        self: FileStateSnapshotIndexRepository, snapshot: StateSnapshot
    ) -> StateSnapshot:
        if str(self.index_uri) == str(env["mirror_index"]):
            raise OSError("scratch mirror is read-only")
        return original_upsert(self, snapshot)

    monkeypatch.setattr(
        FileStateSnapshotIndexRepository, "upsert_state_snapshot", _failing_upsert
    )

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(_cli_args(env, "--apply", "--receipt", str(receipt_path)))

    # The primary error is the mirror divergence, with its repair directive.
    assert "repair the mirror before t*" in str(excinfo.value)
    notes = list(getattr(excinfo.value, "__notes__", []))
    assert any("FileExistsError" in note and "receipt persistence failed" in note for note in notes)
    assert receipt_path.read_bytes() == before


def test_multi_pair_dry_run_refusal_still_produces_no_receipt(tmp_path: Path) -> None:
    """The receipt-on-abort guarantee is scoped to rows that actually exist.

    Same two-pair shape as above but WITHOUT ``--apply``: nothing was written to
    either index, so there is nothing to declare and the O_EXCL receipt path
    stays free for the corrected re-run. This pins that the new write-on-abort
    branch keys on "a pair was applied", not on "a pair was processed".
    """

    env = _build_cli_environment(tmp_path)
    second = _add_recalibration_pair(
        env,
        source_model_id="huai_dg_gfs_v3",
        target_model_id="huai_dg_gfs_v4",
        target_ic=_IC_V2,
    )
    receipt_path = tmp_path / "dry-run-refusal-receipt.json"

    with pytest.raises(CutoverCloneError, match="state_compatibility_unequal"):
        dispatch(
            _cli_args(
                env,
                "--receipt",
                str(receipt_path),
                "--pairs",
                f"{M1_MODEL_ID}:{M1P_MODEL_ID},"
                f"{second['source_model_id']}:{second['target_model_id']}",
            )
        )

    assert not receipt_path.exists()


def test_mirror_write_failure_marks_the_invocation_aborted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror-failure receipt must not read as a clean run either."""

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "mirror-failure-outcome-receipt.json"
    original_upsert = FileStateSnapshotIndexRepository.upsert_state_snapshot

    def _failing_upsert(
        self: FileStateSnapshotIndexRepository, snapshot: StateSnapshot
    ) -> StateSnapshot:
        if str(self.index_uri) == str(env["mirror_index"]):
            raise OSError("scratch mirror is read-only")
        return original_upsert(self, snapshot)

    monkeypatch.setattr(
        FileStateSnapshotIndexRepository, "upsert_state_snapshot", _failing_upsert
    )

    with pytest.raises(CutoverCloneError, match="repair the mirror before t\\*"):
        dispatch(_cli_args(env, "--apply", "--receipt", str(receipt_path)))

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["invocation_outcome"] == "aborted"
    assert receipt["failed_pair"]["failure_kind"] == "mirror_write_failed"
    assert receipt["failed_pair"]["target_model_id"] == M1P_MODEL_ID
    assert "scratch mirror is read-only" in receipt["failed_pair"]["error"]


def test_clean_multi_pair_apply_reports_a_complete_invocation(tmp_path: Path) -> None:
    """The control case: two admitted pairs -> ``complete`` and no failed pair."""

    env = _build_cli_environment(tmp_path)
    second = _add_recalibration_pair(
        env,
        source_model_id="huai_dg_gfs_v3",
        target_model_id="huai_dg_gfs_v4",
        target_ic=_IC_V1,
    )

    receipt = dispatch(
        _cli_args(
            env,
            "--apply",
            "--pairs",
            f"{M1_MODEL_ID}:{M1P_MODEL_ID},"
            f"{second['source_model_id']}:{second['target_model_id']}",
        )
    )

    assert receipt["invocation_outcome"] == "complete"
    assert receipt["failed_pair"] is None
    assert receipt["declared_pair_count"] == 2
    assert receipt["cloned_pair_count"] == 2
    for label, index_path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        entries = json.loads(Path(index_path).read_text(encoding="utf-8"))["entries"]
        model_ids = {item["model_id"] for item in entries}
        assert {M1P_MODEL_ID, second["target_model_id"]} <= model_ids, label
