#!/usr/bin/env python3
"""Evidence-capture primitives for the six-basin replay driver (#1164 change 2).

Split out of :mod:`scripts.replay_driver` so the driver module stays orchestration
only.  Everything here is read-only and three-way: ``present`` / ``absent`` /
``undeterminable`` are distinct outcomes and an uncompletable probe is never
reported as a completed negative.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from packages.common.safe_fs import (
    SafeFilesystemError,
    list_directory_no_follow_limited,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id

RESET_RECEIPT_SCHEMA_VERSION = "nhms.replay_state_scope_reset.v1"
#: The only reset outcome a replacement may be built on; ``commit_uncertain``
#: and ``refused`` are typed refusals for the driver.
RESET_RECEIPT_COMPLETED_OUTCOME = "completed"

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_DIGEST_FILES = 20_000
MAX_DIGEST_FILE_BYTES = 512 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 20_000
#: The census enumerates the WHOLE ``runs/`` directory, not a window slice, so it
#: needs a much wider bound than a single package listing.  Hitting the bound is
#: reported as ``undeterminable``; the census never silently truncates.
MAX_CENSUS_DIRECTORY_ENTRIES = 500_000

PROBE_PRESENT = "present"
PROBE_ABSENT = "absent"
PROBE_UNDETERMINABLE = "undeterminable"

#: Journal statuses/stages that carry a cycle's terminal completion record.  The
#: journal narrows the stage set to ``state_save_qc`` when the compute-state
#: terminal mode is enabled (the node-22 posture) and otherwise accepts
#: ``parse``/``publish``; the driver keeps the union because convergence also
#: requires a new state-index entry, which only ``state_save_qc`` writes.  A mode
#: difference can therefore only make the driver wait and halt, never declare a
#: cycle finished early.
TERMINAL_SUCCESS_STATUSES = frozenset({"succeeded", "complete", "published"})
TERMINAL_COMPLETION_STAGES = frozenset({"state_save_qc", "parse", "publish"})


class ReplayDriverRefused(RuntimeError):
    """Refusal decided before anything was submitted: exit 2."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": "refused", "refusal_reason": self.reason, **self.details}


# ---------------------------------------------------------------------------
# digests and probes
# ---------------------------------------------------------------------------


def _sha256_file(path: Path, *, containment_root: Path | None = None) -> str:
    content = read_bytes_limited_no_follow(
        path,
        max_bytes=MAX_DIGEST_FILE_BYTES,
        containment_root=containment_root,
    )
    return hashlib.sha256(content).hexdigest()


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            names = list_directory_no_follow_limited(current, max_entries=MAX_DIRECTORY_ENTRIES)
        except (FileNotFoundError, OSError, SafeFilesystemError) as error:
            raise _DigestUndeterminable(str(error)) from error
        for name in sorted(names):
            path = current / name
            try:
                metadata = stat_no_follow(path)
            except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                raise _DigestUndeterminable(str(error)) from error
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(path)
            if len(files) > MAX_DIGEST_FILES:
                raise _DigestUndeterminable("directory fan-out exceeds the digest bound")
    return sorted(files)


class _DigestUndeterminable(RuntimeError):
    pass


def directory_digest(root: Path | None, *, label: str, include_file_names: bool = False) -> dict[str, Any]:
    """Three-way content digest of a directory tree (sorted relpath + sha256).

    ``include_file_names`` additionally records the sorted relative file names.
    The run OUTPUT inventory needs them: a replay legitimately changes every
    output byte, so the only key-stability signal a run tree carries is which
    files exist, not what is in them.
    """

    record: dict[str, Any] = {
        "label": label,
        "path": str(root) if root is not None else None,
        "status": PROBE_UNDETERMINABLE,
        "file_count": None,
        "total_bytes": None,
        "sha256": None,
        "files": None,
        "detail": None,
    }
    if root is None:
        record["status"] = PROBE_ABSENT
        record["detail"] = "path not resolved"
        return record
    try:
        metadata = stat_no_follow(root)
    except FileNotFoundError:
        record["status"] = PROBE_ABSENT
        record["detail"] = "directory absent"
        return record
    except (OSError, SafeFilesystemError) as error:
        record["detail"] = f"stat failed: {error}"
        return record
    if not stat.S_ISDIR(metadata.st_mode):
        record["detail"] = "path is not a directory"
        return record
    try:
        files = _iter_files(root)
        digest = hashlib.sha256()
        total = 0
        for path in files:
            relative = path.relative_to(root).as_posix()
            content = read_bytes_limited_no_follow(path, max_bytes=MAX_DIGEST_FILE_BYTES)
            total += len(content)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
            digest.update(b"\n")
    except (_DigestUndeterminable, OSError, SafeFilesystemError) as error:
        record["detail"] = f"digest failed: {error}"
        return record
    record["status"] = PROBE_PRESENT
    record["file_count"] = len(files)
    record["total_bytes"] = total
    record["sha256"] = digest.hexdigest()
    if include_file_names:
        record["files"] = [path.relative_to(root).as_posix() for path in files]
    return record


def _read_json(path: Path, *, max_bytes: int) -> Mapping[str, Any] | None:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes)
    except (FileNotFoundError, OSError, SafeFilesystemError):
        return None
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    candidate = str(uri_or_key).strip()
    prefix = (object_store_prefix or "").rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")


# ---------------------------------------------------------------------------
# run tree helpers
# ---------------------------------------------------------------------------


def run_id_for(source_id: str, cycle_time: datetime, model_id: str) -> str:
    return f"fcst_{normalize_source_id(source_id).lower()}_{cycle_time.strftime('%Y%m%d%H')}_{model_id}"


def _run_root(object_store_root: Path, run_id: str) -> Path:
    return object_store_root / "runs" / run_id


def run_capture(object_store_root: Path, run_id: str) -> dict[str, Any]:
    """Manifest sha256 + output inventory digests of one run tree."""

    root = _run_root(object_store_root, run_id)
    manifest_path = root / "input" / "manifest.json"
    capture: dict[str, Any] = {
        "run_id": run_id,
        "run_root": str(root),
        "manifest_sha256": None,
        "manifest": None,
        "output_inventory": directory_digest(root / "output", label="run_output", include_file_names=True),
        "no_prior_run": False,
    }
    try:
        capture["manifest_sha256"] = _sha256_file(manifest_path)
    except (FileNotFoundError, OSError, SafeFilesystemError):
        capture["manifest_sha256"] = None
    manifest = _read_json(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    capture["manifest"] = manifest
    if manifest is None and capture["output_inventory"]["status"] == PROBE_ABSENT:
        capture["no_prior_run"] = True
    return capture


def manifest_summary(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project the fields a real run manifest actually carries.

    Shape source: ``chain_manifests.build_forecast_run_manifest`` and 36 sampled
    node-22 production manifests.  There is NO ``outputs.variables`` key — the
    key-consistency assertion used to hang off it and was therefore dead code
    (round-1 B-P1-3).  What is always there is
    ``model.river_network_version_id`` and the output segment count, both of
    which the timeseries rows are keyed by.
    """

    if manifest is None:
        return {
            "state_id": None,
            "state_checksum": None,
            "init_mode": None,
            "quality": None,
            "packaged_ic_checksum": None,
            "river_network_version_id": None,
            "output_segment_count": None,
        }
    initial_state = manifest.get("initial_state") if isinstance(manifest.get("initial_state"), Mapping) else {}
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), Mapping) else {}
    model = manifest.get("model") if isinstance(manifest.get("model"), Mapping) else {}
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
    init_mode = runtime.get("init_mode")
    return {
        "state_id": initial_state.get("state_id"),
        "state_checksum": initial_state.get("checksum"),
        "init_mode": int(init_mode) if isinstance(init_mode, int) else None,
        "quality": initial_state.get("quality"),
        "packaged_ic_checksum": initial_state.get("packaged_ic_checksum"),
        "river_network_version_id": model.get("river_network_version_id"),
        "output_segment_count": _optional_int(
            model.get("output_segment_count"),
            outputs.get("output_segment_count"),
            model.get("segment_count"),
        ),
    }


def _optional_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


#: Directory a run tree keeps its state checkpoints in (``workers.shud_runtime``
#: writes ``<output>/state_checkpoints/``).
CHECKPOINT_DIR_NAME = "state_checkpoints"
#: Suffix of the canonicalized-IC cache
#: :func:`packages.common.state_cli._normalized_checkpoint_ic_file` writes as
#: ``.{ic_file_name}.normalized`` NEXT TO the checkpoint it normalized.
CHECKPOINT_NORMALIZATION_SUFFIX = ".normalized"


def _is_checkpoint_normalization_sidecar(name: str) -> bool:
    """True for the ``.{ic}.normalized`` cache the run's own state-save writes.

    ``save_state_for_run`` (``state_cli.py:128``, the ``state_save_qc`` stage of
    THIS run's own cycle) canonicalizes each checkpoint IC before snapshotting
    it, and writes the canonical copy beside the checkpoint -- but only when the
    content needs it: a retimed header or at least one clamped negative residual
    (``state_cli.py:229-247``).  So the file's presence tracks whether that
    half's IC needed normalizing, not the key set of the run.  Attempt-4 is
    exactly that: the replayed dth_ls IC needed normalization while its July
    original did not (prior 14 files vs new 15), whereas the hhe / jialingjiang
    July originals did need it and their priors already carried theirs.  The
    physical signal in that difference is real and separately known; it is not a
    key-set difference, which is what R3's predicate-key axis compares.
    """

    parts = PurePosixPath(name).parts
    if len(parts) < 2 or CHECKPOINT_DIR_NAME not in parts[:-1]:
        return False
    basename = parts[-1]
    if not basename.startswith(".") or not basename.endswith(CHECKPOINT_NORMALIZATION_SUFFIX):
        return False
    # ``.{ic_file_name}.normalized``: the stem between the dot-prefix and the
    # suffix must be non-empty, so a bare ``.normalized`` is not a sidecar.
    return len(basename) > 1 + len(CHECKPOINT_NORMALIZATION_SUFFIX)


def _partition_normalization_sidecars(
    names: Sequence[str] | None,
) -> tuple[list[str] | None, list[str]]:
    """Split an inventory name list into (comparable names, excluded sidecars)."""

    if names is None:
        return None, []
    kept: list[str] = []
    excluded: list[str] = []
    for name in names:
        (excluded if _is_checkpoint_normalization_sidecar(name) else kept).append(name)
    return kept, excluded


def _inventory_file_names(inventory: Any) -> list[str] | None:
    if not isinstance(inventory, Mapping):
        return None
    if str(inventory.get("status") or "") != PROBE_PRESENT:
        return None
    files = inventory.get("files")
    if not isinstance(files, Sequence) or isinstance(files, str | bytes):
        return None
    return sorted(str(item) for item in files)


def key_consistency(prior: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """R3: a replay must not leave rows under a drifted key set behind.

    The parser deletes the union window, so window residue is not the risk; a
    changed ``river_network_version_id``, a changed output segment count or a
    changed output file inventory is, because those rows fall outside the delete
    predicate.  Every axis compared here is one a real manifest / run tree
    carries (round-1 B-P1-3 replaced the ``outputs.variables`` axis, which no
    production manifest has, with these).

    Comparison is per axis and loud: an axis present on the prior half but
    missing or different on the new half is ``drift``.  ``undeterminable`` is
    reserved for the one honest case -- no comparable prior evidence at all
    (the GFS 070712 rows with no prior run).

    The ``output_file_names`` axis compares the inventories with the checkpoint
    IC normalization sidecars (``state_checkpoints/.{ic}.normalized``) removed
    from BOTH halves: the run's own ``state_save_qc`` writes that file only when
    that half's IC content needed canonicalizing (see
    :func:`_is_checkpoint_normalization_sidecar`), so it is a per-half content
    fact, not a key-set fact -- and either half may be the one carrying it.
    Nothing is hidden: the recorded ``prior_output_file_names`` /
    ``new_output_file_names`` stay the FULL inventories and
    ``excluded_normalization_sidecars`` names exactly what the comparison
    disregarded whenever it disregarded anything.  Every other inventory
    difference -- the real checkpoint files included -- still drifts.
    """

    prior_names = _inventory_file_names(prior.get("output_inventory"))
    new_names = _inventory_file_names(new.get("output_inventory"))
    prior_compared, prior_excluded = _partition_normalization_sidecars(prior_names)
    new_compared, new_excluded = _partition_normalization_sidecars(new_names)
    axes: dict[str, tuple[Any, Any]] = {
        "river_network_version_id": (
            prior.get("river_network_version_id"),
            new.get("river_network_version_id"),
        ),
        "output_segment_count": (prior.get("output_segment_count"), new.get("output_segment_count")),
        "output_file_names": (prior_compared, new_compared),
    }
    comparable = {name: pair for name, pair in axes.items() if pair[0] not in (None, "")}
    record: dict[str, Any] = {
        "prior_river_network_version_id": prior.get("river_network_version_id"),
        "new_river_network_version_id": new.get("river_network_version_id"),
        "prior_output_segment_count": prior.get("output_segment_count"),
        "new_output_segment_count": new.get("output_segment_count"),
        "prior_output_file_names": prior_names,
        "new_output_file_names": new_names,
        "compared_axes": sorted(comparable),
    }
    if prior_excluded or new_excluded:
        record["excluded_normalization_sidecars"] = {
            "prior": prior_excluded,
            "new": new_excluded,
        }
    if not comparable:
        return {
            **record,
            "status": PROBE_UNDETERMINABLE,
            "detail": "prior run evidence does not carry a comparable key set",
            "drifted_axes": [],
        }
    drifted = sorted(name for name, (prior_value, new_value) in comparable.items() if prior_value != new_value)
    return {
        **record,
        "status": "drift" if drifted else "consistent",
        "detail": None if not drifted else f"key drift on: {', '.join(drifted)}",
        "drifted_axes": drifted,
    }


# ---------------------------------------------------------------------------
# reset receipt / state index
# ---------------------------------------------------------------------------


def load_reset_removed_entries(
    path: Path,
    *,
    source_id: str,
    model_ids: Sequence[str],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index the reset receipt's removed entries by (model, source, valid_time).

    This is the ONLY admissible source for the prior state fields: the scope was
    cleared before the replay started, so the live index is empty by
    construction and a "read it back" implementation would silently record
    nulls.

    The receipt's own ``scopes[]`` must cover every (model, source) this run
    replays (round-2 B2-4).  A globally non-empty receipt from the OTHER source
    passes a mere non-emptiness check while supplying no entry for any key of
    this run: every ``prior.state`` would be null and
    :func:`replaced_successor_entries` would treat an empty prior checksum as
    "any checksum counts as replaced", collapsing the convergence oracle to its
    journal leg over an un-cleared scope.
    """

    payload = _read_json(path, max_bytes=MAX_RECEIPT_BYTES)
    if payload is None:
        raise ReplayDriverRefused("reset_receipt_unreadable", {"path": str(path)})
    if payload.get("schema_version") != RESET_RECEIPT_SCHEMA_VERSION:
        raise ReplayDriverRefused("reset_receipt_schema_unsupported", {"path": str(path)})
    outcome = str(payload.get("outcome") or "")
    if outcome != RESET_RECEIPT_COMPLETED_OUTCOME:
        # ``commit_uncertain`` (exit 3) means the reset's own read-back could not
        # confirm the write, and ``refused`` means it never ran.  Starting an
        # irreversible replacement off either would overwrite production runs on
        # top of a state scope whose actual content is unknown.
        raise ReplayDriverRefused(
            "reset_receipt_not_completed",
            {"path": str(path), "outcome": outcome or None},
        )
    _assert_reset_receipt_scope_covers(path, payload, source_id=source_id, model_ids=model_ids)
    lanes = payload.get("lanes")
    if not isinstance(lanes, Sequence) or not lanes:
        raise ReplayDriverRefused("reset_receipt_lanes_missing", {"path": str(path)})
    resolved: dict[tuple[str, str, str], dict[str, Any]] = {}
    for lane in lanes:
        if not isinstance(lane, Mapping):
            continue
        for entry in lane.get("removed_entries") or []:
            if not isinstance(entry, Mapping):
                continue
            key = (
                str(entry.get("model_id") or ""),
                normalize_source_id(str(entry.get("source_id") or "")),
                str(entry.get("valid_time") or ""),
            )
            resolved.setdefault(
                key,
                {
                    "state_id": entry.get("state_id"),
                    "checksum": entry.get("checksum"),
                    "created_at": entry.get("created_at"),
                    "valid_time": entry.get("valid_time"),
                    "source": "reset_receipt",
                },
            )
    if not resolved:
        raise ReplayDriverRefused("reset_receipt_has_no_removed_entries", {"path": str(path)})
    return resolved


def _assert_reset_receipt_scope_covers(
    path: Path,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    model_ids: Sequence[str],
) -> None:
    """Refuse a reset receipt whose ``scopes[]`` does not cover this run."""

    scopes = payload.get("scopes")
    covered: set[tuple[str, str]] = set()
    if isinstance(scopes, Sequence) and not isinstance(scopes, str | bytes):
        for scope in scopes:
            if not isinstance(scope, Mapping):
                continue
            scope_model = str(scope.get("model_id") or "").strip()
            scope_source = str(scope.get("source_id") or "").strip()
            if not scope_model or not scope_source:
                continue
            covered.add((scope_model, normalize_source_id(scope_source)))
    canonical_source = normalize_source_id(source_id)
    required = {(str(model_id), canonical_source) for model_id in model_ids}
    missing = sorted(required - covered)
    if missing:
        raise ReplayDriverRefused(
            "reset_receipt_scope_mismatch",
            {
                "path": str(path),
                "source_id": canonical_source,
                "uncovered_scopes": [
                    {"model_id": model_id, "source_id": scope_source} for model_id, scope_source in missing
                ],
                "receipt_scopes": [
                    {"model_id": model_id, "source_id": scope_source}
                    for model_id, scope_source in sorted(covered)
                ],
            },
        )


def read_state_index_entries(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path, max_bytes=MAX_INDEX_BYTES)
    if payload is None:
        raise ReplayDriverRefused("state_index_unreadable", {"path": str(path)})
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ReplayDriverRefused("state_index_entries_invalid", {"path": str(path)})
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def successor_state_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    source_id: str,
    model_ids: Sequence[str],
    valid_time: datetime,
) -> dict[str, dict[str, Any]]:
    """Successor entries per model at ``valid_time`` -- no ``created_at`` gate.

    Production never sets ``created_at`` on state-index entries (1753/1753 null
    in both lanes), so a since-gate can only ever be unsatisfiable; freshness is
    decided by :func:`replaced_successor_entries` against the reset receipt
    instead (round-1 B-P1-1).
    """

    canonical_source = normalize_source_id(source_id)
    found: dict[str, dict[str, Any]] = {}
    for entry in entries:
        model_id = str(entry.get("model_id") or "")
        if model_id not in set(model_ids):
            continue
        if normalize_source_id(str(entry.get("source_id") or "")) != canonical_source:
            continue
        entry_valid_time = _parse_time(entry.get("valid_time"))
        if entry_valid_time is None or entry_valid_time != valid_time:
            continue
        found[model_id] = dict(entry)
    return found


def replaced_successor_entries(
    successors: Mapping[str, Mapping[str, Any]],
    *,
    prior_entries: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Successor entries that are demonstrably NOT the pre-replay ones.

    An entry counts as replaced when the reset receipt recorded no removed entry
    for that (model, source, valid_time) key, or when its checksum differs from
    the removed entry's checksum.  A successor whose checksum cannot be read, or
    whose checksum equals the removed entry's, is not counted: the driver then
    keeps waiting and finally halts rather than declaring convergence on the
    strength of an entry the replay may not have written.
    """

    replaced: dict[str, dict[str, Any]] = {}
    for model_id, entry in successors.items():
        checksum = str(entry.get("checksum") or "").strip()
        if not checksum:
            continue
        prior = prior_entries.get(model_id)
        prior_checksum = str((prior or {}).get("checksum") or "").strip()
        if prior_checksum and checksum == prior_checksum:
            continue
        replaced[model_id] = dict(entry)
    return replaced


def terminal_completion_job_ids(
    jobs: Sequence[Mapping[str, Any]],
    *,
    model_ids: Sequence[str],
) -> dict[str, list[str]]:
    """Terminal completion job ids per model, cohort jobs included.

    The production completion record is cohort scoped (``model_id`` null, run id
    ``cycle_<src>_<stamp>_forecast_cohort_*``) and matches every candidate of the
    cycle, so a model-less terminal job is attributed to all requested models.
    A job that names a model is attributed to that model only.
    """

    resolved: dict[str, list[str]] = {model_id: [] for model_id in model_ids}
    for job in jobs:
        if str(job.get("status") or "") not in TERMINAL_SUCCESS_STATUSES:
            continue
        if str(job.get("stage") or "") not in TERMINAL_COMPLETION_STAGES:
            continue
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            continue
        job_model_id = str(job.get("model_id") or "").strip()
        for model_id in resolved:
            if job_model_id in ("", model_id) and job_id not in resolved[model_id]:
                resolved[model_id].append(job_id)
    return {model_id: sorted(ids) for model_id, ids in resolved.items()}


_RUN_ID_CYCLE_TOKEN_LENGTH = 10


def census_run_cycles(
    object_store_root: Path,
    *,
    source_id: str,
    model_ids: Sequence[str],
) -> dict[str, Any]:
    """Enumerate every run tree on disk for these scopes -- no window slice.

    The census is the driver's on-site inventory (design D5 step 0a), so it must
    not be an echo of the requested cycle range: a frontier that moved after the
    survey, or a run outside the requested window, has to show up here.
    """

    runs_root = object_store_root / "runs"
    census: dict[str, Any] = {
        "runs_root": str(runs_root),
        "status": PROBE_UNDETERMINABLE,
        "detail": None,
        "cycles_by_model": {model_id: [] for model_id in model_ids},
    }
    try:
        names = list_directory_no_follow_limited(runs_root, max_entries=MAX_CENSUS_DIRECTORY_ENTRIES)
    except FileNotFoundError:
        census["status"] = PROBE_ABSENT
        census["detail"] = "runs directory absent"
        return census
    except (OSError, SafeFilesystemError) as error:
        census["detail"] = f"runs directory unreadable: {error}"
        return census
    if len(names) >= MAX_CENSUS_DIRECTORY_ENTRIES:
        census["detail"] = "runs directory fan-out exceeds the census bound"
        return census
    prefix = f"fcst_{normalize_source_id(source_id).lower()}_"
    for name in names:
        if not name.startswith(prefix):
            continue
        remainder = name[len(prefix) :]
        cycle_token, separator, model_id = remainder.partition("_")
        if not separator or len(cycle_token) != _RUN_ID_CYCLE_TOKEN_LENGTH or not cycle_token.isdigit():
            continue
        if model_id in census["cycles_by_model"] and cycle_token not in census["cycles_by_model"][model_id]:
            census["cycles_by_model"][model_id].append(cycle_token)
    census["status"] = PROBE_PRESENT
    census["cycles_by_model"] = {
        model_id: sorted(cycles) for model_id, cycles in census["cycles_by_model"].items()
    }
    return census
