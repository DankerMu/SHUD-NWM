"""Forecast data retention cleanup.

Removes aged per-cycle forecast artifacts (raw source data and compute
intermediates) from the object store while preserving published display
products and static assets.

Safety posture (never-break-userspace):
- Disabled by default; deletion only happens when explicitly enabled and
  dry-run is explicitly turned off.
- Age is determined from the cycle directory name (``%Y%m%d%H``). When the age
  cannot be determined the artifact is skipped, never deleted.
- Published artifacts (``tiles/``, published artifact root) and static assets
  (``canonical/{source}/grid/``) are always protected.
- Artifacts at or after the pipeline's active lower bound are exempt even when
  they are older than the wall-clock cutoff (issue #1307): during replay
  catch-up the frontier lags wall clock, so a pure wall-clock criterion deletes
  what the very same pass just produced. The bound is supplied by the caller
  (``active_lower_bound``); ``None`` keeps the historical pure wall-clock
  behaviour.
- Individual deletion failures are recorded and do not abort the pass.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Per-cycle prefixes whose second path segment ({source}) contains cycle
# directories named ``%Y%m%d%H``. Confirmed against worker key construction:
#   raw/{source}/{cycle}/...        (workers/data_adapters/*_adapter.py)
#   canonical/{source}/{cycle}/...  (workers/canonical_converter/converter.py)
#   forcing/{source}/{cycle}/...    (workers/forcing_producer/producer.py)
CYCLE_SCOPED_PREFIXES: tuple[str, ...] = ("raw", "canonical", "forcing")

# ``runs/{run_id}/...`` holds per-run workspace artifacts (chain.py). Run ids
# embed the compact cycle, e.g. ``fcst_gfs_2026051600_<model>`` or carry a
# trailing ``_%Y%m%d%H`` token.
RUNS_PREFIX = "runs"

# Always-protected top-level prefixes (published display products).
PROTECTED_PREFIXES: frozenset[str] = frozenset({"tiles", "states"})

# Static asset segment under a cycle-scoped source (e.g.
# ``canonical/gfs/grid/gfs_0p25/grid.json``). Never treated as a cycle.
STATIC_SEGMENTS: frozenset[str] = frozenset({"grid"})

CYCLE_NAME_LENGTH = 10  # len("%Y%m%d%H")

# Skip reason for artifacts that aged past the wall-clock cutoff but are at or
# after the pipeline's active lower bound (issue #1307 / design D4). Named from
# the *protection* side: "below the frontier" is the collectable side, so the
# issue's wording (``below_pipeline_frontier``) would read inverted here.
PIPELINE_FRONTIER_EXEMPT_REASON = "pipeline_frontier_exempt"


@dataclass
class RetentionConfig:
    """Resolved retention behaviour."""

    enabled: bool
    dry_run: bool
    retention_days: int

    @classmethod
    def from_env(cls) -> RetentionConfig:
        return cls(
            enabled=_env_flag("NHMS_RETENTION_ENABLED", default=False),
            dry_run=_env_flag("NHMS_RETENTION_DRY_RUN", default=True),
            retention_days=_env_int("NHMS_RETENTION_DAYS", default=14),
        )


@dataclass
class RetentionTarget:
    """A single artifact path selected (or considered) for removal."""

    path: Path
    key: str
    cycle_time: datetime
    reason: str
    size_bytes: int


@dataclass
class RetentionResult:
    """Structured outcome of a retention pass."""

    enabled: bool
    dry_run: bool
    retention_days: int
    cutoff: str
    planned: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    freed_bytes: int = 0
    active_lower_bound: datetime | None = None
    active_lower_bound_source: str | None = None

    def frontier(self) -> dict[str, Any]:
        """Receipt block describing the frontier bound applied to this plan.

        ``protected_count`` is derived from the recorded skips so the count can
        never drift from the entries it summarises. A ``None`` bound records a
        null source too: no bound was applied, so no source produced it.
        """
        bound = self.active_lower_bound
        return {
            "active_lower_bound": None if bound is None else bound.astimezone(UTC).isoformat(),
            "source": None if bound is None else self.active_lower_bound_source,
            "protected_count": sum(
                1 for entry in self.skipped if entry.get("reason") == PIPELINE_FRONTIER_EXEMPT_REASON
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "nhms.production_scheduler.retention.v1",
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "retention_days": self.retention_days,
            "cutoff": self.cutoff,
            "frontier": self.frontier(),
            "counts": {
                "planned": len(self.planned),
                "deleted": len(self.deleted),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
            "planned": self.planned,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
            "freed_bytes": self.freed_bytes,
        }


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _parse_cycle_name(name: str) -> datetime | None:
    """Parse a ``%Y%m%d%H`` cycle directory name, or None if not a cycle.

    Scope note (intentional): retention only cleans **forecast cycles** named
    ``%Y%m%d%H``. Reanalysis/reference ``date_key`` directories (``%Y-%m-%d``)
    are deliberately out of scope and must be retained. Such names fail the
    length/``isdigit`` checks below and are therefore never selected for
    deletion. This is a design choice, not an oversight.
    """
    if len(name) != CYCLE_NAME_LENGTH or not name.isdigit():
        return None
    try:
        return datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _extract_run_cycle(run_id: str) -> datetime | None:
    """Find an embedded ``%Y%m%d%H`` token inside a run id."""
    for token in run_id.split("_"):
        parsed = _parse_cycle_name(token)
        if parsed is not None:
            return parsed
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


def _frontier_exempt_entry(key: str, path: Path, cycle_time: datetime) -> dict[str, Any]:
    """Skip entry for a target protected by the pipeline frontier.

    Deliberately carries no ``size_bytes``: the exemption is adjudicated before
    ``_dir_size``, so a protected directory is never rglob/stat-walked. During
    catch-up the protected directories are the largest and hottest ones, and
    the walk runs over NFS every pass (design D4).
    """
    return {
        "key": key,
        "path": str(path),
        "cycle_time": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": PIPELINE_FRONTIER_EXEMPT_REASON,
    }


def _collect_cycle_targets(
    root: Path,
    cutoff: datetime,
    active_lower_bound: datetime | None = None,
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Scan cycle-scoped prefixes (raw/canonical/forcing)."""
    targets: list[RetentionTarget] = []
    skipped: list[dict[str, Any]] = []
    for prefix in CYCLE_SCOPED_PREFIXES:
        prefix_root = root / prefix
        if not prefix_root.is_dir():
            continue
        for source_dir in _iter_dirs(prefix_root):
            for cycle_dir in _iter_dirs(source_dir):
                _classify_cycle_dir(
                    cycle_dir,
                    prefix,
                    root,
                    cutoff,
                    targets,
                    skipped,
                    active_lower_bound=active_lower_bound,
                )
    return targets, skipped


def _classify_cycle_dir(
    cycle_dir: Path,
    prefix: str,
    root: Path,
    cutoff: datetime,
    targets: list[RetentionTarget],
    skipped: list[dict[str, Any]],
    *,
    active_lower_bound: datetime | None = None,
) -> None:
    key = cycle_dir.relative_to(root).as_posix()
    if cycle_dir.name in STATIC_SEGMENTS:
        skipped.append({"key": key, "reason": "static_asset_protected"})
        return
    cycle_time = _parse_cycle_name(cycle_dir.name)
    if cycle_time is None:
        skipped.append({"key": key, "reason": "unparseable_cycle_name"})
        return
    # Adjudication order (design D4): not-yet-expired first so the two skip
    # reasons stay distinguishable, frontier exemption second, deletion last.
    if cycle_time >= cutoff:
        skipped.append({"key": key, "reason": "within_retention_window"})
        return
    if active_lower_bound is not None and cycle_time >= active_lower_bound:
        skipped.append(_frontier_exempt_entry(key, cycle_dir, cycle_time))
        return
    targets.append(
        RetentionTarget(
            path=cycle_dir,
            key=key,
            cycle_time=cycle_time,
            reason=f"{prefix}_cycle_aged_out",
            size_bytes=_dir_size(cycle_dir),
        )
    )


def _collect_run_targets(
    root: Path,
    cutoff: datetime,
    active_lower_bound: datetime | None = None,
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Scan per-run workspace directories under ``runs/``."""
    targets: list[RetentionTarget] = []
    skipped: list[dict[str, Any]] = []
    runs_root = root / RUNS_PREFIX
    if not runs_root.is_dir():
        return targets, skipped
    for run_dir in _iter_dirs(runs_root):
        key = run_dir.relative_to(root).as_posix()
        cycle_time = _extract_run_cycle(run_dir.name)
        if cycle_time is None:
            skipped.append({"key": key, "reason": "unparseable_run_cycle"})
            continue
        # Same two-level adjudication as cycle targets (design D2/D4): a failed
        # run workspace whose cycle is still in flight keeps its SHUD
        # stdout/stderr readable for post-mortem.
        if cycle_time >= cutoff:
            skipped.append({"key": key, "reason": "within_retention_window"})
            continue
        if active_lower_bound is not None and cycle_time >= active_lower_bound:
            skipped.append(_frontier_exempt_entry(key, run_dir, cycle_time))
            continue
        targets.append(
            RetentionTarget(
                path=run_dir,
                key=key,
                cycle_time=cycle_time,
                reason="run_cycle_aged_out",
                size_bytes=_dir_size(run_dir),
            )
        )
    return targets, skipped


def _iter_dirs(parent: Path) -> list[Path]:
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if entry.is_dir() and not entry.is_symlink()]


def plan_retention(
    *,
    object_store_root: Path | str | None,
    cutoff: datetime,
    retention_days: int,
    enabled: bool,
    dry_run: bool,
    published_artifact_root: Path | str | None = None,
    active_lower_bound: datetime | None = None,
    active_lower_bound_source: str | None = None,
) -> RetentionResult:
    """Build a retention plan (no deletion performed).

    ``active_lower_bound`` is the pipeline's active lower bound: cycles at or
    after it are exempt from deletion regardless of wall-clock age. ``None``
    (the default, and what any caller outside a scheduler pass gets) keeps the
    pure wall-clock criterion. ``active_lower_bound_source`` is a free-form
    label recorded in the receipt so the receipt says *why* the bound is where
    it is; retention itself never interprets it, which keeps this module
    scheduler-agnostic.
    """
    bound = _normalize_bound(active_lower_bound)
    result = RetentionResult(
        enabled=enabled,
        dry_run=dry_run,
        retention_days=retention_days,
        cutoff=cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        active_lower_bound=bound,
        active_lower_bound_source=active_lower_bound_source,
    )
    if object_store_root is None:
        return result
    root = Path(object_store_root).expanduser().resolve()
    if not root.is_dir():
        return result

    published_resolved = (
        Path(published_artifact_root).expanduser().resolve()
        if published_artifact_root is not None
        else None
    )

    cycle_targets, cycle_skipped = _collect_cycle_targets(root, cutoff, bound)
    run_targets, run_skipped = _collect_run_targets(root, cutoff, bound)
    result.skipped.extend(cycle_skipped)
    result.skipped.extend(run_skipped)

    for target in [*cycle_targets, *run_targets]:
        if _is_protected(target.path, root, published_resolved):
            result.skipped.append({"key": target.key, "reason": "protected_path"})
            continue
        result.planned.append(_target_payload(target))
    return result


def _normalize_bound(value: datetime | None) -> datetime | None:
    """Normalise a caller-supplied bound to UTC-aware, or None."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_protected(path: Path, root: Path, published_resolved: Path | None) -> bool:
    parts = path.relative_to(root).parts
    if parts and parts[0] in PROTECTED_PREFIXES:
        return True
    if published_resolved is not None:
        try:
            path.resolve().relative_to(published_resolved)
            return True
        except ValueError:
            pass
    return False


def _target_payload(target: RetentionTarget) -> dict[str, Any]:
    return {
        "key": target.key,
        "path": str(target.path),
        "cycle_time": target.cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": target.reason,
        "size_bytes": target.size_bytes,
    }


def run_retention(
    *,
    object_store_root: Path | str | None,
    now: datetime,
    config: RetentionConfig | None = None,
    published_artifact_root: Path | str | None = None,
    active_lower_bound: datetime | None = None,
    active_lower_bound_source: str | None = None,
) -> RetentionResult:
    """Plan and (when enabled and not dry-run) execute retention cleanup.

    Never raises for individual deletion failures; they are recorded in the
    result so the scheduler pass is not interrupted.

    ``active_lower_bound`` / ``active_lower_bound_source`` are forwarded to
    :func:`plan_retention` unchanged; omitting them keeps the historical pure
    wall-clock behaviour.
    """
    resolved = config or RetentionConfig.from_env()
    cutoff = now.astimezone(UTC) - timedelta(days=resolved.retention_days)
    result = plan_retention(
        object_store_root=object_store_root,
        cutoff=cutoff,
        retention_days=resolved.retention_days,
        enabled=resolved.enabled,
        dry_run=resolved.dry_run,
        published_artifact_root=published_artifact_root,
        active_lower_bound=active_lower_bound,
        active_lower_bound_source=active_lower_bound_source,
    )
    if not resolved.enabled or resolved.dry_run:
        return result
    for entry in result.planned:
        _delete_entry(entry, result)
    return result


def _delete_entry(entry: dict[str, Any], result: RetentionResult) -> None:
    path = Path(entry["path"])
    try:
        shutil.rmtree(path)
    except OSError as error:
        result.failed.append({**entry, "error": str(error)})
        return
    result.deleted.append(entry)
    result.freed_bytes += int(entry.get("size_bytes", 0))
