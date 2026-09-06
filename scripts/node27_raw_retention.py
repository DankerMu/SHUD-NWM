#!/usr/bin/env python
"""Retention cleanup for node-27-owned raw bundles, canonical precipitation
mirrors and their rendered PNG cache.

This script targets exactly three lanes, all on ONE cutoff in ONE run:

    <object-store-root>/raw/<source>/<YYYYMMDDHH>
    <object-store-root>/canonical/<storage-source>/<YYYYMMDDHH>
    <precip-cache-root>/precip/<storage-source>/<YYYYMMDDHH>

A cycle's canonical mirror directory and its PNG cache directory therefore live
and die together (issue #2011): the display API cannot keep serving rendered
precipitation for a cycle whose mirror is gone.

It deliberately does not touch `canonical/<storage-source>/grid/**` (grid
definitions cannot be regenerated on node-27), anything under the precipitation
cache root outside `precip/` (the MVT tile cache is a sibling there), forcing,
runs, or published products. Production retention deletes aged directories after
safety preflight and emits bounded JSON evidence for operator review.

Two environment gates exist for staged rollout and rollback and default to the
execute-only behaviour: ``NODE27_RAW_RETENTION_ENABLED`` (default true) and
``NODE27_RAW_RETENTION_PLAN_ONLY`` (default false). The cutoff anchor is the
display watermark, not the pipeline frontier; every summary discloses that
choice and its residual risk in its ``anchor`` block (issue #1407).
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

from packages.common.display_watermark import fetch_display_watermark
from packages.common.source_identity import normalize_source_id

# `services.precip.constants` is deliberately stdlib-only (its own docstring
# pins that), so naming the cache env here costs no numpy/netCDF4 import.
from services.precip.constants import FILE_CACHE_DIR_ENV

SCHEMA_VERSION = "nhms.node27_raw_retention.production.v4"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_SOURCES = ("gfs", "ifs")
CYCLE_NAME_LENGTH = 10

# Lane identity: (target key prefix, `planned[].reason`).
RAW_LANE_KEY = "raw"
RAW_LANE_REASON = "raw_cycle_aged_out"
CANONICAL_LANE_KEY = "canonical"
CANONICAL_LANE_REASON = "canonical_cycle_aged_out"
PRECIP_CACHE_LANE_KEY = "precip-cache"
PRECIP_CACHE_LANE_REASON = "precip_cache_aged_out"
# The one directory name under `canonical/<S>/` that is never a cycle: grid
# definitions are not reproducible from node-27, so they are pinned out of the
# target set explicitly instead of relying on `_parse_cycle_name` alone.
GRID_DIR_NAME = "grid"

# Anchor disclosure (issue #1407 / design D4). This process keeps the display
# watermark as its cutoff anchor instead of the pipeline frontier used by the
# out-of-pass cleanup CLI: the scheduler pass receipts and journal live on
# node-22 private /scratch, which node-27 cannot reach, and publishing the
# frontier across nodes needs a shared-store write surface that is out of scope
# here. The residual risk is disclosed in every summary rather than left
# implicit.
ANCHOR_MODE = "display_watermark"
ANCHOR_DECISION = "issue-1407-keep-watermark-anchor"
ANCHOR_RESIDUAL_RISK = "backfill cycles older than watermark - retention_days are unprotected"

# #1714: default pg_stat_activity attribution for this component. libpq treats
# fallback_application_name as a default only, so an operator's explicit
# ?application_name=... in NODE27_DISPLAY_WATERMARK_DATABASE_URL still wins.
_APPLICATION_NAME = "nhms-raw-retention"


def _attributed_connect(*args: Any, **kwargs: Any) -> Any:
    """``psycopg2.connect`` with this component's #1714 identity attached.

    This runner never opens a connection itself; its ONLY database touch is
    the watermark read delegated to
    ``packages.common.display_watermark.fetch_display_watermark``. Injecting
    this callable is what keeps that delegated connection attributable in
    ``pg_stat_activity``.

    psycopg2 is imported lazily so importing this module stays possible without
    the driver, exactly as ``display_watermark`` does it.
    """
    import psycopg2  # type: ignore[import-untyped]

    return psycopg2.connect(*args, fallback_application_name=_APPLICATION_NAME, **kwargs)


@dataclass(frozen=True)
class RawRetentionConfig:
    object_store_root: Path
    retention_days: int
    sources: frozenset[str]
    summary_path: Path | None
    # Env gates (issue #1407 / design D5). Defaults reproduce the execute-only
    # behaviour byte for byte; they exist for staged rollout and rollback, and
    # deliberately have no CLI flags (the ones 9c1625ee removed stay removed).
    enabled: bool = True
    dry_run: bool = False
    # Display-side PNG cache root (`NHMS_MVT_FILE_CACHE_DIR`). Optional on
    # purpose: an unconfigured cache root is a per-lane skip, never a preflight
    # blocker, so the first production tick after deploy still prunes raw and
    # canonical instead of returning zero deletions.
    precip_cache_root: Path | None = None


@dataclass(frozen=True)
class RetentionTarget:
    path: Path
    key: str
    source: str
    cycle_time: datetime
    size_bytes: int
    reason: str


def _env_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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

    # Same env name the display API reads (`services.precip.constants` /
    # `services.tiles.mvt`); it must hold the value the display PROCESS has, not
    # the value in infra/env/display.example. Blank or unset is not a blocker --
    # the cache lane skips itself and the other two lanes still prune.
    cache_value = (os.getenv(FILE_CACHE_DIR_ENV) or "").strip()
    precip_cache_root = Path(cache_value).expanduser() if cache_value else None

    if blockers or resolved_root is None:
        return None, blockers
    return (
        RawRetentionConfig(
            object_store_root=resolved_root,
            retention_days=retention_days,
            sources=sources,
            summary_path=summary_path,
            enabled=_env_flag("NODE27_RAW_RETENTION_ENABLED", default=True),
            # Deliberately not the retired NODE27_RAW_RETENTION_DRY_RUN name: that
            # one defaulted to true, so a leftover line in node-27's live config
            # would silently return production to zero deletions. Under the new
            # name any such leftover line is inert.
            dry_run=_env_flag("NODE27_RAW_RETENTION_PLAN_ONLY", default=False),
            precip_cache_root=precip_cache_root,
        ),
        [],
    )


def _safe_target(lane_root: Path, target: Path) -> bool:
    """True only for a real `<lane_root>/<S>/<K>` directory (never a symlink).

    The parts count is what pins every lane at exactly two levels below its own
    root, so no lane can ever reach a sibling tree or a grid definition.
    """
    try:
        relative = target.resolve(strict=True).relative_to(lane_root)
    except (OSError, ValueError):
        return False
    return len(relative.parts) == 2 and target.is_dir() and not target.is_symlink()


def _resolve_lane_root(
    root: Path, *, key: str, prefix: str
) -> tuple[Path | None, dict[str, Any] | None]:
    """The resolved lane root, or the skip entry that retires ONLY this lane.

    Absence and unsafety are symmetric across the three lanes and always local:
    making any of them a preflight blocker would zero out raw retention on the
    first production tick after deploy, before an operator has edited the env
    file -- strictly worse than not pruning a cache.
    """
    if root.is_symlink():
        return None, {
            "key": key,
            "reason": f"{prefix}_root_unsafe",
            "path": str(root),
            "detail": "path_is_symlink",
        }
    if not root.exists():
        return None, {"key": key, "reason": f"{prefix}_root_missing", "path": str(root)}
    if not root.is_dir():
        return None, {
            "key": key,
            "reason": f"{prefix}_root_unsafe",
            "path": str(root),
            "detail": "path_not_directory",
        }
    resolved, blocker = _safe_resolved_dir(root, label=f"{prefix}_root")
    if resolved is None:
        return None, {
            "key": key,
            "reason": f"{prefix}_root_unsafe",
            "path": str(root),
            "detail": (blocker or {}).get("reason", "path_unsafe"),
        }
    return resolved, None


def _collect_raw_lane(
    config: RawRetentionConfig, *, raw_root: Path, cutoff: datetime
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Raw lane: iterate the directories that exist and match them case-insensitively."""
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []
    for source_dir in _iter_dirs(raw_root):
        source_key = source_dir.name.lower()
        if source_key not in config.sources:
            skipped.append({"key": f"{RAW_LANE_KEY}/{source_dir.name}", "reason": "source_not_enabled"})
            continue
        for cycle_dir in _iter_dirs(source_dir):
            key = f"{RAW_LANE_KEY}/{source_dir.name}/{cycle_dir.name}"
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
                    reason=RAW_LANE_REASON,
                )
            )
    return targets, skipped


def _collect_mapped_lane(
    config: RawRetentionConfig,
    *,
    lane_root: Path,
    key_prefix: str,
    reason: str,
    cutoff: datetime,
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Canonical / PNG-cache lane: enumerate CONFIGURED sources through
    `normalize_source_id`, never the directory names found on disk.

    That direction is the constructive guarantee that no `canonical/ifs/...` or
    `precip-cache/ifs/...` path can be produced from the lower-case configured
    token: the storage spelling can only come out of the shared normalizer, so
    the mirror tree and the PNG cache tree are addressed by one identity.
    """
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []
    for source in sorted(config.sources):
        try:
            storage_source = normalize_source_id(source)
        except ValueError:
            # `_split_sources` accepts arbitrary free text from the env file; an
            # unmappable entry retires itself, never the run.
            skipped.append({"key": f"{key_prefix}/{source}", "reason": "source_unmappable"})
            continue
        source_root = lane_root / storage_source
        if source_root.is_symlink() or not source_root.is_dir():
            continue
        for cycle_dir in _iter_dirs(source_root):
            key = f"{key_prefix}/{storage_source}/{cycle_dir.name}"
            if cycle_dir.name == GRID_DIR_NAME:
                skipped.append({"key": key, "reason": "grid_definitions_preserved"})
                continue
            cycle_time = _parse_cycle_name(cycle_dir.name)
            if cycle_time is None:
                skipped.append({"key": key, "reason": "unparseable_cycle_name"})
                continue
            if cycle_time >= cutoff:
                skipped.append({"key": key, "reason": "within_retention_window"})
                continue
            if not _safe_target(lane_root, cycle_dir):
                skipped.append({"key": key, "reason": "unsafe_target_path"})
                continue
            targets.append(
                RetentionTarget(
                    path=cycle_dir,
                    key=key,
                    source=storage_source,
                    cycle_time=cycle_time,
                    size_bytes=_dir_size(cycle_dir),
                    reason=reason,
                )
            )
    return targets, skipped


def collect_targets(config: RawRetentionConfig, *, now: datetime) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """The three lanes of one retention run, on ONE cutoff.

    Raw, canonical and PNG-cache targets are collected together so a cycle's
    mirror directory and its rendered PNGs are removed by the same tick.
    """
    cutoff = now.astimezone(UTC) - timedelta(days=config.retention_days)
    skipped: list[dict[str, Any]] = []
    targets: list[RetentionTarget] = []

    raw_root, raw_blocker = _resolve_lane_root(
        config.object_store_root / "raw", key=RAW_LANE_KEY, prefix="raw"
    )
    if raw_blocker is not None:
        skipped.append(raw_blocker)
    elif raw_root is not None:
        lane_targets, lane_skipped = _collect_raw_lane(config, raw_root=raw_root, cutoff=cutoff)
        targets.extend(lane_targets)
        skipped.extend(lane_skipped)

    canonical_root, canonical_blocker = _resolve_lane_root(
        config.object_store_root / "canonical", key=CANONICAL_LANE_KEY, prefix="canonical"
    )
    if canonical_blocker is not None:
        skipped.append(canonical_blocker)
    elif canonical_root is not None:
        lane_targets, lane_skipped = _collect_mapped_lane(
            config,
            lane_root=canonical_root,
            key_prefix=CANONICAL_LANE_KEY,
            reason=CANONICAL_LANE_REASON,
            cutoff=cutoff,
        )
        targets.extend(lane_targets)
        skipped.extend(lane_skipped)

    if config.precip_cache_root is None:
        skipped.append({"key": PRECIP_CACHE_LANE_KEY, "reason": "precip_cache_root_unconfigured"})
        return targets, skipped
    # Only ever descend into `<cache>/precip`: the MVT tile cache is a sibling
    # under the same root and must never be enumerated, let alone deleted.
    cache_root, cache_blocker = _resolve_lane_root(
        config.precip_cache_root / "precip", key=PRECIP_CACHE_LANE_KEY, prefix="precip_cache"
    )
    if cache_blocker is not None:
        skipped.append(cache_blocker)
    elif cache_root is not None:
        lane_targets, lane_skipped = _collect_mapped_lane(
            config,
            lane_root=cache_root,
            key_prefix=PRECIP_CACHE_LANE_KEY,
            reason=PRECIP_CACHE_LANE_REASON,
            cutoff=cutoff,
        )
        targets.extend(lane_targets)
        skipped.extend(lane_skipped)
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
        "reason": target.reason,
    }


def run_retention(
    config: RawRetentionConfig,
    *,
    now: datetime,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    started_at = now.astimezone(UTC)
    reference_time = (reference_time or started_at).astimezone(UTC)
    cutoff = reference_time - timedelta(days=config.retention_days)
    base = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reference_time": reference_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "object_store_root": str(config.object_store_root),
        "raw_root": str(config.object_store_root / "raw"),
        "canonical_root": str(config.object_store_root / "canonical"),
        "precip_cache_root": (
            None if config.precip_cache_root is None else str(config.precip_cache_root)
        ),
        "sources": sorted(config.sources),
        "retention_days": config.retention_days,
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enabled": config.enabled,
        "dry_run": config.dry_run,
        "anchor": _anchor_disclosure(reference_time),
    }
    if not config.enabled:
        return {
            **base,
            "status": "disabled",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "execution_mode": "disabled",
            "counts": {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0},
            "planned": [],
            "deleted": [],
            "skipped": [],
            "failed": [],
            "freed_bytes": 0,
        }
    targets, skipped = collect_targets(config, now=reference_time)
    planned = [_target_payload(target) for target in targets]
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    freed_bytes = 0
    if not config.dry_run:
        for target, payload in zip(targets, planned, strict=True):
            try:
                shutil.rmtree(target.path)
            except OSError as error:
                # Known limit (measured on node-27, 2026-09-06): the canonical
                # tree is `755 frd_muziyao nfsdata` while this runner is `nwm`,
                # so every canonical target fails here with PermissionError on
                # each tick. `error_type` keeps that distinguishable from other
                # IO failures in the receipt. The remedy is a directory-mode or
                # group change on the mirror producers (#2008/#2069) or an ops
                # group membership change -- both outside this script.
                failed.append({**payload, "error": str(error), "error_type": type(error).__name__})
                continue
            deleted.append(payload)
            freed_bytes += int(payload["size_bytes"])
    finished_at = datetime.now(UTC)
    return {
        **base,
        "status": "completed",
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "execution_mode": "plan_only" if config.dry_run else "production_execute",
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


def _anchor_disclosure(reference_time: datetime) -> dict[str, Any]:
    """Say in the summary which anchor bounded this run, and what it misses."""
    return {
        "mode": ANCHOR_MODE,
        "reference_time": reference_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frontier_active_lower_bound": None,
        "decision": ANCHOR_DECISION,
        "residual_risk": ANCHOR_RESIDUAL_RISK,
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
    parser.add_argument("--reference-time")
    return parser


def _parse_reference_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("reference time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reference time must be timezone-aware")
    return parsed.astimezone(UTC)


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
    try:
        reference_time = (
            _parse_reference_time(args.reference_time)
            if args.reference_time is not None
            else fetch_display_watermark(
                os.getenv("NODE27_DISPLAY_WATERMARK_DATABASE_URL", ""),
                connect=_attributed_connect,
            )
        )
    except Exception as error:
        payload = _blocked_payload(
            [{"field": "display_watermark", "reason": type(error).__name__}]
        )
        if config.summary_path is not None:
            _write_summary(config.summary_path, payload)
        print(json.dumps(payload, sort_keys=True))
        return 2
    payload = run_retention(
        config, now=datetime.now(UTC), reference_time=reference_time
    )
    if config.summary_path is not None:
        _write_summary(config.summary_path, payload)
    print(json.dumps(payload, sort_keys=True))
    return 1 if payload["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
