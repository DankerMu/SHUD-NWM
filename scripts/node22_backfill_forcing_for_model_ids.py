#!/usr/bin/env python
"""Backfill per-model forcing after a direct-grid ``model_id`` change.

Forcing is stored per model:
``<forcing_root>/<source>/<cycle>/<basin_version_id>/<model_id>/``.  A republish
that changes a model's package content changes its ``dg_*`` identity, so every
already-produced cycle keeps its artifacts under the OLD id and the new model
has none.  The scheduler judges forcing completeness per CYCLE, so it will not
re-enter the forcing stage for such a cycle: the forecast is submitted and dies
on ``ARTIFACT_NOT_FOUND`` within seconds.

The fix is a *replay*, not a copy.  When the republish did not move any station
(the usual case -- a calibration-only or metadata-only package change), the
``station_bindings`` rows are physically identical and only the ``dg-<src>-…::``
identity prefix differs.  Re-running the producer under the new id therefore
yields numerically identical forcing with self-consistent ids and checksums,
through the same code path production uses.  Copying the old directory instead
would bake the old ``model_input_package_id`` / ``binding_uri`` / station ids
into every member file while ``met.met_station`` is registered under the new
binding identity.

This tool refuses to run when the bindings actually moved: a real re-binding is
not a backfill and must go through normal provisioning.

Execution host: node-22 (the forcing artifacts and raw/canonical inputs live on
its ``/scratch`` object store; the tool is DB-free).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.common.source_identity import normalize_source_id  # noqa: E402


def _object_source_segment(source_id: str) -> str:
    """The forcing path's source segment.

    Mirrors ``workers.forcing_producer.producer._object_source_segment``: the
    canonical id is lower-cased for the object key, so canonical ``IFS`` lives
    under ``forcing/ifs/``.  Keying the scan on the canonical id instead would
    silently find nothing for every upper-cased source.
    """

    return normalize_source_id(source_id).lower()

#: ``dg-<source>-<hex>`` model-input package ids, as they appear standalone and
#: as the ``…::cell:<n>`` station-id prefix.  Normalising these away is what
#: makes the old and new packages comparable.
_PACKAGE_ID_PATTERN = re.compile(rb"dg-[A-Za-z0-9]+-[0-9a-f]{8,}")
_MODEL_ID_PATTERN = re.compile(rb"dg_[0-9a-f]{32}")

#: Files whose bytes must match exactly.  ``shud/`` is what SHUD actually reads,
#: and it is keyed by filename, not by station id, so identity churn must not
#: touch it at all.
_EXACT_SUBDIR = "shud"

#: Data members that legitimately carry identity strings and are compared
#: normalised.  The three JSON manifests are deliberately NOT here: they carry
#: member checksums, which must differ once the members' identity strings do.
_NORMALISED_MEMBERS = (
    "forcing.tsd.forc",
    "forcing_debug.csv",
    "payloads/interp_weights.json",
    "payloads/station_inventory.json",
    "payloads/station_timeseries.json",
)


class BackfillError(RuntimeError):
    """A precondition of the backfill is not met."""

    def __init__(self, code: str, message: str, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})

    def as_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.message, "context": self.context}


@dataclass(frozen=True)
class ModelRow:
    model_id: str
    basin_version_id: str
    source_id: str
    sp_att_path: str
    station_bindings: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Rename:
    key: tuple[str, str]
    previous: ModelRow
    current: ModelRow


@dataclass
class WorkItem:
    source_id: str
    cycle: str
    basin_version_id: str
    previous_model_id: str
    model_id: str
    previous_dir: Path
    target_dir: Path
    status: str = "pending"
    #: True when ``target_dir`` already existed at discovery time and FAILED
    #: verification.  Such an item is reported, never silently skipped, and is
    #: only replaced under the explicit opt-in flag.
    existing_target: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "cycle": self.cycle,
            "basin_version_id": self.basin_version_id,
            "previous_model_id": self.previous_model_id,
            "model_id": self.model_id,
            "previous_dir": str(self.previous_dir),
            "target_dir": str(self.target_dir),
            "status": self.status,
            "existing_target": self.existing_target,
            **({"detail": self.detail} if self.detail else {}),
        }


#: Where an artifact that must not stay live is moved to.  The leading
#: underscore is load-bearing: a model directory is always ``dg_<32hex>``, so a
#: name under this directory can never be mistaken for one by the scheduler, by
#: this tool's own scan, or by an operator reading a listing.
_QUARANTINE_DIRNAME = "_backfill_quarantine"


def _quarantine_dir(item: WorkItem, *, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = item.target_dir.parent / _QUARANTINE_DIRNAME
    # The stamp has second resolution and `--jobs` runs several producers at
    # once, so disambiguate by pid and by a counter rather than trusting it.
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f".{attempt}"
        candidate = base / f"quarantined-{item.model_id}.{label}.{stamp}.pid{os.getpid()}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate a quarantine path under {base}")


def quarantine_target(item: WorkItem, *, label: str) -> str | None:
    """Move ``target_dir`` out of the live model path.

    The forecast stage reads ``<basin_version_id>/<model_id>/`` directly, so an
    artifact this tool produced and could not verify -- or the debris a
    producer left behind when it exited non-zero -- must not be left standing
    there.  Moving rather than deleting keeps it available for diagnosis.
    """

    if not item.target_dir.is_dir():
        return None
    try:
        destination = _quarantine_dir(item, label=label)
        destination.parent.mkdir(parents=True, exist_ok=True)
        item.target_dir.rename(destination)
    except (OSError, RuntimeError) as error:
        item.detail["quarantine_error"] = f"{type(error).__name__}: {error}"
        return None
    return str(destination)


def _load_models(path: Path) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BackfillError(
            "BACKFILL_MANIFEST_UNREADABLE",
            f"Registry manifest could not be read: {error}",
            {"path": str(path)},
        ) from error
    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, list):
        raise BackfillError(
            "BACKFILL_MANIFEST_SHAPE_INVALID",
            "Registry manifest has no 'models' list.",
            {"path": str(path)},
        )
    return models


def _index_direct_grid(models: Iterable[Mapping[str, Any]], *, path: Path) -> dict[tuple[str, str], ModelRow]:
    index: dict[tuple[str, str], ModelRow] = {}
    for model in models:
        profile = model.get("resource_profile") or {}
        direct_grid = profile.get("direct_grid_forcing")
        if not isinstance(direct_grid, Mapping):
            continue
        source_ids = direct_grid.get("applicable_source_ids") or []
        if len(source_ids) != 1:
            raise BackfillError(
                "BACKFILL_MODEL_SOURCE_AMBIGUOUS",
                "A direct-grid model must declare exactly one applicable source id.",
                {"path": str(path), "model_id": model.get("model_id"), "applicable_source_ids": list(source_ids)},
            )
        source_id = normalize_source_id(str(source_ids[0]))
        sp_att_path = str(direct_grid.get("sp_att_path") or "")
        if not sp_att_path:
            raise BackfillError(
                "BACKFILL_MODEL_SP_ATT_MISSING",
                "A direct-grid model must declare sp_att_path; it is the basin key for pairing.",
                {"path": str(path), "model_id": model.get("model_id")},
            )
        key = (sp_att_path, source_id)
        if key in index:
            raise BackfillError(
                "BACKFILL_MODEL_KEY_DUPLICATE",
                "Two direct-grid models share one (sp_att_path, source_id) key; pairing would be arbitrary.",
                {"path": str(path), "key": list(key), "model_ids": [index[key].model_id, model.get("model_id")]},
            )
        index[key] = ModelRow(
            model_id=str(model.get("model_id") or ""),
            basin_version_id=str(model.get("basin_version_id") or ""),
            source_id=source_id,
            sp_att_path=sp_att_path,
            station_bindings=tuple(direct_grid.get("station_bindings") or ()),
        )
    return index


def _binding_signature(rows: Sequence[Mapping[str, Any]]) -> str:
    """Station rows with the identity prefix removed.

    Everything physical -- ``grid_cell_id``, coordinates, ``shud_forcing_index``,
    ``forcing_filename`` -- survives; only ``dg-<src>-<hex>`` disappears.
    """

    normalised = []
    for row in rows:
        item = dict(row)
        station_id = str(item.get("station_id") or "")
        item["station_id"] = _PACKAGE_ID_PATTERN.sub(b"<pkg>", station_id.encode("utf-8")).decode("utf-8")
        normalised.append(item)
    normalised.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return json.dumps(normalised, sort_keys=True)


def resolve_renames(previous: Path, current: Path) -> tuple[list[Rename], list[dict[str, Any]]]:
    """Pair the two manifests and return the (model_id changed) rows.

    Refusals, not warnings: a changed key set means the topology moved, and a
    changed binding signature means the stations moved.  Neither is a backfill.
    """

    previous_index = _index_direct_grid(_load_models(previous), path=previous)
    current_index = _index_direct_grid(_load_models(current), path=current)
    if set(previous_index) != set(current_index):
        raise BackfillError(
            "BACKFILL_MANIFEST_KEYSET_DIVERGED",
            "The two manifests do not describe the same (sp_att_path, source_id) set; "
            "this is an onboarding/retirement diff, not an identity change.",
            {
                "only_previous": sorted(list(key) for key in set(previous_index) - set(current_index)),
                "only_current": sorted(list(key) for key in set(current_index) - set(previous_index)),
            },
        )
    renames: list[Rename] = []
    rebindings: list[dict[str, Any]] = []
    for key in sorted(previous_index):
        before, after = previous_index[key], current_index[key]
        if before.model_id == after.model_id:
            continue
        if _binding_signature(before.station_bindings) != _binding_signature(after.station_bindings):
            rebindings.append(
                {
                    "key": list(key),
                    "previous_model_id": before.model_id,
                    "model_id": after.model_id,
                    "previous_station_count": len(before.station_bindings),
                    "station_count": len(after.station_bindings),
                }
            )
            continue
        if before.basin_version_id != after.basin_version_id:
            raise BackfillError(
                "BACKFILL_BASIN_VERSION_CHANGED",
                "A renamed model changed basin_version_id; its forcing path root moved and a replay "
                "would not land where the old artifacts are.",
                {"key": list(key), "previous": before.basin_version_id, "current": after.basin_version_id},
            )
        renames.append(Rename(key=key, previous=before, current=after))
    return renames, rebindings


def _source_dir(rename: Rename, forcing_root: Path) -> Path:
    return forcing_root / _object_source_segment(rename.current.source_id)


def probe_coverage(renames: Sequence[Rename], forcing_root: Path, cycles: Sequence[str]) -> dict[str, Any]:
    """What the scan was pointed at, and what of it actually exists.

    ``renamed_model_count: N, work_item_count: 0`` is the steady state of a
    fully-covered rerun AND the signature of a wrong ``--forcing-root`` (or an
    unmounted NFS share, or the wrong environment), so it cannot discriminate
    the two.  This records the probe itself, which can.
    """

    probed = sorted({str(_source_dir(rename, forcing_root)) for rename in renames})
    found = sorted(path for path in probed if Path(path).is_dir())
    previous_dirs_found = 0
    for rename in renames:
        source_dir = _source_dir(rename, forcing_root)
        if not source_dir.is_dir():
            continue
        candidates = sorted(cycles) if cycles else sorted(p.name for p in source_dir.iterdir() if p.is_dir())
        for cycle in candidates:
            if (source_dir / cycle / rename.current.basin_version_id / rename.previous.model_id).is_dir():
                previous_dirs_found += 1
    return {
        "forcing_root": str(forcing_root),
        "forcing_root_is_dir": forcing_root.is_dir(),
        "cycle_filter": sorted(cycles),
        "renames_probed": len(renames),
        "source_dirs_probed": probed,
        "source_dirs_found": found,
        "previous_model_dirs_found": previous_dirs_found,
    }


def require_coverage(coverage: Mapping[str, Any]) -> None:
    """Fail closed on total under-coverage.

    Partial under-coverage stays visible in the receipt rather than fatal --
    a cycle filter legitimately narrows the scan -- but "the tool looked and
    found literally nothing" must never be reported as "nothing to do".
    """

    if not coverage["renames_probed"]:
        return
    if not coverage["forcing_root_is_dir"]:
        raise BackfillError(
            "BACKFILL_FORCING_ROOT_ABSENT",
            "--forcing-root is not a directory; nothing could be scanned.",
            dict(coverage),
        )
    if not coverage["source_dirs_found"]:
        raise BackfillError(
            "BACKFILL_FORCING_ROOT_UNCOVERED",
            "No renamed model's source directory exists under --forcing-root; the root, the mount, or "
            "the environment is wrong. Refusing rather than reporting zero work items.",
            dict(coverage),
        )


def discover_work(renames: Sequence[Rename], forcing_root: Path, cycles: Sequence[str]) -> list[WorkItem]:
    """Cycles that have the old id's artifacts but not a VERIFIED new one.

    An existing ``target_dir`` is only skipped when it passes the same
    acceptance oracle a fresh replay must pass.  Producer writes are atomic per
    file, never per directory, so a producer killed mid-write leaves the
    directory present holding only some members; gating on ``is_dir()`` alone
    would drop that (cycle, model_id) out of the receipt forever, looking
    exactly like a correct backfill.
    """

    items: list[WorkItem] = []
    for rename in renames:
        source_dir = _source_dir(rename, forcing_root)
        if not source_dir.is_dir():
            continue
        candidates = sorted(cycles) if cycles else sorted(p.name for p in source_dir.iterdir() if p.is_dir())
        for cycle in candidates:
            basin_dir = source_dir / cycle / rename.current.basin_version_id
            previous_dir = basin_dir / rename.previous.model_id
            target_dir = basin_dir / rename.current.model_id
            if not previous_dir.is_dir():
                continue
            item = WorkItem(
                source_id=rename.current.source_id,
                cycle=cycle,
                basin_version_id=rename.current.basin_version_id,
                previous_model_id=rename.previous.model_id,
                model_id=rename.current.model_id,
                previous_dir=previous_dir,
                target_dir=target_dir,
            )
            if target_dir.is_dir():
                try:
                    verification = verify_item(item)
                except Exception as error:  # noqa: BLE001 - an unreadable member is a finding, not a crash
                    verification = {"verified": False, "reason": f"{type(error).__name__}: {error}"}
                if verification.get("verified"):
                    continue
                item.existing_target = True
                item.status = "existing_target_unverified"
                item.detail["verification"] = verification
            items.append(item)
    return items


def _normalise(payload: bytes) -> bytes:
    return _MODEL_ID_PATTERN.sub(b"<model>", _PACKAGE_ID_PATTERN.sub(b"<pkg>", payload))


def verify_item(item: WorkItem) -> dict[str, Any]:
    """Compare the replayed package against the old one.

    Free acceptance oracle: because the bindings are physically identical, the
    replay must reproduce the old package exactly once identity strings are
    normalised away -- and the ``shud/`` station CSVs, which carry no identity,
    must match byte for byte.
    """

    if not item.target_dir.is_dir():
        return {"verified": False, "reason": "target directory was not created"}
    previous_shud = item.previous_dir / _EXACT_SUBDIR
    target_shud = item.target_dir / _EXACT_SUBDIR
    previous_names = sorted(p.name for p in previous_shud.iterdir()) if previous_shud.is_dir() else []
    target_names = sorted(p.name for p in target_shud.iterdir()) if target_shud.is_dir() else []
    if previous_names != target_names:
        return {
            "verified": False,
            "reason": "shud station file set differs",
            "only_previous": sorted(set(previous_names) - set(target_names))[:10],
            "only_target": sorted(set(target_names) - set(previous_names))[:10],
        }
    mismatched_exact = []
    for name in previous_names:
        try:
            differs = (previous_shud / name).read_bytes() != (target_shud / name).read_bytes()
        except OSError as error:
            # A member we cannot read is a member we cannot accept.  Raising
            # here would cost the whole receipt, not just this item.
            mismatched_exact.append(f"{name} (unreadable: {type(error).__name__})")
            continue
        if differs:
            mismatched_exact.append(name)
    mismatched_normalised = []
    for name in _NORMALISED_MEMBERS:
        previous_member, target_member = item.previous_dir / name, item.target_dir / name
        if not previous_member.is_file() or not target_member.is_file():
            mismatched_normalised.append(f"{name} (missing)")
            continue
        try:
            differs = _normalise(previous_member.read_bytes()) != _normalise(target_member.read_bytes())
        except OSError as error:
            mismatched_normalised.append(f"{name} (unreadable: {type(error).__name__})")
            continue
        if differs:
            mismatched_normalised.append(name)
    verified = not mismatched_exact and not mismatched_normalised
    return {
        "verified": verified,
        "shud_files_compared": len(previous_names),
        "shud_files_mismatched": mismatched_exact[:10],
        "normalised_members_mismatched": mismatched_normalised,
    }


def run_item(
    item: WorkItem,
    producer_argv: Sequence[str],
    *,
    dry_run: bool,
    replace_unverified_target: bool = False,
) -> None:
    """Replay one work item, and never raise.

    A per-item fault costs that item's status, not the whole receipt: the loop
    that calls this has no other record of the items that already finished.
    """

    try:
        _run_item(item, producer_argv, dry_run=dry_run, replace_unverified_target=replace_unverified_target)
    except Exception as error:  # noqa: BLE001 - deliberate: the receipt must survive any item
        item.status = "errored"
        item.detail["error"] = f"{type(error).__name__}: {error}"


def _run_item(
    item: WorkItem,
    producer_argv: Sequence[str],
    *,
    dry_run: bool,
    replace_unverified_target: bool,
) -> None:
    if item.existing_target:
        if not replace_unverified_target:
            # Reported, non-zero, and left exactly as found: this artifact was
            # not written by this run, so destroying it is the operator's call.
            item.status = "existing_target_unverified"
            return
        if dry_run:
            item.status = "dry_run"
            item.detail["would_replace_target"] = True
            return
        quarantined = quarantine_target(item, label="replaced_unverified")
        item.detail["replaced_target"] = True
        item.detail["replaced_target_quarantine_path"] = quarantined
    argv = [
        *producer_argv,
        "produce",
        "--source-id",
        item.source_id,
        "--cycle-time",
        item.cycle,
        "--model-id",
        item.model_id,
    ]
    item.detail["command"] = argv
    if dry_run:
        item.status = "dry_run"
        return
    completed = subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True, check=False)
    item.detail["returncode"] = completed.returncode
    item.detail["stdout_tail"] = completed.stdout[-2000:]
    item.detail["stderr_tail"] = completed.stderr[-2000:]
    if completed.returncode != 0:
        item.status = "produce_failed"
        # A producer that died mid-write leaves a partial package standing on
        # the path the forecast stage reads.  Move it out of the way.
        item.detail["quarantine_path"] = quarantine_target(item, label="produce_failed")
        return
    verification = verify_item(item)
    item.detail["verification"] = verification
    if verification.get("verified"):
        item.status = "verified"
        return
    item.status = "verification_failed"
    item.detail["quarantine_path"] = quarantine_target(item, label="verification_failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--previous-manifest", required=True, type=Path, help="Registry manifest before the id change.")
    parser.add_argument("--current-manifest", required=True, type=Path, help="Registry manifest after the id change.")
    parser.add_argument("--forcing-root", required=True, type=Path, help="Object-store forcing root.")
    parser.add_argument(
        "--cycle",
        action="append",
        default=[],
        dest="cycles",
        help="Restrict to this cycle (repeatable). Default: every cycle that has the old id's artifacts.",
    )
    parser.add_argument(
        "--producer",
        default=f"{sys.executable} -m workers.forcing_producer.cli",
        help="Producer CLI invocation prefix.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually run the producer. Default is a dry run.")
    parser.add_argument(
        "--replace-unverified-target",
        action="store_true",
        help="DESTRUCTIVE. For a target directory that already exists but does NOT pass verification (a "
        "producer killed mid-write leaves one), remove it from the live model path and re-produce it from "
        "scratch. Without this flag such an item is reported as 'existing_target_unverified', left exactly "
        "as found, and the command exits non-zero. The removed directory is moved to "
        f"<basin_version_id>/{_QUARANTINE_DIRNAME}/ rather than deleted outright.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Run this many producer invocations at once. Each writes its own model directory, so they do not "
        "contend; keep it well under the host's core count.",
    )
    parser.add_argument("--output", type=Path, default=None, help="Write the receipt JSON here.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        renames, rebindings = resolve_renames(args.previous_manifest, args.current_manifest)
        coverage = probe_coverage(renames, args.forcing_root, args.cycles)
        require_coverage(coverage)
        items = discover_work(renames, args.forcing_root, args.cycles)
    except BackfillError as error:
        json.dump(error.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 1

    producer_argv = args.producer.split()
    if args.jobs < 1:
        json.dump(
            BackfillError("BACKFILL_JOBS_INVALID", "--jobs must be at least 1.", {"jobs": args.jobs}).as_dict(),
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 1
    # Nothing between here and the receipt write may cost the receipt: it is
    # the only record of what the completed items did.
    loop_error: str | None = None
    try:
        if args.jobs == 1 or not args.execute:
            for item in items:
                run_item(
                    item,
                    producer_argv,
                    dry_run=not args.execute,
                    replace_unverified_target=args.replace_unverified_target,
                )
        else:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                list(
                    pool.map(
                        lambda item: run_item(
                            item,
                            producer_argv,
                            dry_run=False,
                            replace_unverified_target=args.replace_unverified_target,
                        ),
                        items,
                    )
                )
    except Exception as error:  # noqa: BLE001 - the receipt is written either way
        loop_error = f"{type(error).__name__}: {error}"

    statuses: dict[str, int] = {}
    for item in items:
        statuses[item.status] = statuses.get(item.status, 0) + 1
    receipt = {
        "previous_manifest": str(args.previous_manifest),
        "current_manifest": str(args.current_manifest),
        "forcing_root": str(args.forcing_root),
        "executed": bool(args.execute),
        "replace_unverified_target": bool(args.replace_unverified_target),
        "coverage": coverage,
        "renamed_model_count": len(renames),
        "renames": [
            {
                "key": list(rename.key),
                "previous_model_id": rename.previous.model_id,
                "model_id": rename.current.model_id,
            }
            for rename in renames
        ],
        "rebound_models_skipped": rebindings,
        "work_item_count": len(items),
        "status_counts": statuses,
        "work_items": [item.as_dict() for item in items],
        **({"loop_error": loop_error} if loop_error else {}),
    }
    text = json.dumps(receipt, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")

    if rebindings or loop_error:
        return 1
    failed = sum(
        statuses.get(status, 0)
        for status in ("produce_failed", "verification_failed", "existing_target_unverified", "errored")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
