"""Cold-only filesystem accounting from bounded observations."""

from __future__ import annotations

from typing import Any, Mapping

from packages.common import node27_resource_governance_collection as resource_collection


def _cold_relation_bytes(postgres: Mapping[str, Any] | object) -> tuple[int | None, str | None]:
    """Account cold-relation bytes only from an exact observed PostgreSQL inventory."""

    if not isinstance(postgres, Mapping) or postgres.get("status") != "ok":
        return None, "cold relation inventory is unavailable"
    if "cold_relation_by_tablespace" not in postgres:
        return None, "cold relation inventory is unavailable"
    cold_rows = postgres.get("cold_relation_by_tablespace")
    if not isinstance(cold_rows, list):
        return None, "cold relation inventory is unavailable"
    total = 0
    for row in cold_rows:
        if not isinstance(row, Mapping) or "bytes" not in row:
            return None, "cold relation inventory is unavailable"
        value = row["bytes"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, "cold relation inventory is unavailable"
        total += value
    return total, None


def cold_governance_sample(
    filesystem: Mapping[str, Any], postgres: Mapping[str, Any], *, path: str, observed_at: str
) -> dict[str, Any]:
    """Build one bounded category sample without recursively scanning shared roots."""

    filesystems = filesystem.get("filesystems") if isinstance(filesystem.get("filesystems"), Mapping) else {}
    path_sizes = filesystem.get("path_sizes") if isinstance(filesystem.get("path_sizes"), Mapping) else {}
    source = filesystems.get("home") if path == "/home" else filesystems.get("cold")
    source = source if isinstance(source, Mapping) else {}
    blockers: list[str] = []
    if source.get("status") != "ok":
        blockers.append(f"{path} disk observation is unavailable")
    identity = source.get("device_identity")
    if not isinstance(identity, str) or not identity:
        blockers.append(f"{path} filesystem identity is unavailable")
        identity = None
    total = resource_collection.observation_int(source.get("total_bytes"))
    free = resource_collection.observation_int(source.get("free_bytes"))
    used = resource_collection.observation_int(source.get("used_bytes"))
    reserved = resource_collection.observation_int(source.get("reserved_bytes"))
    if None in {total, free, used, reserved}:
        blockers.append(f"{path} capacity observation is incomplete")
    pgdata = path_sizes.get("pgdata_root") if isinstance(path_sizes.get("pgdata_root"), Mapping) else {}
    object_store_value = path_sizes.get("object_store_root")
    object_store = object_store_value if isinstance(object_store_value, Mapping) else {}
    pgdata_bytes = 0
    pgdata_identity = pgdata.get("device_identity")
    placements = [
        root
        for label, root in (("home", "/home"), ("cold", "/data/GHDC"))
        if isinstance(filesystems.get(label), Mapping)
        and filesystems[label].get("status") == "ok"
        and filesystems[label].get("device_identity") == pgdata_identity
    ]
    if pgdata.get("status") != "ok" or resource_collection.observation_int(pgdata.get("bytes")) is None:
        blockers.append("PGDATA du observation is unavailable")
    elif not isinstance(pgdata_identity, str) or not pgdata_identity or len(placements) != 1:
        blockers.append("PGDATA filesystem placement is unavailable or ambiguous")
    elif placements[0] == path:
        pgdata_bytes = int(pgdata["bytes"])
    object_store_path = str(object_store.get("path") or "")
    if object_store_path.startswith("/home/"):
        object_store_on = "/home"
    elif object_store_path.startswith("/data/GHDC/"):
        object_store_on = "/data/GHDC"
    else:
        object_store_on = None
    object_store_bytes = 0
    if object_store_on == path:
        if object_store.get("status") != "ok" or resource_collection.observation_int(object_store.get("bytes")) is None:
            blockers.append("object-store du observation is unavailable")
        else:
            object_store_bytes = int(object_store["bytes"])
    cold_bytes: int | None = 0
    if path == "/data/GHDC":
        cold_bytes, inventory_blocker = _cold_relation_bytes(postgres)
        if inventory_blocker is not None:
            blockers.append(inventory_blocker)
    unavailable = bool(blockers)
    return {
        "path": path,
        "observed_at": observed_at,
        "identity": None if unavailable else identity,
        "total_bytes": None if unavailable else total,
        "free_bytes": None if unavailable else free,
        "used_bytes": None if unavailable else used,
        "reserved_bytes": None if unavailable else reserved,
        "pgdata_bytes": None if unavailable else pgdata_bytes,
        "nhms_cold_relation_bytes": None if unavailable else cold_bytes,
        "object_store_bytes": None if unavailable else object_store_bytes,
        "status": "unavailable" if unavailable else "ok",
        "blockers": blockers,
    }
