"""Root admission and overlap classification for retention (#1616/#1617/#3142).

Moved verbatim out of :mod:`services.orchestrator.retention` when that module
crossed the 1,000-line guard. One admission path for every configured root:
pre-resolution hygiene (blank / relative / unexpandable values never reach
``resolve()``), equal-alias deduplication, and rejection of a root whose
POTENTIAL deletion target tree intersects an already-admitted root's. The
target-lane shapes the overlap check classifies against (``runs/``, the
cycle-scoped prefixes, the ``%Y%m%d%H`` cycle name, canonical run ids and the
Slurm log lane's names) live here too, so admission and the collectors read one
definition.

The dependency runs one way: ``retention`` imports this module and re-exports
every name callers and tests import from ``retention``; this module imports
nothing from ``retention``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.orchestrator.retention_slurm_log_lane import slurm_log_lane_cycle
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

CYCLE_NAME_LENGTH = 10  # len("%Y%m%d%H")


# Skip reason for an additional root whose configured value is relative (#1318
# task 5.1). Resolving it would anchor a deletion surface to whatever working
# directory the process happens to have been started in; recorded rather than
# dropped silently because -- unlike an unset root -- it is a misconfiguration
# somebody has to see.
EXTRA_ROOT_NOT_ABSOLUTE_REASON = "extra_root_not_absolute"

# Skip reason for an explicitly empty/whitespace primary root (#1616). The
# value must be recorded BEFORE it can collapse into ``None`` in
# ``ProductionSchedulerConfig`` normalization, so the receipt stays readable
# instead of silently looking like an unset root.
PRIMARY_ROOT_BLANK_REASON = "primary_root_blank"

# Skip reason for a non-blank but relative primary root (#1616). Resolving it
# would anchor the deletion surface to the process working directory (or, for
# the pass, the scheduler workspace); recorded rather than dropped because it
# is a misconfiguration somebody has to see.
PRIMARY_ROOT_NOT_ABSOLUTE_REASON = "primary_root_not_absolute"

# Skip reason for a root rejected because its potential canonical deletion
# tree intersects an already-admitted root's (#1617). The equality-only dedup
# of #1318 could not see ``A/runs/<canonical_run_id>`` beneath ``A``, so both
# roots would scan and select the same subtree twice. Directory ancestry alone
# is not overlap: only target-tree intersection (``runs/<canonical_run_id>``
# everywhere, ``raw|canonical|forcing/<source>/<valid_cycle>`` on the primary)
# rejects a root. The loser is recorded with ``conflicting_root`` naming the
# accepted winner.
ROOT_OVERLAP_REASON = "root_overlap"

# Skip reasons for a root whose leading ``~user`` has no home directory (#2596):
# ``expanduser()`` raises an errno-less ``RuntimeError`` there. The published-root
# sibling token (``PUBLISHED_ROOT_UNEXPANDABLE_REASON``) stays in ``retention``.
PRIMARY_ROOT_UNEXPANDABLE_REASON = "primary_root_unexpandable"
EXTRA_ROOT_UNEXPANDABLE_REASON = "extra_root_unexpandable"


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


def _sanitize_root_candidate(
    value: Path | str | None,
    *,
    reason_blank: str | None,
    reason_not_absolute: str,
    reason_unexpandable: str,
) -> tuple[str | None, Path | None, str | None]:
    """Shared pre-resolution root hygiene (issues #1616/#1617).

    One admission path for both the primary root and every additional root, so
    a blank or relative value can never reach ``Path()`` construction or
    ``resolve()`` and derive a working-directory deletion surface. Returns
    ``(raw, resolved, rejected_reason)``; exactly one of ``resolved`` /
    ``rejected_reason`` is set.

    - ``None`` -> ordinary unset no-op: neither resolved nor rejected.
    - empty/whitespace -> rejected with ``reason_blank`` when the caller
      supplies a token, otherwise a silent no-op. The primary lane freezes its
      own token so an explicitly blank primary stays distinguishable from an
      unset one even after config normalization collapsed it; the additional
      lane keeps the #1318 contract that blank values are discarded silently.
    - non-blank but still relative after ``expanduser()`` -> rejected with
      ``reason_not_absolute`` (the ``extra_root_not_absolute`` token for
      additional roots stays stable; the primary lane carries its own token).
    - ``~user`` with no home directory -> rejected with ``reason_unexpandable``.
    - otherwise -> resolved to an absolute path (``Path(value).expanduser()
      .resolve()``).
    """
    if value is None:
        return None, None, None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return value, None, reason_blank
    try:
        candidate = Path(value).expanduser()
    except RuntimeError:
        return str(value), None, reason_unexpandable
    # A degenerate ``Path`` (``Path("")`` / whitespace-only) is blank too.
    if isinstance(value, Path) and not str(value).strip():
        return str(value), None, reason_blank
    if not candidate.is_absolute():
        return str(value), None, reason_not_absolute
    return str(value), candidate.resolve(), None


def _resolve_runs_only_roots(
    values: Sequence[Path | str | None],
    *,
    primary: Path | None,
    slurm_log_root: Path | None = None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Resolve, sanitise and de-duplicate the additional ``runs/``-only roots.

    Returns the resolved roots plus the skip entries for the values that were
    discarded loudly; this function is where *all* additional-root path hygiene
    lives, and neither caller validates a root before handing it over.

    Hygiene (issue #1318 task 1.2): ``None``, empty and whitespace-only values
    are discarded **before** ``Path()`` is ever constructed --
    ``Path("").expanduser().resolve()`` is the process working directory, which
    would drag ``<cwd>/runs`` into the deletion surface. Those discards stay
    silent (no skip entry), and the decision is keyed on the value's shape
    alone: this function sees only the values, never the deployment topology.
    ``tests/test_retention_extra_roots.py`` pins exactly that shape rule --
    ``test_unset_and_blank_additional_roots_are_discarded_silently`` (no root,
    no skip entry) and
    ``test_relative_additional_root_is_recorded_while_absolute_is_admitted``.
    Unverified operational context, not pinned by those tests and not visible
    here: ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` is expected to be unset
    (``None``) on deployments that are not db-free, which is why silence is the
    chosen shape for an unset value.

    A non-blank value that is still relative after ``expanduser()`` is discarded
    **before** ``resolve()`` and recorded in ``skipped`` (task 5.1): resolving it
    would anchor the deletion surface to the invocation working directory. The
    value is stripped first, because ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` reaches
    here as the bare environment string -- surrounding whitespace included.

    Overlap (issue #1617 / design D2): a root is rejected only when its
    POTENTIAL deletion target tree intersects an already-admitted root's --
    ``runs/<canonical_run_id>`` on every root, plus
    ``raw|canonical|forcing/<source>/<valid_cycle>`` on the primary. Directory
    ancestry outside those lanes is admitted (a parent workspace with a nested
    child object-store stays fully swept). The primary root always wins; among
    additional roots the first accepted configured root wins. The loser is
    omitted from the resolved list and recorded in ``skipped`` with
    ``reason=root_overlap`` and ``conflicting_root=<winner>``. Equal aliases
    (path / symlink / trailing-slash) resolve to the same absolute path and
    remain the #1318 silent dedup.

    ``slurm_log_root`` (resolved, #3142) is the additional root that would carry
    the root-level Slurm log lane; its potential targets additionally include
    ``<canonical_run_id>/**`` and ``(gfs|ifs|era5)_<valid_cycle>/**`` directly
    beneath it. ``None`` keeps the pre-#3142 adjudication exactly.
    """
    resolved: list[Path] = []
    skipped: list[dict[str, Any]] = []
    admitted: list[tuple[Path, bool]] = []
    if primary is not None:
        admitted.append((primary, True))
    for value in values:
        raw, candidate, rejected = _sanitize_root_candidate(
            value,
            # #1318 contract: an unset/blank additional root is a SILENT discard,
            # keyed on the value's shape only (pinned by the two shape tests
            # named in the docstring). Only the relative shape is a
            # misconfiguration worth recording. That ``None`` is the usual value
            # on non-db-free deployments is operational context, not verified here.
            reason_blank=None,
            reason_not_absolute=EXTRA_ROOT_NOT_ABSOLUTE_REASON,
            reason_unexpandable=EXTRA_ROOT_UNEXPANDABLE_REASON,
        )
        if rejected is not None:
            skipped.append(
                {
                    "key": RUNS_PREFIX,
                    "root": str(raw),
                    "reason": rejected,
                }
            )
            continue
        if candidate is None:
            continue
        if any(candidate == admitted_path for admitted_path, _is_primary in admitted):
            continue
        conflicting = _find_conflicting_root(candidate, admitted, slurm_log_root=slurm_log_root)
        if conflicting is not None:
            skipped.append(
                {
                    "key": RUNS_PREFIX,
                    "root": str(candidate),
                    "reason": ROOT_OVERLAP_REASON,
                    "conflicting_root": str(conflicting),
                }
            )
            continue
        admitted.append((candidate, False))
        resolved.append(candidate)
    return resolved, skipped


def _find_conflicting_root(
    candidate: Path,
    admitted: Sequence[tuple[Path, bool]],
    *,
    slurm_log_root: Path | None = None,
) -> Path | None:
    """Return the admitted root whose potential deletion tree intersects the
    candidate's, or None.

    ``admitted`` is the list of already-accepted roots as ``(path, is_primary)``
    pairs, in admission order (the primary first when configured). "Overlap"
    means either root lies at or below the other root's POTENTIAL retention
    deletion target -- the same canonical shapes the collectors can delete, so
    admission cannot drift from the scan (issue #1617 / design D2):

    - every root owns potential ``runs/<canonical_run_id>/**`` targets, and
    - the primary additionally owns potential
      ``raw|canonical|forcing/<source>/<valid_cycle>/**`` targets.

    Directory ancestry outside those lanes is NOT overlap: a parent workspace
    with a nested child object-store (``WORKSPACE_ROOT`` / ``OBJECT_STORE_ROOT``)
    has disjoint ``runs/`` and cycle trees and both remain admitted. The
    comparison is purely lexical on resolved absolute path components -- no
    filesystem enumeration and no dependence on whether the target currently
    exists or is within its window.
    """
    for admitted_path, admitted_is_primary in admitted:
        if _potential_targets_intersect(
            candidate,
            is_primary=False,
            other=admitted_path,
            other_is_primary=admitted_is_primary,
            slurm_log_root=slurm_log_root,
        ):
            return admitted_path
    return None


def _potential_targets_intersect(
    path: Path,
    *,
    is_primary: bool,
    other: Path,
    other_is_primary: bool,
    slurm_log_root: Path | None = None,
) -> bool:
    """True when two resolved roots' potential deletion target trees intersect.

    Both containment directions are checked with the same lane predicate: the
    contained root's relative components are classified against the container's
    target lanes (``runs/<canonical>`` always; cycle prefixes only for the
    primary), because the primary's cycle lane is not shared by additional
    roots. The root-level Slurm log lane belongs to the additional root equal
    to ``slurm_log_root`` only (#3142).
    """
    try:
        relative = path.relative_to(other).parts
        if _parts_lie_in_potential_target(
            relative,
            primary=other_is_primary,
            slurm_log_lane=not other_is_primary and other == slurm_log_root,
        ):
            return True
    except ValueError:
        pass
    try:
        relative = other.relative_to(path).parts
        if _parts_lie_in_potential_target(
            relative,
            primary=is_primary,
            slurm_log_lane=not is_primary and path == slurm_log_root,
        ):
            return True
    except ValueError:
        pass
    return False


def _parts_lie_in_potential_target(
    parts: tuple[str, ...],
    *,
    primary: bool,
    slurm_log_lane: bool = False,
) -> bool:
    """Classify relative path components against a root's potential target lanes.

    Mirrors the collectors exactly (no filesystem access):

    - ``runs/<canonical_run_id>/...`` is a target on every root: ``parts[0]``
      must be ``runs`` and ``parts[1]`` must parse as a canonical run id via the
      shared ``_extract_run_cycle``. A root at ``A/runs`` or
      ``A/runs/<not-a-canonical-id>`` is not inside a deletable run tree.
    - ``raw|canonical|forcing/<source>/<valid_cycle>/...`` is a target on the
      primary only: ``parts[0]`` must be a cycle-scoped prefix and ``parts[2]``
      must parse as a ``%Y%m%d%H`` cycle via ``_parse_cycle_name`` (the source
      segment is any directory name, matching ``_collect_cycle_targets``).
      ``A/raw/gfs/not-a-cycle/...`` is not a deletable cycle tree, and the
      static ``grid`` segment never parses as a cycle.
    - ``<lane_name>/...`` directly beneath the root is a target on the Slurm
      log root only (#3142): ``parts[0]`` must pass ``slurm_log_lane_cycle``,
      the same predicate the lane collector uses.
    """
    if slurm_log_lane and parts and slurm_log_lane_cycle(parts[0]) is not None:
        return True
    if len(parts) >= 2 and parts[0] == RUNS_PREFIX:
        if _extract_run_cycle(parts[1]) is not None:
            return True
    if primary and len(parts) >= 3 and parts[0] in CYCLE_SCOPED_PREFIXES:
        if _parse_cycle_name(parts[2]) is not None:
            return True
    return False


def _silently_resolved_root(value: Path | str | None) -> Path | None:
    """``value`` through the shared admission hygiene, or None; records nothing.

    Used for the roots that are NAMED a second time (#2238, #3142): when the
    gate is open the same values travel in ``runs_only_roots``, whose admission
    already records every loud rejection.
    """
    return _sanitize_root_candidate(
        value,
        reason_blank=None,
        reason_not_absolute=EXTRA_ROOT_NOT_ABSOLUTE_REASON,
        reason_unexpandable=EXTRA_ROOT_UNEXPANDABLE_REASON,
    )[1]


def _slurm_log_lane_root(
    slurm_log_root: Path | str | None,
    copyback_root: Path | str | None,
) -> Path | None:
    """The resolved Slurm log root unless it aliases the copyback root (#3142)."""
    lane_root = _silently_resolved_root(slurm_log_root)
    if lane_root is None or lane_root == _silently_resolved_root(copyback_root):
        return None
    return lane_root
