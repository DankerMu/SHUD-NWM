"""TimescaleDB 2.10.2 compressed-chunk cold-residency contract (#1892).

Public seam for eligibility, complete physical-group resolution, the single
accepted shell-first sequence plan, and live-identity refusal. This module
does not open production connections, create tablespaces, or run Docker.
Engine success is proven only by the isolated 2.10.2 probe.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

_SNAPSHOT_PATH = Path(__file__).resolve().parent / "node27_external_contract_snapshot.json"
_SNAPSHOT = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))

PINNED_IMAGE_ID = str(_SNAPSHOT["host_context"]["nhms_db_image_id"]["value"])
PINNED_IMAGE_REF = str(_SNAPSHOT["host_context"]["nhms_db_image_ref"]["value"])
PINNED_PG_VERSION_PREFIX = "15.2"
PINNED_TIMESCALEDB_VERSION = str(_SNAPSHOT["host_context"]["timescaledb_version"]["value"])

LIVE_CONTAINER_NAME = "nhms-db"
LIVE_PORT = 55432
LIVE_PGDATA = "/home/nwm/nhms-pgdata"
COLD_TABLESPACE_NAME = "nhms_cold"
SOURCE_TABLESPACE_NAME = "pg_default"
ACCEPTED_SEQUENCE_NAME = "shell_first_decompress_recompress_atomic"
REJECTED_SEQUENCE_NAMES: frozenset[str] = frozenset(
    {
        "alter_tablespace_oid_order",
        "timescaledb_experimental.move_chunk",
        "direct_compressed_heap_alter",
        "direct_toast_alter",
        "decompress_first",
        "internal_compressed_hypertable_attach",
        "two_transaction",
    }
)

ALLOWED_HYPERTABLES: frozenset[tuple[str, str]] = frozenset(
    {
        ("hydro", "river_timeseries"),
        ("met", "forcing_station_timeseries"),
    }
)

LIVE_PATH_PREFIXES: tuple[str, ...] = (
    "/home/nwm/nhms-pgdata",
    "/home/nwm/NWM",
    "/home/nwm/nhms-evidence",
    "/data/GHDC",
    "/home/ghdc/nwm",
    "/data/GHDC/nwm-archive/nhms-tablespace",
    "/home/postgres/pgdata/tablespaces/ghdc",
    "/data/GHDC/nhms-cold-tablespace",
    "/home/postgres/pgdata/data",
    "/home/postgres/pgdata/tablespaces/nhms_cold",
)

SHELL_FIRST_PHASES: tuple[str, ...] = (
    "begin_timeouts",
    "lock_heaps",
    "move_origin_shell_and_indexes",
    "decompress",
    "prove_expanded_cold",
    "recompress",
    "prove_complete_cold",
    "commit",
)

ResidencyKind = Literal[
    "origin_heap",
    "compressed_heap",
    "index",
    "toast_heap",
    "toast_index",
]
Eligibility = Literal[
    "eligible",
    "ineligible_uncompressed",
    "ineligible_newer",
    "ineligible_hypertable",
    "refused_watermark",
]
ResidencyState = Literal["all_source", "all_target", "already_target", "mixed", "unknown"]
MoveKind = Literal["migrate", "already_cold", "blocked"]
Reconciliation = Literal["complete_source", "complete_target", "mixed", "unknown"]


class ColdResidencyError(RuntimeError):
    """Fail-closed residency contract error before any mutation."""


@dataclass(frozen=True)
class CatalogRelation:
    oid: int
    schema: str
    name: str
    relkind: str
    tablespace: str
    bytes: int
    toast_oid: int | None = None
    heap_oid: int | None = None


@dataclass(frozen=True)
class CatalogChunk:
    hypertable_schema: str
    hypertable_name: str
    origin_oid: int
    origin_schema: str
    origin_name: str
    compressed_oid: int | None
    compressed_schema: str | None
    compressed_name: str | None
    range_start: datetime
    range_end: datetime
    is_compressed: bool


@dataclass(frozen=True)
class ResidencyMember:
    kind: ResidencyKind
    oid: int
    schema: str
    name: str
    relkind: str
    tablespace: str
    bytes: int
    heap_oid: int | None = None
    toast_oid: int | None = None


@dataclass(frozen=True)
class DurableIdentity:
    hypertable_schema: str
    hypertable_name: str
    origin_oid: int
    origin_schema: str
    origin_name: str
    range_start: datetime
    range_end: datetime


@dataclass(frozen=True)
class ResidencyGroup:
    hypertable_schema: str
    hypertable_name: str
    origin_oid: int
    origin_schema: str
    origin_name: str
    compressed_oid: int | None
    compressed_schema: str | None
    compressed_name: str | None
    range_start: datetime
    range_end: datetime
    is_compressed: bool
    members: tuple[ResidencyMember, ...]
    blocker: str | None = None

    @property
    def durable_identity(self) -> DurableIdentity:
        return DurableIdentity(
            hypertable_schema=self.hypertable_schema,
            hypertable_name=self.hypertable_name,
            origin_oid=self.origin_oid,
            origin_schema=self.origin_schema,
            origin_name=self.origin_name,
            range_start=self.range_start,
            range_end=self.range_end,
        )


@dataclass(frozen=True)
class ShellFirstPlan:
    kind: MoveKind
    phases: tuple[str, ...]
    prefix_sql: tuple[str, ...]
    lock_sql: tuple[str, ...]
    shell_move_sql: tuple[str, ...]
    decompress_sql: str | None
    compress_sql: str | None
    lock_oids: tuple[int, ...]
    shell_move_oids: tuple[int, ...]
    reason: str | None = None


def quote_ident(name: str) -> str:
    if name == "":
        raise ColdResidencyError("identifier must be non-empty")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def qualified_ident(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def compute_cutoff(watermark: datetime | None, lag_seconds: int) -> datetime:
    if watermark is None:
        raise ColdResidencyError("display watermark is unavailable")
    if not isinstance(watermark, datetime):
        raise ColdResidencyError("display watermark must be timezone-aware")
    if watermark.tzinfo is None or watermark.utcoffset() is None:
        raise ColdResidencyError("display watermark must be timezone-aware")
    if lag_seconds <= 0:
        raise ColdResidencyError("lag_seconds must be positive")
    return watermark.astimezone(UTC) - timedelta(seconds=lag_seconds)


def classify_eligibility(
    *,
    hypertable_schema: str,
    hypertable_name: str,
    is_compressed: bool,
    range_end: datetime | None,
    watermark: datetime | None,
    lag_seconds: int,
) -> Eligibility:
    if watermark is None:
        return "refused_watermark"
    try:
        cutoff = compute_cutoff(watermark, lag_seconds)
    except ColdResidencyError:
        return "refused_watermark"
    if (hypertable_schema, hypertable_name) not in ALLOWED_HYPERTABLES:
        return "ineligible_hypertable"
    if range_end is None or range_end.tzinfo is None or range_end.utcoffset() is None:
        return "refused_watermark"
    if not is_compressed:
        return "ineligible_uncompressed"
    if range_end.astimezone(UTC) <= cutoff:
        return "eligible"
    return "ineligible_newer"


def _member_kind(*, is_origin: bool, relkind: str, is_toast: bool) -> ResidencyKind:
    if is_toast and relkind == "i":
        return "toast_index"
    if is_toast:
        return "toast_heap"
    if relkind == "i":
        return "index"
    if is_origin:
        return "origin_heap"
    return "compressed_heap"


def resolve_residency_group(
    chunk: CatalogChunk,
    relations: Sequence[CatalogRelation],
) -> ResidencyGroup:
    by_oid = {relation.oid: relation for relation in relations}
    if len(by_oid) != len(relations):
        return _blocked_group(chunk, "duplicated catalog oid")
    origin = by_oid.get(chunk.origin_oid)
    if origin is None:
        return _blocked_group(chunk, "origin heap is missing")
    if origin.relkind not in {"r", "t"}:
        return _blocked_group(chunk, "origin oid is not a heap")
    if chunk.is_compressed and chunk.compressed_oid is None:
        return _blocked_group(chunk, "compressed relation is missing")
    compressed = None
    if chunk.compressed_oid is not None:
        compressed = by_oid.get(chunk.compressed_oid)
        if compressed is None:
            return _blocked_group(chunk, "compressed relation is missing")
        if compressed.oid == origin.oid:
            return _blocked_group(chunk, "compressed relation aliases the origin")

    heap_oids = {origin.oid}
    toast_oids: set[int] = set()
    if origin.toast_oid:
        toast_oids.add(origin.toast_oid)
    if compressed is not None:
        heap_oids.add(compressed.oid)
        if compressed.toast_oid:
            toast_oids.add(compressed.toast_oid)

    members: list[ResidencyMember] = []
    seen: set[int] = set()

    def add_member(relation: CatalogRelation, *, is_origin: bool, is_toast: bool) -> str | None:
        if relation.oid in seen:
            return "duplicated catalog oid"
        seen.add(relation.oid)
        members.append(
            ResidencyMember(
                kind=_member_kind(is_origin=is_origin, relkind=relation.relkind, is_toast=is_toast),
                oid=relation.oid,
                schema=relation.schema,
                name=relation.name,
                relkind=relation.relkind,
                tablespace=relation.tablespace or SOURCE_TABLESPACE_NAME,
                bytes=relation.bytes,
                heap_oid=relation.heap_oid,
                toast_oid=relation.toast_oid,
            )
        )
        return None

    error = add_member(origin, is_origin=True, is_toast=False)
    if error:
        return _blocked_group(chunk, error)
    if compressed is not None:
        error = add_member(compressed, is_origin=False, is_toast=False)
        if error:
            return _blocked_group(chunk, error)

    for toast_oid in sorted(toast_oids):
        toast = by_oid.get(toast_oid)
        if toast is None:
            return _blocked_group(chunk, "owned TOAST heap is missing")
        is_origin_toast = toast_oid == origin.toast_oid
        error = add_member(toast, is_origin=is_origin_toast, is_toast=True)
        if error:
            return _blocked_group(chunk, error)

    for relation in sorted(relations, key=lambda item: item.oid):
        if relation.relkind != "i":
            continue
        owner = relation.heap_oid
        if owner is None:
            return _blocked_group(chunk, "index is missing heap ownership")
        if owner not in heap_oids and owner not in toast_oids:
            return _blocked_group(chunk, "cross-group index mapping")
        is_toast = owner in toast_oids
        is_origin = owner == origin.oid or owner == origin.toast_oid
        error = add_member(relation, is_origin=is_origin, is_toast=is_toast)
        if error:
            return _blocked_group(chunk, error)

    if chunk.is_compressed and all(member.kind != "compressed_heap" for member in members):
        return _blocked_group(chunk, "compressed relation is missing")

    members_sorted = tuple(sorted(members, key=lambda member: member.oid))
    return ResidencyGroup(
        hypertable_schema=chunk.hypertable_schema,
        hypertable_name=chunk.hypertable_name,
        origin_oid=chunk.origin_oid,
        origin_schema=chunk.origin_schema,
        origin_name=chunk.origin_name,
        compressed_oid=chunk.compressed_oid,
        compressed_schema=chunk.compressed_schema,
        compressed_name=chunk.compressed_name,
        range_start=chunk.range_start,
        range_end=chunk.range_end,
        is_compressed=chunk.is_compressed,
        members=members_sorted,
    )


def _blocked_group(chunk: CatalogChunk, reason: str) -> ResidencyGroup:
    return ResidencyGroup(
        hypertable_schema=chunk.hypertable_schema,
        hypertable_name=chunk.hypertable_name,
        origin_oid=chunk.origin_oid,
        origin_schema=chunk.origin_schema,
        origin_name=chunk.origin_name,
        compressed_oid=chunk.compressed_oid,
        compressed_schema=chunk.compressed_schema,
        compressed_name=chunk.compressed_name,
        range_start=chunk.range_start,
        range_end=chunk.range_end,
        is_compressed=chunk.is_compressed,
        members=(),
        blocker=reason,
    )


def classify_residency(
    members: Sequence[ResidencyMember],
    *,
    source: str = SOURCE_TABLESPACE_NAME,
    target: str = COLD_TABLESPACE_NAME,
) -> ResidencyState:
    if not members:
        return "unknown"
    spaces = {member.tablespace or source for member in members}
    if spaces == {target}:
        return "already_target"
    if spaces == {source}:
        return "all_source"
    return "mixed"


def lockable_heaps(group: ResidencyGroup) -> tuple[ResidencyMember, ...]:
    return tuple(
        sorted(
            (member for member in group.members if member.relkind == "r"),
            key=lambda member: member.oid,
        )
    )


def origin_shell_members(group: ResidencyGroup) -> tuple[ResidencyMember, ...]:
    origin_oid = group.origin_oid
    members = [
        member
        for member in group.members
        if member.kind == "origin_heap" or (member.kind == "index" and member.heap_oid == origin_oid)
    ]
    return tuple(sorted(members, key=lambda member: member.oid))


def _lock_sql(member: ResidencyMember) -> str:
    return f"LOCK TABLE {qualified_ident(member.schema, member.name)} IN ACCESS EXCLUSIVE MODE"


def _move_sql(member: ResidencyMember, target: str) -> str:
    rel = qualified_ident(member.schema, member.name)
    space = quote_ident(target)
    if member.relkind == "i":
        return f"ALTER INDEX {rel} SET TABLESPACE {space}"
    return f"ALTER TABLE {rel} SET TABLESPACE {space}"


def _chunk_regclass(group: ResidencyGroup) -> str:
    return f"{group.origin_schema}.{group.origin_name}"


def accepted_transaction_prefix(*, lock_timeout: str, statement_timeout: str) -> tuple[str, ...]:
    return (
        "BEGIN",
        f"SET LOCAL lock_timeout = {quote_literal(lock_timeout)}",
        f"SET LOCAL statement_timeout = {quote_literal(statement_timeout)}",
    )


def build_shell_first_plan(
    group: ResidencyGroup,
    *,
    target: str = COLD_TABLESPACE_NAME,
    source: str = SOURCE_TABLESPACE_NAME,
    lock_timeout: str = "2s",
    statement_timeout: str = "30s",
) -> ShellFirstPlan:
    prefix = accepted_transaction_prefix(lock_timeout=lock_timeout, statement_timeout=statement_timeout)
    if group.blocker:
        return ShellFirstPlan("blocked", (), prefix, (), (), None, None, (), (), group.blocker)
    if not group.members:
        return ShellFirstPlan("blocked", (), prefix, (), (), None, None, (), (), "residency group is empty")
    state = classify_residency(group.members, source=source, target=target)
    locks = lockable_heaps(group)
    shell = origin_shell_members(group)
    lock_sql = tuple(_lock_sql(member) for member in locks)
    lock_oids = tuple(member.oid for member in locks)
    if state == "already_target":
        return ShellFirstPlan(
            kind="already_cold",
            phases=("begin_timeouts", "lock_heaps"),
            prefix_sql=prefix,
            lock_sql=lock_sql,
            shell_move_sql=(),
            decompress_sql=None,
            compress_sql=None,
            lock_oids=lock_oids,
            shell_move_oids=(),
            reason="already_cold",
        )
    if state != "all_source":
        return ShellFirstPlan(
            "blocked",
            (),
            prefix,
            (),
            (),
            None,
            None,
            (),
            (),
            f"group residency is {state}",
        )
    if group.compressed_oid is None or not group.is_compressed:
        return ShellFirstPlan("blocked", (), prefix, (), (), None, None, (), (), "compressed relation is missing")
    origin = f"{group.origin_schema}.{group.origin_name}"
    return ShellFirstPlan(
        kind="migrate",
        phases=SHELL_FIRST_PHASES,
        prefix_sql=prefix,
        lock_sql=lock_sql,
        shell_move_sql=tuple(_move_sql(member, target) for member in shell),
        decompress_sql=f"SELECT decompress_chunk({quote_literal(origin)}::regclass)::text",
        compress_sql=f"SELECT compress_chunk({quote_literal(origin)}::regclass)::text",
        lock_oids=lock_oids,
        shell_move_oids=tuple(member.oid for member in shell),
    )


def move_chunk_candidate_sql(chunk_regclass: str, target: str = COLD_TABLESPACE_NAME) -> str:
    return (
        "CALL timescaledb_experimental.move_chunk("
        f"{quote_literal(chunk_regclass)}::regclass, "
        f"{quote_literal(target)}, "
        f"{quote_literal(target)})"
    )


def _normalize_path(path: str) -> str:
    raw = path.strip()
    if raw == "":
        raise ColdResidencyError("path must be non-empty")
    posix = PurePosixPath(raw)
    if not posix.is_absolute():
        raise ColdResidencyError(f"path must be absolute: {path}")
    normalized = str(posix)
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def path_is_forbidden(path: str) -> bool:
    normalized = _normalize_path(path)
    for prefix in LIVE_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def refuse_live_identity(
    *,
    container_name: str,
    host_port: int,
    pgdata: str,
    extra_paths: Iterable[str] = (),
) -> None:
    if container_name.strip() == LIVE_CONTAINER_NAME:
        raise ColdResidencyError(f"refusing live container {LIVE_CONTAINER_NAME}: tablespaces are cluster-scoped")
    if int(host_port) == LIVE_PORT:
        raise ColdResidencyError(f"refusing live PostgreSQL port {LIVE_PORT}")
    if int(host_port) <= 0 or int(host_port) > 65535:
        raise ColdResidencyError("host port is out of range")
    for path in (pgdata, *extra_paths):
        if path_is_forbidden(path):
            raise ColdResidencyError(f"refusing live/production path {path}: tablespace DDL is cluster-scoped")


def _parity_matches(
    before_parity: Mapping[str, Any] | None,
    after_parity: Mapping[str, Any] | None,
) -> bool:
    if before_parity is None or after_parity is None:
        return False
    return dict(before_parity) == dict(after_parity)


def classify_reconciliation(
    before: ResidencyGroup,
    after: ResidencyGroup | None,
    *,
    before_parity: Mapping[str, Any] | None = None,
    after_parity: Mapping[str, Any] | None = None,
    source: str = SOURCE_TABLESPACE_NAME,
    target: str = COLD_TABLESPACE_NAME,
) -> Reconciliation:
    if after is None:
        return "unknown"
    if after.blocker or not after.members:
        return "unknown"
    if after.durable_identity != before.durable_identity:
        return "unknown"
    if before_parity is None or after_parity is None:
        return "unknown"
    if not _parity_matches(before_parity, after_parity):
        return "unknown"
    state = classify_residency(after.members, source=source, target=target)
    if state == "mixed":
        return "mixed"
    if state == "already_target":
        if not after.is_compressed or after.compressed_oid is None:
            return "unknown"
        return "complete_target"
    if state == "all_source":
        if not after.is_compressed or after.compressed_oid is None:
            return "unknown"
        if (
            after.compressed_oid != before.compressed_oid
            or after.compressed_schema != before.compressed_schema
            or after.compressed_name != before.compressed_name
        ):
            return "unknown"
        return "complete_source"
    return "unknown"


def _normalize_image_id(value: str) -> str:
    text = value.strip()
    if text.startswith("sha256:"):
        return text
    if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text.lower()):
        return f"sha256:{text}"
    return text


def _image_ref_matches_pin(value: str, image_id: str) -> bool:
    text = value.strip()
    if text == PINNED_IMAGE_REF:
        return True
    return _normalize_image_id(text) == _normalize_image_id(image_id) == PINNED_IMAGE_ID


def check_engine_identity(
    *,
    live_image_id: str,
    live_image_ref: str,
    requested_image_id: str,
    requested_image_ref: str,
    used_image_id: str | None = None,
    used_image_ref: str | None = None,
    server_version: str | None = None,
    timescaledb_version: str | None = None,
) -> dict[str, bool]:
    live_id = _normalize_image_id(live_image_id)
    requested_id = _normalize_image_id(requested_image_id)
    used_id = requested_id if used_image_id is None else _normalize_image_id(used_image_id)
    live_ref = (live_image_ref or "").strip()
    requested_ref = (requested_image_ref or "").strip()
    used_ref = requested_ref if used_image_ref is None else used_image_ref.strip()
    live_matches = live_id == PINNED_IMAGE_ID and _image_ref_matches_pin(live_ref, live_id)
    requested_matches = requested_id == PINNED_IMAGE_ID and _image_ref_matches_pin(requested_ref, requested_id)
    used_matches = used_id == requested_id and _image_ref_matches_pin(used_ref, used_id)
    image_pin_ok = live_matches and requested_matches and used_matches
    pg_matches = True if server_version is None else str(server_version).startswith(PINNED_PG_VERSION_PREFIX)
    ts_matches = True if timescaledb_version is None else str(timescaledb_version) == PINNED_TIMESCALEDB_VERSION
    if not image_pin_ok or not pg_matches or not ts_matches:
        raise ColdResidencyError("engine identity drift")
    return {
        "image_pin_ok": True,
        "pg_matches_pin": pg_matches,
        "ts_matches_pin": ts_matches,
        "live_matches_pin": True,
        "requested_matches_pin": True,
        "used_matches_requested": True,
    }


_CAPACITY_INT_MAX = 2**63 - 1


@dataclass(frozen=True)
class CapacityDecision:
    approved: bool
    before_compression_total_bytes: int
    retained_source_bytes: int
    cold_free_bytes: int
    cold_reserve_bytes: int
    required_cold_bytes: int
    cold_headroom_bytes: int
    hot_free_bytes: int
    wal_reserve_bytes: int
    required_hot_bytes: int
    hot_headroom_bytes: int
    blockers: tuple[str, ...]

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "before_compression_total_bytes": self.before_compression_total_bytes,
            "retained_source_bytes": self.retained_source_bytes,
            "cold_free_bytes": self.cold_free_bytes,
            "cold_reserve_bytes": self.cold_reserve_bytes,
            "required_cold_bytes": self.required_cold_bytes,
            "cold_headroom_bytes": self.cold_headroom_bytes,
            "hot_free_bytes": self.hot_free_bytes,
            "wal_reserve_bytes": self.wal_reserve_bytes,
            "required_hot_bytes": self.required_hot_bytes,
            "hot_headroom_bytes": self.hot_headroom_bytes,
            "blockers": list(self.blockers),
        }


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ColdResidencyError(f"capacity {name} must be a nonnegative integer")
    if value < 0:
        raise ColdResidencyError(f"capacity {name} must be a nonnegative integer")
    if value > _CAPACITY_INT_MAX:
        raise ColdResidencyError(f"capacity {name} overflow")
    return value


def _add_capacity(left: int, right: int, *, label: str) -> int:
    if right > _CAPACITY_INT_MAX - left:
        raise ColdResidencyError(f"capacity {label} overflow")
    return left + right


def evaluate_capacity_preflight(
    *,
    before_compression_total_bytes: object,
    cold_free_bytes: object,
    cold_reserve_bytes: object,
    hot_free_bytes: object,
    wal_reserve_bytes: object,
    retained_source_bytes: object,
) -> CapacityDecision:
    expanded = _require_nonneg_int("before_compression_total_bytes", before_compression_total_bytes)
    cold_free = _require_nonneg_int("cold_free_bytes", cold_free_bytes)
    cold_reserve = _require_nonneg_int("cold_reserve_bytes", cold_reserve_bytes)
    hot_free = _require_nonneg_int("hot_free_bytes", hot_free_bytes)
    wal_reserve = _require_nonneg_int("wal_reserve_bytes", wal_reserve_bytes)
    retained = _require_nonneg_int("retained_source_bytes", retained_source_bytes)
    required_cold = _add_capacity(expanded, cold_reserve, label="required_cold")
    required_hot = wal_reserve
    blockers: list[str] = []
    if cold_free < required_cold:
        blockers.append(f"cold free {cold_free} < required {required_cold}")
    if hot_free < required_hot:
        blockers.append(f"hot free {hot_free} < required wal {required_hot}")
    return CapacityDecision(
        approved=not blockers,
        before_compression_total_bytes=expanded,
        retained_source_bytes=retained,
        cold_free_bytes=cold_free,
        cold_reserve_bytes=cold_reserve,
        required_cold_bytes=required_cold,
        cold_headroom_bytes=cold_free - required_cold,
        hot_free_bytes=hot_free,
        wal_reserve_bytes=wal_reserve,
        required_hot_bytes=required_hot,
        hot_headroom_bytes=hot_free - required_hot,
        blockers=tuple(blockers),
    )


def validate_catalog_path(*, catalog_location: str, expected_location: str) -> None:
    if _normalize_path(catalog_location) != _normalize_path(expected_location):
        raise ColdResidencyError(
            "catalog/path identity mismatch: "
            f"catalog={catalog_location!r} expected={expected_location!r}"
        )


def origin_shell_is_not_complete(group: ResidencyGroup, *, target: str = COLD_TABLESPACE_NAME) -> bool:
    if group.blocker or not group.members:
        return True
    origin = next((member for member in group.members if member.kind == "origin_heap"), None)
    if origin is None or origin.tablespace != target:
        return True
    return any(member.tablespace != target for member in group.members)


def _owned_toast_complete(group: ResidencyGroup, heap: ResidencyMember) -> bool:
    if heap.toast_oid is None:
        return True
    toast = next((member for member in group.members if member.oid == heap.toast_oid), None)
    if toast is None or toast.kind != "toast_heap":
        return False
    return any(member.kind == "toast_index" and member.heap_oid == toast.oid for member in group.members)


def expanded_uncompressed_group_is_complete(group: ResidencyGroup, *, target: str = COLD_TABLESPACE_NAME) -> bool:
    if group.blocker or not group.members:
        return False
    if group.is_compressed or group.compressed_oid is not None:
        return False
    if any(member.kind == "compressed_heap" for member in group.members):
        return False
    origin = next((member for member in group.members if member.kind == "origin_heap"), None)
    if origin is None or origin.tablespace != target:
        return False
    if not _owned_toast_complete(group, origin):
        return False
    return all(member.tablespace == target for member in group.members)


def recompressed_group_is_complete(group: ResidencyGroup, *, target: str = COLD_TABLESPACE_NAME) -> bool:
    if group.blocker or not group.members:
        return False
    if not group.is_compressed or group.compressed_oid is None:
        return False
    origin = next((member for member in group.members if member.kind == "origin_heap"), None)
    compressed = next((member for member in group.members if member.kind == "compressed_heap"), None)
    if origin is None or compressed is None or compressed.oid != group.compressed_oid:
        return False
    if not _owned_toast_complete(group, origin) or not _owned_toast_complete(group, compressed):
        return False
    return classify_residency(group.members, target=target) == "already_target"


def same_window_groups_are_separate(left: ResidencyGroup, right: ResidencyGroup) -> bool:
    left_oids = {member.oid for member in left.members}
    right_oids = {member.oid for member in right.members}
    return left.durable_identity != right.durable_identity and left_oids.isdisjoint(right_oids)


def snapshot_image_identity() -> Mapping[str, str]:
    return {
        "image_id": PINNED_IMAGE_ID,
        "image_ref": PINNED_IMAGE_REF,
        "pg_version_prefix": PINNED_PG_VERSION_PREFIX,
        "timescaledb_version": PINNED_TIMESCALEDB_VERSION,
    }


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if type(value).__name__ == "Decimal":
        return str(value)
    return value
