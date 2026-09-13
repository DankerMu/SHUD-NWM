#!/usr/bin/env python3
"""Read-only pre-target cold-residency census for the node-27 #1895 rollout.

Issue #1895 needs one frozen group/capacity preimage *before* the ``nhms_cold``
target exists.  The #1893 runner cannot produce it: its tick binds every
observation to the production target preflight (#1929), which refuses while the
cold bind/tablespace is absent.  This census is the pre-target branch of the same
contract: it calls the shipped production catalog/inventory/parity owners
(:func:`ranked_candidates_from_execute`, :func:`derive_bound_inventories`,
:func:`collect_residency_group`, :func:`compute_window_parity`,
:func:`compression_before_bytes`, :func:`retained_source_bytes`) directly and
never imports or invokes target preflight, a probe-private module, or any
movement SQL.

Read-only by construction: one connection forced through ``SET SESSION
CHARACTERISTICS AS TRANSACTION READ ONLY`` with a verified
``transaction_read_only`` flag, finite statement/lock timeouts, and an
explicit rollback of the observation transaction.  Every scan is bounded and the
ceiling is pinned here, not supplied by a caller: catalog discovery keeps the
#1893 owner's per-hypertable byte ceiling and a row limit derived from the
required count plus one *extra slot per table* so a seventh candidate is
reported as drift instead of being truncated; the business-window scan is capped
and restricted to the two allowlisted hypertables; parity returns one aggregate
row per window and no business row is ever materialized client-side.

Publication is descriptor-bound and no-clobber: the artifact and its
parent are opened without following symlinks, the parent must be a real
directory owned by the effective user, an existing target is never replaced, the
temporary sibling is created with ``O_EXCL`` and refused if anything (including a
symlink or a non-regular file) already holds it, the payload is size-capped, and
the file and parent directory are fsynced.  The artifact is mode 0600 and carries
only a masked DSN.

A census that does not resolve exactly the required number of complete
all-source groups, or that finds mixed/unknown/already-cold residency, a
cold-resident hot group, drifted inventory, or a capacity overflow, publishes a
truthful ``NO-GO`` artifact and exits non-zero — it never rounds, defaults,
substitutes, or selects an arbitrary oldest subset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from packages.common.compressed_chunk_cold_residency import (
    ALLOWED_HYPERTABLES,
    PINNED_PG_VERSION_PREFIX,
    PINNED_TIMESCALEDB_VERSION,
    CatalogChunk,
    compute_cutoff,
    json_ready,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
    ColdRuntimeError,
    WindowParity,
    collect_residency_group,
    compression_before_bytes,
    compute_window_parity,
    derive_bound_inventories,
    engine_versions,
    ranked_candidates_from_execute,
    retained_source_bytes,
    snapshot_group,
)
from packages.common.display_watermark import DisplayWatermarkError, fetch_display_watermark
from packages.common.node27_cold_residency_census_policy import capacity_policy as _policy_capacity_policy

ARTIFACT_NAME = "nhms-node27-cold-residency-census"
ARTIFACT_VERSION = "1.0"
VERDICT_GO = "GO"
VERDICT_NO_GO = "NO-GO"

APPLICATION_NAME = "nhms-ts-cold-census"
CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MS = 3_600_000
LOCK_TIMEOUT = "5s"
COMPRESSION_LAG_DEFAULT = 604_800
COMPRESSION_LAG_KEYS = (
    "NODE27_COLD_RESIDENCY_LAG_SECONDS",
    "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS",
)

# Pinned to the #1893 runner's own catalog ceilings so the census can never
# claim a wider (or narrower) discovery window than the lane it freezes.
CATALOG_BYTE_CEILING = 16 * 1024**2
MAX_MEMBERS_PER_GROUP = 64
MAX_REQUIRE_COUNT = 64
# One extra candidate slot per allowlisted hypertable: the scanner must be able
# to *see* a seventh group and call it drift, not truncate it into agreement.
EXTRA_CANDIDATE_SLOTS_PER_TABLE = 1
MAX_WINDOW_ROWS = 2_000
MAX_REPORTED_HOT_GROUPS = 200
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

_CANONICAL_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_HYPERTABLE_VALUE_LIST = ", ".join(f"('{schema}', '{name}')" for schema, name in sorted(ALLOWED_HYPERTABLES))

# The census only reports external pg_tblspc targets; #1894's backup-coverage
# gate is what consumes them as a precondition.
EXTERNAL_TBLSPACE_SQL = """
SELECT spcname, pg_tablespace_location(oid) AS location
FROM pg_tablespace
WHERE pg_tablespace_location(oid) <> ''
ORDER BY location, spcname
"""

# Chunk windows for exactly the two allowlisted business hypertables. The
# origin-heap LEFT JOINs keep a concurrently dropped chunk visible as a null
# tablespace (a refusal) instead of silently shrinking the inventory, and the
# fixed LIMIT turns a runaway scan into a refusal.
CHUNK_WINDOW_ROWS_SQL = f"""
SELECT ch.hypertable_schema, ch.hypertable_name, ch.chunk_schema, ch.chunk_name,
       ch.range_start, ch.range_end, ch.is_compressed,
       CASE WHEN o.oid IS NULL THEN NULL ELSE COALESCE(ts.spcname, 'pg_default') END AS origin_tablespace
FROM timescaledb_information.chunks ch
LEFT JOIN pg_namespace n ON n.nspname = ch.chunk_schema
LEFT JOIN pg_class o ON o.relnamespace = n.oid AND o.relname = ch.chunk_name
LEFT JOIN pg_tablespace ts ON ts.oid = o.reltablespace
WHERE (ch.hypertable_schema, ch.hypertable_name) IN ({_HYPERTABLE_VALUE_LIST})
ORDER BY ch.range_end, ch.hypertable_schema, ch.hypertable_name, ch.chunk_schema, ch.chunk_name
LIMIT %s
"""


class CensusError(Exception):
    """A stable, non-secret census refusal. Never carries a DSN or row values."""

    def __init__(self, message: str, *, error_class: str = "census", stage: str = "census") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.stage = stage


def execute_on(connection: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    """Narrow read-only cursor seam: run one statement and return dict rows.

    Identical in effect to the movement module's adapter but deliberately local:
    importing ``compressed_chunk_cold_runtime`` would transitively load the
    target-preflight owner (#1929), which the pre-target census must never do.
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return []
        names = [item[0] for item in cursor.description]
        return [
            dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False)) for row in cursor.fetchall()
        ]


def assert_engine_versions(server_version: str, timescaledb_version: str) -> None:
    """Pinned engine identity check, from the narrow contract constants only."""

    if not str(server_version).startswith(PINNED_PG_VERSION_PREFIX):
        raise ColdRuntimeError(
            f"PostgreSQL version {server_version} is not {PINNED_PG_VERSION_PREFIX}",
            error_class="engine_identity",
            stage="preflight",
        )
    if str(timescaledb_version) != PINNED_TIMESCALEDB_VERSION:
        raise ColdRuntimeError(
            f"TimescaleDB version {timescaledb_version} is not {PINNED_TIMESCALEDB_VERSION}",
            error_class="engine_identity",
            stage="preflight",
        )


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def mask_database_url(dsn: str) -> str:
    """A credential-free DSN echo: userinfo is dropped, host/port/path stay."""

    try:
        parts = urlsplit(dsn)
    except Exception:
        return "postgresql://***@***/***"
    netloc = parts.hostname or "***"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username is not None or parts.password is not None:
        netloc = f"***@{netloc}"
    return urlunsplit((parts.scheme or "postgresql", netloc, parts.path or "", "", ""))


def _parse_canonical_int(raw: str | None, *, name: str, minimum: int) -> int:
    if raw is None:
        raise CensusError(f"{name} must be set", error_class="config", stage="config")
    if raw != raw.strip() or not _CANONICAL_DECIMAL.fullmatch(raw):
        raise CensusError(f"{name} must be a canonical decimal integer", error_class="config", stage="config")
    value = int(raw)
    if value < minimum:
        raise CensusError(f"{name} must be >= {minimum}", error_class="config", stage="config")
    return value


def require_count_from_arg(raw: str) -> int:
    count = _parse_canonical_int(raw, name="--require-count", minimum=1)
    if count > MAX_REQUIRE_COUNT:
        raise CensusError("--require-count is above the census ceiling", error_class="config", stage="config")
    return count


def lag_source_key(env: Mapping[str, str]) -> str:
    """Which configured key supplied the lag, or the shared contract default."""

    for key in COMPRESSION_LAG_KEYS:
        candidate = env.get(key)
        if candidate is not None and candidate != "":
            return key
    return "configured-compression-contract-default"


def lag_seconds_from_env(env: Mapping[str, str]) -> int:
    """Resolve the configured compression lag exactly as the #1893 runner does.

    The first present-and-non-empty key wins; only when neither lane names a lag
    does the shared compression contract default apply. No second lag is invented.
    """

    raw: str | None = None
    for key in COMPRESSION_LAG_KEYS:
        candidate = env.get(key)
        if candidate is not None and candidate != "":
            raw = candidate
            break
    if raw is None:
        raw = str(COMPRESSION_LAG_DEFAULT)
    return _parse_canonical_int(raw, name=COMPRESSION_LAG_KEYS[0], minimum=1)


def per_table_catalog_limit(require_count: int) -> int:
    """Per-hypertable candidate ceiling that can still surface an extra group.

    ``ranked_candidates_from_execute`` fetches ``limit + 1`` rows per allowlisted
    hypertable and refuses a scan that exceeds the ceiling, so a limit of exactly
    ``require_count`` would let one over-eager hypertable abort discovery before
    the census could name the extras.  Adding one slot per table keeps the scan
    bounded while the artifact's own exact-count check reports every surplus key
    as drift — detection, never truncation.
    """

    if require_count + EXTRA_CANDIDATE_SLOTS_PER_TABLE > MAX_REQUIRE_COUNT:
        raise CensusError(
            "census candidate ceiling is too close to the scan bound",
            error_class="bound",
            stage="catalog",
        )
    return require_count + EXTRA_CANDIDATE_SLOTS_PER_TABLE


def total_catalog_row_limit(per_table_limit: int) -> int:
    return per_table_limit * len(ALLOWED_HYPERTABLES)


def durable_key(durable: Mapping[str, Any]) -> str:
    """The durable census key: hypertable, origin name, window, origin OID."""

    return (
        f"{durable['hypertable_schema']}.{durable['hypertable_name']}"
        f"|{durable['origin_schema']}.{durable['origin_name']}"
        f"|{durable['range_start']}|{durable['range_end']}"
        f"|{durable['origin_oid']}"
    )


def group_key(chunk: CatalogChunk) -> str:
    return durable_key(
        {
            "hypertable_schema": chunk.hypertable_schema,
            "hypertable_name": chunk.hypertable_name,
            "origin_schema": chunk.origin_schema,
            "origin_name": chunk.origin_name,
            "range_start": iso_utc(chunk.range_start),
            "range_end": iso_utc(chunk.range_end),
            "origin_oid": chunk.origin_oid,
        }
    )


def observe_head(repo_root: Path | None = None) -> tuple[str | None, bool, bool]:
    """Exact HEAD, HEAD-observed, worktree-dirty — same shape as the runner."""

    root = Path(__file__).resolve().parents[1] if repo_root is None else repo_root
    try:
        parsed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        unstaged = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, check=False, timeout=10)
        staged = subprocess.run(["git", "diff", "--quiet", "--cached", "--"], cwd=root, check=False, timeout=10)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise CensusError("cannot bind census to repository HEAD", error_class="head", stage="freeze_head") from error
    head_sha = parsed.stdout.strip()
    observed = parsed.returncode == 0 and _HEAD_RE.fullmatch(head_sha) is not None
    dirty = bool(unstaged.returncode or staged.returncode or (untracked.stdout or "").strip())
    if not observed:
        return None, False, dirty
    return head_sha, True, dirty


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(json_ready(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CensusObserver:
    """One read-only connection holding every census observation."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def binder(self):
        return lambda sql, params=None: execute_on(self._connection, sql, params)

    def session_read_only(self) -> bool:
        rows = execute_on(self._connection, "SELECT current_setting('transaction_read_only') AS read_only")
        return bool(rows) and str(rows[0]["read_only"]).strip().lower() in {"on", "true"}

    def versions(self) -> tuple[str, str]:
        return engine_versions(self.binder())

    def inventories(self) -> BoundInventories:
        return derive_bound_inventories(self.binder())

    def candidates(self, *, inventories: BoundInventories, cutoff: datetime, per_table_limit: int) -> list[Any]:
        return ranked_candidates_from_execute(
            self.binder(),
            inventories=inventories,
            cutoff=cutoff,
            per_table_limit=per_table_limit,
            max_catalog_bytes=CATALOG_BYTE_CEILING,
        )

    def parity(self, inventories: BoundInventories, chunk: CatalogChunk) -> WindowParity:
        return compute_window_parity(
            self.binder(),
            inventories.for_hypertable(chunk.hypertable_schema, chunk.hypertable_name),
            chunk,
        )

    def before_bytes(self, chunk: CatalogChunk) -> int:
        return compression_before_bytes(self.binder(), chunk)

    def window_rows(self) -> list[Mapping[str, Any]]:
        rows = execute_on(self._connection, CHUNK_WINDOW_ROWS_SQL, (MAX_WINDOW_ROWS + 1,))
        if len(rows) > MAX_WINDOW_ROWS:
            raise CensusError(
                "business chunk window exceeds the census row ceiling",
                error_class="bound",
                stage="window",
            )
        return rows

    def external_targets(self) -> list[Mapping[str, Any]]:
        return execute_on(self._connection, EXTERNAL_TBLSPACE_SQL)


def capacity_policy(*, expansions: Sequence[int], retained: Sequence[int], group_count: int) -> dict[str, Any]:
    """Checked canonical-decimal policy: E = max expansion, S = sum retained.

    Arithmetic lives in the shared policy module; this CLI seam re-exports it
    with the historical ``CensusError`` as the refusal type so callers keep one
    exception class.  ``WAL_RESERVE=E`` is a deliberately conservative
    same-order proxy taken from fresh live expansion.  It is not a WAL
    measurement, not a per-group WAL attribution, not an LSN calculation, and
    never the disposable 165736-byte observation.
    """

    return _policy_capacity_policy(
        expansions=expansions,
        retained=retained,
        group_count=group_count,
        error_type=CensusError,
    )


def _observe_group(observer: CensusObserver, inventories: BoundInventories, chunk: CatalogChunk) -> dict[str, Any]:
    blockers: list[str] = []
    expansion: int | None = None
    retained: int | None = None
    key = group_key(chunk)
    group = collect_residency_group(observer.binder(), chunk)
    snapshot = snapshot_group(group)
    members = list(snapshot["members"])
    residency = str(snapshot["residency"])
    if group.blocker:
        blockers.append(f"{key} group is incomplete: {group.blocker}")
    if not members:
        blockers.append(f"{key} group has no members")
    if len(members) > MAX_MEMBERS_PER_GROUP:
        blockers.append(f"{key} group exceeds the member ceiling")
    if not group.is_compressed:
        blockers.append(f"{key} is not compressed")
    if group.compressed_oid is None:
        blockers.append(f"{key} has no current compressed sibling")
    if residency in {"mixed", "unknown"}:
        blockers.append(f"{key} residency is {residency}")
    if residency == "already_target":
        blockers.append(f"{key} is already cold; the pre-first-movement census requires all-source")
    parity: dict[str, Any] | None = None
    try:
        parity = observer.parity(inventories, chunk).as_dict()
    except ColdRuntimeError as error:
        blockers.append(f"{key} window parity failed: {type(error).__name__}")
    try:
        before = observer.before_bytes(chunk)
        if before <= 0:
            blockers.append(f"{key} before_compression_total_bytes is not positive")
        else:
            expansion = before
    except ColdRuntimeError as error:
        blockers.append(f"{key} compression statistics failed: {type(error).__name__}")
    group_retained = retained_source_bytes(group)
    if group_retained <= 0:
        blockers.append(f"{key} retained_source_bytes is not positive")
    else:
        retained = group_retained
    artifact = {
        "key": key,
        "durable": snapshot["durable"],
        "compressed": snapshot["compressed"],
        "is_compressed": snapshot["is_compressed"],
        "residency": residency,
        "member_count": len(members),
        "member_digest": _canonical_digest(list(members)),
        "inventory_digest": inventories.for_hypertable(chunk.hypertable_schema, chunk.hypertable_name).digest,
        "group_digest": _canonical_digest(snapshot),
        "members": members,
        "parity": parity,
        "before_compression_total_bytes": expansion,
        "retained_source_bytes": group_retained,
    }
    return {
        "artifact": artifact,
        "blockers": tuple(dict.fromkeys(blockers)),
        "expansion": expansion,
        "retained": retained,
    }


def _classify_window_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    hot: list[dict[str, Any]] = []
    blockers: list[str] = []
    for row in rows:
        if (row["hypertable_schema"], row["hypertable_name"]) not in ALLOWED_HYPERTABLES:
            raise CensusError(
                "business chunk window returned a non-allowlisted hypertable",
                error_class="inventory_drift",
                stage="window",
            )
        range_end = row["range_end"]
        if not isinstance(range_end, datetime) or range_end.tzinfo is None:
            raise CensusError("business chunk window returned a naive range_end", error_class="catalog", stage="window")
        if range_end.astimezone(UTC) <= cutoff:
            continue
        entry = {
            "hypertable": f"{row['hypertable_schema']}.{row['hypertable_name']}",
            "chunk": f"{row['chunk_schema']}.{row['chunk_name']}",
            "range_start": iso_utc(row["range_start"]),
            "range_end": iso_utc(range_end),
            "is_compressed": bool(row["is_compressed"]),
            "origin_tablespace": row["origin_tablespace"],
            "classification": "hot_active_compressed" if row["is_compressed"] else "hot_active_uncompressed",
        }
        hot.append(entry)
        if entry["origin_tablespace"] != "pg_default":
            blockers.append(f"hot/active group {entry['chunk']} is not pg_default ({entry['origin_tablespace']})")
    return hot, blockers


def _artifact(
    *,
    now_utc: datetime,
    head_sha: str,
    database_url: str,
    watermark: datetime,
    lag_seconds: int,
    cutoff: datetime,
    server_version: str,
    timescaledb_version: str,
    require_count: int,
    per_table_limit: int,
    groups: Sequence[Mapping[str, Any]],
    hot_rows: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    blockers: Sequence[str],
    policy: Mapping[str, Any] | None = None,
    inventories: BoundInventories | None = None,
    lag_source: str = "configured-compression-contract-default",
    session_read_only: bool,
) -> dict[str, Any]:
    keys = [str(group["key"]) for group in groups]
    residency_counts: dict[str, int] = {}
    for group in groups:
        residency = str(group["residency"])
        residency_counts[residency] = residency_counts.get(residency, 0) + 1
    resolved = list(dict.fromkeys(blockers))
    bound = [{"key": group["key"], "group_digest": group["group_digest"]} for group in groups]
    return json_ready(
        {
            "artifact": ARTIFACT_NAME,
            "artifact_version": ARTIFACT_VERSION,
            "verdict": VERDICT_GO if not resolved else VERDICT_NO_GO,
            "generated_at": iso_utc(now_utc),
            "head_sha": head_sha,
            "config": {
                "database_url_masked": mask_database_url(database_url),
                "application_name": APPLICATION_NAME,
                "session_read_only": session_read_only,
                "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
                "lock_timeout": LOCK_TIMEOUT,
                "require_count": require_count,
                "catalog_per_table_limit": per_table_limit,
                "catalog_row_limit": total_catalog_row_limit(per_table_limit),
                "catalog_byte_ceiling": CATALOG_BYTE_CEILING,
                "max_members_per_group": MAX_MEMBERS_PER_GROUP,
                "window_row_ceiling": MAX_WINDOW_ROWS,
                "lag_source_key": lag_source,
            },
            "cluster": {
                "server_version": server_version,
                "timescaledb_version": timescaledb_version,
                "allowlisted_hypertables": [f"{schema}.{name}" for schema, name in sorted(ALLOWED_HYPERTABLES)],
            },
            "watermark": iso_utc(watermark),
            "lag_seconds": lag_seconds,
            "cutoff": iso_utc(cutoff),
            "inventory": None
            if inventories is None
            else {
                "digest": inventories.digest,
                "river_digest": inventories.river.digest,
                "forcing_digest": inventories.forcing.digest,
                "river_column_count": len(inventories.river.columns),
                "forcing_column_count": len(inventories.forcing.columns),
            },
            "required_group_count": require_count,
            "resolved_group_count": len(keys),
            "census_digest": _canonical_digest(bound),
            "census_key_set_digest": _canonical_digest(sorted(keys)),
            "group_keys": keys,
            "groups": list(groups),
            "residency_counts": residency_counts,
            "hot_active_group_count": len(hot_rows),
            "hot_active_groups_reported": min(len(hot_rows), MAX_REPORTED_HOT_GROUPS),
            "hot_active_groups_truncated": len(hot_rows) > MAX_REPORTED_HOT_GROUPS,
            "hot_active_groups": [dict(row) for row in hot_rows[:MAX_REPORTED_HOT_GROUPS]],
            "external_pg_tblspc_targets": [dict(row) for row in targets],
            "capacity_policy": dict(policy or {"status": "unresolved"}),
            "blockers": resolved,
        }
    )


def observe_census(
    observer: CensusObserver,
    *,
    require_count: int,
    lag_seconds: int,
    watermark: datetime,
    now_utc: datetime,
    head_sha: str,
    database_url: str,
    env: Mapping[str, str] | None = None,
    lag_source: str = "configured-compression-contract-default",
) -> dict[str, Any]:
    """Build the census artifact from the shipped production owners only."""

    server_version, timescaledb_version = observer.versions()
    try:
        assert_engine_versions(server_version, timescaledb_version)
    except ColdRuntimeError as error:
        raise CensusError(str(error), error_class="engine_identity", stage="preflight") from error
    read_only = observer.session_read_only()
    if not read_only:
        raise CensusError("census transaction is not read-only", error_class="session", stage="preflight")

    cutoff = compute_cutoff(watermark, lag_seconds)
    limit = per_table_catalog_limit(require_count)
    common: dict[str, Any] = {
        "now_utc": now_utc,
        "head_sha": head_sha,
        "database_url": database_url,
        "watermark": watermark,
        "lag_seconds": lag_seconds,
        "cutoff": cutoff,
        "server_version": server_version,
        "timescaledb_version": timescaledb_version,
        "require_count": require_count,
        "per_table_limit": limit,
        "lag_source": lag_source_key(env) if env is not None else lag_source,
        "session_read_only": read_only,
    }
    try:
        inventories = observer.inventories()
    except ColdRuntimeError as error:
        return _artifact(
            **common,
            groups=[],
            hot_rows=[],
            targets=[],
            inventories=None,
            blockers=[f"business inventory failed: {type(error).__name__}"],
        )

    try:
        ranked = observer.candidates(inventories=inventories, cutoff=cutoff, per_table_limit=limit)
    except ColdRuntimeError as error:
        return _artifact(
            **common,
            groups=[],
            hot_rows=[],
            targets=[],
            inventories=inventories,
            blockers=[f"catalog scan failed: {type(error).__name__}"],
        )

    groups: list[dict[str, Any]] = []
    expansions: list[int] = []
    retained: list[int] = []
    blockers: list[str] = []
    for _rank, _range_end, _schema, _name, _oid, chunk in ranked:
        entry = _observe_group(observer, inventories, chunk)
        groups.append(entry["artifact"])
        blockers.extend(entry["blockers"])
        if entry["expansion"] is not None:
            expansions.append(entry["expansion"])
        if entry["retained"] is not None:
            retained.append(entry["retained"])

    try:
        window_rows = observer.window_rows()
    except (ColdRuntimeError, CensusError) as error:
        return _artifact(
            **common,
            groups=groups,
            hot_rows=[],
            targets=[],
            inventories=inventories,
            blockers=[*blockers, f"business chunk window failed: {type(error).__name__}"],
        )
    hot_rows, hot_blockers = _classify_window_rows(window_rows, cutoff=cutoff)
    blockers.extend(hot_blockers)
    targets = observer.external_targets()

    if len(groups) != require_count:
        surplus = [str(group["key"]) for group in groups[require_count:]]
        missing = "missing" if len(groups) < require_count else "extra"
        blockers.append(
            f"census resolved {len(groups)} eligible groups, exactly {require_count} are required ({missing})"
        )
        if surplus:
            blockers.append("unexplained extra eligible group(s): " + ", ".join(surplus))
    policy: dict[str, Any] = {"status": "unresolved"}
    if len(groups) == require_count:
        try:
            policy = capacity_policy(expansions=expansions, retained=retained, group_count=require_count)
        except CensusError as error:
            blockers.append(str(error))
            policy = {"status": "overflow"}
    else:
        blockers.append("capacity policy requires exactly the census-bound group set")

    return _artifact(
        **common,
        groups=groups,
        hot_rows=hot_rows,
        targets=targets,
        blockers=blockers,
        policy=policy,
        inventories=inventories,
    )


def _attributed_connect(*args: Any, **kwargs: Any) -> Any:
    import psycopg2  # type: ignore[import-not-found]

    return psycopg2.connect(*args, fallback_application_name=APPLICATION_NAME, **kwargs)


def open_readonly_connection(database_url: str) -> Any:
    """A connection that cannot write, with finite timeouts and no DSN echo.

    The read-only character is set through the driver's native ``set_session``
    *before* any cursor SQL.  psycopg2 begins a transaction only when the first
    ``cursor.execute`` runs, and the transaction's read-write/read-only flag is
    fixed at ``BEGIN`` time by the session default.  A later
    ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` would only change
    the *next* transaction's default, leaving the observation transaction
    read-write — which is exactly why the owner convention
    (``display_watermark``) sets ``set_session(readonly=True, autocommit=False)``
    first.  The bounded timeouts follow inside that already read-only
    transaction via ``SET LOCAL`` so they never leak to a later transaction.
    """

    connection = _attributed_connect(database_url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    connection.set_session(readonly=True, autocommit=False)
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    return connection


def close_observer_connection(connection: Any) -> None:
    """Roll the observation transaction back, then close it."""

    try:
        connection.rollback()
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


def _promote_artifact(temp: Path, path: Path) -> None:
    """Atomically promote one prepared temp file to its no-clobber public name.

    POSIX ``rename`` silently replaces an existing destination, which would let
    a concurrent publisher or attacker clobber already-published evidence in the
    window between an existence check and promotion.  This seam uses the
    repository's exclusive link-first primitive instead: ``link`` reserves the
    destination atomically and fails with ``FileExistsError`` when the name is
    already taken, so the census refuses rather than overwrites.
    """

    from packages.common.safe_fs_publication import move_regular_file_no_follow_exclusive

    try:
        move_regular_file_no_follow_exclusive(
            temp.parent,
            temp.name,
            path.parent,
            path.name,
        )
    except FileExistsError:
        raise
    except Exception as error:
        # The primitive's descriptor-bound parent proofs fail as
        # SafeFilesystemError (a RuntimeError) or OSError; any such failure is a
        # stable publication refusal, never a misclassified connection error.
        raise CensusError("artifact could not be published", error_class="artifact", stage="publish") from error


def publish_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish one mode-0600 census artifact into a pinned parent.

    Every refusal happens before a byte is written: no symlinked or non-directory
    parent, no parent owned by anyone else, no pre-existing target (receipts are
    never clobbered), and no pre-existing or non-regular temporary sibling.  The
    final promotion is an exclusive hard-link claim of the destination name, so
    a publisher that wins a race to create the target keeps its bytes and this
    run refuses with a stable ``artifact`` error instead of overwriting it.
    """

    if not path.is_absolute() or "\x00" in str(path):
        raise CensusError("artifact path must be absolute", error_class="artifact", stage="publish")
    parent = path.parent
    try:
        parent_info = os.lstat(parent)
    except OSError as error:
        raise CensusError(
            "artifact parent directory is unavailable",
            error_class="artifact",
            stage="publish",
        ) from error
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise CensusError("artifact parent must be a real directory", error_class="artifact", stage="publish")
    if parent_info.st_uid != os.geteuid():
        raise CensusError(
            "artifact parent directory owner differs from the effective user",
            error_class="artifact",
            stage="publish",
        )
    if os.path.lexists(path):
        raise CensusError(
            "artifact path already exists; census never clobbers evidence",
            error_class="artifact",
            stage="publish",
        )
    encoded = (json.dumps(json_ready(dict(payload)), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise CensusError("artifact exceeds the census byte ceiling", error_class="bound", stage="publish")
    temp = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temp):
        raise CensusError("artifact temporary sibling already exists", error_class="artifact", stage="publish")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(temp, flags, 0o600)
    except OSError as error:
        raise CensusError(
            "artifact temporary sibling cannot be created safely",
            error_class="artifact",
            stage="publish",
        ) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise CensusError(
                "artifact temporary sibling is not a private regular file",
                error_class="artifact",
                stage="publish",
            )
        os.fchmod(fd, 0o600)
        written = 0
        while written < len(encoded):
            written += os.write(fd, encoded[written:])
        os.fsync(fd)
    except (OSError, CensusError) as error:
        os.close(fd)
        try:
            os.unlink(temp)
        except OSError:
            pass
        if isinstance(error, CensusError):
            raise
        raise CensusError("artifact could not be written durably", error_class="artifact", stage="publish") from error
    else:
        os.close(fd)
    try:
        _promote_artifact(temp, path)
    except FileExistsError as error:
        # A concurrent publisher claimed the destination first: the published
        # bytes are theirs, never ours.  The private temp is removed and the
        # target is left byte-identical.
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise CensusError(
            "artifact path was created concurrently; census never clobbers evidence",
            error_class="artifact",
            stage="publish",
        ) from error
    except CensusError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only pre-target cold-residency census (#1895 task 4.0).")
    parser.add_argument(
        "--require-count",
        required=True,
        help="Exact eligible complete all-source group count required.",
    )
    parser.add_argument("--output", required=True, help="Absolute mode-0600 census artifact path.")
    return parser


def _emit_failure(error_class: str, stage: str) -> None:
    print(json.dumps({"status": "failed", "class": error_class, "stage": stage}, sort_keys=True), file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    connect: Any = None,
    head_observer: Any = None,
    watermark_fetcher: Any = None,
) -> int:
    args = _parser().parse_args(argv)
    caller_env = os.environ if env is None else env
    try:
        output = Path(str(args.output))
    except (TypeError, ValueError):
        _emit_failure("config", "output")
        return 2
    if not output.is_absolute() or "\x00" in str(output):
        _emit_failure("config", "output")
        return 2
    try:
        require_count = require_count_from_arg(str(args.require_count))
    except CensusError as error:
        _emit_failure(error.error_class, error.stage)
        return 2
    database_url = str(caller_env.get("DATABASE_URL") or "").strip()
    if not database_url:
        _emit_failure("config", "database")
        return 2
    try:
        observer_head = observe_head if head_observer is None else head_observer
        head_sha, observed, dirty = observer_head()
        if not observed or dirty:
            raise CensusError(
                "census requires a clean worktree at an observed HEAD",
                error_class="head",
                stage="freeze_head",
            )
        lag_seconds = lag_seconds_from_env(caller_env)
        fetcher = fetch_display_watermark if watermark_fetcher is None else watermark_fetcher
        try:
            watermark = fetcher(database_url, connect=_attributed_connect)
        except DisplayWatermarkError as error:
            raise CensusError(
                f"display watermark is unavailable ({type(error).__name__})",
                error_class="watermark",
                stage="watermark",
            ) from error
        connector = open_readonly_connection if connect is None else connect
        connection = connector(database_url)
        try:
            artifact = observe_census(
                CensusObserver(connection),
                require_count=require_count,
                lag_seconds=lag_seconds,
                watermark=watermark,
                now_utc=datetime.now(UTC),
                head_sha=str(head_sha),
                database_url=database_url,
                env=caller_env,
            )
        finally:
            close_observer_connection(connection)
    except CensusError as error:
        _emit_failure(error.error_class, error.stage)
        return 2
    except ColdRuntimeError as error:
        _emit_failure(error.error_class, error.stage)
        return 2
    except Exception as error:  # a driver or OS failure must never echo a DSN
        _emit_failure(type(error).__name__.lower(), "connection")
        return 2
    try:
        publish_artifact(output, artifact)
    except CensusError as error:
        _emit_failure(error.error_class, error.stage)
        return 2
    print(
        json.dumps(
            {
                "status": artifact["verdict"],
                "groups": artifact["resolved_group_count"],
                "blockers": len(artifact["blockers"]),
                "census_digest": artifact["census_digest"],
                "artifact": str(output),
            },
            sort_keys=True,
        )
    )
    return 0 if artifact["verdict"] == VERDICT_GO else 1


if __name__ == "__main__":
    raise SystemExit(main())
