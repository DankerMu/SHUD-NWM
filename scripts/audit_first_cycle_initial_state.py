#!/usr/bin/env python3
"""Read-only audit: did each basin's FIRST cycle consume its packaged calibrated IC?

Issue #1164.  For every registered model x forecast source this tool reconciles
two independent facts and writes a schema-versioned receipt:

1. **Packaged-IC qualification** — read from the model package manifest
   referenced by the registry row (``resource_profile.manifest_uri``) using the
   SAME two-tier criteria the scheduler gate applies at planning time
   (``services.orchestrator.scheduler_generation.classify_packaged_initial_condition``):

   a. *inventory tier* — when the manifest carries an ``included_files``
      inventory, qualification needs exactly ONE ``*.cfg.ic`` entry anywhere in
      the inventory (a second one, e.g. a stray ``CALIB/*.cfg.ic``, is ambiguous
      and disqualifies), that entry must be the CANONICAL one
      (``<shud_input_name>.cfg.ic`` when the manifest names the SHUD input
      directory, otherwise a top-level path), its ``sha256`` must differ from the
      empty-file digest and its ``size_bytes`` must be positive.  No package
      object is opened.
   b. *object-probe tier* — when the manifest is readable but carries NO
      ``included_files`` inventory (the direct-grid variant shape, whose only
      top-level key is ``direct_grid_forcing``), the registry row's
      ``resource_profile.shud_input_name`` and ``model_package_uri`` derive the
      single canonical IC object ``{model_package_uri}{shud_input_name}.cfg.ic``
      and a bounded no-follow stat + sha256 read of THAT object decides:
      present and non-empty qualifies (carrying the probed digest), missing or
      empty does not, and a failed stat or read stays unreadable rather than
      "no IC".

   Each row records which tier produced its verdict (``ic_qualification_source``).
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
    Evidence is missing, undecidable, or does not describe either case (no run
    manifest found, the package manifest is unreadable, the run-evidence sweep
    could not be completed so the earliest cycle is not provably the earliest —
    ``first_run_evidence_complete=false`` — or the first run warm-started from a
    state snapshot rather than bootstrapping).

Read-only by construction: every filesystem access is a no-follow read or a
bounded directory listing, and the ONLY thing written is the receipt at
``--receipt-path``.  No production state, package, index, or journal content is
modified.

Limits recorded in the receipt: inventory-tier package objects are NOT re-hashed
— qualification trusts the digests recorded in the package manifest (NFS IO
budget), and a manifest whose recorded digest disagrees with the object on disk
is caught at run time by the runtime's end-to-end checksum verification, not
here.  Object-probe-tier rows DO hash one object (the canonical
``<shud_input_name>.cfg.ic``) because that shape publishes no digest to trust.
"""

from __future__ import annotations

import argparse
import hashlib
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
    MAX_PACKAGED_IC_PROBE_BYTES,
    PACKAGED_IC_QUALIFIED,
    PACKAGED_IC_QUALITY,
    PACKAGED_IC_UNQUALIFIED,
    PACKAGED_IC_UNREADABLE,
    PackagedIcObjectProbe,
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
    first_run_evidence_complete: bool,
) -> str:
    """Return the verdict for one model x source row.

    ``first_run_evidence_complete`` is deliberately REQUIRED (no ``True``
    default): it is a completeness contract, and a default would let a future
    caller omit it and silently claim a complete sweep it never performed.

    Total and order-sensitive:

    0. The run-evidence sweep could not be completed (a lane could not be
       enumerated, or an earlier cycle's manifest could not be parsed) →
       ``undetermined``.  Whatever run was found is not provably the FIRST one,
       so neither a defect nor a clean verdict may be claimed from it — this is
       the same three-way discipline the qualification lanes apply.
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
    if not first_run_evidence_complete:
        return "undetermined"
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
    """TWO-WAY predicate: absent, unsafe, and unreadable all collapse to ``False``.

    Deliberately kept two-way and deliberately NOT used on any call site that
    carries a completeness / qualification / verdict contract.  It is safe only
    where a ``False`` return fails CLOSED — i.e. where the caller refuses
    (``AuditBlocked``) or degrades to "cannot tell" (``ic_qualified=None`` →
    ``undetermined``).  Its two surviving call sites are exactly those:

    - :func:`load_registered_models` — ``False`` raises ``AuditBlocked``;
    - :func:`packaged_ic_qualification` — ``False`` yields the ``unknown`` view
      (``ic_status=unreadable``, ``ic_qualified=None``) → ``undetermined``.

    Every site where the collapse would be fail-OPEN (the canonical-IC object
    probe, the run-manifest sweep) inlines a THREE-way stat instead; see
    :func:`_canonical_ic_object_probe` and :func:`earliest_run_evidence`.
    """
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
    # Two-way guard, fail-CLOSED: absent / symlinked / unreadable registry
    # manifests all refuse the whole audit with ``AuditBlocked``.  No receipt row
    # is produced from a collapsed outcome, so no completeness or verdict
    # contract rides on telling the three cases apart here.
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
        # Fail-CLOSED: every read/parse failure refuses the audit rather than
        # producing rows.  No contract rides on separating them.
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
        profile = resource_profile if isinstance(resource_profile, Mapping) else {}
        manifest_uri = str(profile.get("manifest_uri") or "").strip()
        if not manifest_uri:
            manifest_uri = str(model.get("manifest_uri") or "").strip()
        # ``model_package_uri`` / ``shud_input_name`` feed the tier-(b) canonical
        # object probe; the row-level fallback exists because the scheduler
        # registry publishes ``model_package_uri`` at both levels.
        model_package_uri = str(profile.get("model_package_uri") or "").strip()
        if not model_package_uri:
            model_package_uri = str(model.get("model_package_uri") or "").strip()
        shud_input_name = str(profile.get("shud_input_name") or "").strip()
        if not shud_input_name:
            shud_input_name = str(model.get("shud_input_name") or "").strip()
        rows.append(
            {
                "model_id": model_id,
                "manifest_uri": manifest_uri,
                "model_package_uri": model_package_uri,
                "shud_input_name": shud_input_name,
            }
        )
    return sorted(rows, key=lambda row: row["model_id"])


def _canonical_ic_object_probe(
    object_uri: str,
    *,
    object_store_root: Path,
    object_store_prefix: str,
) -> PackagedIcObjectProbe:
    """Bounded no-follow stat + sha256 probe of ONE canonical packaged-IC object.

    The audit's tier-(b) mirror of the gate probe.  Still read-only: a bounded
    ``read_bytes_limited_no_follow`` under the object-store containment root.
    A probe that cannot complete reports ``unreadable_detail`` so the row stays
    ``undetermined`` instead of being reported as a clean cold start.

    The stat is deliberately inlined here instead of delegating to
    :func:`_is_regular_file`, which is a two-way predicate: it folds
    ``SafeFilesystemError`` / ``OSError`` into the same ``False`` as a genuine
    ``FileNotFoundError``.  On this call site that collapse is fail-OPEN — a
    symlink at the key, a directory at the key, an unreadable parent or an NFS
    ``EIO`` would read as ``exists=False``, the classifier would emit
    ``packaged_initial_condition_object_missing``, and the row would be reported
    as a CLEAN ``cold_start_no_ic``.  So the three outcomes stay distinct:

    - ``FileNotFoundError`` → the object is genuinely absent (``exists=False``);
    - ``SafeFilesystemError`` / ``OSError`` → the probe could not complete;
    - stat succeeded but the entry is not a regular file → it exists and is
      unusable as an IC object.

    Both non-absence failures carry ``unreadable_detail``, which
    ``_classify_packaged_ic_by_object_probe`` evaluates BEFORE ``exists``, so
    either lands on ``PACKAGED_IC_UNREADABLE``.  The detail is a fixed token —
    no exception text — so the receipt never inlines filesystem contents or
    paths.
    """
    key = _safe_relative_key(_object_key(object_uri, object_store_prefix))
    if key is None:
        return PackagedIcObjectProbe(
            exists=False,
            unreadable_detail="packaged initial condition object escapes the object-store root",
        )
    path = object_store_root / key
    try:
        entry_stat = stat_no_follow(path, containment_root=object_store_root)
    except FileNotFoundError:
        return PackagedIcObjectProbe(exists=False)
    except (SafeFilesystemError, OSError):
        return PackagedIcObjectProbe(
            exists=False,
            unreadable_detail="packaged initial condition object could not be inspected",
        )
    if not stat.S_ISREG(entry_stat.st_mode):
        return PackagedIcObjectProbe(
            exists=True,
            unreadable_detail="packaged initial condition object is not a regular file",
        )
    try:
        content = read_bytes_limited_no_follow(
            path, max_bytes=MAX_PACKAGED_IC_PROBE_BYTES, containment_root=object_store_root
        )
    except (OSError, SafeFilesystemError):
        return PackagedIcObjectProbe(
            exists=True,
            unreadable_detail="packaged initial condition object could not be read",
        )
    if len(content) > MAX_PACKAGED_IC_PROBE_BYTES:
        return PackagedIcObjectProbe(
            exists=True,
            unreadable_detail="packaged initial condition object exceeds the bounded probe limit",
        )
    return PackagedIcObjectProbe(
        exists=True,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def packaged_ic_qualification(
    model: Mapping[str, Any],
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
    defect or clean.  ``ic_qualification_source`` names the tier that decided
    (``inventory`` / ``object_probe``) and is ``None`` when no tier ran.
    """
    manifest_uri = str(model.get("manifest_uri") or "").strip()
    unknown: dict[str, Any] = {
        "ic_status": PACKAGED_IC_UNREADABLE,
        "ic_qualified": None,
        "ic_sha256": None,
        "ic_relative_path": None,
        "ic_qualification_source": None,
    }
    if not manifest_uri:
        return {
            **unknown,
            "ic_status": "absent",
            "detail": "registry row publishes no package manifest reference",
        }
    key = _safe_relative_key(_object_key(manifest_uri, object_store_prefix))
    if key is None:
        return {**unknown, "detail": "package manifest reference escapes the object-store root"}
    path = object_store_root / key
    # Two-way guard, fail-CLOSED: absent / symlinked / unreadable manifests all
    # land on the ``unknown`` view (``ic_status=unreadable``,
    # ``ic_qualified=None``), which the verdict table refuses to collapse into
    # either the defect or a clean cold start — the row is ``undetermined``
    # either way.  Nothing downstream distinguishes the three, so the collapse
    # cannot promote an undecidable check to a negative result.
    if not _is_regular_file(path, containment_root=object_store_root):
        return {**unknown, "detail": "package manifest object is missing or not a regular file"}
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
        # Same fail-CLOSED target as the guard above: unreadable, never "no IC".
        return {**unknown, "detail": "package manifest could not be read or parsed"}
    signal = classify_packaged_initial_condition(
        payload,
        resource_profile={
            "model_package_uri": model.get("model_package_uri"),
            "shud_input_name": model.get("shud_input_name"),
        },
        canonical_object_probe=lambda object_uri: _canonical_ic_object_probe(
            object_uri,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
        ),
    )
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
        "ic_qualification_source": signal.qualification_source or None,
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
) -> tuple[list[tuple[str, str]], bool]:
    """Return ``((cycle, run_id) pairs, enumerable)`` for ``source``/``model_id``.

    Three-way, because "this lane holds no runs" and "this lane could not be
    listed" are different facts and only the first one may inform a verdict:

    - ``FileNotFoundError`` — the lane genuinely does not exist (a workspace
      that never ran, an object store without a ``runs/`` prefix): confirmed
      absence, ``enumerable=True`` with no candidates.
    - ``SafeFilesystemError`` / ``OSError`` (symlinked or non-directory lane
      root, EACCES, NFS EIO), or a listing that hit
      :data:`MAX_RUN_DIRECTORY_ENTRIES` and was therefore truncated — the lane
      could not be enumerated: ``enumerable=False``.  Collapsing this into "no
      runs here" would let the audit report a LATER cycle as the first one, and
      with it a confident ``consumed_package_ic`` / ``cold_start_no_ic``.
    - otherwise the matching ``(cycle, run_id)`` pairs with ``enumerable=True``.
    """
    try:
        names = list_directory_no_follow_limited(runs_root, max_entries=MAX_RUN_DIRECTORY_ENTRIES)
    except FileNotFoundError:
        return [], True
    except (NotADirectoryError, SafeFilesystemError, OSError):
        return [], False
    if len(names) > MAX_RUN_DIRECTORY_ENTRIES:
        # ``list_directory_no_follow_limited`` returns one sentinel entry past
        # the bound: the lane is larger than the audit will read, so the earliest
        # cycle may not be in hand.
        return [], False
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
    return sorted(candidates), True


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

    ``first_run_evidence_complete`` reports whether that sweep could actually be
    completed.  It is ``False`` when a lane could not be enumerated or when an
    EARLIER cycle's manifest was present but unparseable, i.e. when the returned
    run is not provably the first one.  The verdict table refuses to draw either
    a defect or a clean conclusion from an incomplete sweep instead of silently
    promoting a later cycle to "first".
    """
    lanes: list[tuple[str, Path]] = [(OBJECT_STORE_LANE, object_store_root / "runs")]
    if workspace_root is not None:
        lanes.append((WORKSPACE_LANE, workspace_root / "runs"))
    candidates: list[tuple[str, str, str, Path]] = []
    evidence_complete = True
    for lane, runs_root in lanes:
        containment_root = object_store_root if lane == OBJECT_STORE_LANE else workspace_root
        assert containment_root is not None
        lane_candidates, enumerable = _run_lane_candidates(
            runs_root, source=source, model_id=model_id
        )
        evidence_complete = evidence_complete and enumerable
        for cycle, run_id in lane_candidates:
            candidates.append((cycle, lane, run_id, containment_root))
    empty = {
        "first_cycle": None,
        "first_run_id": None,
        "first_run_quality": None,
        "first_run_init_mode": None,
        "first_run_evidence_lane": None,
        "first_run_evidence_complete": evidence_complete,
    }
    if not candidates:
        return empty
    # Earliest cycle first; the object-store lane wins ties (it is the durable
    # face) because ``OBJECT_STORE_LANE`` sorts before ``WORKSPACE_LANE``.
    for cycle, lane, run_id, containment_root in sorted(candidates):
        runs_root = containment_root / "runs"
        manifest_path = runs_root / run_id / "input" / "manifest.json"
        # Three-way, mirroring :func:`_canonical_ic_object_probe`.  The two-way
        # :func:`_is_regular_file` predicate folds an UNDECIDABLE stat (a
        # symlinked manifest or an EACCES ``input/`` — ``SafeFilesystemError`` —
        # or an NFS ``EIO`` — ``OSError``) into the same ``False`` as a genuinely
        # absent manifest.  On THIS call site that collapse is fail-OPEN for the
        # completeness contract: an earlier cycle whose manifest could not be
        # inspected would be skipped silently, the NEXT cycle promoted to
        # "first", and ``first_run_evidence_complete`` would stay ``True`` — a
        # confident ``consumed_package_ic`` / ``cold_start_no_ic`` about a cycle
        # that is not provably the earliest.
        #
        # The stat guard itself must STAY (rather than be dropped in favour of
        # letting the read fail): ``open_file_no_follow`` raises
        # ``FileNotFoundError`` for a genuinely missing manifest, which the parse
        # half's ``except`` below would swallow into ``evidence_complete=False``
        # and degrade every ordinary manifest-less run directory.
        try:
            manifest_stat = stat_no_follow(manifest_path, containment_root=containment_root)
        except FileNotFoundError:
            # Confirmed absence: this run directory (or its ``input/``) never got
            # a manifest.  Decidable, so the sweep stays complete.
            continue
        except (SafeFilesystemError, OSError):
            # The manifest may or may not be there; this cycle is undecidable.
            evidence_complete = False
            continue
        if not stat.S_ISREG(manifest_stat.st_mode):
            # Something IS at the manifest path (a directory, a device) and it is
            # not readable run evidence — undecidable, not absent.
            evidence_complete = False
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
            # The manifest IS there but could not be read or parsed, so this
            # cycle's behavior is undecidable.  Skipping to the next cycle is
            # still the useful thing to do, but the row can no longer claim the
            # cycle it lands on is the first one.
            evidence_complete = False
            continue
        if not isinstance(payload, Mapping):
            evidence_complete = False
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
            "first_run_evidence_complete": evidence_complete,
        }
    return {**empty, "first_run_evidence_complete": evidence_complete}


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
            model,
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
                "ic_qualification_source": qualification["ic_qualification_source"],
                **evidence,
                "verdict": classify_verdict(
                    ic_qualified=qualification["ic_qualified"],
                    first_run_quality=evidence["first_run_quality"],
                    first_run_init_mode=evidence["first_run_init_mode"],
                    first_run_evidence_complete=evidence["first_run_evidence_complete"],
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
            "inventory_tier_package_objects_rehashed": False,
            "probe_tier_max_object_bytes": MAX_PACKAGED_IC_PROBE_BYTES,
            "run_evidence_lanes": (
                [OBJECT_STORE_LANE, WORKSPACE_LANE] if workspace_root is not None else [OBJECT_STORE_LANE]
            ),
            "note": (
                "Every row records ic_qualification_source. 'inventory' rows trust the "
                "sha256/size_bytes recorded in the package manifest and re-hash no package "
                "object; end-to-end digest verification for them happens at run time in the "
                "SHUD runtime, not in this audit. 'object_probe' rows (inventory-less "
                "direct-grid variant manifests) carry the sha256 of a bounded no-follow read "
                "of the single canonical <shud_input_name>.cfg.ic object, and their inventory "
                "is not enumerable, so package-level IC ambiguity is not detectable for them."
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
        # Fail-LOUD: no receipt was produced, so no row carries a collapsed
        # outcome; the CLI reports blocked and exits non-zero.
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
        # failure; the stderr line above is the authoritative signal.  Carries no
        # completeness contract: the run has ALREADY refused, and the caller
        # returns non-zero regardless of whether the blocked receipt landed.
        return


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
