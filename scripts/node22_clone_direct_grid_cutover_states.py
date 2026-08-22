#!/usr/bin/env python
"""Clone node-22 warm states for a declared direct-grid state carry-over.

Two modes, selected by ``--transfer-mode``:

``baseline_cutover`` (default, unchanged)
    The July baseline -> direct-grid cutover.  Bounded to one cutover time and
    an explicit warm/cold basin partition.  Warm candidates pass the platform's
    ten-surface hydrologic core fingerprint gate before their clone row is
    written to the DB-free scheduler state index.  Cold candidates are verified
    to have no target state at the cutover time; no state is fabricated.

``recalibration`` (change ``recalibration-state-carryover``)
    An ``M1 -> M1'`` package update whose only hydrologic-core change is
    calibration parameters.  Source AND target are both direct-grid variants,
    named explicitly by ``--pairs <M1>:<M1prime>,...`` and resolved by
    ``model_id`` out of the registry payload -- never through the
    baseline-keyed variant map, whose ``resource_profile.baseline_model_id``
    still points at the ORIGINAL baseline for an ``M1'`` produced by re-running
    the provisioning script.  Eligibility is the eight-surface
    state-compatibility gate (the ten G10 labels minus ``calibration`` and
    ``solver_config``), with ``cfg.ic`` / ``cfg.para`` bytes read PER SIDE so a
    new ``cfg.ic`` in ``M1'`` refuses instead of false-passing.  The clone row
    is written into BOTH state indexes -- the NFS canonical index and the
    node-22-local scratch mirror -- in one invocation.

Execution host: node-22.  It is the only host that can reach both the shared
NFS canonical index and the node-22-local ``/scratch`` mirror, and it is
DB-free, so the recalibration mode is file-index-only and takes no DB handle.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore
from packages.common.source_identity import normalize_source_id
from packages.common.state_clone import (
    CLONE_GATE_KIND_STATE_COMPATIBILITY,
    fingerprint_gated_state_clone,
)
from packages.common.state_manager import FileStateSnapshotIndexRepository, StateSnapshot
from scripts.provision_direct_grid_scheduler_registry import _category_files, _required_single
from workers.data_adapters.base import parse_cycle_time
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)
from workers.mapping_builder.rewrite import verify_hydrologic_core_fingerprint_equal

TRANSFER_MODE_BASELINE_CUTOVER = "baseline_cutover"
TRANSFER_MODE_RECALIBRATION = "recalibration"

RECALIBRATION_RECEIPT_SCHEMA_VERSION = "nhms.recalibration_state_clone.v1"

# The spin-up-distortion announcement obligation is RETAINED across a
# recalibration carry-over: warm carry-over is lower distortion than a cold
# start but is not zero distortion, and this receipt is the declared
# carry-over evidence that records it.
RECALIBRATION_SPIN_UP_DISTORTION_ANNOUNCEMENT = (
    "declared warm carry-over across a recalibration: lower spin-up distortion "
    "than a cold start but not zero; the announcement obligation is retained"
)

# Six file categories for the eight-surface state-compatibility gate. No
# ``calibration``: 8-surface mode neither requires nor accepts it, so
# ``cfg.calib`` / ``CALIB/*`` are never enumerated.
#
# Deliberately NOT reusing ``provision_direct_grid_scheduler_registry::_category_files``:
# that builder derives relative paths from ONE root and falls its ``lake``
# category back to the package's ``*.cfg.para`` when the basin has no
# ``*.lake.*`` file. Under a recalibration ``cfg.para`` is exactly the file
# that changes, so a single-root enumeration would hash ``cfg.para`` into the
# ``lake`` surface and refuse the very case this mode exists to permit -- and
# would additionally false-pass a lake file REMOVED in ``M1'``, since an
# ``M1'``-derived list would never name it.
_STATE_COMPATIBILITY_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "mesh": ("*.sp.mesh",),
    "river": ("*.sp.riv", "*.sp.rivseg"),
    "lake": ("*.lake.*",),
    "soil": ("*.para.soil",),
    "geol": ("*.para.geol",),
    "land": ("*.para.lc",),
}


class CutoverCloneError(RuntimeError):
    pass


class _RefusalRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_refusal(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CutoverCloneError(f"JSON object required: {path}")
    return payload


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _source_for_variant(model: Mapping[str, Any]) -> str:
    profile = model.get("resource_profile")
    if not isinstance(profile, Mapping):
        raise CutoverCloneError(f"resource_profile missing for {model.get('model_id')}")
    source_id = profile.get("direct_grid_source_id")
    if not source_id:
        raise CutoverCloneError(f"direct_grid_source_id missing for {model.get('model_id')}")
    return normalize_source_id(str(source_id))


def _baseline_for_variant(model: Mapping[str, Any]) -> str:
    profile = model.get("resource_profile")
    if not isinstance(profile, Mapping) or not profile.get("baseline_model_id"):
        raise CutoverCloneError(f"baseline_model_id missing for {model.get('model_id')}")
    return str(profile["baseline_model_id"])


def _model_map(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    models = payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes):
        raise CutoverCloneError("registry models must be an array")
    return {str(model["model_id"]): dict(model) for model in models if isinstance(model, Mapping)}


def _variant_map(payload: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for model in _model_map(payload).values():
        key = (_baseline_for_variant(model), _source_for_variant(model))
        if key in result:
            raise CutoverCloneError(f"duplicate direct-grid variant for {key}")
        result[key] = model
    return result


def _package_root(store: LocalObjectStore, model: Mapping[str, Any]) -> Path:
    uri = str(model.get("model_package_uri") or "")
    if not uri:
        raise CutoverCloneError(f"model_package_uri missing for {model.get('model_id')}")
    root = store.resolve_path(uri)
    if not root.is_dir():
        raise CutoverCloneError(f"model package missing: {root}")
    return root


def _package_root_from_uri(store: LocalObjectStore, uri: str | None, *, identity: str) -> Path:
    if not uri:
        raise CutoverCloneError(f"model_package_version missing for {identity}")
    root = store.resolve_path(uri)
    if not root.is_dir():
        raise CutoverCloneError(f"state-producing model package missing for {identity}: {root}")
    return root



def _relative_matches(root: Path, patterns: Sequence[str]) -> set[str]:
    """Relative paths under ``root`` matching any of ``patterns`` (may be empty)."""

    matches: set[str] = set()
    for pattern in patterns:
        matches.update(
            str(path.relative_to(root)) for path in root.rglob(pattern) if path.is_file()
        )
    return matches


def _state_compatibility_category_files(m0_root: Path, m1_root: Path) -> dict[str, tuple[str, ...]]:
    """Six-category enumeration for the eight-surface gate, union of both roots.

    Each category's relative-path set is the UNION of what each root
    enumerates.  A file added on either side, or removed on either side, is
    therefore declared and the side lacking it raises
    ``MissingPackageFileError`` inside the gate -> the clone refuses with
    ``state_compatibility_unequal``.  A single-root enumeration would silently
    false-pass a REMOVED file, because the surviving root's listing would never
    name it.

    ``lake`` is the only category allowed to be empty on both roots -- many
    basins have no lake file at all.  ``_validate_category_files`` rejects an
    empty sequence, so the builder substitutes the package's ``*.sp.mesh``
    relative path as a non-empty placeholder.  ``sp.mesh`` is invariant under a
    recalibration and is already covered by the ``mesh`` surface, so the
    substitution adds no signal and removes none -- mesh drift still refuses
    via the ``mesh`` face.  It is visible in ``covered_paths`` as
    ``lake:<basin>.sp.mesh``: intended and auditable, not hidden.
    """

    category_files: dict[str, tuple[str, ...]] = {}
    for category, patterns in _STATE_COMPATIBILITY_CATEGORY_PATTERNS.items():
        union = _relative_matches(m0_root, patterns) | _relative_matches(m1_root, patterns)
        if not union:
            if category != "lake":
                raise CutoverCloneError(
                    f"no {category!r} file matching {list(patterns)!r} under either "
                    f"package root ({m0_root}, {m1_root})"
                )
            union = {str(_required_single(m1_root, "*.sp.mesh").relative_to(m1_root))}
        category_files[category] = tuple(sorted(union))
    return category_files


def _classify_direct_grid(root: Path, *, model_id: str) -> Mapping[str, Any]:
    """Return the model's ``direct_grid_forcing`` manifest section, or refuse.

    Both sides of a recalibration pair MUST classify direct-grid.  The target's
    classification is re-checked inside the clone (the SUB-7 no-reverse-clone
    guard); the source's is checked here so an operator typo cannot silently
    point the tool at a legacy model.
    """

    manifest = _read_json(root / "manifest.json")
    section = dict(manifest.get("direct_grid_forcing") or {})
    try:
        contract = load_forcing_mapping_contract_from_manifest(section)
    except DirectGridContractError as error:
        raise CutoverCloneError(
            f"model {model_id} does not classify as direct-grid: {error}"
        ) from error
    if contract is None:
        raise CutoverCloneError(f"model {model_id} does not classify as direct-grid")
    return section


def _parse_pairs(value: str) -> tuple[tuple[str, str], ...]:
    """Parse ``--pairs M1:M1prime,...`` into ordered ``(source, target)`` pairs."""

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in _csv(value):
        if item.count(":") != 1:
            raise CutoverCloneError(f"malformed --pairs entry {item!r}; expected <M1>:<M1prime>")
        source_model_id, target_model_id = (part.strip() for part in item.split(":"))
        if not source_model_id or not target_model_id:
            raise CutoverCloneError(f"malformed --pairs entry {item!r}; expected <M1>:<M1prime>")
        if source_model_id == target_model_id:
            raise CutoverCloneError(
                f"--pairs entry {item!r} names the same model on both sides; a "
                "recalibration carry-over requires a distinct target package"
            )
        pair = (source_model_id, target_model_id)
        if pair in seen:
            raise CutoverCloneError(f"duplicate --pairs entry {item!r}")
        seen.add(pair)
        pairs.append(pair)
    if not pairs:
        raise CutoverCloneError("--pairs declared no <M1>:<M1prime> pair")
    return tuple(pairs)


class _DryRunStateIndexRepository:
    """Read-through repository that runs the full gate but writes nothing.

    Dry run must exercise every validation AND the gate -- the point of a dry
    run is to learn whether the clone would be admitted -- so lookups delegate
    to the canonical repository verbatim and only the write is captured.
    ``FileStateSnapshotIndexRepository.upsert_state_snapshot`` returns its
    argument unchanged, so the captured row is identical to what an ``--apply``
    run would persist.
    """

    def __init__(self, inner: FileStateSnapshotIndexRepository) -> None:
        self._inner = inner
        self.captured: list[StateSnapshot] = []

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None:
        return self._inner.get_state_snapshot_by_model_time(
            model_id=model_id,
            valid_time=valid_time,
            source_id=source_id,
            cycle_id=cycle_id,
            lead_hours=lead_hours,
        )

    def get_latest_state_before(
        self,
        *,
        model_id: str,
        source_id: str,
        before_time: datetime,
    ) -> StateSnapshot | None:
        return self._inner.get_latest_state_before(
            model_id=model_id,
            source_id=source_id,
            before_time=before_time,
        )

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        self.captured.append(snapshot)
        return snapshot


def _write_receipt(path_value: str, receipt: Mapping[str, Any]) -> None:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(path, flags, 0o644), "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def run_recalibration(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the ``M1 -> M1'`` recalibration carry-over for every declared pair.

    Per pair, all fail-closed and all BEFORE any write: both model rows exist in
    the registry payload, both package roots resolve to directories, both
    classify direct-grid, both declare the same normalized
    ``direct_grid_source_id``, and ``M1 != M1'``.

    The gate runs ONCE, against the canonical repository; the same returned
    ``StateSnapshot`` object is then upserted into the mirror repository.
    Running the gate twice would risk two differing rows, and the copyback merge
    treats ``current == source_entry`` as a no-conflict replay -- byte-identical
    entries on both indexes are what keeps that branch reachable.
    """

    cutover_time = parse_cycle_time(args.cutover_time)
    pairs = _parse_pairs(args.pairs)
    models = _model_map(_read_json(Path(args.variant_registry)))
    if args.baseline_registry:
        # A pair may legally span two registry payloads (e.g. the pre-update
        # canonical registry and the freshly provisioned one). Merge rather than
        # choose, and refuse a model_id that disagrees between them.
        for model_id, model in _model_map(_read_json(Path(args.baseline_registry))).items():
            if models.setdefault(model_id, model) != model:
                raise CutoverCloneError(
                    f"model {model_id} differs between --variant-registry and --baseline-registry"
                )

    store = LocalObjectStore(
        root=Path(args.object_store_root),
        object_store_prefix=args.object_store_prefix,
    )
    canonical_repo = FileStateSnapshotIndexRepository(
        index_uri=args.state_index,
        object_store_root=args.object_store_root,
        object_store_prefix=args.object_store_prefix,
        create_missing=False,
    )
    mirror_repo = FileStateSnapshotIndexRepository(
        index_uri=args.mirror_state_index,
        object_store_root=args.object_store_root,
        object_store_prefix=args.object_store_prefix,
        create_missing=False,
    )

    pair_records: list[dict[str, Any]] = []
    mirror_failure: str | None = None

    for source_model_id, target_model_id in pairs:
        source_model = models.get(source_model_id)
        target_model = models.get(target_model_id)
        if source_model is None:
            raise CutoverCloneError(f"registry entry missing for --pairs source {source_model_id}")
        if target_model is None:
            raise CutoverCloneError(f"registry entry missing for --pairs target {target_model_id}")

        source_root = _package_root(store, source_model)
        target_root = _package_root(store, target_model)
        _classify_direct_grid(source_root, model_id=source_model_id)
        target_manifest_section = _classify_direct_grid(target_root, model_id=target_model_id)

        source_id = _source_for_variant(source_model)
        target_source_id = _source_for_variant(target_model)
        if source_id != target_source_id:
            raise CutoverCloneError(
                f"--pairs {source_model_id}:{target_model_id} spans two sources "
                f"({source_id} != {target_source_id}); a recalibration carry-over "
                "runs once per pair for one source"
            )

        source_sp_att = _required_single(source_root, "*.sp.att")
        target_sp_att = _required_single(target_root, "*.sp.att")
        category_files = _state_compatibility_category_files(source_root, target_root)
        # Per-side gate bytes: M1's own cfg.ic/cfg.para against M1's own. Feeding
        # one side's bytes to both sides would make the state_schema surface
        # compare equal even when M1' ships a new cfg.ic -- the exact false pass
        # this mode must not ship.
        source_state_schema_bytes = _required_single(source_root, "*.cfg.ic").read_bytes()
        target_state_schema_bytes = _required_single(target_root, "*.cfg.ic").read_bytes()
        source_solver_config_bytes = _required_single(source_root, "*.cfg.para").read_bytes()
        target_solver_config_bytes = _required_single(target_root, "*.cfg.para").read_bytes()

        recorder = _RefusalRecorder()
        gate_repo: Any = canonical_repo if args.apply else _DryRunStateIndexRepository(canonical_repo)
        result = fingerprint_gated_state_clone(
            m0_model_id=source_model_id,
            m1_model_id=target_model_id,
            m1_model_package_version=str(target_model["model_package_uri"]),
            m1_model_package_checksum=str(target_model["package_checksum"]),
            source_id=source_id,
            cutover_valid_time=cutover_time,
            m0_package_root=source_root,
            m1_package_root=target_root,
            m0_sp_att_path=source_sp_att,
            m1_sp_att_path=target_sp_att,
            m1_category_files=category_files,
            # D5: the direct-grid provisioning script records no
            # hydrologic_core_fingerprint anywhere, so there is no independent
            # recorded value to cross-check against. The waiver is explicit and
            # recorded in this receipt rather than satisfied vacuously by
            # handing the gate back the value it just computed.
            m1_recorded_hydrologic_core_fingerprint=None,
            state_schema_bytes=target_state_schema_bytes,
            solver_config_bytes=target_solver_config_bytes,
            m0_state_schema_bytes=source_state_schema_bytes,
            m0_solver_config_bytes=source_solver_config_bytes,
            m1_forcing_mapping_manifest=target_manifest_section,
            repository=gate_repo,
            audit_recorder=recorder,
            transfer_mode=TRANSFER_MODE_RECALIBRATION,
        )
        if result.refused or result.cloned_row is None:
            raise CutoverCloneError(
                f"state clone refused: {source_model_id}->{target_model_id}/{source_id}: "
                f"{result.refusal_scope}; audit={recorder.records}"
            )
        clone_row = result.cloned_row

        canonical_outcome: dict[str, Any] = {
            "index_uri": args.state_index,
            "outcome": "written" if args.apply else "dry_run_not_written",
            "error": None,
        }
        mirror_outcome: dict[str, Any] = {
            "index_uri": args.mirror_state_index,
            "outcome": "dry_run_not_written",
            "error": None,
        }
        if args.apply:
            try:
                mirror_repo.upsert_state_snapshot(clone_row)
            except Exception as error:  # noqa: BLE001 - reported, never swallowed
                mirror_outcome["outcome"] = "not_written"
                mirror_outcome["error"] = f"{type(error).__name__}: {error}"
                mirror_failure = (
                    f"{source_model_id}->{target_model_id}/{source_id}: "
                    f"{mirror_outcome['error']}"
                )
            else:
                mirror_outcome["outcome"] = "written"

        pair_records.append(
            {
                "source_model_id": source_model_id,
                "target_model_id": target_model_id,
                "source_id": source_id,
                "state_compatibility_fingerprint": clone_row.clone_gate_fingerprint,
                "clone_gate_kind": clone_row.clone_gate_kind,
                "evidence_fingerprint_cross_check": "skipped_no_recorded_value",
                "covered_paths": sorted(
                    f"{category}:{';'.join(paths)}" for category, paths in category_files.items()
                ),
                "source_model_package_version": str(source_model["model_package_uri"]),
                "source_model_package_checksum": str(source_model["package_checksum"]),
                "target_model_package_version": clone_row.model_package_version,
                "target_model_package_checksum": clone_row.model_package_checksum,
                "state_id": clone_row.state_id if args.apply else None,
                "cloned_from_state_id": clone_row.cloned_from_state_id,
                "cloned_from_model_id": clone_row.cloned_from_model_id,
                "state_index_outcomes": {
                    "canonical": canonical_outcome,
                    "mirror": mirror_outcome,
                },
            }
        )
        if mirror_failure is not None:
            # Fail closed on the first mirror divergence: continuing would widen
            # the window in which the two indexes disagree. The receipt below
            # names the canonical row as written and the mirror as not written so
            # the operator can repair the mirror before t*.
            break

    receipt = {
        "schema_version": RECALIBRATION_RECEIPT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "cutover_time": cutover_time.isoformat().replace("+00:00", "Z"),
        "transfer_mode": TRANSFER_MODE_RECALIBRATION,
        "clone_gate_kind": CLONE_GATE_KIND_STATE_COMPATIBILITY,
        "dry_run": not args.apply,
        "declared_pair_count": len(pairs),
        "cloned_pair_count": sum(
            record["state_index_outcomes"]["canonical"]["outcome"] == "written"
            for record in pair_records
        ),
        "state_index": args.state_index,
        "mirror_state_index": args.mirror_state_index,
        "evidence_fingerprint_cross_check": "skipped_no_recorded_value",
        "spin_up_distortion_announcement": RECALIBRATION_SPIN_UP_DISTORTION_ANNOUNCEMENT,
        "pairs": pair_records,
    }
    if args.receipt:
        _write_receipt(args.receipt, receipt)
    if mirror_failure is not None:
        raise CutoverCloneError(
            "mirror state-index write failed after the canonical write succeeded; "
            f"repair the mirror before t*: {mirror_failure}"
        )
    return receipt


def run(args: argparse.Namespace) -> dict[str, Any]:
    cutover_time = parse_cycle_time(args.cutover_time)
    warm_basins = frozenset(_csv(args.warm_basins))
    cold_basins = frozenset(_csv(args.cold_basins))
    if warm_basins & cold_basins:
        raise CutoverCloneError("warm/cold basin partitions overlap")
    if len(warm_basins) != args.expected_warm_count or len(cold_basins) != args.expected_cold_count:
        raise CutoverCloneError("warm/cold basin counts do not match the declared authority")

    baseline_models = _model_map(_read_json(Path(args.baseline_registry)))
    variants = _variant_map(_read_json(Path(args.variant_registry)))
    registry_basins = frozenset(baseline_id for baseline_id, _source_id in variants)
    if warm_basins | cold_basins != registry_basins:
        missing = sorted(registry_basins - warm_basins - cold_basins)
        extra = sorted((warm_basins | cold_basins) - registry_basins)
        raise CutoverCloneError(f"warm/cold partition mismatch: missing={missing}, extra={extra}")
    for basin_id in registry_basins:
        sources = {source_id for model_id, source_id in variants if model_id == basin_id}
        if sources != {"gfs", "IFS"}:
            raise CutoverCloneError(f"expected GFS/IFS variants for {basin_id}, got {sorted(sources)}")

    store = LocalObjectStore(
        root=Path(args.object_store_root),
        object_store_prefix=args.object_store_prefix,
    )
    file_repo = FileStateSnapshotIndexRepository(
        index_uri=args.state_index,
        object_store_root=args.object_store_root,
        object_store_prefix=args.object_store_prefix,
        create_missing=False,
    )
    decisions: list[dict[str, Any]] = []

    for baseline_id in sorted(registry_basins):
        baseline = baseline_models.get(baseline_id)
        if baseline is None:
            raise CutoverCloneError(f"baseline registry entry missing: {baseline_id}")
        for source_id in ("gfs", "IFS"):
            variant = variants[(baseline_id, source_id)]
            target_id = str(variant["model_id"])
            existing_file = file_repo.get_state_snapshot_by_model_time(
                model_id=target_id,
                source_id=source_id,
                valid_time=cutover_time,
            )
            if baseline_id in cold_basins:
                if existing_file is not None:
                    raise CutoverCloneError(
                        f"cold basin already has target state: {baseline_id}/{source_id}/{target_id}"
                    )
                decisions.append(
                    {
                        "basin_model_id": baseline_id,
                        "source_id": source_id,
                        "target_model_id": target_id,
                        "decision": "cold_new_basin",
                        "state_id": None,
                    }
                )
                continue

            source_state = file_repo.get_state_snapshot_by_model_time(
                model_id=baseline_id,
                source_id=source_id,
                valid_time=cutover_time,
            )
            if source_state is None or not source_state.usable_flag or source_state.lead_hours != 12:
                raise CutoverCloneError(f"qualified source state missing: {baseline_id}/{source_id}")
            baseline_root = _package_root_from_uri(
                store,
                source_state.model_package_version,
                identity=f"{baseline_id}/{source_id}/{source_state.state_id}",
            )
            variant_root = _package_root(store, variant)
            baseline_sp_att = _required_single(baseline_root, "*.sp.att")
            variant_sp_att = _required_single(variant_root, "*.sp.att")
            categories = _category_files(variant_root)
            state_schema_bytes = _required_single(baseline_root, "*.cfg.ic").read_bytes()
            solver_config_bytes = _required_single(baseline_root, "*.cfg.para").read_bytes()
            fingerprint = verify_hydrologic_core_fingerprint_equal(
                baseline_root,
                variant_root,
                baseline_sp_att_path=baseline_sp_att,
                variant_sp_att_path=variant_sp_att,
                category_files=categories,
                baseline_state_schema_bytes=state_schema_bytes,
                variant_state_schema_bytes=state_schema_bytes,
                baseline_solver_config_bytes=solver_config_bytes,
                variant_solver_config_bytes=solver_config_bytes,
            )
            if args.dry_run:
                state_id = None
            else:
                manifest = _read_json(variant_root / "manifest.json")
                recorder = _RefusalRecorder()
                result = fingerprint_gated_state_clone(
                    m0_model_id=baseline_id,
                    m1_model_id=target_id,
                    m1_model_package_version=str(variant["model_package_uri"]),
                    m1_model_package_checksum=str(variant["package_checksum"]),
                    source_id=source_id,
                    cutover_valid_time=cutover_time,
                    m0_package_root=baseline_root,
                    m1_package_root=variant_root,
                    m0_sp_att_path=baseline_sp_att,
                    m1_sp_att_path=variant_sp_att,
                    m1_category_files=categories,
                    m1_recorded_hydrologic_core_fingerprint=fingerprint.hash,
                    state_schema_bytes=state_schema_bytes,
                    solver_config_bytes=solver_config_bytes,
                    m1_forcing_mapping_manifest=dict(manifest.get("direct_grid_forcing") or {}),
                    repository=file_repo,
                    audit_recorder=recorder,
                )
                if result.refused or result.cloned_row is None:
                    raise CutoverCloneError(
                        f"state clone refused: {baseline_id}/{source_id}: "
                        f"{result.refusal_scope}; audit={recorder.records}"
                    )
                state_id = result.cloned_row.state_id
            decisions.append(
                {
                    "basin_model_id": baseline_id,
                    "source_id": source_id,
                    "target_model_id": target_id,
                    "decision": "warm_clone",
                    "state_id": state_id,
                    "hydrologic_core_fingerprint": fingerprint.hash,
                }
            )

    receipt = {
        "schema_version": "nhms.direct_grid_cutover_state_clone.v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "cutover_time": cutover_time.isoformat().replace("+00:00", "Z"),
        "dry_run": bool(args.dry_run),
        "warm_basin_count": len(warm_basins),
        "cold_basin_count": len(cold_basins),
        "warm_candidate_count": sum(item["decision"] == "warm_clone" for item in decisions),
        "cold_candidate_count": sum(item["decision"] == "cold_new_basin" for item in decisions),
        "decisions": decisions,
    }
    if args.receipt:
        _write_receipt(args.receipt, receipt)
    return receipt


# Per-mode required flags. Enforced AFTER parsing rather than through
# ``required=True`` / subparsers so the existing baseline_cutover invocation
# keeps working verbatim while the recalibration mode does not have to supply
# a warm/cold basin partition it has no concept of.
_REQUIRED_FLAGS_BY_MODE: dict[str, tuple[tuple[str, str], ...]] = {
    TRANSFER_MODE_BASELINE_CUTOVER: (
        ("baseline_registry", "--baseline-registry"),
        ("variant_registry", "--variant-registry"),
        ("warm_basins", "--warm-basins"),
        ("cold_basins", "--cold-basins"),
    ),
    TRANSFER_MODE_RECALIBRATION: (
        ("variant_registry", "--variant-registry"),
        ("pairs", "--pairs"),
        ("mirror_state_index", "--mirror-state-index"),
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-store-root", required=True)
    parser.add_argument("--object-store-prefix", default="s3://nhms")
    parser.add_argument("--state-index", required=True)
    parser.add_argument(
        "--transfer-mode",
        choices=(TRANSFER_MODE_BASELINE_CUTOVER, TRANSFER_MODE_RECALIBRATION),
        default=TRANSFER_MODE_BASELINE_CUTOVER,
    )
    parser.add_argument("--baseline-registry")
    parser.add_argument("--variant-registry")
    parser.add_argument("--cutover-time", required=True)
    parser.add_argument("--warm-basins")
    parser.add_argument("--cold-basins")
    parser.add_argument("--expected-warm-count", type=int, default=12)
    parser.add_argument("--expected-cold-count", type=int, default=6)
    parser.add_argument(
        "--pairs",
        help="recalibration only: <M1_model_id>:<M1prime_model_id>,... resolved by model_id",
    )
    parser.add_argument(
        "--mirror-state-index",
        help="recalibration only: the node-22-local scratch state-index mirror",
    )
    parser.add_argument("--receipt")
    parser.add_argument("--apply", action="store_true")
    return parser


def enforce_mode_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse a missing per-mode flag with the same message ``required=True`` gave."""

    missing = [
        flag
        for attribute, flag in _REQUIRED_FLAGS_BY_MODE[args.transfer_mode]
        if getattr(args, attribute, None) in (None, "")
    ]
    if missing:
        parser.error(
            f"--transfer-mode {args.transfer_mode} requires: {', '.join(missing)}"
        )


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.transfer_mode == TRANSFER_MODE_RECALIBRATION:
        return run_recalibration(args)
    return run(args)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    enforce_mode_flags(parser, args)
    args.dry_run = not args.apply
    print(json.dumps(dispatch(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
