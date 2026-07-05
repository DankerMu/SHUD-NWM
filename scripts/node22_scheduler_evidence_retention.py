#!/usr/bin/env python
"""Retention cleanup for node-22 scheduler evidence artefacts.

Scoped to `NHMS_SCHEDULER_EVIDENCE_ROOT` (top-level plus its `retention/`
subdirectory). Applies an age-then-size policy against three explicit
whitelist branches:

    1. Scheduler pass evidence — files whose basename begins with the literal
       prefix `scheduler_` and ends with `.json` or `.pre_execution.json`
       (real on-disk shapes: `scheduler_<cycle>_<hex12>.json`,
       `scheduler_<cycle>_<hex12>.pre_execution.json`; see
       `services/orchestrator/scheduler_runtime.py:469` and
       `services/orchestrator/scheduler_evidence.py:295,357`).
    2. Retention receipts — `retention/retention-*.json` (this script's own
       receipts), subject to a longer receipt-retention window (default 180
       days) recorded in a `receipt_pass` bucket separate from `deleted_paths`.
    3. Env-glob whitelist — colon-separated `fnmatch` patterns supplied via
       `NHMS_SCHEDULER_EVIDENCE_RETENTION_WHITELIST_GLOBS`.

Any file not matched by one of the three branches is recorded as
`skipped: unrecognised` and NEVER deleted — including the fallback
`evidence_write_error.json` artefact (explicitly out of scope so operators
retain a trail of evidence-write incidents).

Safety filters run before whitelist matching: a file with a sibling `.tmp`
or `.lock` is `skipped: in-flight`; a file with mtime younger than one hour
is `skipped: safety-window`.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "nhms.node22_scheduler_evidence_retention.v1"
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_MB = 512
DEFAULT_RECEIPT_RETENTION_DAYS = 180
SAFETY_WINDOW_SECONDS = 3600
PASS_EVIDENCE_PREFIX = "scheduler_"
PASS_EVIDENCE_SUFFIXES = (".pre_execution.json", ".json")
RECEIPT_DIR_NAME = "retention"
RECEIPT_FILENAME_PREFIX = "retention-"
RECEIPT_FILENAME_SUFFIX = ".json"


@dataclass(frozen=True)
class SchedulerEvidenceRetentionConfig:
    evidence_root: Path
    retention_days: int
    max_bytes: int
    receipt_retention_days: int
    whitelist_globs: tuple[str, ...]
    summary_path: Path | None


@dataclass(frozen=True)
class FileEntry:
    path: Path
    name: str
    size_bytes: int
    mtime: datetime


def _env_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _split_globs(raw: str | None) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return tuple()
    parts = [item.strip() for item in raw.split(":") if item.strip()]
    return tuple(parts)


def _safe_resolved_dir(path: Path, *, label: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not path.is_absolute():
        return None, {"field": label, "reason": "path_not_absolute", "path": str(path)}
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        return None, {"field": label, "reason": "path_unavailable", "path": str(path), "error": str(error)}
    if resolved == Path("/"):
        return None, {"field": label, "reason": "path_is_root", "path": str(resolved)}
    if not resolved.is_dir():
        return None, {"field": label, "reason": "path_not_directory", "path": str(resolved)}
    if resolved.is_symlink():
        return None, {"field": label, "reason": "path_is_symlink", "path": str(resolved)}
    return resolved, None


def config_from_env(
    args: argparse.Namespace,
) -> tuple[SchedulerEvidenceRetentionConfig | None, list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    root_value = (args.evidence_root or os.getenv("NHMS_SCHEDULER_EVIDENCE_ROOT") or "").strip()
    if not root_value:
        blockers.append({"field": "evidence_root", "reason": "missing"})
        root = Path()
    else:
        root = Path(root_value)
    resolved_root, blocker = _safe_resolved_dir(root, label="evidence_root")
    if blocker is not None:
        blockers.append(blocker)

    retention_days = args.retention_days or _env_int(
        "NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS", default=DEFAULT_RETENTION_DAYS
    )
    if retention_days <= 0:
        blockers.append({"field": "retention_days", "reason": "must_be_positive", "value": retention_days})

    max_mb = args.max_mb or _env_int("NHMS_SCHEDULER_EVIDENCE_MAX_MB", default=DEFAULT_MAX_MB)
    if max_mb <= 0:
        blockers.append({"field": "max_mb", "reason": "must_be_positive", "value": max_mb})

    receipt_retention_days = args.receipt_retention_days or _env_int(
        "NHMS_SCHEDULER_EVIDENCE_RECEIPT_RETENTION_DAYS", default=DEFAULT_RECEIPT_RETENTION_DAYS
    )
    if receipt_retention_days <= 0:
        blockers.append(
            {
                "field": "receipt_retention_days",
                "reason": "must_be_positive",
                "value": receipt_retention_days,
            }
        )

    whitelist_source = (
        args.whitelist_globs
        if args.whitelist_globs is not None
        else os.getenv("NHMS_SCHEDULER_EVIDENCE_RETENTION_WHITELIST_GLOBS")
    )
    whitelist_globs = _split_globs(whitelist_source)

    summary_value = args.summary_path or ""
    summary_path = Path(summary_value).expanduser() if summary_value.strip() else None
    if summary_path is not None and not summary_path.is_absolute():
        blockers.append({"field": "summary_path", "reason": "path_not_absolute", "path": str(summary_path)})

    if blockers or resolved_root is None:
        return None, blockers
    return (
        SchedulerEvidenceRetentionConfig(
            evidence_root=resolved_root,
            retention_days=retention_days,
            max_bytes=max_mb * 1024 * 1024,
            receipt_retention_days=receipt_retention_days,
            whitelist_globs=whitelist_globs,
            summary_path=summary_path,
        ),
        [],
    )


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_for_filename(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _load_entry(path: Path) -> FileEntry | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    return FileEntry(path=path, name=path.name, size_bytes=int(stat.st_size), mtime=mtime)


def _iter_scope(evidence_root: Path, receipt_dir: Path) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for parent in (evidence_root, receipt_dir):
        if not parent.is_dir():
            continue
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_file():
                continue
            if child.is_symlink():
                continue
            entry = _load_entry(child)
            if entry is not None:
                entries.append(entry)
    return entries


def _has_inflight_sibling(path: Path) -> bool:
    for suffix in (".tmp", ".lock"):
        if path.with_name(path.name + suffix).exists():
            return True
    return False


def _is_pass_evidence(name: str) -> bool:
    if not name.startswith(PASS_EVIDENCE_PREFIX):
        return False
    return any(name.endswith(suffix) for suffix in PASS_EVIDENCE_SUFFIXES)


def _is_retention_receipt(path: Path, receipt_dir: Path) -> bool:
    if path.parent != receipt_dir:
        return False
    name = path.name
    return name.startswith(RECEIPT_FILENAME_PREFIX) and name.endswith(RECEIPT_FILENAME_SUFFIX)


def _matches_env_glob(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _entry_payload(entry: FileEntry) -> dict[str, Any]:
    return {
        "path": str(entry.path),
        "mtime": _iso(entry.mtime),
        "size_bytes": entry.size_bytes,
    }


def _delete_payload(entry: FileEntry, *, pass_name: str) -> dict[str, Any]:
    payload = _entry_payload(entry)
    payload["pass"] = pass_name
    return payload


def run_retention(config: SchedulerEvidenceRetentionConfig, *, now: datetime) -> dict[str, Any]:
    started_at = now.astimezone(UTC)
    receipt_dir = config.evidence_root / RECEIPT_DIR_NAME
    receipt_dir.mkdir(parents=True, exist_ok=True)

    entries = _iter_scope(config.evidence_root, receipt_dir)
    total_before_bytes = sum(entry.size_bytes for entry in entries)

    skipped_by_reason: dict[str, list[dict[str, Any]]] = {
        "in-flight": [],
        "safety-window": [],
        "unrecognised": [],
    }
    pass_evidence_entries: list[FileEntry] = []
    receipt_entries: list[FileEntry] = []
    env_whitelist_entries: list[FileEntry] = []
    safety_cutoff = started_at - timedelta(seconds=SAFETY_WINDOW_SECONDS)

    for entry in entries:
        if _has_inflight_sibling(entry.path):
            skipped_by_reason["in-flight"].append(_entry_payload(entry))
            continue
        if entry.mtime > safety_cutoff:
            skipped_by_reason["safety-window"].append(_entry_payload(entry))
            continue
        if _is_pass_evidence(entry.name) and entry.path.parent == config.evidence_root:
            pass_evidence_entries.append(entry)
            continue
        if _is_retention_receipt(entry.path, receipt_dir):
            receipt_entries.append(entry)
            continue
        if config.whitelist_globs and _matches_env_glob(entry.name, config.whitelist_globs):
            env_whitelist_entries.append(entry)
            continue
        skipped_by_reason["unrecognised"].append(_entry_payload(entry))

    deleted_paths: list[dict[str, Any]] = []
    receipt_pass: list[dict[str, Any]] = []
    partial_failure = False

    age_cutoff = started_at - timedelta(days=config.retention_days)
    surviving_pass_evidence: list[FileEntry] = []
    for entry in pass_evidence_entries:
        if entry.mtime < age_cutoff:
            if _try_unlink(entry.path):
                deleted_paths.append(_delete_payload(entry, pass_name="age"))
            else:
                partial_failure = True
                surviving_pass_evidence.append(entry)
            continue
        surviving_pass_evidence.append(entry)

    # Env-whitelist entries are subject to the same age pass as pass evidence
    # so operators can opt additional artefact families in with a matching TTL.
    for entry in env_whitelist_entries:
        if entry.mtime < age_cutoff:
            if _try_unlink(entry.path):
                deleted_paths.append(_delete_payload(entry, pass_name="age"))
            else:
                partial_failure = True
                surviving_pass_evidence.append(entry)
            continue
        surviving_pass_evidence.append(entry)

    receipt_cutoff = started_at - timedelta(days=config.receipt_retention_days)
    for entry in receipt_entries:
        if entry.mtime < receipt_cutoff:
            if _try_unlink(entry.path):
                receipt_pass.append(_entry_payload(entry))
            else:
                partial_failure = True

    if surviving_pass_evidence:
        surviving_pass_evidence.sort(key=lambda item: item.mtime)
        current_size = sum(item.size_bytes for item in surviving_pass_evidence)
        index = 0
        while current_size > config.max_bytes and index < len(surviving_pass_evidence):
            entry = surviving_pass_evidence[index]
            if _try_unlink(entry.path):
                deleted_paths.append(_delete_payload(entry, pass_name="size"))
                current_size -= entry.size_bytes
            else:
                partial_failure = True
            index += 1

    total_after_bytes = _current_total_bytes(config.evidence_root, receipt_dir)
    finished_at = datetime.now(UTC)

    skipped_count = sum(len(items) for items in skipped_by_reason.values())
    payload = {
        "schema_version": SCHEMA_VERSION,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "evidence_root": str(config.evidence_root),
        "total_before_bytes": total_before_bytes,
        "total_after_bytes": total_after_bytes,
        "deleted_count": len(deleted_paths),
        "deleted_paths": deleted_paths,
        "receipt_pass": receipt_pass,
        "skipped_count": skipped_count,
        "skipped_paths_by_reason": skipped_by_reason,
        "policy": {
            "retention_days": config.retention_days,
            "max_mb": config.max_bytes // (1024 * 1024),
            "receipt_retention_days": config.receipt_retention_days,
            "whitelist_globs": list(config.whitelist_globs),
        },
        "partial_failure": partial_failure,
    }
    return payload


def _try_unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _current_total_bytes(evidence_root: Path, receipt_dir: Path) -> int:
    total = 0
    for parent in (evidence_root, receipt_dir):
        if not parent.is_dir():
            continue
        try:
            children = parent.iterdir()
        except OSError:
            continue
        for child in children:
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    return total


def _blocked_payload(blockers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    now = _iso(datetime.now(UTC).replace(microsecond=0))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "preflight_blocked",
        "started_at": now,
        "finished_at": now,
        "blockers": list(blockers),
        "total_before_bytes": 0,
        "total_after_bytes": 0,
        "deleted_count": 0,
        "deleted_paths": [],
        "receipt_pass": [],
        "skipped_count": 0,
        "skipped_paths_by_reason": {"in-flight": [], "safety-window": [], "unrecognised": []},
        "policy": {
            "retention_days": 0,
            "max_mb": 0,
            "receipt_retention_days": 0,
            "whitelist_globs": [],
        },
    }


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    _write_receipt(path, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root")
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--max-mb", type=int)
    parser.add_argument("--receipt-retention-days", type=int)
    parser.add_argument("--whitelist-globs")
    parser.add_argument("--summary-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config, blockers = config_from_env(args)
    if config is None:
        payload = _blocked_payload(blockers)
        print(json.dumps(payload, sort_keys=True))
        return 2
    started = datetime.now(UTC)
    payload = run_retention(config, now=started)
    receipt_name = f"{RECEIPT_FILENAME_PREFIX}{_stamp_for_filename(started)}{RECEIPT_FILENAME_SUFFIX}"
    receipt_path = config.evidence_root / RECEIPT_DIR_NAME / receipt_name
    _write_receipt(receipt_path, payload)
    if config.summary_path is not None:
        _write_summary(config.summary_path, payload)
    print(json.dumps(payload, sort_keys=True))
    return 1 if payload.get("partial_failure") else 0


if __name__ == "__main__":
    raise SystemExit(main())
