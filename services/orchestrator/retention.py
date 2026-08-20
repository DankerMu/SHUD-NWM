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
- Additional run-workspace roots (issue #1318) are swept ``runs/``-only under
  their own window, behind a default-off gate. Their cycle-scoped prefixes are
  never touched: the copyback root's ``forcing/`` tree is node-27's live
  disk-only display serving surface.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, rmtree_no_follow
from services.orchestrator.run_identity import parse_run_cycle

# Per-cycle prefixes whose second path segment ({source}) contains cycle
# directories named ``%Y%m%d%H``. Confirmed against worker key construction:
#   raw/{source}/{cycle}/...        (workers/data_adapters/*_adapter.py)
#   canonical/{source}/{cycle}/...  (workers/canonical_converter/converter.py)
#   forcing/{source}/{cycle}/...    (workers/forcing_producer/producer.py)
CYCLE_SCOPED_PREFIXES: tuple[str, ...] = ("raw", "canonical", "forcing")

# ``runs/{run_id}/...`` holds per-run workspace artifacts (chain.py). Only the
# canonical run-id shapes (``services.orchestrator.run_identity``) are admitted
# here, e.g. ``fcst_gfs_2026051600_<model>``; anything else is preserved.
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

# Skip reason for an additional root whose ``runs/`` entry is a symlink (#1318 /
# design D6). ``runs_root.is_dir()`` follows symlinks, so a swapped ``runs/``
# would point the enumeration -- and therefore the deletion surface -- outside
# the root. Recorded rather than silently ignored so the receipt says why the
# root produced nothing.
RUNS_ROOT_SYMLINK_REASON = "runs_root_symlink_skipped"


@dataclass
class RetentionConfig:
    """Resolved retention behaviour.

    The two additional-root fields carry defaults because ``cli.py`` and the
    tests construct this dataclass positionally; that makes it a hazard, not a
    convenience. Every construction point must derive its values from
    :meth:`from_env` (``dataclasses.replace`` on a ``from_env()`` base), or the
    dataclass defaults silently substitute for the operator's environment.
    """

    enabled: bool
    dry_run: bool
    retention_days: int
    extra_roots_enabled: bool = False
    extra_roots_retention_days: int = 30

    @classmethod
    def from_env(cls) -> RetentionConfig:
        return cls(
            enabled=_env_flag("NHMS_RETENTION_ENABLED", default=False),
            dry_run=_env_flag("NHMS_RETENTION_DRY_RUN", default=True),
            retention_days=_env_int("NHMS_RETENTION_DAYS", default=14),
            extra_roots_enabled=_env_flag("NHMS_RETENTION_EXTRA_ROOTS_ENABLED", default=False),
            extra_roots_retention_days=_env_int("NHMS_RETENTION_EXTRA_ROOTS_DAYS", default=30),
        )


@dataclass
class RetentionTarget:
    """A single artifact path selected (or considered) for removal."""

    path: Path
    key: str
    cycle_time: datetime
    reason: str
    size_bytes: int
    root: Path


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
    extra_roots_enabled: bool = False
    extra_roots_retention_days: int = 0
    extra_roots_cutoff: str = ""
    extra_roots: list[str] = field(default_factory=list)

    def extra_roots_block(self) -> dict[str, Any]:
        """Receipt block describing the additional-root window (issue #1318).

        Always present and always populated, gate open or closed: with the gate
        closed ``enabled`` is false and ``roots`` is empty, but the window and
        cutoff still report the *configured* values so a reader can tell what
        opening the gate would reclaim. ``roots`` lists every root that survived
        hygiene (blank discarded) and deduplication, resolved to an absolute
        path -- including ones that do not exist on disk, so a mistyped root is
        visible in the receipt instead of vanishing silently.
        """
        return {
            "enabled": self.extra_roots_enabled,
            "retention_days": self.extra_roots_retention_days,
            "cutoff": self.extra_roots_cutoff,
            "roots": list(self.extra_roots),
        }

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
            "schema_version": "nhms.production_scheduler.retention.v2",
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "retention_days": self.retention_days,
            "cutoff": self.cutoff,
            "frontier": self.frontier(),
            "extra_roots": self.extra_roots_block(),
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
    """Resolve the cycle of a canonical run id, or None (#1405).

    Delegates to the shared canonical shapes so a deletion surface admits only
    names the pipeline actually mints. The previous token scan took the first
    ``_``-separated token that parsed as ``%Y%m%d%H``, which both accepted
    stray non-run directories and could bind a run to the wrong timestamp.
    """
    return parse_run_cycle(run_id)


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _frontier_exempt_entry(key: str, path: Path, cycle_time: datetime, root: Path) -> dict[str, Any]:
    """Skip entry for a target protected by the pipeline frontier.

    Deliberately carries no ``size_bytes``: the exemption is adjudicated before
    ``_dir_size``, so a protected directory is never rglob/stat-walked. During
    catch-up the protected directories are the largest and hottest ones, and
    the walk runs over NFS every pass (design D4). ``root`` is what makes
    identically named ``runs/<run_id>`` entries on different roots
    distinguishable (issue #1318); it costs no filesystem access.
    """
    return {
        "key": key,
        "path": str(path),
        "cycle_time": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": PIPELINE_FRONTIER_EXEMPT_REASON,
        "root": str(root),
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
        skipped.append({"key": key, "root": str(root), "reason": "static_asset_protected"})
        return
    cycle_time = _parse_cycle_name(cycle_dir.name)
    if cycle_time is None:
        skipped.append({"key": key, "root": str(root), "reason": "unparseable_cycle_name"})
        return
    # Adjudication order (design D4): not-yet-expired first so the two skip
    # reasons stay distinguishable, frontier exemption second, deletion last.
    if cycle_time >= cutoff:
        skipped.append({"key": key, "root": str(root), "reason": "within_retention_window"})
        return
    if active_lower_bound is not None and cycle_time >= active_lower_bound:
        skipped.append(_frontier_exempt_entry(key, cycle_dir, cycle_time, root))
        return
    targets.append(
        RetentionTarget(
            path=cycle_dir,
            key=key,
            cycle_time=cycle_time,
            reason=f"{prefix}_cycle_aged_out",
            size_bytes=_dir_size(cycle_dir),
            root=root,
        )
    )


def _collect_run_targets(
    root: Path,
    cutoff: datetime,
    active_lower_bound: datetime | None = None,
    *,
    reject_symlinked_runs_root: bool = False,
) -> tuple[list[RetentionTarget], list[dict[str, Any]]]:
    """Scan per-run workspace directories under ``runs/``.

    ``reject_symlinked_runs_root`` is set for additional roots (issue #1318 /
    design D6): ``Path.is_dir()`` follows symlinks, so a ``runs/`` entry that
    has been replaced by a link would silently extend the enumeration -- and
    the deletion surface -- outside the root. The object-store root keeps its
    historical behaviour; changing it is out of this change's scope.
    """
    targets: list[RetentionTarget] = []
    skipped: list[dict[str, Any]] = []
    runs_root = root / RUNS_PREFIX
    if reject_symlinked_runs_root and runs_root.is_symlink():
        skipped.append({"key": RUNS_PREFIX, "root": str(root), "reason": RUNS_ROOT_SYMLINK_REASON})
        return targets, skipped
    if not runs_root.is_dir():
        return targets, skipped
    for run_dir in _iter_dirs(runs_root):
        key = run_dir.relative_to(root).as_posix()
        cycle_time = _extract_run_cycle(run_dir.name)
        if cycle_time is None:
            skipped.append({"key": key, "root": str(root), "reason": "unparseable_run_cycle"})
            continue
        # Same two-level adjudication as cycle targets (design D2/D4): a failed
        # run workspace whose cycle is still in flight keeps its SHUD
        # stdout/stderr readable for post-mortem.
        if cycle_time >= cutoff:
            skipped.append({"key": key, "root": str(root), "reason": "within_retention_window"})
            continue
        if active_lower_bound is not None and cycle_time >= active_lower_bound:
            skipped.append(_frontier_exempt_entry(key, run_dir, cycle_time, root))
            continue
        targets.append(
            RetentionTarget(
                path=run_dir,
                key=key,
                cycle_time=cycle_time,
                reason="run_cycle_aged_out",
                size_bytes=_dir_size(run_dir),
                root=root,
            )
        )
    return targets, skipped


def _iter_dirs(parent: Path) -> list[Path]:
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if entry.is_dir() and not entry.is_symlink()]


def _resolve_runs_only_roots(
    values: Sequence[Path | str | None],
    *,
    primary: Path | None,
) -> list[Path]:
    """Resolve, sanitise and de-duplicate the additional ``runs/``-only roots.

    Hygiene (issue #1318 task 1.2): ``None``, empty and whitespace-only values
    are discarded **before** ``Path()`` is ever constructed --
    ``Path("").expanduser().resolve()`` is the process working directory, which
    would drag ``<cwd>/runs`` into the deletion surface, and
    ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` is unset (``None``) on any deployment
    that is not db-free.

    De-duplication is by resolved absolute path (design D5): the object-store
    root wins, because it is swept with the fuller cycle-prefix semantics.
    Overlapping-but-unequal roots are *not* rejected -- additional roots only
    ever scan ``<root>/runs``, and ``A/runs`` cannot intersect ``A/b/runs``, so
    equality is the only way to produce a duplicate target.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()
    if primary is not None:
        seen.add(primary)
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        candidate = Path(value).expanduser().resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)
    return resolved


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
    runs_only_roots: Sequence[Path | str | None] = (),
    extra_roots_cutoff: datetime | None = None,
    extra_roots_retention_days: int | None = None,
    extra_roots_enabled: bool = False,
) -> RetentionResult:
    """Build a retention plan (no deletion performed).

    ``active_lower_bound`` is the pipeline's active lower bound: cycles at or
    after it are exempt from deletion regardless of wall-clock age. ``None``
    (the default, and what any caller outside a scheduler pass gets) keeps the
    pure wall-clock criterion. ``active_lower_bound_source`` is a free-form
    label recorded in the receipt so the receipt says *why* the bound is where
    it is; retention itself never interprets it, which keeps this module
    scheduler-agnostic.

    ``runs_only_roots`` are additional run-workspace roots (issue #1318). They
    are swept ``runs/``-only: no cycle-scoped prefix on them is ever considered,
    because the copyback root's ``forcing/`` tree is node-27's live display
    serving surface. They use ``extra_roots_cutoff`` /
    ``extra_roots_retention_days``, which default to the object-store window
    when a direct caller supplies neither. The adjudication order, the frontier
    exemption and the protected-path check are identical on every root.
    ``extra_roots_enabled`` is recorded verbatim in the receipt; gating happens
    in :func:`run_retention`, which passes an empty root sequence when the gate
    is closed.
    """
    bound = _normalize_bound(active_lower_bound)
    extra_cutoff = extra_roots_cutoff if extra_roots_cutoff is not None else cutoff
    extra_days = (
        extra_roots_retention_days if extra_roots_retention_days is not None else retention_days
    )
    primary_root = (
        Path(object_store_root).expanduser().resolve() if object_store_root is not None else None
    )
    extra_roots = _resolve_runs_only_roots(runs_only_roots, primary=primary_root)
    result = RetentionResult(
        enabled=enabled,
        dry_run=dry_run,
        retention_days=retention_days,
        cutoff=cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        active_lower_bound=bound,
        active_lower_bound_source=active_lower_bound_source,
        extra_roots_enabled=extra_roots_enabled,
        extra_roots_retention_days=extra_days,
        extra_roots_cutoff=extra_cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        extra_roots=[str(root) for root in extra_roots],
    )

    published_resolved = (
        Path(published_artifact_root).expanduser().resolve()
        if published_artifact_root is not None
        else None
    )

    if primary_root is not None and primary_root.is_dir():
        cycle_targets, cycle_skipped = _collect_cycle_targets(primary_root, cutoff, bound)
        run_targets, run_skipped = _collect_run_targets(primary_root, cutoff, bound)
        result.skipped.extend(cycle_skipped)
        result.skipped.extend(run_skipped)
        _record_targets(result, [*cycle_targets, *run_targets], primary_root, published_resolved)

    # Deliberately *after* the object-store root but *outside* its availability
    # check (task 1.2d): an unconfigured or missing OBJECT_STORE_ROOT used to
    # return early, which would have made the additional roots silently dead on
    # exactly the CLI path where OBJECT_STORE_ROOT is an unvalidated getenv.
    for extra_root in extra_roots:
        extra_targets, extra_skipped = _collect_run_targets(
            extra_root,
            extra_cutoff,
            bound,
            reject_symlinked_runs_root=True,
        )
        result.skipped.extend(extra_skipped)
        _record_targets(result, extra_targets, extra_root, published_resolved)
    return result


def _record_targets(
    result: RetentionResult,
    targets: Sequence[RetentionTarget],
    root: Path,
    published_resolved: Path | None,
) -> None:
    for target in targets:
        if _is_protected(target.path, root, published_resolved):
            result.skipped.append(
                {"key": target.key, "root": str(root), "reason": "protected_path"}
            )
            continue
        result.planned.append(_target_payload(target))


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
        "root": str(target.root),
    }


def run_retention(
    *,
    object_store_root: Path | str | None,
    now: datetime,
    config: RetentionConfig | None = None,
    published_artifact_root: Path | str | None = None,
    active_lower_bound: datetime | None = None,
    active_lower_bound_source: str | None = None,
    runs_only_roots: Sequence[Path | str | None] = (),
) -> RetentionResult:
    """Plan and (when enabled and not dry-run) execute retention cleanup.

    Never raises for individual deletion failures; they are recorded in the
    result so the scheduler pass is not interrupted.

    ``active_lower_bound`` / ``active_lower_bound_source`` are forwarded to
    :func:`plan_retention` unchanged; omitting them keeps the historical pure
    wall-clock behaviour.

    ``runs_only_roots`` are the additional run-workspace roots (issue #1318).
    They are swept only when ``config.extra_roots_enabled`` is true; with the
    gate closed an empty sequence is forwarded, so the plan is identical key
    for key to the pre-#1318 plan. The additional-root window
    (``config.extra_roots_retention_days``) is independent of the object-store
    window and is reported in the receipt either way.
    """
    resolved = config or RetentionConfig.from_env()
    now_utc = now.astimezone(UTC)
    cutoff = now_utc - timedelta(days=resolved.retention_days)
    extra_cutoff = now_utc - timedelta(days=resolved.extra_roots_retention_days)
    result = plan_retention(
        object_store_root=object_store_root,
        cutoff=cutoff,
        retention_days=resolved.retention_days,
        enabled=resolved.enabled,
        dry_run=resolved.dry_run,
        published_artifact_root=published_artifact_root,
        active_lower_bound=active_lower_bound,
        active_lower_bound_source=active_lower_bound_source,
        runs_only_roots=tuple(runs_only_roots) if resolved.extra_roots_enabled else (),
        extra_roots_cutoff=extra_cutoff,
        extra_roots_retention_days=resolved.extra_roots_retention_days,
        extra_roots_enabled=resolved.extra_roots_enabled,
    )
    if not resolved.enabled or resolved.dry_run:
        return result
    extra_roots = set(result.extra_roots)
    for entry in result.planned:
        root = entry.get("root")
        _delete_entry(
            entry,
            result,
            containment_root=Path(root) if root in extra_roots else None,
        )
    return result


def _delete_entry(
    entry: dict[str, Any],
    result: RetentionResult,
    *,
    containment_root: Path | None = None,
) -> None:
    """Remove one planned entry, recording failure instead of raising.

    ``containment_root`` is set for additional roots (design D6): removal goes
    through ``rmtree_no_follow`` so the walk cannot follow a symlink out of the
    root that is being swept. The object-store root keeps the historical
    ``shutil.rmtree``; changing it is out of this change's scope.

    ``SafeFilesystemError`` is a ``RuntimeError``, **not** an ``OSError``, so it
    must be named explicitly here: letting it escape would collapse the pass
    receipt to ``{"status": "error"}`` (scheduler_runtime) and abort the
    ``cleanup`` CLI mid-sweep (cli.py wraps nothing), both violating this
    module's "failures never abort the pass" contract.
    """
    path = Path(entry["path"])
    try:
        if containment_root is not None:
            rmtree_no_follow(path, containment_root=containment_root)
        else:
            shutil.rmtree(path)
    except (OSError, SafeFilesystemError) as error:
        result.failed.append({**entry, "error": str(error)})
        return
    result.deleted.append(entry)
    result.freed_bytes += int(entry.get("size_bytes", 0))
