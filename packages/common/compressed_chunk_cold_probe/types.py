"""Shared types, constants, and SQL for the isolated-cluster cold-residency probe."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROBE_NAME_PREFIX = "nhms-1892-probe-"
CONTAINER_PGDATA = "/home/postgres/pgdata/data"
CONTAINER_COLD = "/home/postgres/pgdata/tablespaces/nhms_cold"
CONTAINER_FULL = "/home/postgres/pgdata/tablespaces/probe_full"
DEFAULT_HOST_PORT = 55492
DEFAULT_LOCK_TIMEOUT = "2s"
DEFAULT_STATEMENT_TIMEOUT = "30s"
WATERMARK = datetime(2026, 7, 9, tzinfo=UTC)
LAG_SECONDS = 604800
CUTOFF = WATERMARK - timedelta(seconds=LAG_SECONDS)
WINDOW_STARTS = (
    datetime(2026, 6, 18, tzinfo=UTC),
    datetime(2026, 6, 25, tzinfo=UTC),
    datetime(2026, 7, 2, tzinfo=UTC),
    datetime(2026, 7, 9, tzinfo=UTC),
)
OWNED_NAME_RE = re.compile(r"^nhms-1892-probe-[0-9a-f]{8,32}$")

CHUNK_INFO_SQL = """
SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
       range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE (hypertable_schema, hypertable_name) IN (
    ('hydro', 'river_timeseries'),
    ('met', 'forcing_station_timeseries')
)


ORDER BY hypertable_schema, hypertable_name, range_end
"""
RELATION_OID_SQL = """
SELECT c.oid FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""
COMPRESSED_SIBLING_SQL = """
SELECT sibling.schema_name, sibling.table_name
FROM _timescaledb_catalog.chunk AS origin
JOIN _timescaledb_catalog.chunk AS sibling
  ON sibling.id = origin.compressed_chunk_id
WHERE origin.schema_name = %s AND origin.table_name = %s
  AND NOT origin.dropped AND NOT sibling.dropped
"""
RELATION_SQL = """
SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind,
       COALESCE(ts.spcname, 'pg_default') AS tablespace,
       pg_relation_size(c.oid) AS bytes,
       NULLIF(c.reltoastrelid, 0) AS toast_oid,
       i.indrelid AS heap_oid
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
LEFT JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.oid = ANY(%s)
ORDER BY c.oid
"""
INDEX_OIDS_SQL = "SELECT indexrelid FROM pg_index WHERE indrelid = ANY(%s) ORDER BY indexrelid"
WAL_LIMITATION = "instance-level pg_wal_lsn_diff from 0/0, not per-group WAL volume"


class ProbeError(RuntimeError):
    """Fail-closed probe error."""


class CommitAckLost(ProbeError):
    """Client commit acknowledgement was lost after the server commit completed."""


@dataclass
class OwnedResources:
    container_name: str | None = None
    work_root: Path | None = None
    created_work_root: bool = False
    created_container: bool = False
    created_paths: tuple[Path, ...] = ()

    def identity_bound(self) -> bool:
        if self.container_name and not OWNED_NAME_RE.fullmatch(self.container_name):
            return False
        if self.work_root is not None and PROBE_NAME_PREFIX not in self.work_root.name:
            return False
        return True


@dataclass
class ProbeConfig:
    mode: str
    container_name: str
    host_port: int
    work_root: Path
    image_id: str
    image_ref: str
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT
    statement_timeout: str = DEFAULT_STATEMENT_TIMEOUT
    docker_bin: str = "docker"
    output_path: Path | None = None
    keep: bool = False
    password: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    capacity_decision: Any = None
