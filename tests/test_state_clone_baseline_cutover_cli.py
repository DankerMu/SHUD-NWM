"""Baseline cutover CLI: partial-apply abort receipts and legacy compatibility.

Covers the ``run()`` half of the ``fingerprint-gated-state-clone`` file-index
CLI (change ``state-clone-receipt-fail-safe``): a later basin/source failure
after an earlier warm clone went live must persist an aborted receipt naming
every completed decision and the failed basin/source before the exact original
exception propagates, while a first-item / no-live-row failure and every
dry-run failure must create NO abort receipt. Baseline ``--receipt`` stays
optional and its successful receipt mapping retains the existing fields.

Fixtures here reuse the recalibration suite's shared package writer
(:file:`tests/state_clone_recalibration_fixtures.py`) so both modes build
real packages from ONE source; the baseline environment additionally needs a
baseline registry, a variant registry, a cold basin partition (required by the
baseline per-mode flags), and a qualified warm source row per warm basin/source
in the file index.
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
    publish_state_snapshot_index,
    state_snapshot_id,
)
from scripts.node22_clone_direct_grid_cutover_states import (
    CutoverCloneError,
    build_parser,
    dispatch,
    enforce_mode_flags,
)
from tests.state_clone_recalibration_fixtures import (
    _CALIB_TABLE_V1,
    _CALIB_V1,
    _IC_V1,
    _PARA_V1,
    _write_package,
)
from workers.mapping_builder.rewrite import HydrologicCoreFingerprintMismatchError

BASIN_A = "huai"  # warm
BASIN_B = "huai_ift"  # warm
BASIN_COLD = "z_huai_cold"  # cold; sorts after both warm basins
GFS = "gfs"
IFS = "IFS"
CUTOVER_TIME = datetime(2026, 7, 1, 12, tzinfo=UTC)
CUTOVER_TIME_STR = "2026070112"
BASELINE_CHECKSUM = "sha256:baseline"


def _baseline_id(basin: str) -> str:
    return f"{basin}_baseline"


def _baseline_version(basin: str) -> str:
    return f"s3://nhms/models/{_baseline_id(basin)}/package"


# The warm/cold partitions name BASELINE MODEL IDs (the ``_variant_map`` keys),
# exactly as the production invocation does.
WARM_BASINS_CSV = f"{_baseline_id(BASIN_A)},{_baseline_id(BASIN_B)}"
COLD_BASIN_ID = _baseline_id(BASIN_COLD)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _valid_ic_bytes(content: bytes) -> bytes:
    """Structurally-valid SHUD ``.cfg.ic`` body for the state object itself."""

    minute = 27_000_000.0 + (int.from_bytes(content[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [f"2\t1\t{minute:.6f}", "0.1\t0.2", "0.3\t0.4"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _registry_payload(models: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"schema_version": "nhms.scheduler.model_registry.v1", "models": list(models)}


def _registry_model(
    model_id: str,
    *,
    package_uri: str,
    package_checksum: str,
    source_id: str,
    baseline_model_id: str,
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "model_package_uri": package_uri,
        "package_checksum": package_checksum,
        "resource_profile": {
            "direct_grid_source_id": source_id,
            "baseline_model_id": baseline_model_id,
        },
    }


def _write_registry(path: Path, models: Sequence[Mapping[str, Any]]) -> Path:
    path.write_text(json.dumps(_registry_payload(list(models))), encoding="utf-8")
    return path


def _write_baseline_package(store: LocalObjectStore, basin: str) -> Path:
    """A minimal SHUD-shaped baseline package for one basin.

    Carries every file the fingerprint gate touches, with the SAME ``huai.*``
    relative paths the ``_write_package`` variant fixtures use -- the gate hashes
    identical relative paths under both roots, so a baseline package must mirror
    the variant's paths exactly. Each basin gets its OWN package root
    (``s3://nhms/models/<baseline_id>/package``), which is what keeps the
    single-root ``rglob`` patterns at exactly one match per file and mirrors the
    production registries where every baseline model has its own package URI.
    """

    root = store.resolve_path(_baseline_version(basin))
    root.mkdir(parents=True, exist_ok=True)
    baseline_files: dict[str, bytes] = {
        "huai.sp.mesh": b"mesh-topology-v1\n",
        "huai.sp.riv": b"river-reach-v1\n",
        "huai.sp.rivseg": b"river-segment-v1\n",
        "huai.para.soil": b"soil-para-v1\n",
        "huai.para.geol": b"geol-para-v1\n",
        "huai.para.lc": b"land-cover-v1\n",
        "huai.lake.att": b"lake-topology-v1\n",
        "huai.cfg.ic": _IC_V1,
        "huai.cfg.para": _PARA_V1,
        "huai.cfg.calib": _CALIB_V1,
    }
    for relative_path, payload in baseline_files.items():
        (root / relative_path).write_bytes(payload)
    (root / "CALIB").mkdir(exist_ok=True)
    (root / "CALIB" / "table.csv").write_bytes(_CALIB_TABLE_V1)
    sp_att_lines = [
        "4\t9",
        "INDEX\tSOIL\tGEOL\tLC\tFORC\tMF\tBC\tSS\tLAKE",
        *[f"{i+1}\t1\t1\t11\t{v}\t1\t0\t0\t0" for i, v in enumerate((1, 2, 3, 4))],
    ]
    (root / "huai.sp.att").write_text("\n".join(sp_att_lines) + "\n", encoding="utf-8")
    return root


def _build_cli_environment(tmp_path: Path, *, variant_b_bad: bool = False) -> dict[str, Any]:
    """Baseline environment: 2 warm + 1 cold basin, each with GFS/IFS variants.

    Every warm basin/source gets a qualified ``(baseline, source, t*)`` +12h row
    written into the file index BEFORE the invocation so the clone finds its
    qualified source. The cold basin's variants are declared in the registries
    but never resolved (the cold branch of ``run()`` skips package work).
    ``variant_b_bad`` drifts ``huai_ift``/IFS's ``cfg.ic`` so the fingerprint
    gate raises on the LAST warm unit -- after every earlier warm clone has gone
    live (processing order is sorted basin ids: ``huai``, ``huai_ift``,
    ``z_huai_cold``).
    """

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    store = LocalObjectStore(object_root, "s3://nhms")

    registry_models: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []
    package_roots: dict[str, Path] = {}
    baseline_models: list[dict[str, Any]] = []
    for basin in (BASIN_A, BASIN_B, BASIN_COLD):
        baseline_id = _baseline_id(basin)
        baseline_models.append(
            {
                "model_id": baseline_id,
                "model_package_uri": _baseline_version(basin),
                "package_checksum": BASELINE_CHECKSUM,
                "resource_profile": {},
            }
        )
        if basin != BASIN_COLD:
            _write_baseline_package(store, basin)
        for source_id in (GFS, IFS):
            model_id = f"{basin}_dg_{source_id}"
            package_uri = f"s3://nhms/models/{model_id}/package"
            checksum = f"sha256:pkg-{model_id}"
            bad = variant_b_bad and basin == BASIN_B and source_id == IFS
            variant_root = _write_package(
                store.resolve_path(package_uri),
                model_id=model_id,
                calib=_CALIB_V1,
                calib_table=_CALIB_TABLE_V1,
                para=_PARA_V1,
                ic=_IC_V1,
                # A drifted hydrologic-core category file (land cover) makes the
                # ten-surface fingerprint gate raise on this unit in BOTH apply
                # and dry-run modes -- the baseline fingerprint check reads the
                # baseline's state-schema/solver bytes for both sides, so only a
                # disk category file drift is a clean failure that happens AFTER
                # earlier warm clones went live.
                core_overrides={"huai.para.lc": b"land-cover-v2-drifted\n"} if bad else None,
            )
            package_roots[model_id] = variant_root
            registry_models.append(
                _registry_model(
                    model_id,
                    package_uri=package_uri,
                    package_checksum=checksum,
                    source_id=source_id,
                    baseline_model_id=baseline_id,
                )
            )
            if basin == BASIN_COLD:
                continue
            state_content = _valid_ic_bytes(f"{basin}-{source_id}".encode("utf-8"))
            state_uri = store.write_bytes_atomic(
                f"states/{source_id}/{baseline_id}/2026070112/state.cfg.ic", state_content
            )
            checksum = f"sha256:{sha256_bytes(state_content)}"
            source_entries.append(
                {
                    "state_id": state_snapshot_id(
                        baseline_id,
                        CUTOVER_TIME,
                        source_id=source_id,
                        cycle_id=f"{source_id}_2026063000",
                        lead_hours=12,
                    ),
                    "model_id": baseline_id,
                    "run_id": f"fcst_{source_id}_2026063000_{baseline_id}",
                    "source_id": source_id,
                    "valid_time": _iso(CUTOVER_TIME),
                    "state_uri": state_uri,
                    "checksum": checksum,
                    "usable_flag": True,
                    "created_at": _iso(CUTOVER_TIME),
                    "cycle_id": f"{source_id}_2026063000",
                    "lead_hours": 12,
                    "model_package_version": _baseline_version(basin),
                    "model_package_checksum": BASELINE_CHECKSUM,
                }
            )

    canonical_index = object_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        source_entries,
        canonical_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        verify_objects=False,
    )
    baseline_registry = _write_registry(tmp_path / "baseline-registry.json", baseline_models)
    variant_registry = _write_registry(tmp_path / "variant-registry.json", registry_models)
    return {
        "object_root": object_root,
        "canonical_index": canonical_index,
        "baseline_registry": baseline_registry,
        "variant_registry": variant_registry,
        "package_roots": package_roots,
        "source_entries": source_entries,
    }


def _cli_args(
    env: Mapping[str, Any],
    *extra: str,
    warm_basins: str = WARM_BASINS_CSV,
    cold_basins: str = COLD_BASIN_ID,
) -> Any:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--object-store-prefix",
            "s3://nhms",
            "--state-index",
            str(env["canonical_index"]),
            "--baseline-registry",
            str(env["baseline_registry"]),
            "--variant-registry",
            str(env["variant_registry"]),
            "--cutover-time",
            CUTOVER_TIME_STR,
            "--warm-basins",
            warm_basins,
            "--cold-basins",
            cold_basins,
            "--expected-warm-count",
            str(len(warm_basins.split(","))),
            "--expected-cold-count",
            str(len(cold_basins.split(","))),
            *extra,
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = not args.apply
    return args


def _index_model_ids(env: Mapping[str, Any]) -> set[str]:
    payload = json.loads(Path(env["canonical_index"]).read_text(encoding="utf-8"))
    return {str(item["model_id"]) for item in payload["entries"]}


def _expected_fingerprint_hash(env: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    """Recompute the ten-surface fingerprint from the fixture bytes alone.

    Mirrors the independent oracle pattern of the recalibration suite: this
    never calls the production fingerprint code, so a drift in either the
    surface set or the hash format detonates here rather than agreeing with
    itself. ``category_files`` is derived per variant root exactly as ``run()``
    derives it (via ``provision_direct_grid_scheduler_registry._category_files``),
    and the same relative paths are fingerprinted on the baseline root.
    """

    from packages.common.object_store import LocalObjectStore as _Store
    from scripts.provision_direct_grid_scheduler_registry import _category_files as _cat
    from workers.mapping_builder.rewrite import compute_hydrologic_core_fingerprint

    store = _Store(env["object_root"], "s3://nhms")
    variant_root = store.resolve_path(str(model["model_package_uri"]))
    basin = str(model["model_id"]).split("_dg_")[0]
    baseline_root = store.resolve_path(_baseline_version(basin))
    categories = _cat(variant_root)
    fingerprint = compute_hydrologic_core_fingerprint(
        baseline_root,
        sp_att_path=baseline_root / "huai.sp.att",
        category_files=categories,
        state_schema_bytes=_IC_V1,
        solver_config_bytes=_PARA_V1,
    )
    return fingerprint.hash


def _complete_receipt_mapping(env: Mapping[str, Any]) -> dict[str, Any]:
    """Expected complete receipt fields for a clean baseline run.

    The source-of-truth values come from the fixture inputs themselves: one
    ``warm_clone`` decision per warm basin/source carrying the variant's minted
    ``state_id`` and the shared ten-surface fingerprint hash, and one
    ``cold_new_basin`` per cold basin/source with ``state_id=None``.
    ``generated_at`` is naturally variable and compared structurally.
    """

    variants = json.loads(Path(env["variant_registry"]).read_text(encoding="utf-8"))["models"]
    by_id = {str(model["model_id"]): model for model in variants}
    decisions = []
    # Production iterates sorted baseline model IDs, so the cold basin
    # (``z_huai_cold_baseline``) sorts after both warm basins and its cold
    # decisions land last in the receipt.
    for basin in (BASIN_A, BASIN_B, BASIN_COLD):
        for source_id in (GFS, IFS):
            model_id = f"{basin}_dg_{source_id}"
            if basin == BASIN_COLD:
                decisions.append(
                    {
                        "basin_model_id": _baseline_id(basin),
                        "source_id": source_id,
                        "target_model_id": model_id,
                        "decision": "cold_new_basin",
                        "state_id": None,
                    }
                )
                continue
            decisions.append(
                {
                    "basin_model_id": _baseline_id(basin),
                    "source_id": source_id,
                    "target_model_id": model_id,
                    "decision": "warm_clone",
                    "state_id": state_snapshot_id(
                        model_id,
                        CUTOVER_TIME,
                        source_id=source_id,
                        cycle_id=f"{source_id}_2026063000",
                        lead_hours=12,
                    ),
                    "hydrologic_core_fingerprint": _expected_fingerprint_hash(
                        env, by_id[model_id]
                    ),
                }
            )
    return {
        "schema_version": "nhms.direct_grid_cutover_state_clone.v1",
        "cutover_time": CUTOVER_TIME.isoformat().replace("+00:00", "Z"),
        "dry_run": False,
        "warm_basin_count": 2,
        "cold_basin_count": 1,
        "warm_candidate_count": 4,
        "cold_candidate_count": 2,
        "decisions": decisions,
    }


def _apply_and_fail_env(
    tmp_path: Path, *, first_bad: bool = False, dry_run: bool = False
) -> tuple[dict[str, Any], Any, Path]:
    """A baseline environment where the FIRST or LAST warm unit fails.

    Processing order is sorted baseline IDs (``huai_baseline`` first) and per
    basin ``gfs`` before ``IFS``, so the very first warm unit is
    ``huai_baseline``/gfs. By default the ``huai_ift``/IFS variant carries a
    drifted land-cover file so the ten-surface fingerprint gate raises a
    ``HydrologicCoreFingerprintMismatchError`` on the LAST warm unit -- after
    every earlier warm clone has gone live (and in dry-run too, since the
    fingerprint check runs before the clone). ``first_bad=True`` drifts
    ``huai``/gfs instead so the very FIRST warm unit fails, leaving no live row.
    """

    env = _build_cli_environment(tmp_path, variant_b_bad=not first_bad)
    if first_bad:
        store = LocalObjectStore(env["object_root"], "s3://nhms")
        bad_root = store.resolve_path(f"s3://nhms/models/{BASIN_A}_dg_{GFS}/package")
        (bad_root / "huai.para.lc").write_bytes(b"land-cover-v2-drifted\n")
    receipt_path = tmp_path / "abort-receipt.json"
    args = _cli_args(
        env,
        *(
            ["--apply", "--receipt", str(receipt_path)]
            if not dry_run
            else ["--receipt", str(receipt_path)]
        ),
    )
    return env, args, receipt_path


# --- A later warm clone fails after earlier ones went live (row 1) ----------


def test_baseline_abort_after_first_warm_clone_writes_aborted_receipt(
    tmp_path: Path,
) -> None:
    """Row 1: first warm source persists, a later basin/source fails.

    The requested receipt must enumerate every completed persisted decision
    (with its ``state_id``), mark the invocation aborted, and name the failed
    basin/source + reason -- while the original fingerprint-mismatch exception
    still propagates and the process reports non-zero.
    """

    env, args, receipt_path = _apply_and_fail_env(tmp_path)

    with pytest.raises(HydrologicCoreFingerprintMismatchError) as excinfo:
        dispatch(args)

    # The original failure is the raised exception -- not a receipt error.
    assert excinfo.type is HydrologicCoreFingerprintMismatchError

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "nhms.direct_grid_cutover_state_clone.v1"
    assert receipt["dry_run"] is False
    assert receipt["invocation_outcome"] == "aborted"

    # Every completed persisted decision is enumerated with its live state_id.
    completed = [item for item in receipt["decisions"] if item["decision"] == "warm_clone"]
    assert [item["basin_model_id"] for item in completed] == [
        _baseline_id(BASIN_A),
        _baseline_id(BASIN_A),
        _baseline_id(BASIN_B),
    ]
    assert [item["source_id"] for item in completed] == [GFS, IFS, GFS]
    for item in completed:
        assert item["state_id"] is not None
    # None of the completed units is the failed one.
    assert all(item["target_model_id"] != f"{BASIN_B}_dg_{IFS}" for item in completed)
    # No cold decision was reached -- the abort stopped before the cold basin.
    assert all(item["decision"] != "cold_new_basin" for item in receipt["decisions"])

    failed = receipt["failed_basin_source"]
    assert failed["basin_model_id"] == _baseline_id(BASIN_B)
    assert failed["source_id"] == IFS
    assert failed["failure_kind"] == "basin_source_not_completed"
    assert "HydrologicCoreFingerprintMismatchError" in failed["error"]

    # The live rows the receipt declares are really in the index; the failed
    # target is in neither.
    model_ids = _index_model_ids(env)
    assert {f"{BASIN_A}_dg_{GFS}", f"{BASIN_A}_dg_{IFS}", f"{BASIN_B}_dg_{GFS}"} <= model_ids
    assert f"{BASIN_B}_dg_{IFS}" not in model_ids


def test_baseline_abort_when_a_later_baseline_registry_row_is_missing(
    tmp_path: Path,
) -> None:
    """Row 1 variant: basin B absent from the baseline registry after A's clones.

    The ``baseline registry entry missing`` failure used to raise OUTSIDE the
    basin/source unit capture, so an abort after A's gfs/IFS clones went live
    bypassed aborted-receipt assembly entirely -- the exact #1709 mid-loop gap
    this change closes. The receipt must list A's completed live rows, name the
    missing basin with NO fabricated source (``source_id=None``), mark the
    invocation aborted, and re-raise the exact original ``CutoverCloneError``
    message.

    The minimum fixture shape that reaches the named branch: keep B's variant
    registry rows and warm source rows (preflight partitions and the qualified
    source exist), remove only B's BASELINE registry row -- the ``baseline
    registry entry missing`` check runs at the top of each basin iteration,
    AFTER A fully completed and BEFORE any B source unit begins. Preflight
    (partition match, GFS/IFS variant coverage) is untouched and still passes.
    """

    env = _build_cli_environment(tmp_path)
    baseline_payload = json.loads(Path(env["baseline_registry"]).read_text(encoding="utf-8"))
    baseline_payload["models"] = [
        model
        for model in baseline_payload["models"]
        if str(model["model_id"]) != _baseline_id(BASIN_B)
    ]
    Path(env["baseline_registry"]).write_text(
        json.dumps(baseline_payload), encoding="utf-8"
    )
    receipt_path = tmp_path / "missing-baseline-row-receipt.json"
    args = _cli_args(env, "--apply", "--receipt", str(receipt_path))

    with pytest.raises(CutoverCloneError) as excinfo:
        dispatch(args)

    # The exact original message propagates, not a receipt error.
    assert f"baseline registry entry missing: {_baseline_id(BASIN_B)}" in str(excinfo.value)

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["invocation_outcome"] == "aborted"

    # A's gfs/IFS clones are listed as completed live decisions with state_ids.
    completed = [item for item in receipt["decisions"] if item["decision"] == "warm_clone"]
    assert [item["basin_model_id"] for item in completed] == [
        _baseline_id(BASIN_A),
        _baseline_id(BASIN_A),
    ]
    assert [item["source_id"] for item in completed] == [GFS, IFS]
    for item in completed:
        assert item["state_id"] is not None
    assert all(item["decision"] != "cold_new_basin" for item in receipt["decisions"])

    # The failed location names B with no fabricated source and no target.
    failed = receipt["failed_basin_source"]
    assert failed["basin_model_id"] == _baseline_id(BASIN_B)
    assert failed["source_id"] is None
    assert failed["target_model_id"] is None
    assert failed["failure_kind"] == "basin_source_not_completed"
    assert f"baseline registry entry missing: {_baseline_id(BASIN_B)}" in failed["error"]

    # The live rows the receipt declares are really in the index.
    model_ids = _index_model_ids(env)
    assert {f"{BASIN_A}_dg_{GFS}", f"{BASIN_A}_dg_{IFS}"} <= model_ids
    assert f"{BASIN_B}_dg_{GFS}" not in model_ids


# --- No-live-row failures create no abort receipt (row 6) -------------------


def test_baseline_first_item_failure_creates_no_abort_receipt(tmp_path: Path) -> None:
    """Row 6: a failure before ANY live clone leaves the O_EXCL path free."""

    env, args, receipt_path = _apply_and_fail_env(tmp_path, first_bad=True)

    with pytest.raises(HydrologicCoreFingerprintMismatchError):
        dispatch(args)

    assert not receipt_path.exists()
    # Nothing was written into the index either.
    assert f"{BASIN_A}_dg_{GFS}" not in _index_model_ids(env)


def test_baseline_dry_run_failure_creates_no_abort_receipt(tmp_path: Path) -> None:
    """Row 6: dry-run processes full decisions but persists no clone row.

    Byte-equality oracle: the ENTIRE canonical index is snapshotted immediately
    before the invocation and asserted byte-identical after the expected
    fingerprint exception. A mutant that persists earlier passing dry-run units
    -- any metadata, ordering, or content mutation -- reddens here even though
    the no-abort-receipt and failed-target-absent checks would stay green.
    """

    env, args, receipt_path = _apply_and_fail_env(tmp_path, dry_run=True)
    canonical_index_path = Path(env["canonical_index"])
    before_bytes = canonical_index_path.read_bytes()

    with pytest.raises(HydrologicCoreFingerprintMismatchError):
        dispatch(args)

    assert not receipt_path.exists()
    # Nothing was written into the index either -- byte-for-byte, not merely
    # the failed target absent.
    assert canonical_index_path.read_bytes() == before_bytes
    assert f"{BASIN_B}_dg_{IFS}" not in _index_model_ids(env)


def test_baseline_dry_run_failure_no_receipt_flag_keeps_behavior(tmp_path: Path) -> None:
    """Row 6: dry-run without ``--receipt`` still aborts with no artifact.

    Same byte-equality oracle as the receipt-path sibling: the whole canonical
    index must be byte-identical after the abort, so a mutant that persists
    earlier passing dry-run units reddens here too.
    """

    env = _build_cli_environment(tmp_path, variant_b_bad=True)
    args = _cli_args(env)
    canonical_index_path = Path(env["canonical_index"])
    before_bytes = canonical_index_path.read_bytes()

    with pytest.raises(HydrologicCoreFingerprintMismatchError):
        dispatch(args)

    assert canonical_index_path.read_bytes() == before_bytes
    assert f"{BASIN_B}_dg_{IFS}" not in _index_model_ids(env)


# --- Clean baseline invocation / legacy compatibility (row 5) ---------------


def test_baseline_clean_run_without_receipt_parses_and_returns_payload(
    tmp_path: Path,
) -> None:
    """Row 5: a legacy baseline invocation (no ``--receipt``) still works."""

    env = _build_cli_environment(tmp_path)
    args = _cli_args(env, "--apply")

    receipt = dispatch(args)

    assert receipt["dry_run"] is False
    assert receipt["warm_candidate_count"] == 4
    assert receipt["cold_candidate_count"] == 2
    assert len(receipt["decisions"]) == 6
    model_ids = _index_model_ids(env)
    assert {
        f"{BASIN_A}_dg_{GFS}",
        f"{BASIN_A}_dg_{IFS}",
        f"{BASIN_B}_dg_{GFS}",
        f"{BASIN_B}_dg_{IFS}",
    } <= model_ids


def test_baseline_clean_receipt_retains_exact_complete_mapping(tmp_path: Path) -> None:
    """Row 5: successful receipt fields/values are unchanged (aside from generated_at)."""

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "clean-receipt.json"
    args = _cli_args(env, "--apply", "--receipt", str(receipt_path))

    receipt = dispatch(args)

    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "nhms.direct_grid_cutover_state_clone.v1"
    assert isinstance(persisted["generated_at"], str)
    expected = _complete_receipt_mapping(env)
    assert {key: value for key, value in persisted.items() if key != "generated_at"} == expected
    assert receipt["decisions"] == persisted["decisions"]


def test_baseline_cold_and_warm_partition_receipt_keeps_cold_decisions(
    tmp_path: Path,
) -> None:
    """Row 5: the cold-basin branch keeps writing ``cold_new_basin`` decisions."""

    env = _build_cli_environment(tmp_path)
    receipt_path = tmp_path / "cold-receipt.json"
    args = _cli_args(env, "--apply", "--receipt", str(receipt_path))

    receipt = dispatch(args)

    assert receipt["cold_basin_count"] == 1
    assert receipt["warm_basin_count"] == 2
    cold = [item for item in receipt["decisions"] if item["decision"] == "cold_new_basin"]
    assert len(cold) == 2
    assert all(item["state_id"] is None for item in cold)
    assert {item["source_id"] for item in cold} == {GFS, IFS}


# --- Receipt masking regressions (row 3) ------------------------------------


def test_baseline_abort_receipt_failure_annotates_original_error_without_replacing(
    tmp_path: Path,
) -> None:
    """Row 3 (baseline leg): existing receipt path + later failure.

    The original fingerprint-mismatch exception propagates as the primary
    error, a note naming ``FileExistsError`` is attached, and the pre-existing
    receipt file is byte-for-byte unchanged.
    """

    env, args, receipt_path = _apply_and_fail_env(tmp_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text('{"pre_existing": true}\n', encoding="utf-8")
    before = receipt_path.read_bytes()

    with pytest.raises(HydrologicCoreFingerprintMismatchError) as excinfo:
        dispatch(args)

    assert excinfo.type is HydrologicCoreFingerprintMismatchError
    notes = list(getattr(excinfo.value, "__notes__", []))
    assert any("FileExistsError" in note and "receipt persistence failed" in note for note in notes)
    assert receipt_path.read_bytes() == before
