"""Recalibration state carry-over: the dual-index CLI end-to-end.

Covers the change's evidence mapping rows 9 and 10 (§6.7-6.9) — ``--apply`` and
dry-run through ``dispatch``, the copyback replay of the two written entries,
``--pairs`` resolution, and the parser's per-mode required flags (§4.1).

The eight-surface gate half lives in
:file:`tests/test_state_clone_recalibration.py`; the fixtures, fakes and the
independent fingerprint oracle both halves share live in
:file:`tests/state_clone_recalibration_fixtures.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateManagerError,
    StateSnapshot,
    merge_state_snapshot_index_copyback,
    publish_state_snapshot_index,
    state_snapshot_id,
)
from scripts.node22_clone_direct_grid_cutover_states import (
    CutoverCloneError,
    _state_compatibility_category_files,
    build_parser,
    dispatch,
    enforce_mode_flags,
)
from tests.state_clone_recalibration_fixtures import (
    _CALIB_TABLE_V1,
    _CALIB_TABLE_V2,
    _CALIB_V1,
    _CALIB_V2,
    _IC_V1,
    _IC_V2,
    _LAKE_RELATIVE_PATH,
    _PARA_V1,
    _PARA_V2,
    CUTOVER_VALID_TIME,
    CYCLE_ID,
    M1_MODEL_ID,
    M1_PACKAGE_CHECKSUM,
    M1_PACKAGE_URI,
    M1P_MODEL_ID,
    M1P_PACKAGE_CHECKSUM,
    M1P_PACKAGE_URI,
    ORIGINAL_BASELINE_MODEL_ID,
    SOURCE_ID,
    _expected_state_compatibility_fingerprint,
    _m1_source_snapshot,
    _write_package,
)

# --- Dual-index CLI end-to-end (rows 9, 10; §6.7-6.9) ----------------------


def _valid_ic_bytes(content: bytes) -> bytes:
    """Structurally-valid SHUD ``.cfg.ic`` body for the state object itself."""

    minute = 27_000_000.0 + (int.from_bytes(content[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [f"2\t1\t{minute:.6f}", "0.1\t0.2", "0.3\t0.4"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _registry_payload(
    models: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {"schema_version": "nhms.scheduler.model_registry.v1", "models": list(models)}


def _registry_model(
    model_id: str,
    *,
    package_uri: str,
    package_checksum: str,
    source_id: str = SOURCE_ID,
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "model_package_uri": package_uri,
        "package_checksum": package_checksum,
        "resource_profile": {
            "direct_grid_source_id": source_id,
            # Still the ORIGINAL baseline, even for M1' -- the field the
            # baseline-keyed variant map would (wrongly) resolve through.
            "baseline_model_id": ORIGINAL_BASELINE_MODEL_ID,
        },
    }


def _build_cli_environment(
    tmp_path: Path,
    *,
    target_ic: bytes = _IC_V1,
    target_source_id: str = SOURCE_ID,
    target_legacy_manifest: bool = False,
    source_legacy_manifest: bool = False,
) -> dict[str, Any]:
    """Object store + both packages + both state indexes + a registry payload."""

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    store = LocalObjectStore(object_root, "s3://nhms")

    source_root = _write_package(
        store.resolve_path(M1_PACKAGE_URI),
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
        legacy_manifest=source_legacy_manifest,
    )
    target_root = _write_package(
        store.resolve_path(M1P_PACKAGE_URI),
        model_id=M1P_MODEL_ID,
        calib=_CALIB_V2,
        calib_table=_CALIB_TABLE_V2,
        para=_PARA_V2,
        ic=target_ic,
        legacy_manifest=target_legacy_manifest,
    )

    state_content = _valid_ic_bytes(b"m1-warm-state")
    state_uri = store.write_bytes_atomic(
        f"states/{SOURCE_ID}/{M1_MODEL_ID}/2026081512/state.cfg.ic", state_content
    )
    checksum = f"sha256:{sha256_bytes(state_content)}"
    source_snapshot = _m1_source_snapshot(state_uri=state_uri, checksum=checksum)

    entry = {
        "state_id": source_snapshot.state_id,
        "model_id": M1_MODEL_ID,
        "run_id": source_snapshot.run_id,
        "source_id": SOURCE_ID,
        "valid_time": _iso(CUTOVER_VALID_TIME),
        "state_uri": state_uri,
        "checksum": checksum,
        "usable_flag": True,
        "created_at": _iso(CUTOVER_VALID_TIME),
        "cycle_id": CYCLE_ID,
        "lead_hours": 12,
        "model_package_version": M1_PACKAGE_URI,
        "model_package_checksum": M1_PACKAGE_CHECKSUM,
    }
    canonical_index = object_root / "scheduler/state-index/index-last.json"
    mirror_index = object_root / "scheduler/state-index-mirror/index-last.json"
    for index_path in (canonical_index, mirror_index):
        publish_state_snapshot_index(
            [entry],
            index_path,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            verify_objects=False,
        )

    registry_path = tmp_path / "variant-registry.json"
    registry_path.write_text(
        json.dumps(
            _registry_payload(
                [
                    _registry_model(
                        M1_MODEL_ID,
                        package_uri=M1_PACKAGE_URI,
                        package_checksum=M1_PACKAGE_CHECKSUM,
                    ),
                    _registry_model(
                        M1P_MODEL_ID,
                        package_uri=M1P_PACKAGE_URI,
                        package_checksum=M1P_PACKAGE_CHECKSUM,
                        source_id=target_source_id,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )
    return {
        "object_root": object_root,
        "source_root": source_root,
        "target_root": target_root,
        "canonical_index": canonical_index,
        "mirror_index": mirror_index,
        "registry_path": registry_path,
        "source_snapshot": source_snapshot,
        "state_uri": state_uri,
        "checksum": checksum,
    }


def _cli_args(env: Mapping[str, Any], *extra: str) -> Any:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--object-store-prefix",
            "s3://nhms",
            "--state-index",
            str(env["canonical_index"]),
            "--mirror-state-index",
            str(env["mirror_index"]),
            "--transfer-mode",
            "recalibration",
            "--variant-registry",
            str(env["registry_path"]),
            "--cutover-time",
            "2026081512",
            "--pairs",
            f"{M1_MODEL_ID}:{M1P_MODEL_ID}",
            *extra,
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = not args.apply
    return args


def _write_registry(path: Path, models: Sequence[Mapping[str, Any]]) -> Path:
    path.write_text(json.dumps(_registry_payload(list(models))), encoding="utf-8")
    return path


def _registry_models_by_id(env: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(env["registry_path"]).read_text(encoding="utf-8"))
    return {str(model["model_id"]): dict(model) for model in payload["models"]}


def _add_recalibration_pair(
    env: dict[str, Any],
    *,
    source_model_id: str,
    target_model_id: str,
    target_ic: bytes,
) -> dict[str, Any]:
    """Append a SECOND ``M1->M1'`` pair to an existing CLI environment.

    Writes both packages, publishes a qualified ``(source, gfs, t*)`` +12h row
    into BOTH indexes, and appends both registry rows -- everything the second
    pair needs to reach the gate on its own merits.
    """

    object_root = env["object_root"]
    store = LocalObjectStore(object_root, "s3://nhms")
    source_uri = f"s3://nhms/models/{source_model_id}/package"
    target_uri = f"s3://nhms/models/{target_model_id}/package"
    source_checksum = f"sha256:pkg-{source_model_id}"
    target_checksum = f"sha256:pkg-{target_model_id}"

    source_root = _write_package(
        store.resolve_path(source_uri),
        model_id=source_model_id,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    target_root = _write_package(
        store.resolve_path(target_uri),
        model_id=target_model_id,
        calib=_CALIB_V2,
        calib_table=_CALIB_TABLE_V2,
        para=_PARA_V2,
        ic=target_ic,
    )

    state_content = _valid_ic_bytes(source_model_id.encode("utf-8"))
    state_uri = store.write_bytes_atomic(
        f"states/{SOURCE_ID}/{source_model_id}/2026081512/state.cfg.ic", state_content
    )
    checksum = f"sha256:{sha256_bytes(state_content)}"
    entry = {
        "state_id": state_snapshot_id(
            source_model_id,
            CUTOVER_VALID_TIME,
            source_id=SOURCE_ID,
            cycle_id=CYCLE_ID,
            lead_hours=12,
        ),
        "model_id": source_model_id,
        "run_id": f"fcst_{SOURCE_ID}_{CYCLE_ID}_{source_model_id}",
        "source_id": SOURCE_ID,
        "valid_time": _iso(CUTOVER_VALID_TIME),
        "state_uri": state_uri,
        "checksum": checksum,
        "usable_flag": True,
        "created_at": _iso(CUTOVER_VALID_TIME),
        "cycle_id": CYCLE_ID,
        "lead_hours": 12,
        "model_package_version": source_uri,
        "model_package_checksum": source_checksum,
    }
    for index_path in (env["canonical_index"], env["mirror_index"]):
        entries = json.loads(Path(index_path).read_text(encoding="utf-8"))["entries"]
        publish_state_snapshot_index(
            [*entries, entry],
            Path(index_path),
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            verify_objects=False,
        )

    models = list(_registry_models_by_id(env).values())
    models.extend(
        [
            _registry_model(
                source_model_id, package_uri=source_uri, package_checksum=source_checksum
            ),
            _registry_model(
                target_model_id, package_uri=target_uri, package_checksum=target_checksum
            ),
        ]
    )
    _write_registry(Path(env["registry_path"]), models)
    return {
        "source_root": source_root,
        "target_root": target_root,
        "source_model_id": source_model_id,
        "target_model_id": target_model_id,
    }


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

    # The receipt is written O_EXCL and matches the returned mapping.
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


# --- §6.8 --pairs resolution (row 10) --------------------------------------


def test_pairs_resolves_by_model_id_not_through_the_baseline_keyed_map(
    tmp_path: Path,
) -> None:
    """Both variants declare the ORIGINAL baseline and still resolve correctly."""

    env = _build_cli_environment(tmp_path)
    registry = json.loads(Path(env["registry_path"]).read_text(encoding="utf-8"))
    baselines = {
        model["model_id"]: model["resource_profile"]["baseline_model_id"]
        for model in registry["models"]
    }
    # The precondition that makes the baseline-keyed variant map unusable.
    assert baselines == {
        M1_MODEL_ID: ORIGINAL_BASELINE_MODEL_ID,
        M1P_MODEL_ID: ORIGINAL_BASELINE_MODEL_ID,
    }

    receipt = dispatch(_cli_args(env, "--apply"))

    assert receipt["pairs"][0]["source_model_id"] == M1_MODEL_ID
    assert receipt["pairs"][0]["target_model_id"] == M1P_MODEL_ID


def test_pairs_spanning_two_sources_refuses_before_any_write(tmp_path: Path) -> None:
    env = _build_cli_environment(tmp_path, target_source_id="IFS")

    with pytest.raises(CutoverCloneError, match="spans two sources"):
        dispatch(_cli_args(env, "--apply"))

    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    assert not any(item["model_id"] == M1P_MODEL_ID for item in payload["entries"])


def test_pairs_with_a_legacy_classifying_source_refuses_before_any_write(
    tmp_path: Path,
) -> None:
    env = _build_cli_environment(tmp_path, source_legacy_manifest=True)

    with pytest.raises(CutoverCloneError, match="does not classify as direct-grid"):
        dispatch(_cli_args(env, "--apply"))

    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    assert not any(item["model_id"] == M1P_MODEL_ID for item in payload["entries"])


def test_pairs_with_identical_sides_refuses(tmp_path: Path) -> None:
    env = _build_cli_environment(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--state-index",
            str(env["canonical_index"]),
            "--mirror-state-index",
            str(env["mirror_index"]),
            "--transfer-mode",
            "recalibration",
            "--variant-registry",
            str(env["registry_path"]),
            "--cutover-time",
            "2026081512",
            "--pairs",
            f"{M1_MODEL_ID}:{M1_MODEL_ID}",
            "--apply",
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = False

    with pytest.raises(CutoverCloneError, match="same model on both sides"):
        dispatch(args)


def test_pairs_naming_an_unknown_model_refuses(tmp_path: Path) -> None:
    env = _build_cli_environment(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--state-index",
            str(env["canonical_index"]),
            "--mirror-state-index",
            str(env["mirror_index"]),
            "--transfer-mode",
            "recalibration",
            "--variant-registry",
            str(env["registry_path"]),
            "--cutover-time",
            "2026081512",
            "--pairs",
            f"{M1_MODEL_ID}:huai_dg_gfs_v9",
            "--apply",
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = False

    with pytest.raises(CutoverCloneError, match="registry entry missing"):
        dispatch(args)


@pytest.mark.parametrize(
    ("pairs_value", "expected_message"),
    [
        (f"{M1_MODEL_ID}:{M1P_MODEL_ID}:extra", "malformed --pairs entry"),
        (f":{M1P_MODEL_ID}", "malformed --pairs entry"),
        (f"{M1_MODEL_ID}:", "malformed --pairs entry"),
        (
            f"{M1_MODEL_ID}:{M1P_MODEL_ID},{M1_MODEL_ID}:{M1P_MODEL_ID}",
            "duplicate --pairs entry",
        ),
        (",", "--pairs declared no <M1>:<M1prime> pair"),
    ],
)
def test_malformed_pairs_refuse_before_any_registry_read(
    tmp_path: Path, pairs_value: str, expected_message: str
) -> None:
    """§6.8: every ``_parse_pairs`` refusal branch, and it runs FIRST.

    ``--variant-registry`` deliberately points at a path that does not exist.
    ``_parse_pairs`` runs before the registry payload is read, so a pairs-shaped
    ``CutoverCloneError`` -- rather than the ``FileNotFoundError`` the read would
    raise -- is itself the proof that no registry or gate work happened. Both
    indexes and the receipt path are checked untouched on top of that.
    """

    env = _build_cli_environment(tmp_path)
    before = {
        label: Path(path).read_text(encoding="utf-8")
        for label, path in (
            ("canonical", env["canonical_index"]),
            ("mirror", env["mirror_index"]),
        )
    }
    receipt_path = tmp_path / "never-written-receipt.json"

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(
            _cli_args(
                env,
                "--apply",
                "--receipt",
                str(receipt_path),
                "--pairs",
                pairs_value,
                "--variant-registry",
                str(tmp_path / "no-such-registry.json"),
            )
        )

    assert expected_message in str(excinfo.value)
    assert not receipt_path.exists()
    for label, path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        assert Path(path).read_text(encoding="utf-8") == before[label], label


# --- --baseline-registry as a second payload under recalibration -----------


def test_a_pair_spanning_two_registry_payloads_resolves_and_clones(
    tmp_path: Path,
) -> None:
    """PR deviation record 3: the two payloads MERGE, they do not compete.

    The realistic node-22 shape: ``M1`` lives only in the pre-update canonical
    registry and ``M1'`` only in the freshly provisioned one. The refusal
    without ``--baseline-registry`` is asserted first, in the same environment,
    so the merge is proven load-bearing rather than incidentally harmless.
    """

    env = _build_cli_environment(tmp_path)
    models = _registry_models_by_id(env)
    _write_registry(Path(env["registry_path"]), [models[M1P_MODEL_ID]])
    baseline_path = _write_registry(
        tmp_path / "baseline-registry.json", [models[M1_MODEL_ID]]
    )

    # One payload alone cannot resolve the pair at all.
    with pytest.raises(CutoverCloneError, match="registry entry missing"):
        dispatch(_cli_args(env, "--apply"))

    receipt = dispatch(
        _cli_args(env, "--apply", "--baseline-registry", str(baseline_path))
    )

    assert receipt["invocation_outcome"] == "complete"
    assert receipt["cloned_pair_count"] == 1
    pair_record = receipt["pairs"][0]
    # The source resolved out of the SECOND payload, with ITS package root.
    assert pair_record["source_model_id"] == M1_MODEL_ID
    assert pair_record["source_model_package_version"] == M1_PACKAGE_URI
    assert pair_record["source_model_package_checksum"] == M1_PACKAGE_CHECKSUM
    assert pair_record["target_model_package_version"] == M1P_PACKAGE_URI
    assert pair_record["target_model_package_checksum"] == M1P_PACKAGE_CHECKSUM
    assert pair_record["clone_gate_kind"] == "state_compatibility"
    for label, index_path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        entries = json.loads(Path(index_path).read_text(encoding="utf-8"))["entries"]
        assert any(item["model_id"] == M1P_MODEL_ID for item in entries), label


def test_a_model_disagreeing_between_the_two_payloads_refuses_before_any_write(
    tmp_path: Path,
) -> None:
    """The fail-closed half of the same contract: disagreement is not merged.

    Same ``model_id`` in both payloads with a differing ``package_checksum``.
    Which of the two is authoritative is unknowable from the payloads alone, so
    the tool refuses by name instead of silently preferring one.
    """

    env = _build_cli_environment(tmp_path)
    before = {
        label: Path(path).read_text(encoding="utf-8")
        for label, path in (
            ("canonical", env["canonical_index"]),
            ("mirror", env["mirror_index"]),
        )
    }
    models = _registry_models_by_id(env)
    disagreeing_source = dict(models[M1_MODEL_ID])
    disagreeing_source["package_checksum"] = "sha256:pkg-m1-recorded-differently"
    baseline_path = _write_registry(
        tmp_path / "baseline-registry.json",
        [disagreeing_source, models[M1P_MODEL_ID]],
    )

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(
            _cli_args(env, "--apply", "--baseline-registry", str(baseline_path))
        )

    assert (
        f"model {M1_MODEL_ID} differs between --variant-registry and "
        "--baseline-registry"
    ) in str(excinfo.value)
    for label, path in (
        ("canonical", env["canonical_index"]),
        ("mirror", env["mirror_index"]),
    ):
        assert Path(path).read_text(encoding="utf-8") == before[label], label


# --- Parser: per-mode required flags (§4.1) --------------------------------


def test_baseline_cutover_still_refuses_its_missing_flags() -> None:
    """The existing mode keeps refusing a missing partition/registry flag."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            "/tmp/object-store",
            "--state-index",
            "/tmp/index.json",
            "--cutover-time",
            "2026081512",
        ]
    )
    assert args.transfer_mode == "baseline_cutover"
    with pytest.raises(SystemExit):
        enforce_mode_flags(parser, args)


def test_baseline_cutover_invocation_parses_verbatim() -> None:
    """The July invocation keeps working with no new flag supplied."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            "/tmp/object-store",
            "--state-index",
            "/tmp/index.json",
            "--baseline-registry",
            "/tmp/baseline.json",
            "--variant-registry",
            "/tmp/variant.json",
            "--cutover-time",
            "2026070112",
            "--warm-basins",
            "a,b",
            "--cold-basins",
            "c",
        ]
    )
    enforce_mode_flags(parser, args)
    assert args.transfer_mode == "baseline_cutover"
    assert args.pairs is None
    assert args.mirror_state_index is None


@pytest.mark.parametrize("dropped", ["--pairs", "--mirror-state-index", "--variant-registry"])
def test_recalibration_refuses_its_missing_flags(dropped: str) -> None:
    supplied = {
        "--pairs": f"{M1_MODEL_ID}:{M1P_MODEL_ID}",
        "--mirror-state-index": "/tmp/mirror.json",
        "--variant-registry": "/tmp/variant.json",
    }
    argv = [
        "--object-store-root",
        "/tmp/object-store",
        "--state-index",
        "/tmp/index.json",
        "--cutover-time",
        "2026081512",
        "--transfer-mode",
        "recalibration",
    ]
    for flag, value in supplied.items():
        if flag == dropped:
            continue
        argv.extend([flag, value])

    parser = build_parser()
    args = parser.parse_args(argv)
    with pytest.raises(SystemExit):
        enforce_mode_flags(parser, args)

