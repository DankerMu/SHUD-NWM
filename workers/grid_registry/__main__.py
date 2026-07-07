"""CLI entry point for the grid registry (SUB-5 / Task 3.1b).

Invocation form:

.. code-block:: shell

    uv run python -m workers.grid_registry \
        --source-id ifs \
        --grid-json canonical/IFS/grid/ifs_0p25/grid.json \
        --sidecar canonical/IFS/grid/ifs_0p25/grid_snapshot_metadata.json \
        --grid-definition-uri canonical/IFS/grid/ifs_0p25/grid.json

On success the CLI prints the inserted ``grid_snapshot_id`` on a single line
of stdout and exits ``0``. On any raised :class:`RegistrationError` or
``ValueError`` (from ``normalize_source_id``) or ``GridSnapshotInputError``
the CLI writes a diagnostic line to stderr and exits ``1``.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from packages.common.grid_registry_store import (
    PsycopgGridRegistryStore,
    RegistryStoreError,
)
from workers.grid_registry.input_record import (
    GridSnapshotInputError,
    read_input_record,
)
from workers.grid_registry.registry import (
    RegistrationError,
    register_snapshot,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m workers.grid_registry",
        description=(
            "Register one canonical grid snapshot from a grid.json + sidecar "
            "pair into the append-only met.canonical_grid_snapshot registry."
        ),
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help=(
            "Source id (case-insensitive at CLI; normalized via "
            "packages.common.source_identity.normalize_source_id — e.g. "
            "'ifs' -> 'IFS', 'GFS' -> 'gfs', 'era5' -> 'ERA5')."
        ),
    )
    parser.add_argument(
        "--grid-json",
        required=True,
        type=pathlib.Path,
        help="Local path to the canonical grid.json.",
    )
    parser.add_argument(
        "--sidecar",
        required=True,
        type=pathlib.Path,
        help="Local path to grid_snapshot_metadata.json (sidecar).",
    )
    parser.add_argument(
        "--grid-definition-uri",
        required=True,
        help=(
            "Canonical object-store URI for the grid.json (e.g. "
            "'canonical/IFS/grid/ifs_0p25/grid.json'). Stored verbatim on the "
            "snapshot row."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "PostgreSQL connection URL. Defaults to the DATABASE_URL "
            "environment variable when omitted."
        ),
    )
    return parser


def _resolve_database_url(argv_value: str | None) -> str:
    if argv_value and argv_value.strip():
        return argv_value.strip()
    env_value = os.getenv("DATABASE_URL", "").strip()
    if env_value:
        return env_value
    raise SystemExit(
        "error: --database-url or the DATABASE_URL environment variable is required."
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    database_url = _resolve_database_url(args.database_url)

    try:
        record = read_input_record(
            args.source_id,
            args.grid_json,
            args.sidecar,
            grid_definition_uri=args.grid_definition_uri,
        )
    except GridSnapshotInputError as error:
        sys.stderr.write(
            f"error: input record rejected on field {error.field!r}: {error}\n"
        )
        return 1

    store = PsycopgGridRegistryStore(database_url=database_url)

    try:
        grid_snapshot_id = register_snapshot(
            record, source_id=args.source_id, store=store
        )
    except RegistrationError as error:
        sys.stderr.write(f"error: registration failed: {error}\n")
        return 1
    except RegistryStoreError as error:
        sys.stderr.write(f"error: store rejected write: {error}\n")
        return 1
    except ValueError as error:
        # Raised by normalize_source_id for unknown ids (matches sibling
        # convention in packages.common.met_store).
        sys.stderr.write(f"error: invalid source_id: {error}\n")
        return 1

    sys.stdout.write(f"{grid_snapshot_id}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
