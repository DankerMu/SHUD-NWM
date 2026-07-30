#!/usr/bin/env python3
"""Read-only audit: did each basin's FIRST cycle consume its packaged calibrated IC?

Issue #1164.  For every registered model x forecast source this tool reconciles
two independent facts and writes a schema-versioned receipt:

1. **Packaged-IC qualification** — read from the model package manifest
   referenced by the registry row (``resource_profile.manifest_uri``) using the
   same criteria the scheduler gate applies at planning time
   (``services.orchestrator.scheduler_generation.classify_packaged_initial_condition``):
   an ``included_files`` entry whose ``relative_path`` ends with ``.cfg.ic``,
   whose ``sha256`` is not the empty-file digest, and whose ``size_bytes`` is
   positive.
2. **What the earliest business run actually did** — the earliest cycle's run
   manifest ``initial_state.quality`` and ``runtime.init_mode``, discovered from
   the workspace lane (``{workspace_root}/runs/``) and the object-store lane
   (``{object_store_root}/runs/``).

Each row lands on one of four verdicts:

``consumed_package_ic``
    First run declared ``packaged_calibrated_state`` with ``init_mode=3``.
``cold_start_with_qualified_ic``
    **The #1164 defect**: the package shipped a qualified calibrated IC and the
    first run still cold-started (``init_mode=1``).
``cold_start_no_ic``
    First run cold-started and the package genuinely ships no usable IC.
``undetermined``
    Evidence is missing or does not describe either case (no run manifest found,
    the package manifest is unreadable, or the first run warm-started from a
    state snapshot rather than bootstrapping).

Read-only by construction: every filesystem access is a no-follow read or a
bounded directory listing, and the ONLY thing written is the receipt at
``--receipt-path``.  No production state, package, index, or journal content is
modified.

Limits recorded in the receipt: package objects are NOT re-hashed — qualification
trusts the digests recorded in the package manifest (NFS IO budget).  A package
manifest whose recorded digest disagrees with the object on disk is caught at run
time by the runtime's end-to-end checksum verification, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import jsonschema

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    list_directory_no_follow_limited,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id
from services.orchestrator.scheduler_generation import (
    PACKAGED_IC_QUALIFIED,
    PACKAGED_IC_QUALITY,
    PACKAGED_IC_UNQUALIFIED,
    PACKAGED_IC_UNREADABLE,
    classify_packaged_initial_condition,
)

SCHEMA_VERSION = "nhms.first_cycle_initial_state_audit.v1"
_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA_PATH = _ROOT / "schemas/first_cycle_initial_state_audit_receipt.schema.json"

#: Canonical source identities (``normalize_source_id`` is deliberately
#: asymmetric — ``gfs`` stays lower-case while ``IFS`` is upper-case — so the
#: default list is normalized here rather than written by hand.
DEFAULT_SOURCES = tuple(normalize_source_id(source) for source in ("gfs", "ifs"))

MAX_REGISTRY_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RUN_MANIFEST_BYTES = 8 * 1024 * 1024
#: Bound on ``runs/`` fan-out per lane.  Production carries 18 models x 2 sources
#: x cycles; the bound exists so a runaway directory cannot stall the audit.
MAX_RUN_DIRECTORY_ENTRIES = 200_000
MAX_REGISTRY_MODELS = 10_000

VERDICTS = (
    "consumed_package_ic",
    "cold_start_with_qualified_ic",
    "cold_start_no_ic",
    "undetermined",
)

REFUSAL_REASONS = ("CONFIG_INVALID", "REGISTRY_UNREADABLE", "RESOURCE_BOUND_EXCEEDED", "RECEIPT_INVALID")

WORKSPACE_LANE = "workspace_runs"
OBJECT_STORE_LANE = "object_store_runs"

#: ``fcst_<source>_<YYYYMMDDHH>_<model_id>`` — the canonical forecast run id.
_RUN_ID_RE = re.compile(r"^fcst_(?P<source>[a-z0-9]+)_(?P<cycle>\d{10})_(?P<model_id>[A-Za-z0-9_.-]+)$")


class AuditBlocked(RuntimeError):
    """Raised when the audit cannot be completed against trustworthy inputs."""

    def __init__(self, message: str, *, reason: str = "REGISTRY_UNREADABLE") -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason: {reason}")
        super().__init__(message)
        self.reason = reason


class AuditConfigError(AuditBlocked):
    def __init__(self, message: str) -> None:
        super().__init__(message, reason="CONFIG_INVALID")


# ---------------------------------------------------------------------------
# Verdict table (pure)
# ---------------------------------------------------------------------------


def classify_verdict(
    *,
    ic_qualified: bool | None,
    first_run_quality: str | None,
    first_run_init_mode: int | None,
) -> str:
    """Return the verdict for one model x source row.

    Total and order-sensitive:

    1. No run evidence at all → ``undetermined`` (never guess from the package).
    2. The run declared a packaged bootstrap → ``consumed_package_ic``.
    3. ``init_mode == 1`` (a cold start) → the defect verdict when the package
       shipped a qualified IC, otherwise ``cold_start_no_ic``.  An unknown
       qualification (unreadable package manifest) stays ``undetermined`` rather
       than being reported as either.
    4. Anything else — notably a first run that warm-started from a state
       snapshot (``init_mode == 3`` with a snapshot quality) — is
       ``undetermined``: it is neither a package consumption nor a cold start.
    """
    if first_run_quality is None and first_run_init_mode is None:
        return "undetermined"
    if str(first_run_quality or "") == PACKAGED_IC_QUALITY:
        return "consumed_package_ic"
    if first_run_init_mode == 1:
        if ic_qualified is None:
            return "undetermined"
        return "cold_start_with_qualified_ic" if ic_qualified else "cold_start_no_ic"
    return "undetermined"


# ---------------------------------------------------------------------------
# Bounded read-only IO
# ---------------------------------------------------------------------------


def _read_json_no_follow(path: Path, *, max_bytes: int, containment_root: Path) -> Any:
    content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=containment_root)
    if len(content) > max_bytes:
        raise AuditBlocked(f"{path} exceeds the bounded read limit", reason="RESOURCE_BOUND_EXCEEDED")
    return json.loads(content)


def _is_regular_file(path: Path, *, containment_root: Path) -> bool:
    try:
        return stat.S_ISREG(stat_no_follow(path, containment_root=containment_root).st_mode)
    except (FileNotFoundError, SafeFilesystemError, OSError):
        return False


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    """Resolve an object reference to a store-relative key.

    Mirrors the runtime's ``_object_key`` so the audit resolves
    ``s3://<prefix>/<key>`` exactly like the readers whose evidence it audits.
    """
    candidate = str(uri_or_key).strip()
    prefix = (object_store_prefix or "").rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")


def _safe_relative_key(key: str) -> Path | None:
    """Return ``key`` as a relative path, or ``None`` when it escapes the store."""
    candidate = Path(key)
    if candidate.is_absolute() or any(part in ("..", "") for part in candidate.parts):
        return None
    return candidate


# ---------------------------------------------------------------------------
# Registry + package manifest
# ---------------------------------------------------------------------------


def load_registered_models(registry_manifest: Path) -> list[dict[str, Any]]:
    """Return the registry manifest's model rows (read-only, bounded)."""
    if not _is_regular_file(registry_manifest, containment_root=registry_manifest.parent):
        raise AuditBlocked(f"registry manifest is not a readable regular file: {registry_manifest}")
    try:
        payload = _read_json_no_follow(
            registry_manifest,
            max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            containment_root=registry_manifest.parent,
        )
    except AuditBlocked:
        raise
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise AuditBlocked(f"registry manifest is unreadable: {error}") from error
    if not isinstance(payload, Mapping):
        raise AuditBlocked("registry manifest is not a JSON object")
    models = payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes):
        raise AuditBlocked("registry manifest carries no models array")
    if len(models) > MAX_REGISTRY_MODELS:
        raise AuditBlocked("registry manifest model count exceeds the audit bound", reason="RESOURCE_BOUND_EXCEEDED")
    rows: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, Mapping):
            continue
        model_id = str(model.get("model_id") or "").strip()
        if not model_id:
            continue
        resource_profile = model.get("resource_profile")
        manifest_uri = ""
        if isinstance(resource_profile, Mapping):
            manifest_uri = str(resource_profile.get("manifest_uri") or "").strip()
        if not manifest_uri:
            manifest_uri = str(model.get("manifest_uri") or "").strip()
        rows.append({"model_id": model_id, "manifest_uri": manifest_uri})
    return sorted(rows, key=lambda row: row["model_id"])


def packaged_ic_qualification(
    manifest_uri: str,
    *,
    object_store_root: Path,
    object_store_prefix: str,
) -> dict[str, Any]:
    """Return the packaged-IC qualification view for one registry row.

    ``ic_status`` is ``absent`` when the registry publishes no manifest
    reference at all (the legacy carve-out the scheduler also honours),
    ``unreadable`` when the reference cannot be read or parsed, and otherwise
    the classifier's own verdict.  ``ic_qualified`` is tri-state: ``None`` means
    "cannot tell", which the verdict table refuses to collapse into either
    defect or clean.
    """
    if not manifest_uri:
        return {
            "ic_status": "absent",
            "ic_qualified": None,
            "ic_sha256": None,
            "ic_relative_path": None,
            "detail": "registry row publishes no package manifest reference",
        }
    key = _safe_relative_key(_object_key(manifest_uri, object_store_prefix))
    if key is None:
        return {
            "ic_status": PACKAGED_IC_UNREADABLE,
            "ic_qualified": None,
            "ic_sha256": None,
            "ic_relative_path": None,
            "detail": "package manifest reference escapes the object-store root",
        }
    path = object_store_root / key
    if not _is_regular_file(path, containment_root=object_store_root):
        return {
            "ic_status": PACKAGED_IC_UNREADABLE,
            "ic_qualified": None,
            "ic_sha256": None,
            "ic_relative_path": None,
            "detail": "package manifest object is missing or not a regular file",
        }
    try:
        payload = _read_json_no_follow(
            path, max_bytes=MAX_PACKAGE_MANIFEST_BYTES, containment_root=object_store_root
        )
    except (
        AuditBlocked,
        OSError,
        SafeFilesystemError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        return {
            "ic_status": PACKAGED_IC_UNREADABLE,
            "ic_qualified": None,
            "ic_sha256": None,
            "ic_relative_path": None,
            "detail": "package manifest could not be read or parsed",
        }
    signal = classify_packaged_initial_condition(payload)
    qualified: bool | None
    if signal.status == PACKAGED_IC_QUALIFIED:
        qualified = True
    elif signal.status == PACKAGED_IC_UNQUALIFIED:
        qualified = False
    else:
        qualified = None
    view: dict[str, Any] = {
        "ic_status": signal.status,
        "ic_qualified": qualified,
        "ic_sha256": signal.ic_sha256 or None,
        "ic_relative_path": signal.ic_relative_path or None,
    }
    if signal.detail:
        view["detail"] = signal.detail
    return view


# ---------------------------------------------------------------------------
# Earliest business run evidence
# ---------------------------------------------------------------------------


def _run_lane_candidates(
    runs_root: Path,
    *,
    source: str,
    model_id: str,
) -> list[tuple[str, str]]:
    """Return ``(cycle, run_id)`` pairs for ``source``/``model_id`` under ``runs_root``."""
    try:
        names = list_directory_no_follow_limited(runs_root, max_entries=MAX_RUN_DIRECTORY_ENTRIES)
    except (FileNotFoundError, NotADirectoryError, SafeFilesystemError, OSError):
        return []
    candidates: list[tuple[str, str]] = []
    for name in names:
        match = _RUN_ID_RE.match(name)
        if match is None:
            continue
        if match.group("model_id") != model_id:
            continue
        if normalize_source_id(match.group("source")) != source:
            continue
        candidates.append((match.group("cycle"), name))
    return sorted(candidates)


def earliest_run_evidence(
    *,
    model_id: str,
    source: str,
    object_store_root: Path,
    workspace_root: Path | None,
) -> dict[str, Any]:
    """Return the earliest business run's initial-state evidence for one row.

    Both lanes are enumerated and the globally earliest cycle wins, so a
    workspace-only first cycle is not shadowed by a later object-store run.  A
    cycle whose manifest cannot be read is skipped and the next cycle is
    considered — the row is only ``undetermined`` when NO lane yields readable
    evidence.
    """
    lanes: list[tuple[str, Path]] = [(OBJECT_STORE_LANE, object_store_root / "runs")]
    if workspace_root is not None:
        lanes.append((WORKSPACE_LANE, workspace_root / "runs"))
    candidates: list[tuple[str, str, str, Path]] = []
    for lane, runs_root in lanes:
        containment_root = object_store_root if lane == OBJECT_STORE_LANE else workspace_root
        assert containment_root is not None
        for cycle, run_id in _run_lane_candidates(runs_root, source=source, model_id=model_id):
            candidates.append((cycle, lane, run_id, containment_root))
    empty = {
        "first_cycle": None,
        "first_run_id": None,
        "first_run_quality": None,
        "first_run_init_mode": None,
        "first_run_evidence_lane": None,
    }
    if not candidates:
        return empty
    # Earliest cycle first; the object-store lane wins ties (it is the durable
    # face) because ``OBJECT_STORE_LANE`` sorts before ``WORKSPACE_LANE``.
    for cycle, lane, run_id, containment_root in sorted(candidates):
        runs_root = containment_root / "runs"
        manifest_path = runs_root / run_id / "input" / "manifest.json"
        if not _is_regular_file(manifest_path, containment_root=containment_root):
            continue
        try:
            payload = _read_json_no_follow(
                manifest_path, max_bytes=MAX_RUN_MANIFEST_BYTES, containment_root=containment_root
            )
        except (
            AuditBlocked,
            OSError,
            SafeFilesystemError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ):
            continue
        if not isinstance(payload, Mapping):
            continue
        initial_state = payload.get("initial_state")
        runtime = payload.get("runtime")
        quality = None
        if isinstance(initial_state, Mapping):
            raw_quality = initial_state.get("quality")
            quality = str(raw_quality) if raw_quality not in (None, "") else None
        init_mode: int | None = None
        if isinstance(runtime, Mapping):
            try:
                raw_init_mode = runtime.get("init_mode")
                init_mode = int(raw_init_mode) if raw_init_mode not in (None, "") else None
            except (TypeError, ValueError):
                init_mode = None
        return {
            "first_cycle": cycle,
            "first_run_id": run_id,
            "first_run_quality": quality,
            "first_run_init_mode": init_mode,
            "first_run_evidence_lane": lane,
        }
    return empty


# ---------------------------------------------------------------------------
# Receipt assembly
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    registry_manifest: Path,
    object_store_root: Path,
    object_store_prefix: str,
    workspace_root: Path | None,
    sources: Sequence[str],
    generated_at: datetime,
) -> dict[str, Any]:
    models = load_registered_models(registry_manifest)
    rows: list[dict[str, Any]] = []
    for model in models:
        qualification = packaged_ic_qualification(
            model["manifest_uri"],
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
        )
        for source in sources:
            evidence = earliest_run_evidence(
                model_id=model["model_id"],
                source=source,
                object_store_root=object_store_root,
                workspace_root=workspace_root,
            )
            row: dict[str, Any] = {
                "model_id": model["model_id"],
                "source": source,
                "ic_qualified": qualification["ic_qualified"],
                "ic_status": qualification["ic_status"],
                "ic_sha256": qualification["ic_sha256"],
                "ic_relative_path": qualification["ic_relative_path"],
                **evidence,
                "verdict": classify_verdict(
                    ic_qualified=qualification["ic_qualified"],
                    first_run_quality=evidence["first_run_quality"],
                    first_run_init_mode=evidence["first_run_init_mode"],
                ),
            }
            detail = qualification.get("detail")
            if detail:
                row["detail"] = str(detail)[:512]
            rows.append(row)
    totals = {"rows": len(rows), **{verdict: 0 for verdict in VERDICTS}}
    for row in rows:
        totals[row["verdict"]] += 1
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(generated_at),
        "outcome": "completed",
        "inputs": {
            "registry_manifest": str(registry_manifest),
            "object_store_prefix": object_store_prefix,
            "sources": list(sources),
            "registered_model_count": len(models),
        },
        "limits": {
            "package_objects_rehashed": False,
            "run_evidence_lanes": (
                [OBJECT_STORE_LANE, WORKSPACE_LANE] if workspace_root is not None else [OBJECT_STORE_LANE]
            ),
            "note": (
                "Packaged-IC qualification trusts the sha256/size_bytes recorded in each package "
                "manifest; package objects are not re-hashed. End-to-end digest verification "
                "happens at run time in the SHUD runtime, not in this audit."
            ),
        },
        "totals": totals,
        "rows": rows,
    }
    _validate_receipt(receipt)
    return receipt


def build_blocked_receipt(reason: str, detail: str, generated_at: datetime) -> dict[str, Any]:
    if reason not in REFUSAL_REASONS:
        raise ValueError(f"unknown refusal reason: {reason}")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(generated_at),
        "outcome": "blocked",
        "refusal_reason": reason,
    }
    if detail:
        receipt["detail"] = detail[:512]
    _validate_receipt(receipt)
    return receipt


def _validate_receipt(receipt: Mapping[str, Any]) -> None:
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(receipt, schema)
    except jsonschema.ValidationError as error:
        raise AuditBlocked(f"receipt violates its schema: {error.message}", reason="RECEIPT_INVALID") from error


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def write_receipt(receipt: Mapping[str, Any], receipt_path: Path) -> None:
    """Write the receipt atomically — the ONLY write this tool performs."""
    content = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ensure_directory_no_follow(receipt_path.parent)
    atomic_write_bytes_no_follow(
        receipt_path, content, containment_root=receipt_path.parent, temp_suffix="part"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _AuditArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise AuditConfigError(f"invalid arguments: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _AuditArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--registry-manifest", required=True)
    parser.add_argument("--object-store-root")
    parser.add_argument("--object-store-prefix")
    parser.add_argument("--workspace-root")
    parser.add_argument("--sources")
    parser.add_argument("--receipt-path", required=True)
    return parser


def _absolute(value: str | None, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise AuditConfigError(f"{field} is required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise AuditConfigError(f"{field} must be an absolute path")
    return path


def _parse_sources(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_SOURCES
    sources = tuple(
        normalize_source_id(token.strip()) for token in str(value).split(",") if token.strip()
    )
    if not sources:
        raise AuditConfigError("--sources must list at least one source")
    return sources


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    generated_at = datetime.now(UTC)
    receipt_path: Path | None = None
    try:
        args = build_parser().parse_args(raw_argv)
        receipt_path = _absolute(args.receipt_path, "--receipt-path")
        registry_manifest = _absolute(args.registry_manifest, "--registry-manifest")
        object_store_root = _absolute(
            args.object_store_root or os.getenv("OBJECT_STORE_ROOT"), "--object-store-root"
        )
        workspace_root = (
            _absolute(args.workspace_root, "--workspace-root") if args.workspace_root else None
        )
        object_store_prefix = str(
            args.object_store_prefix
            if args.object_store_prefix is not None
            else (os.getenv("OBJECT_STORE_PREFIX") or "")
        )
        sources = _parse_sources(args.sources)
        receipt = build_receipt(
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            workspace_root=workspace_root,
            sources=sources,
            generated_at=generated_at,
        )
    except AuditBlocked as error:
        _emit_blocked(error.reason, str(error), generated_at, receipt_path)
        return 1
    try:
        write_receipt(receipt, receipt_path)
    except (OSError, SafeFilesystemError) as error:
        print(
            json.dumps({"status": "blocked", "reason": "RECEIPT_WRITE_FAILED", "message": str(error)[:512]}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "completed", "totals": receipt["totals"]}, sort_keys=True))
    return 0


def _emit_blocked(reason: str, message: str, generated_at: datetime, receipt_path: Path | None) -> None:
    print(json.dumps({"status": "blocked", "reason": reason, "message": message[:512]}), file=sys.stderr)
    if receipt_path is None:
        return
    try:
        write_receipt(build_blocked_receipt(reason, message, generated_at), receipt_path)
    except (AuditBlocked, OSError, SafeFilesystemError, ValueError):
        # A blocked run must not mask its own refusal behind a receipt-write
        # failure; the stderr line above is the authoritative signal.
        return


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
