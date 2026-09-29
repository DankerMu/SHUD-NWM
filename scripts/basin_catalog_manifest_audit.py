"""Audit the public basin catalog against the node-22 scheduler manifest (#2621).

The default ``GET /api/v1/basins`` is the raw ``core.basin`` directory (minus
``evidence-only``), retired basins included; ``has_display_product=true`` is
the displayable subset. Both sets are computed here by the production
``PsycopgModelRegistryStore.list_basins`` itself, paged to the end, so this
audit cannot drift from what the API serves. It passes only when

* the display set equals the manifest's ``models[].basin_id`` set, and
* every basin in the default catalog but not in the manifest has zero active
  ``core.model_instance`` rows (a retired basin or a rename leftover).

Exit codes: 0 pass, 1 violation, 2 configuration error (manifest missing,
unparseable or without basin ids; no database URL). A JSON receipt goes to
stdout in every case; the DSN is never printed. During a basin onboarding
window the display set can lag the manifest: that is reported as exit 1 with
both difference sets, not suppressed.

Every connection is forced read-only (``-c default_transaction_read_only=on``
merged into the DSN's ``options``), and ``list_basins`` opens one connection
per page, so each page is its own snapshot; the receipt says so.

Usage (node-27; ``nhms_display_ro`` is enough):
    uv run python scripts/basin_catalog_manifest_audit.py \\
        --manifest /home/ghdc/nwm/object-store/scheduler/registry/manifest-last.json \\
        [--database-url ... | DATABASE_URL=...] [--page-size 500] > receipt.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

EXIT_PASS = 0
EXIT_VIOLATION = 1
EXIT_CONFIG_ERROR = 2
DEFAULT_PAGE_SIZE = 500
APPLICATION_NAME = "nhms-basin-catalog-audit"
_READ_ONLY_OPTION = "-c default_transaction_read_only=on"
_SNAPSHOT_NOTE = (
    "list_basins opens one read-only connection per page: each page (and the active-model count) "
    "is its own snapshot, not one consistent read"
)
_ACTIVE_MODELS_SQL = """
    SELECT b.basin_id, count(mi.model_id) FILTER (WHERE mi.active_flag) AS active_models
    FROM unnest(%s::text[]) AS b(basin_id)
    LEFT JOIN core.basin_version bv ON bv.basin_id = b.basin_id
    LEFT JOIN core.model_instance mi ON mi.basin_version_id = bv.basin_version_id
    GROUP BY b.basin_id
"""

ActiveModelCounter = Callable[[Sequence[str]], dict[str, int]]


class BasinCatalog(Protocol):
    def list_basins(self, *, limit: int, offset: int, has_display_product: bool = False) -> list[dict[str, Any]]: ...


class ConfigError(Exception):
    """The audit cannot judge anything: exit 2, never a pass."""


def read_only_dsn(database_url: str) -> str:
    """``database_url`` with ``default_transaction_read_only=on`` appended to its libpq ``options``."""
    from psycopg2.extensions import make_dsn, parse_dsn

    parameters = parse_dsn(database_url)
    existing = parameters.get("options", "").strip()
    parameters["options"] = f"{existing} {_READ_ONLY_OPTION}".strip()
    return make_dsn(**parameters)


def load_manifest_basin_ids(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError(f"manifest not found: {path}") from error
    except OSError as error:
        raise ConfigError(f"manifest unreadable: {path}: {error.strerror}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"manifest is not valid JSON: {path}: {error.msg}") from error
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        raise ConfigError(f"manifest has no `models` list: {path}")
    basin_ids: set[str] = set()
    for index, row in enumerate(models):
        basin_id = row.get("basin_id") if isinstance(row, dict) else None
        if not isinstance(basin_id, str) or not basin_id.strip():
            raise ConfigError(f"manifest models[{index}] has no basin_id: {path}")
        basin_ids.add(basin_id)
    if not basin_ids:
        raise ConfigError(f"manifest lists no basin ids: {path}")
    return basin_ids


def collect_basin_ids(catalog: BasinCatalog, *, has_display_product: bool, page_size: int) -> set[str]:
    """Every basin_id ``list_basins`` serves, read page by page until a short page."""
    basin_ids: set[str] = set()
    offset = 0
    while True:
        page = catalog.list_basins(limit=page_size, offset=offset, has_display_product=has_display_product)
        basin_ids.update(str(row["basin_id"]) for row in page)
        if len(page) < page_size:
            return basin_ids
        offset += page_size


def run_audit(
    catalog: BasinCatalog, count_active_models: ActiveModelCounter, *, manifest_path: Path, page_size: int
) -> dict[str, Any]:
    manifest = load_manifest_basin_ids(manifest_path)
    default = collect_basin_ids(catalog, has_display_product=False, page_size=page_size)
    display = collect_basin_ids(catalog, has_display_product=True, page_size=page_size)
    extras = sorted(default - manifest)
    active = count_active_models(extras) if extras else {}
    catalog_extras = [{"basin_id": basin_id, "active_models": int(active.get(basin_id, 0))} for basin_id in extras]
    display_minus_manifest = sorted(display - manifest)
    manifest_minus_display = sorted(manifest - display)
    violations: list[dict[str, Any]] = [
        {"kind": "active_catalog_extra", **extra} for extra in catalog_extras if extra["active_models"] > 0
    ]
    violations += [{"kind": "display_not_in_manifest", "basin_id": basin_id} for basin_id in display_minus_manifest]
    violations += [{"kind": "manifest_not_in_display", "basin_id": basin_id} for basin_id in manifest_minus_display]
    return {
        "verdict": "violation" if violations else "pass",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "manifest_path": str(manifest_path),
        "page_size": page_size,
        "snapshot": _SNAPSHOT_NOTE,
        "default_count": len(default),
        "display_count": len(display),
        "manifest_count": len(manifest),
        "display_minus_manifest": display_minus_manifest,
        "manifest_minus_display": manifest_minus_display,
        "catalog_extras": catalog_extras,
        "violations": violations,
    }


def _connect_catalog(database_url: str) -> tuple[BasinCatalog, ActiveModelCounter]:
    """The production catalog and the active-model count, both on read-only connections."""
    import psycopg2

    from packages.common.model_registry import PsycopgModelRegistryStore

    dsn = read_only_dsn(database_url)

    def count_active_models(basin_ids: Sequence[str]) -> dict[str, int]:
        connection = psycopg2.connect(dsn, fallback_application_name=APPLICATION_NAME)
        try:
            with connection.cursor() as cursor:
                cursor.execute(_ACTIVE_MODELS_SQL, (list(basin_ids),))
                return {str(basin_id): int(count) for basin_id, count in cursor.fetchall()}
        finally:
            connection.rollback()
            connection.close()

    return PsycopgModelRegistryStore(dsn, application_name=APPLICATION_NAME), count_active_models


def _print(receipt: dict[str, Any]) -> None:
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="node-22 scheduler manifest-last.json")
    parser.add_argument("--database-url", default=None, help="default: $DATABASE_URL")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    args = parser.parse_args(argv)
    if args.page_size < 1:
        parser.error("--page-size must be at least 1")
    try:
        load_manifest_basin_ids(args.manifest)  # judged before any connection
        database_url = (args.database_url or os.environ.get("DATABASE_URL", "")).strip()
        if not database_url:
            raise ConfigError("no database URL: pass --database-url or set DATABASE_URL")
    except ConfigError as error:
        _print({"verdict": "config_error", "error": str(error), "manifest_path": str(args.manifest)})
        return EXIT_CONFIG_ERROR
    catalog, count_active_models = _connect_catalog(database_url)
    receipt = run_audit(catalog, count_active_models, manifest_path=args.manifest, page_size=args.page_size)
    _print(receipt)
    return EXIT_PASS if receipt["verdict"] == "pass" else EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
