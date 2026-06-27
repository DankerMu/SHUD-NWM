#!/usr/bin/env python
"""Retention cleanup for node-27-owned raw forecast bundles.

This script only targets source raw data under:

    <object-store-root>/raw/<source>/<YYYYMMDDHH>

It deliberately does not touch canonical, forcing, runs, published products, or
static grids. Production retention always deletes aged raw cycles after safety
preflight and emits bounded JSON evidence for operator review.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "nhms.node27_raw_retention.production.v1"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_SOURCES = ("gfs", "ifs")
CYCLE_NAME_LENGTH = 10


@dataclass(frozen=True)
class RawRetentionConfig:
    object_store_root: Path
    retention_days: int
    sources: frozenset[str]
    summary_path: Path | None


@dataclass(frozen=True)
class RetentionTarget:
    path: Path
    key: str
    source: str
    cycle_time: datetime
    size_bytes: int


def _env_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _split_sources(raw: str | None) -> frozenset[str]:
    values = raw if raw not in (None, "") else ",".join(DEFAULT_SOURCES)
    sources = {item.strip().lower() for item in str(values).split(",") if item.strip()}
    return frozenset(sources or DEFAULT_SOURCES)


def _parse_cycle_name(name: str) -> datetime | None:
    if len(name) != CYCLE_NAME_LENGTH or not name.isdigit():
        return None
    try:
        return datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


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


def config_from_env(args: argparse.Namespace) -> tuple[RawRetentionConfig | None, list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    root_value = (
        args.object_store_root
        or os.getenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT")
        or os.getenv("OBJECT_STORE_ROOT")
        or ""
    ).strip()
    if not root_value:
        blockers.append({"field": "object_store_root", "reason": "missing"})
        root = Path()
    else:
        root = Path(root_value)
    resolved_root, blocker = _safe_resolved_dir(root, label="object_store_root")
    if blocker is not None:
        blockers.append(blocker)

    retention_days = args.retention_days or _env_int("NODE27_RAW_RETENTION_DAYS", default=DEFAULT_RETENTION_DAYS)
    if retention_days <= 0:
        blockers.append({"field": "retention_days", "reason": "must_be_positive", "value": retention_days})

    summary_value = args.summary_path or os.getenv("NODE27_RAW_RETENTION_SUMMARY_PATH") or ""
    summary_path = Path(summary_value).expanduser() if summary_value.strip() else None
    if summary_path is not None and not summary_path.is_absolute():
        blockers.append({"field": "summary_path", "reason": "path_not_absolute", "path": str(summary_path)})

    sources = _split_sources(args.sources or os.getenv("NODE27_RAW_RETENTION_SOURCES"))
    if not sources:
        blockers.append({"field": "sources", "reason": "empty"})

    if blockers or resolved_root is None:
        return None, blockers
    return (
        RawRetentionConfig(
            object_store_root=resolved_root,
            retention_days=retention_days,
            sources=sources,
            summary_path=summary_path,
        ),
        [],
    )


def _safe_target(raw_root: Path, target: Path) -> bool:
    try:
        relative = target.resolve(strict=True).relative_to(raw_root)
    except (OSError, ValueError):
        return False
    return len(relative.parts) == 2 and target.is_dir() and not target.is_symlink()


def collect_targets(config: RawRetentionConfig, *, now: datetime) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    raw_root = config.object_store_root / "raw"
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []
    if not raw_root.is_dir():
        skipped.append({"key": "raw", "reason": "raw_root_missing", "path": str(raw_root)})
        return targets, skipped
    cutoff = now.astimezone(UTC) - timedelta(days=config.retention_days)
    for source_dir in _iter_dirs(raw_root):
        source_key = source_dir.name.lower()
        if source_key not in config.sources:
            skipped.append({"key": f"raw/{source_dir.name}", "reason": "source_not_enabled"})
            continue
        for cycle_dir in _iter_dirs(source_dir):
            key = f"raw/{source_dir.name}/{cycle_dir.name}"
            cycle_time = _parse_cycle_name(cycle_dir.name)
            if cycle_time is None:
                skipped.append({"key": key, "reason": "unparseable_cycle_name"})
                continue
            if cycle_time >= cutoff:
                skipped.append({"key": key, "reason": "within_retention_window"})
                continue
            if not _safe_target(raw_root, cycle_dir):
                skipped.append({"key": key, "reason": "unsafe_target_path"})
                continue
            targets.append(
                RetentionTarget(
                    path=cycle_dir,
                    key=key,
                    source=source_dir.name,
                    cycle_time=cycle_time,
                    size_bytes=_dir_size(cycle_dir),
                )
            )
    return targets, skipped


def _iter_dirs(parent: Path) -> list[Path]:
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if entry.is_dir() and not entry.is_symlink()]


def _target_payload(target: RetentionTarget) -> dict[str, Any]:
    return {
        "key": target.key,
        "path": str(target.path),
        "source": target.source,
        "cycle_time": target.cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size_bytes": target.size_bytes,
        "reason": "raw_cycle_aged_out",
    }


def run_retention(config: RawRetentionConfig, *, now: datetime) -> dict[str, Any]:
    started_at = now.astimezone(UTC)
    cutoff = started_at - timedelta(days=config.retention_days)
    targets, skipped = collect_targets(config, now=started_at)
    planned = [_target_payload(target) for target in targets]
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    freed_bytes = 0
    for target, payload in zip(targets, planned, strict=True):
        try:
            shutil.rmtree(target.path)
        except OSError as error:
            failed.append({**payload, "error": str(error)})
            continue
        deleted.append(payload)
        freed_bytes += int(payload["size_bytes"])
    finished_at = datetime.now(UTC)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "object_store_root": str(config.object_store_root),
        "raw_root": str(config.object_store_root / "raw"),
        "sources": sorted(config.sources),
        "retention_days": config.retention_days,
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "execution_mode": "production_execute",
        "counts": {
            "planned": len(planned),
            "deleted": len(deleted),
            "skipped": len(skipped),
            "failed": len(failed),
        },
        "planned": planned,
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "freed_bytes": freed_bytes,
    }


def _blocked_payload(blockers: Iterable[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "preflight_blocked",
        "execution_mode": "preflight_blocked",
        "started_at": now,
        "finished_at": now,
        "blockers": list(blockers),
        "counts": {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0},
        "planned": [],
        "deleted": [],
        "skipped": [],
        "failed": [],
        "freed_bytes": 0,
    }


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-store-root")
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--sources")
    parser.add_argument("--summary-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config, blockers = config_from_env(args)
    if config is None:
        payload = _blocked_payload(blockers)
        summary_value = args.summary_path or os.getenv("NODE27_RAW_RETENTION_SUMMARY_PATH") or ""
        if summary_value.strip():
            _write_summary(Path(summary_value).expanduser(), payload)
        print(json.dumps(payload, sort_keys=True))
        return 2
    payload = run_retention(config, now=datetime.now(UTC))
    if config.summary_path is not None:
        _write_summary(config.summary_path, payload)
    print(json.dumps(payload, sort_keys=True))
    return 1 if payload["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
