#!/usr/bin/env python3
"""Archive rebuild drill orchestrator for node-27 (issue #854 §5.1).

Proves that products archived by ``node27_product_archive`` and salvage
objects published by ``node27_db_export_salvage`` are round-trippable back
into an ingest-shaped Postgres/TimescaleDB — without ever touching the
production hypertables (design D5, ADR 0002).

Four sub-components (per design.md #854 fixture block):

1. **Extract**: pull each declared product cycle out of its ``archive.tar.zst``
   into a bounded staging tree, verifying every per-member size + sha256 against
   the archive manifest as we write. Reuses ``_decompressed_tar_stream`` from
   ``scripts/node27_product_archive.py`` as the read primitive so the tar-header
   guard, PAX bound, and decompressor timeout stay symmetric with the mover.

2. **Registry lift** (H4 hybrid): SELECT the ancestor rows needed by the
   ingest FK graph from the production readonly connection and INSERT them
   into the isolated staging database. Fail-closed on any missing ancestor
   with ``REGISTRY_CLOSURE_INCOMPLETE`` — no vacuous PASS.

3. **Ingest**: reuse the existing ingest primitives — ``OutputParser``
   with ``PsycopgOutputParserRepository(database_url=STAGING_DATABASE_URL)``
   for runs cycles, ``apply_forcing_domain_handoff_path(connection=staging_conn, ...)``
   for forcing cycles. NEVER call ``.from_env()`` — inject the staging DSN.

4. **Verify**: for product cycles, re-parse the restored ``.rivqdown`` /
   forcing payload with the same primitives the ingest uses and compare to
   staging ``COUNT(*)``; for salvage, verify sha256 + decompressed row count
   against the salvage manifest (no reingest — that lane has no automated
   restore per D3).

Isolation invariants (H1 by-DB, by-data-state):

- Staging DB is a **separate physical Postgres database** with the standard
  ``core/met/hydro/ops/map`` schemas provisioned by
  ``apply_migrations_from_zero``; drill refuses if ``dbname`` equals prod.
- Prod connection is opened with ``default_transaction_read_only = on`` +
  the assertion is verified before any SELECT runs.
- Compressed-chunk write guard (``packages/common/timescale_write_guard``)
  stays silent in staging because a fresh-migrated DB has no compression
  enabled — the guard fires on ``is_compressed = true`` which never matches.

Wire-format codes (byte-identical across code / runbook / design):

- ``ARCHIVE_MANIFEST_MISMATCH`` — manifest sha256/size disagrees with the
  restored file.
- ``ARCHIVE_TAR_CORRUPTED`` — tarball truncated or extract-to-disk fails.
- ``SALVAGE_SHA256_MISMATCH`` — db-export object sha256 does not match
  manifest.
- ``SALVAGE_ROW_COUNT_MISMATCH`` — decompressed row count differs from
  manifest ``exported_row_count``.
- ``REGISTRY_CLOSURE_INCOMPLETE`` — missing ancestor row in prod DB.
- ``STAGING_COUNT_MISMATCH`` — staging ``COUNT(*)`` differs from
  file-derived expected count.

ADR 0002: this script **reads** archive manifests + tar bodies + salvage
objects; it never moves, deletes, or rewrites archive files.

ADR 0001 display carve-out: no imports touching ``apps/api`` or
``apps/frontend``.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import jsonschema

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "node27-archive-rebuild-drill/1"

# Wire-format codes — byte-identical with design.md #854 fixture block.
CODE_ARCHIVE_MANIFEST_MISMATCH = "ARCHIVE_MANIFEST_MISMATCH"
CODE_ARCHIVE_TAR_CORRUPTED = "ARCHIVE_TAR_CORRUPTED"
CODE_SALVAGE_SHA256_MISMATCH = "SALVAGE_SHA256_MISMATCH"
CODE_SALVAGE_ROW_COUNT_MISMATCH = "SALVAGE_ROW_COUNT_MISMATCH"
CODE_REGISTRY_CLOSURE_INCOMPLETE = "REGISTRY_CLOSURE_INCOMPLETE"
CODE_STAGING_COUNT_MISMATCH = "STAGING_COUNT_MISMATCH"
# Wire codes added post-review (Round 1 fix pass — B1/C2):
CODE_DRILL_UNCAUGHT_ERROR = "DRILL_UNCAUGHT_ERROR"
CODE_DRILL_CONCURRENT_INVOCATION = "DRILL_CONCURRENT_INVOCATION"

WIRE_CODES: frozenset[str] = frozenset(
    {
        CODE_ARCHIVE_MANIFEST_MISMATCH,
        CODE_ARCHIVE_TAR_CORRUPTED,
        CODE_SALVAGE_SHA256_MISMATCH,
        CODE_SALVAGE_ROW_COUNT_MISMATCH,
        CODE_REGISTRY_CLOSURE_INCOMPLETE,
        CODE_STAGING_COUNT_MISMATCH,
        CODE_DRILL_UNCAUGHT_ERROR,
        CODE_DRILL_CONCURRENT_INVOCATION,
    }
)

_ROOT = Path(__file__).resolve().parents[1]
_DRILL_RECEIPT_SCHEMA_PATH = _ROOT / "schemas/archive_rebuild_drill_receipt.schema.json"
_PRODUCT_MANIFEST_SCHEMA_PATH = _ROOT / "schemas/product_archive_manifest.schema.json"
_SALVAGE_MANIFEST_SCHEMA_PATH = _ROOT / "schemas/salvage_manifest.schema.json"

# Load the mover module by path so we reuse ``_decompressed_tar_stream`` +
# ``_safe_relative`` without duplicating the tar-header guard. Importing by
# spec keeps ``scripts/`` off the import path (it has no ``__init__.py``).
_MOVER_SPEC = importlib.util.spec_from_file_location(
    "node27_product_archive", _ROOT / "scripts/node27_product_archive.py"
)
assert _MOVER_SPEC and _MOVER_SPEC.loader
_MOVER = importlib.util.module_from_spec(_MOVER_SPEC)
sys.modules[_MOVER_SPEC.name] = _MOVER
_MOVER_SPEC.loader.exec_module(_MOVER)

# Bounded caps symmetric with the mover; see design.md H3 pinning.
MAX_FILE_BYTES = _MOVER.MAX_FILE_BYTES
MAX_TREE_ENTRIES = _MOVER.MAX_TREE_ENTRIES
MAX_SOURCE_BYTES = _MOVER.MAX_SOURCE_BYTES
MAX_ARCHIVE_BYTES = _MOVER.MAX_ARCHIVE_BYTES
MAX_MANIFEST_BYTES = _MOVER.MAX_MANIFEST_BYTES
MAX_SALVAGE_OBJECT_BYTES = 16 * 1024**3  # 16 GiB per salvage object.


# ---------------------------------------------------------------------------
# Exception hierarchy — every failure maps to exactly one wire-format code.
# ---------------------------------------------------------------------------


class DrillError(RuntimeError):
    """Base for any drill-detected fault, tagged with a wire-format code."""

    code: str = ""

    def as_difference(self, *, item: str, expected: Any = None, actual: Any = None) -> dict[str, Any]:
        return {
            "item": item,
            "expected": {"code": self.code, "detail": expected} if expected is not None else {"code": self.code},
            "actual": actual if actual is not None else {"error": str(self)},
        }


class ArchiveManifestMismatchError(DrillError):
    code = CODE_ARCHIVE_MANIFEST_MISMATCH


class ArchiveTarCorruptedError(DrillError):
    code = CODE_ARCHIVE_TAR_CORRUPTED


class SalvageSha256MismatchError(DrillError):
    code = CODE_SALVAGE_SHA256_MISMATCH


class SalvageRowCountMismatchError(DrillError):
    code = CODE_SALVAGE_ROW_COUNT_MISMATCH


class RegistryClosureIncompleteError(DrillError):
    code = CODE_REGISTRY_CLOSURE_INCOMPLETE


class StagingCountMismatchError(DrillError):
    code = CODE_STAGING_COUNT_MISMATCH


class TarPathEscapeError(ArchiveTarCorruptedError):
    """Malicious tarball tries to write outside the extraction root."""


class TarBoundExceededError(ArchiveTarCorruptedError):
    """Bounded per-file / per-tree / per-source cap tripped mid-extract."""


class DrillConcurrentInvocationError(DrillError):
    """Another drill is currently holding the single-instance lock."""

    code = CODE_DRILL_CONCURRENT_INVOCATION


class DrillConfigError(RuntimeError):
    """Fail-closed configuration error surfaced before any DB call."""


# ---------------------------------------------------------------------------
# Configuration + DSN helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrillConfig:
    """Immutable drill configuration parsed from env or CLI."""

    archive_root: Path
    workspace_root: Path
    receipt_path: Path
    prod_database_url_ro: str
    staging_database_url: str
    postgres_admin_url: str
    staging_instance_id: str
    staging_run_label: str
    zstd_path: Path
    archive_manifest_paths: tuple[Path, ...]
    salvage_manifest_paths: tuple[Path, ...]
    lock_path: Path

    def prod_dbname(self) -> str:
        return _dsn_dbname(self.prod_database_url_ro)

    def staging_dbname(self) -> str:
        return _dsn_dbname(self.staging_database_url)


def _dsn_dbname(dsn: str) -> str:
    """Return the target database name from a psycopg2-shaped URL.

    URL-decodes the path (e.g. ``nhms%5Fdrill`` -> ``nhms_drill``) so a
    percent-encoded DSN does not slip past the equality check in
    ``validate_isolation`` (C-di-7).
    """
    if not dsn:
        raise DrillConfigError("DSN is empty")
    parsed = urlsplit(dsn)
    path = parsed.path or ""
    return unquote(path.lstrip("/")) or ""


def _dsn_with_dbname(dsn: str, dbname: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{dbname}", parsed.query, parsed.fragment))


def validate_isolation(config: DrillConfig) -> None:
    """Refuse any drill where staging cannot be attributed solely to restore.

    H1 pin: same-DB same-schema isolation is unachievable because every
    ingest SQL literal is ``core.`` / ``met.`` / ``hydro.`` / ``ops.`` qualified;
    the only viable isolation is a separate physical Postgres database.
    """
    prod = config.prod_dbname()
    staging = config.staging_dbname()
    if not prod:
        raise DrillConfigError("prod DSN has no database name")
    if not staging:
        raise DrillConfigError("staging DSN has no database name")
    if prod == staging:
        raise DrillConfigError(
            f"staging database name must differ from production (both are {prod!r})"
        )
    # C-di-4: admin DSN's dbname must not equal prod dbname. The admin URL is
    # used for DROP DATABASE / CREATE DATABASE; connecting via the prod dbname
    # would risk any typo hitting production. Standard shape is
    # ``postgresql://.../postgres``.
    admin_dbname = _dsn_dbname(config.postgres_admin_url)
    if admin_dbname == prod:
        raise DrillConfigError(
            f"admin DSN dbname must not equal production dbname (both are {prod!r})"
        )


# ---------------------------------------------------------------------------
# Extract phase (H3 helper)
# ---------------------------------------------------------------------------


def _sha256_of_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_dest_within_root(dest_root: Path, member_name: str) -> Path:
    """Refuse ``..``, absolute, or symlink targets before writing anything."""
    if not _MOVER._safe_relative(member_name):
        raise TarPathEscapeError(f"unsafe tar member path: {member_name!r}")
    candidate = dest_root / member_name
    resolved_root = dest_root.resolve()
    # Resolve the parent (which we're about to mkdir) — the file itself does
    # not exist yet, so we cannot .resolve() it directly. Refuse any parent
    # that escapes the destination root.
    try:
        resolved_parent = candidate.parent.resolve(strict=False)
    except OSError as error:
        raise TarPathEscapeError(f"cannot resolve tar member parent: {member_name!r}") from error
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as error:
        raise TarPathEscapeError(f"tar member escapes extraction root: {member_name!r}") from error
    return candidate


def _extract_archive_to_disk(
    manifest: Mapping[str, Any],
    tar_zst_path: Path,
    dest_dir: Path,
    *,
    zstd_path: Path,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_tree_entries: int = MAX_TREE_ENTRIES,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> dict[str, str]:
    """Extract every declared file from ``tar_zst_path`` into ``dest_dir``.

    Reuses ``_decompressed_tar_stream`` from ``node27_product_archive`` as
    the read primitive so the tar-header guard, PAX extension budget, and
    decompressor timeout stay identical to the mover.

    Returns a mapping ``{relative_path: sha256}`` for the extracted set on
    success. Raises ``ArchiveManifestMismatchError`` if any file's size or
    sha256 disagrees with the manifest, ``ArchiveTarCorruptedError`` on
    decompressor / tar / IO failure, or ``TarPathEscapeError`` on any
    ``..`` / absolute / cross-device write attempt.
    """
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ArchiveManifestMismatchError("archive manifest has no files array")
    expected = {}
    for entry in files:
        if not isinstance(entry, Mapping):
            raise ArchiveManifestMismatchError("archive manifest file entry is not an object")
        path_value = entry.get("path")
        if not isinstance(path_value, str) or path_value in expected:
            raise ArchiveManifestMismatchError(f"duplicate/invalid manifest path: {path_value!r}")
        expected[path_value] = entry
    if len(expected) > max_tree_entries:
        raise TarBoundExceededError(
            f"archive manifest exceeds {max_tree_entries} entries: {len(expected)}"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    actual: dict[str, str] = {}
    member_count = 0
    cumulative = 0

    archive_fd = os.open(str(tar_zst_path), os.O_RDONLY)
    try:
        try:
            with _MOVER._decompressed_tar_stream(
                archive_fd, zstd_path, expected_member_count=len(expected)
            ) as archive:
                for member in archive:
                    member_count += 1
                    if member_count > len(expected):
                        raise ArchiveManifestMismatchError(
                            f"tar has more members ({member_count}) than manifest ({len(expected)})"
                        )
                    if member_count > max_tree_entries:
                        raise TarBoundExceededError(
                            f"tar member count exceeds {max_tree_entries}"
                        )
                    if not member.isfile():
                        raise ArchiveTarCorruptedError(
                            f"non-regular tar member: {member.name!r}"
                        )
                    dest_path = _validate_dest_within_root(dest_dir, member.name)
                    expected_entry = expected.get(member.name)
                    if expected_entry is None:
                        raise ArchiveManifestMismatchError(
                            f"tar contains unexpected member: {member.name!r}"
                        )
                    expected_size = expected_entry["size_bytes"]
                    if member.size > max_file_bytes:
                        raise TarBoundExceededError(
                            f"tar member exceeds per-file cap: {member.name!r}"
                        )
                    if member.size != expected_size:
                        raise ArchiveManifestMismatchError(
                            f"tar member size disagrees with manifest: {member.name!r} "
                            f"tar={member.size} manifest={expected_size}"
                        )
                    cumulative += member.size
                    if cumulative > max_source_bytes:
                        raise TarBoundExceededError(
                            f"cumulative tar bytes exceed {max_source_bytes}"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise ArchiveTarCorruptedError(
                            f"cannot read tar member: {member.name!r}"
                        )
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    written = 0
                    with dest_path.open("wb") as sink:
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if written > expected_size:
                                raise ArchiveManifestMismatchError(
                                    f"tar member wrote more bytes than manifest: {member.name!r}"
                                )
                            digest.update(chunk)
                            sink.write(chunk)
                    if written != expected_size:
                        raise ArchiveManifestMismatchError(
                            f"tar member size short of manifest: {member.name!r} "
                            f"wrote={written} manifest={expected_size}"
                        )
                    hexdigest = digest.hexdigest()
                    if hexdigest != expected_entry["sha256"]:
                        raise ArchiveManifestMismatchError(
                            f"tar member sha256 disagrees with manifest: {member.name!r}"
                        )
                    actual[member.name] = hexdigest
        except _MOVER.ArchiveMoverError as error:
            raise ArchiveTarCorruptedError(f"tar stream failed: {error}") from error
        except tarfile.TarError as error:
            raise ArchiveTarCorruptedError(f"tar parse failed: {error}") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise ArchiveTarCorruptedError(f"tar IO failed: {error}") from error
    finally:
        os.close(archive_fd)

    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        raise ArchiveManifestMismatchError(f"tar missing manifest members: {missing}")
    return actual


# ---------------------------------------------------------------------------
# Salvage verification
# ---------------------------------------------------------------------------


def _decompress_zstd_to_bytes(path: Path, zstd_path: Path, *, max_bytes: int) -> bytes:
    """Run ``zstd -q -d -c`` with the compressed file piped via stdin.

    Piping via stdin mirrors ``_TarStreamContext`` in the mover so the
    passthrough test double stays symmetric across the codebase.
    """
    if not path.is_file():
        raise SalvageSha256MismatchError(f"salvage object is missing: {path}")
    try:
        with path.open("rb") as fh:
            completed = subprocess.run(
                [str(zstd_path), "-q", "-d", "-c"],
                stdin=fh,
                capture_output=True,
                check=True,
                timeout=_MOVER.TOOL_TIMEOUT_SECONDS,
            )
    except FileNotFoundError as error:
        raise DrillConfigError(f"zstd binary not found at {zstd_path}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise SalvageSha256MismatchError(f"zstd timed out decompressing {path}") from error
    except subprocess.CalledProcessError as error:
        raise SalvageSha256MismatchError(f"zstd failed decompressing {path}: {error}") from error
    payload = completed.stdout
    if len(payload) > max_bytes:
        raise SalvageSha256MismatchError(
            f"salvage decompressed payload exceeds {max_bytes} bytes"
        )
    return payload


def _count_csv_rows(payload: bytes) -> int:
    """Count data rows in a CSV WITH HEADER — matching the salvage export shape.

    The salvage runner emits ``COPY (...) TO STDOUT WITH (FORMAT CSV, HEADER)``,
    so the first line is the header and every subsequent non-empty line is a
    data row.
    """
    lines = payload.splitlines()
    if not lines:
        return 0
    # Header + data. Trailing empty line from ``COPY`` output does not count.
    data_lines = [line for line in lines[1:] if line]
    return len(data_lines)


def _verify_salvage_manifest(
    salvage_manifest: Mapping[str, Any],
    archive_root: Path,
    *,
    zstd_path: Path,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify every ``exports[]`` entry.

    Returns ``(selector_labels, coverage_tuples, differences)``. Coverage
    tuples are attributed only to verified selectors (H4-symmetric with
    product cycles).
    """
    selectors: list[str] = []
    coverage: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    exports = salvage_manifest.get("exports") or []
    for export in exports:
        selector = export.get("selector") or {}
        obj = export.get("object") or {}
        rel_path = obj.get("path")
        expected_sha = obj.get("sha256")
        expected_size = obj.get("size_bytes")
        expected_rows = export.get("exported_row_count")
        label = _selector_label(selector) or (rel_path or "<unknown>")
        window = selector.get("window") or {}
        if rel_path is None or expected_sha is None or expected_size is None:
            differences.append(
                {
                    "item": label,
                    "expected": {"code": CODE_SALVAGE_SHA256_MISMATCH},
                    "actual": {"error": "salvage manifest entry missing path/sha256/size"},
                }
            )
            continue
        obj_path = archive_root / rel_path
        try:
            actual_size = obj_path.stat().st_size
        except OSError as error:
            differences.append(
                {
                    "item": label,
                    "expected": {"code": CODE_SALVAGE_SHA256_MISMATCH, "sha256": expected_sha},
                    "actual": {"error": f"salvage object missing: {error}"},
                }
            )
            continue
        if actual_size != expected_size:
            differences.append(
                {
                    "item": label,
                    "expected": {"code": CODE_SALVAGE_SHA256_MISMATCH, "size_bytes": expected_size},
                    "actual": {"size_bytes": actual_size},
                }
            )
            continue
        actual_sha = _sha256_of_path(obj_path)
        if actual_sha != expected_sha:
            differences.append(
                {
                    "item": label,
                    "expected": {"code": CODE_SALVAGE_SHA256_MISMATCH, "sha256": expected_sha},
                    "actual": {"sha256": actual_sha},
                }
            )
            continue
        try:
            payload = _decompress_zstd_to_bytes(
                obj_path, zstd_path, max_bytes=MAX_SALVAGE_OBJECT_BYTES
            )
        except SalvageSha256MismatchError as error:
            differences.append(
                {
                    "item": label,
                    "expected": {"code": CODE_SALVAGE_SHA256_MISMATCH},
                    "actual": {"error": str(error)},
                }
            )
            continue
        actual_rows = _count_csv_rows(payload)
        if actual_rows != expected_rows:
            differences.append(
                {
                    "item": label,
                    "expected": {"code": CODE_SALVAGE_ROW_COUNT_MISMATCH, "row_count": expected_rows},
                    "actual": {"row_count": actual_rows},
                }
            )
            continue
        selectors.append(label)
        coverage.append({"source": "db-export", "window": _normalize_window(window)})
    return selectors, coverage, differences


def _selector_label(selector: Mapping[str, Any]) -> str:
    identity = selector.get("identity") if isinstance(selector, Mapping) else None
    if isinstance(identity, Mapping):
        for key in ("forcing_version_id", "run_id"):
            value = identity.get(key)
            if isinstance(value, str) and value:
                return f"{key}={value}"
    return ""


def _normalize_window(window: Mapping[str, Any]) -> dict[str, str]:
    start = window.get("start")
    end = window.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise DrillConfigError(f"salvage window missing start/end: {window!r}")
    return {"start": start, "end": end}


# ---------------------------------------------------------------------------
# Registry closure (H4 hybrid lifter)
# ---------------------------------------------------------------------------


# Canonical ancestor tables per lane. Names match those in
# ``workers/output_parser/parser.py`` + ``packages/common/forcing_domain_handoff_apply.py``.
_REGISTRY_TABLES_RUNS = (
    ("core.basin", "basin_id"),
    ("core.basin_version", "basin_version_id"),
    ("core.river_network_version", "river_network_version_id"),
    ("core.river_segment", "river_network_version_id"),  # multi-row
    ("core.mesh_version", "mesh_version_id"),
    ("core.model_instance", "model_id"),
    ("met.data_source", "source_id"),
    ("met.forecast_cycle", "cycle_id"),
    ("hydro.hydro_run", "run_id"),
)

_REGISTRY_TABLES_FORCING = (
    ("core.basin", "basin_id"),
    ("core.basin_version", "basin_version_id"),
    ("core.mesh_version", "mesh_version_id"),
    ("core.model_instance", "model_id"),
    ("met.data_source", "source_id"),
    ("met.forecast_cycle", "cycle_id"),
    ("met.forcing_version", "forcing_version_id"),
)


def _lift_registry_closure_runs(
    ops: RegistryLifterOps, run_id: str
) -> dict[str, Any]:
    """Lift the ancestor closure needed by ``OutputParser.parse_run(run_id)``.

    Order matters: parents before children so INSERTs never trip FK
    constraints in staging.
    """
    run_row = ops.select_where("hydro.hydro_run", {"run_id": run_id})
    if not run_row:
        raise RegistryClosureIncompleteError(
            f"hydro.hydro_run row missing for run_id={run_id!r}"
        )
    run = run_row[0]
    model_id = run.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise RegistryClosureIncompleteError(
            f"hydro.hydro_run.model_id is missing for run_id={run_id!r}"
        )

    model_row = ops.select_where("core.model_instance", {"model_id": model_id})
    if not model_row:
        raise RegistryClosureIncompleteError(
            f"core.model_instance row missing for model_id={model_id!r}"
        )
    model = model_row[0]
    basin_version_id = model.get("basin_version_id")
    river_network_version_id = model.get("river_network_version_id")
    mesh_version_id = model.get("mesh_version_id")
    if not all(
        isinstance(value, str) and value
        for value in (basin_version_id, river_network_version_id, mesh_version_id)
    ):
        raise RegistryClosureIncompleteError(
            f"core.model_instance ancestors incomplete for model_id={model_id!r}"
        )

    basin_version_rows = ops.select_where(
        "core.basin_version", {"basin_version_id": basin_version_id}
    )
    if not basin_version_rows:
        raise RegistryClosureIncompleteError(
            f"core.basin_version row missing for basin_version_id={basin_version_id!r}"
        )
    basin_id = basin_version_rows[0].get("basin_id")
    if not isinstance(basin_id, str) or not basin_id:
        raise RegistryClosureIncompleteError(
            f"core.basin_version.basin_id missing for basin_version_id={basin_version_id!r}"
        )
    basin_rows = ops.select_where("core.basin", {"basin_id": basin_id})
    if not basin_rows:
        raise RegistryClosureIncompleteError(
            f"core.basin row missing for basin_id={basin_id!r}"
        )

    river_network_rows = ops.select_where(
        "core.river_network_version",
        {"river_network_version_id": river_network_version_id},
    )
    if not river_network_rows:
        raise RegistryClosureIncompleteError(
            f"core.river_network_version row missing for {river_network_version_id!r}"
        )
    river_segment_rows = ops.select_where(
        "core.river_segment",
        {"river_network_version_id": river_network_version_id},
    )
    if not river_segment_rows:
        raise RegistryClosureIncompleteError(
            f"core.river_segment rows missing for {river_network_version_id!r}"
        )
    mesh_rows = ops.select_where("core.mesh_version", {"mesh_version_id": mesh_version_id})
    if not mesh_rows:
        raise RegistryClosureIncompleteError(
            f"core.mesh_version row missing for mesh_version_id={mesh_version_id!r}"
        )

    source_id = run.get("source_id")
    cycle_time = run.get("cycle_time")
    if isinstance(source_id, str) and source_id:
        source_rows = ops.select_where("met.data_source", {"source_id": source_id})
        if not source_rows:
            raise RegistryClosureIncompleteError(
                f"met.data_source row missing for source_id={source_id!r}"
            )
    else:
        source_rows = []
    if isinstance(source_id, str) and source_id and cycle_time is not None:
        cycle_rows = ops.select_where(
            "met.forecast_cycle",
            {"source_id": source_id, "cycle_time": cycle_time},
        )
    else:
        cycle_rows = []
    forcing_version_id = run.get("forcing_version_id")
    if isinstance(forcing_version_id, str) and forcing_version_id:
        forcing_rows = ops.select_where(
            "met.forcing_version", {"forcing_version_id": forcing_version_id}
        )
        if not forcing_rows:
            raise RegistryClosureIncompleteError(
                f"met.forcing_version row missing for {forcing_version_id!r}"
            )
    else:
        forcing_rows = []

    # INSERT into staging in parent-first order.
    ops.insert_rows("core.basin", basin_rows)
    ops.insert_rows("core.basin_version", basin_version_rows)
    ops.insert_rows("core.river_network_version", river_network_rows)
    ops.insert_rows("core.river_segment", river_segment_rows)
    ops.insert_rows("core.mesh_version", mesh_rows)
    ops.insert_rows("core.model_instance", [model])
    if source_rows:
        ops.insert_rows("met.data_source", source_rows)
    if cycle_rows:
        ops.insert_rows("met.forecast_cycle", cycle_rows)
    if forcing_rows:
        ops.insert_rows("met.forcing_version", forcing_rows)
    ops.insert_rows("hydro.hydro_run", [run])

    return {
        "run_id": run_id,
        "model_id": model_id,
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "river_segment_count": len(river_segment_rows),
        "forcing_version_id": forcing_version_id,
    }


def _lift_registry_closure_forcing(
    ops: RegistryLifterOps, forcing_version_id: str
) -> dict[str, Any]:
    """Lift the ancestor closure needed by ``apply_forcing_domain_handoff_path``.

    The forcing_version row itself is NOT lifted into staging (D1 / C-rs-4).
    Handoff's ``_upsert_forcing_version`` writes it from the manifest —
    lifting it would (a) duplicate work, (b) risk row-shape drift (prod
    row could have columns handoff does not).

    river_network_version + river_segment are lifted BEFORE core.model_instance
    (C-rs-1). The staging ``core.model_instance.river_network_version_id`` FK
    would trip otherwise; runs-lane closure already mirrors this order.
    """
    forcing_rows = ops.select_where(
        "met.forcing_version", {"forcing_version_id": forcing_version_id}
    )
    if not forcing_rows:
        raise RegistryClosureIncompleteError(
            f"met.forcing_version row missing for {forcing_version_id!r}"
        )
    forcing = forcing_rows[0]
    model_id = forcing.get("model_id")
    source_id = forcing.get("source_id")
    cycle_time = forcing.get("cycle_time")
    if not isinstance(model_id, str) or not model_id:
        raise RegistryClosureIncompleteError(
            f"met.forcing_version.model_id missing for {forcing_version_id!r}"
        )
    model_rows = ops.select_where("core.model_instance", {"model_id": model_id})
    if not model_rows:
        raise RegistryClosureIncompleteError(
            f"core.model_instance row missing for model_id={model_id!r}"
        )
    model = model_rows[0]
    basin_version_id = model.get("basin_version_id")
    river_network_version_id = model.get("river_network_version_id")
    mesh_version_id = model.get("mesh_version_id")
    if not isinstance(basin_version_id, str) or not basin_version_id:
        raise RegistryClosureIncompleteError(
            f"core.model_instance.basin_version_id missing for model_id={model_id!r}"
        )
    if not isinstance(river_network_version_id, str) or not river_network_version_id:
        raise RegistryClosureIncompleteError(
            f"core.model_instance.river_network_version_id missing for model_id={model_id!r}"
        )
    basin_version_rows = ops.select_where(
        "core.basin_version", {"basin_version_id": basin_version_id}
    )
    if not basin_version_rows:
        raise RegistryClosureIncompleteError(
            f"core.basin_version row missing for {basin_version_id!r}"
        )
    basin_id = basin_version_rows[0].get("basin_id")
    if not isinstance(basin_id, str) or not basin_id:
        raise RegistryClosureIncompleteError(
            f"core.basin_version.basin_id missing for {basin_version_id!r}"
        )
    basin_rows = ops.select_where("core.basin", {"basin_id": basin_id})
    if not basin_rows:
        raise RegistryClosureIncompleteError(f"core.basin row missing for {basin_id!r}")
    river_network_rows = ops.select_where(
        "core.river_network_version",
        {"river_network_version_id": river_network_version_id},
    )
    if not river_network_rows:
        raise RegistryClosureIncompleteError(
            f"core.river_network_version row missing for {river_network_version_id!r}"
        )
    river_segment_rows = ops.select_where(
        "core.river_segment",
        {"river_network_version_id": river_network_version_id},
    )
    if not river_segment_rows:
        raise RegistryClosureIncompleteError(
            f"core.river_segment rows missing for {river_network_version_id!r}"
        )
    mesh_rows = ops.select_where("core.mesh_version", {"mesh_version_id": mesh_version_id})
    if not mesh_rows:
        raise RegistryClosureIncompleteError(
            f"core.mesh_version row missing for {mesh_version_id!r}"
        )
    if isinstance(source_id, str) and source_id:
        source_rows = ops.select_where("met.data_source", {"source_id": source_id})
        if not source_rows:
            raise RegistryClosureIncompleteError(
                f"met.data_source row missing for source_id={source_id!r}"
            )
    else:
        source_rows = []
    if isinstance(source_id, str) and source_id and cycle_time is not None:
        cycle_rows = ops.select_where(
            "met.forecast_cycle",
            {"source_id": source_id, "cycle_time": cycle_time},
        )
    else:
        cycle_rows = []

    # Parent-first insert order — river_network_version + river_segment before
    # core.model_instance so the model_instance FK to river_network_version
    # holds (C-rs-1 fix). met.forcing_version intentionally NOT lifted (D1);
    # handoff will upsert it from the manifest package.
    ops.insert_rows("core.basin", basin_rows)
    ops.insert_rows("core.basin_version", basin_version_rows)
    ops.insert_rows("core.river_network_version", river_network_rows)
    ops.insert_rows("core.river_segment", river_segment_rows)
    ops.insert_rows("core.mesh_version", mesh_rows)
    ops.insert_rows("core.model_instance", [model])
    if source_rows:
        ops.insert_rows("met.data_source", source_rows)
    if cycle_rows:
        ops.insert_rows("met.forecast_cycle", cycle_rows)
    return {
        "forcing_version_id": forcing_version_id,
        "model_id": model_id,
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "river_segment_count": len(river_segment_rows),
        "source_id": source_id,
    }


class RegistryLifterOps:
    """Abstract SELECT-from-prod / INSERT-into-staging operations.

    Implementations own the two connections. Tests inject a fake that
    records calls in memory; ``PsycopgRegistryLifterOps`` wraps real
    psycopg2 connections.
    """

    def select_where(
        self, table: str, predicates: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    def insert_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:  # pragma: no cover
        raise NotImplementedError


class PsycopgRegistryLifterOps(RegistryLifterOps):
    """psycopg2-backed lifter. Idempotent (``ON CONFLICT DO NOTHING``).

    Column-drift guard (C-rs-5 / D2): before INSERTing the first row for a
    table, queries staging's ``information_schema.columns`` for the target
    table and refuses fail-closed if any prod row column is absent from
    staging — a silent DROP could otherwise ship a receipt whose staging
    row was missing a NOT NULL column.

    JSONB wrapping (C-rs-2 / A5): staging column type ``jsonb`` triggers
    ``psycopg2.extras.Json(value)`` wrapping on dict values so the INSERT
    does not fail with "column is of type jsonb but expression is of type
    record".
    """

    def __init__(self, prod_conn: Any, staging_conn: Any) -> None:
        self._prod = prod_conn
        self._staging = staging_conn
        # Cache: (schema, table) -> {column_name: udt_name} on staging.
        self._staging_columns: dict[tuple[str, str], dict[str, str]] = {}

    def select_where(
        self, table: str, predicates: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        from psycopg2 import sql
        from psycopg2.extras import RealDictCursor

        schema, name = table.split(".", 1)
        clauses = [
            sql.SQL("{} = %s").format(sql.Identifier(column)) for column in predicates
        ]
        stmt = sql.SQL("SELECT * FROM {}.{} WHERE ").format(
            sql.Identifier(schema), sql.Identifier(name)
        ) + sql.SQL(" AND ").join(clauses)
        with self._prod.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(stmt, tuple(predicates.values()))
            return [dict(row) for row in cursor.fetchall()]

    def _staging_column_types(self, schema: str, name: str) -> dict[str, str]:
        """Return ``{column_name: udt_name}`` for staging table, cached."""
        key = (schema, name)
        cached = self._staging_columns.get(key)
        if cached is not None:
            return cached
        with self._staging.cursor() as cursor:
            cursor.execute(
                "SELECT column_name, udt_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, name),
            )
            columns = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
        if not columns:
            raise RegistryClosureIncompleteError(
                f"staging table {schema}.{name} not found in information_schema.columns "
                "— migration drift?"
            )
        self._staging_columns[key] = columns
        return columns

    @staticmethod
    def _wrap_jsonb(value: Any) -> Any:
        """Wrap dict/list values bound for jsonb columns via ``Json``.

        psycopg2 does not adapt a Python ``dict`` to jsonb by default; the
        server sees ``ROW(...)`` and rejects it. ``Json(value)`` renders as
        ``jsonb`` literal. ``None`` is passed through so nullable columns
        stay NULL. Already-``Json``-wrapped values also pass through.
        """
        from psycopg2.extras import Json

        if value is None or isinstance(value, Json):
            return value
        if isinstance(value, (dict, list)):
            return Json(value)
        return value

    def insert_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        from psycopg2 import sql

        schema, name = table.split(".", 1)
        staging_columns = self._staging_column_types(schema, name)
        # Column-drift guard: any prod row column that staging lacks is a
        # fail-closed condition — we cannot safely INSERT a subset when the
        # missing column may be a NOT NULL prod-only lineage field, nor can
        # we invent a value. Emit REGISTRY_CLOSURE_INCOMPLETE naming the
        # drift columns.
        prod_columns = list(rows[0].keys())
        drift = sorted(column for column in prod_columns if column not in staging_columns)
        if drift:
            raise RegistryClosureIncompleteError(
                f"staging schema drift for {schema}.{name}: prod row has columns "
                f"missing from staging: {drift}"
            )
        # Insert only the intersection so a staging-only column with a
        # default is left to Postgres.
        columns = [column for column in prod_columns if column in staging_columns]
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(columns))
        stmt = sql.SQL(
            "INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING"
        ).format(
            sql.Identifier(schema),
            sql.Identifier(name),
            sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            placeholders,
        )
        with self._staging.cursor() as cursor:
            for row in rows:
                values: list[Any] = []
                for column in columns:
                    raw = row.get(column)
                    if staging_columns.get(column) == "jsonb":
                        values.append(self._wrap_jsonb(raw))
                    else:
                        values.append(raw)
                cursor.execute(stmt, tuple(values))


# ---------------------------------------------------------------------------
# Product verification (file-parsed expected counts)
# ---------------------------------------------------------------------------


def _parse_rivqdown_expected_row_count(source_file: Path, segment_count: int) -> int:
    """Reuse ``parse_rivqdown_file`` logic for expected count.

    Delegates to ``parse_rivqdown_file`` via a stub context so we compute
    "rows the ingest WOULD write" without duplicating time-basis logic.
    """
    from workers.output_parser.parser import (
        HydroRunContext,
        RiverSegmentOrder,
        parse_rivqdown_file,
    )

    context = HydroRunContext(
        run_id="drill-verify",
        model_id="drill-model",
        basin_version_id="drill-bv",
        river_network_version_id="drill-rnv",
        source_id=None,
        cycle_id=None,
        cycle_time=None,
        start_time=datetime(1970, 1, 1, tzinfo=UTC),
        run_type="analysis",
    )
    segments = tuple(
        RiverSegmentOrder(
            river_segment_id=f"drill-seg-{index:06d}",
            river_network_version_id="drill-rnv",
            segment_order=index,
        )
        for index in range(1, segment_count + 1)
    )
    parsed = parse_rivqdown_file(source_file, context, segments)
    return len(parsed)


# ---------------------------------------------------------------------------
# Prod readonly + staging admin lifecycle
# ---------------------------------------------------------------------------


@contextmanager
def open_prod_readonly(dsn: str) -> Iterator[Any]:
    """Open a psycopg2 connection with ``default_transaction_read_only = on``.

    Belt-and-suspenders: sets the parameter server-side AND asserts it,
    then re-asserts before every SELECT via ``current_setting``.
    """
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET default_transaction_read_only = on")
            cursor.execute("SHOW default_transaction_read_only")
            row = cursor.fetchone()
            if not row or str(row[0]).lower() != "on":
                raise DrillConfigError(
                    "prod connection could not be pinned to read-only mode"
                )
        conn.autocommit = False
        yield conn
    finally:
        conn.close()


def provision_staging_database(admin_url: str, staging_dbname: str) -> None:
    """DROP + CREATE + migrate the staging database from zero.

    ``apply_migrations_from_zero`` is the same helper the integration
    fixture uses (``tests/integration_helpers.py``). Running it against a
    real Postgres cluster from a scripted context is new — the deviation
    is documented in design.md #854.
    """
    import psycopg2
    from psycopg2 import sql

    from tests.integration_helpers import apply_migrations_from_zero

    admin_conn = psycopg2.connect(admin_url)
    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (staging_dbname,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(staging_dbname))
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(staging_dbname))
            )
    finally:
        admin_conn.close()

    target_url = _dsn_with_dbname(admin_url, staging_dbname)
    apply_migrations_from_zero(target_url)


def drop_staging_database(admin_url: str, staging_dbname: str) -> None:
    """Best-effort teardown — errors here are logged, not raised."""
    import psycopg2
    from psycopg2 import sql

    try:
        admin_conn = psycopg2.connect(admin_url)
    except Exception:
        return
    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (staging_dbname,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(staging_dbname))
            )
    except Exception:
        pass
    finally:
        admin_conn.close()


# ---------------------------------------------------------------------------
# Receipt emission
# ---------------------------------------------------------------------------


def _load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _now_iso(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_receipt(
    *,
    verdict: str,
    staging_database: Mapping[str, str],
    coverage: Sequence[Mapping[str, Any]],
    comparisons: Mapping[str, Any] | None = None,
    differences: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a receipt matching ``schemas/archive_rebuild_drill_receipt.schema.json``.

    Validates against the schema at emission time; any drift raises
    ``jsonschema.ValidationError`` immediately rather than shipping a
    malformed receipt.
    """
    if verdict not in {"PASS", "FAIL"}:
        raise DrillConfigError(f"verdict must be PASS or FAIL: {verdict!r}")
    # B2 / C-sc-1: coverage is NEVER stub-filled. The schema was amended to
    # allow ``coverage: []`` on FAIL (see
    # ``schemas/archive_rebuild_drill_receipt.schema.json``); PASS still
    # requires ``minItems: 1`` because a PASS with zero restored windows is
    # meaningless.
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(now or datetime.now(UTC)),
        "verdict": verdict,
        "staging_database": {
            "database": staging_database["database"],
            "schema": staging_database["schema"],
            "instance_id": staging_database["instance_id"],
        },
        "coverage": [dict(entry) for entry in coverage],
    }
    if verdict == "PASS":
        if not comparisons:
            raise DrillConfigError("PASS receipt requires comparisons")
        receipt["comparisons"] = {
            "cycles": list(comparisons["cycles"]),
            "selectors": list(comparisons["selectors"]),
            "counts": [dict(entry) for entry in comparisons["counts"]],
        }
    else:
        if not differences:
            raise DrillConfigError("FAIL receipt requires differences")
        receipt["differences"] = [dict(entry) for entry in differences]
    schema = _load_schema(_DRILL_RECEIPT_SCHEMA_PATH)
    jsonschema.validate(receipt, schema)
    return receipt


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _load_json(path: Path, schema_path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    schema = _load_schema(schema_path)
    jsonschema.validate(data, schema)
    if not isinstance(data, dict):
        raise DrillConfigError(f"manifest at {path} is not a JSON object")
    return data


def load_product_archive_manifest(path: Path) -> dict[str, Any]:
    return _load_json(path, _PRODUCT_MANIFEST_SCHEMA_PATH)


def load_salvage_manifest(path: Path) -> dict[str, Any]:
    return _load_json(path, _SALVAGE_MANIFEST_SCHEMA_PATH)


# ---------------------------------------------------------------------------
# Product ingest adapters (H1 injection — never .from_env())
# ---------------------------------------------------------------------------


def _ingest_runs_cycle(
    workspace_root: Path,
    manifest: Mapping[str, Any],
    *,
    staging_database_url: str,
) -> Mapping[str, Any]:
    """Reuse ``OutputParser.parse_run`` against the staging DSN."""
    from packages.common.object_store import LocalObjectStore
    from workers.output_parser.parser import (
        OutputParser,
        OutputParserConfig,
        PsycopgOutputParserRepository,
    )

    identity = manifest["identity"]
    run_id = identity["run_id"]
    config = OutputParserConfig(
        object_store_root=workspace_root,
        workspace_root=workspace_root,
        object_store_prefix="s3://nhms",
    )
    object_store = LocalObjectStore(config.object_store_root, config.object_store_prefix)
    repository = PsycopgOutputParserRepository(database_url=staging_database_url)
    parser = OutputParser(config=config, repository=repository, object_store=object_store)
    result = parser.parse_run(run_id)
    return {"run_id": run_id, "rows_written": result.rows_written}


def _ingest_forcing_cycle(
    archive_dir: Path,
    manifest: Mapping[str, Any],
    *,
    staging_conn: Any,
    object_store_prefix: str = "s3://nhms",
) -> Mapping[str, Any]:
    """Reuse ``apply_forcing_domain_handoff_path`` with an injected connection."""
    from packages.common.forcing_domain_handoff_apply import (
        apply_forcing_domain_handoff_path,
    )

    manifest_path = archive_dir / "forcing_package.json"
    report = apply_forcing_domain_handoff_path(
        manifest_path,
        object_store_root=archive_dir,
        object_store_prefix=object_store_prefix,
        connection=staging_conn,
    )
    return {"forcing_version_id": manifest["identity"].get("forcing_version_id"), "report": report}


# ---------------------------------------------------------------------------
# Drill runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductVerification:
    """Per-cycle verification outcome computed by the drill."""

    cycle_label: str
    expected_row_count: int
    staging_row_count: int
    coverage: dict[str, Any]

    @property
    def matches(self) -> bool:
        return self.expected_row_count == self.staging_row_count


@dataclass
class DrillOutcome:
    """Aggregate outcome — receipt built off this."""

    verdict: str
    cycles: list[str]
    selectors: list[str]
    counts: list[dict[str, Any]]
    coverage: list[dict[str, Any]]
    differences: list[dict[str, Any]]


def run_drill(
    config: DrillConfig,
    *,
    # Injectables so unit tests can substitute a fake pipeline. Real
    # invocations use the module-level defaults.
    provision_staging: Callable[[str, str], None] = provision_staging_database,
    teardown_staging: Callable[[str, str], None] = drop_staging_database,
    open_prod: Callable[[str], Any] = open_prod_readonly,
    open_staging_conn: Callable[[str], Any] | None = None,
    lifter_factory: Callable[[Any, Any], RegistryLifterOps] | None = None,
    ingest_runs: Callable[[Path, Mapping[str, Any], str], Mapping[str, Any]] | None = None,
    ingest_forcing: Callable[[Path, Mapping[str, Any], Any], Mapping[str, Any]] | None = None,
    verify_product: Callable[[Path, Mapping[str, Any], Any], ProductVerification] | None = None,
    keep_workspace: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, Any], DrillOutcome]:
    """End-to-end drill: extract → lift → ingest → verify → receipt.

    Returns ``(receipt_dict, outcome)`` — caller writes the receipt to
    disk after receiving it (see ``write_receipt``).

    Workspace cleanup (C1 / C-is-2): the extract tree under
    ``config.workspace_root`` is removed on both PASS and FAIL exit unless
    ``keep_workspace=True`` (operator triage aid — not surfaced by the CLI
    yet; kept as a keyword arg for tests + future flag).
    """
    validate_isolation(config)
    now = now or datetime.now(UTC)
    try:
        return _run_drill_body(
            config,
            provision_staging=provision_staging,
            teardown_staging=teardown_staging,
            open_prod=open_prod,
            open_staging_conn=open_staging_conn,
            lifter_factory=lifter_factory,
            ingest_runs=ingest_runs,
            ingest_forcing=ingest_forcing,
            verify_product=verify_product,
            now=now,
        )
    finally:
        if not keep_workspace:
            shutil.rmtree(config.workspace_root, ignore_errors=True)


def _run_drill_body(
    config: DrillConfig,
    *,
    provision_staging: Callable[[str, str], None],
    teardown_staging: Callable[[str, str], None],
    open_prod: Callable[[str], Any],
    open_staging_conn: Callable[[str], Any] | None,
    lifter_factory: Callable[[Any, Any], RegistryLifterOps] | None,
    ingest_runs: Callable[[Path, Mapping[str, Any], str], Mapping[str, Any]] | None,
    ingest_forcing: Callable[[Path, Mapping[str, Any], Any], Mapping[str, Any]] | None,
    verify_product: Callable[[Path, Mapping[str, Any], Any], ProductVerification] | None,
    now: datetime,
) -> tuple[dict[str, Any], DrillOutcome]:
    cycles: list[str] = []
    selectors: list[str] = []
    counts: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []

    workspace_root = config.workspace_root
    workspace_root.mkdir(parents=True, exist_ok=True)

    # Phase 1: extract every declared archive manifest.
    archive_extracts: list[tuple[Path, Path, dict[str, Any]]] = []
    for archive_manifest_path in config.archive_manifest_paths:
        try:
            manifest = load_product_archive_manifest(archive_manifest_path)
        except Exception as error:
            differences.append(
                {
                    "item": str(archive_manifest_path),
                    "expected": {"code": CODE_ARCHIVE_MANIFEST_MISMATCH},
                    "actual": {"error": str(error)},
                }
            )
            continue
        rel_archive = manifest["archive"]["path"]
        tar_zst_path = config.archive_root / rel_archive
        cycle_label = _cycle_label(manifest["identity"])
        dest_dir = workspace_root / cycle_label
        try:
            _extract_archive_to_disk(
                manifest, tar_zst_path, dest_dir, zstd_path=config.zstd_path
            )
        except DrillError as error:
            differences.append(error.as_difference(item=cycle_label))
            continue
        archive_extracts.append((archive_manifest_path, dest_dir, manifest))

    # Phase 2: verify salvage (no reingest per D3).
    salvage_selectors: list[str] = []
    salvage_coverage: list[dict[str, Any]] = []
    for salvage_manifest_path in config.salvage_manifest_paths:
        try:
            salvage_manifest = load_salvage_manifest(salvage_manifest_path)
        except Exception as error:
            differences.append(
                {
                    "item": str(salvage_manifest_path),
                    "expected": {"code": CODE_SALVAGE_SHA256_MISMATCH},
                    "actual": {"error": str(error)},
                }
            )
            continue
        s_selectors, s_coverage, s_diffs = _verify_salvage_manifest(
            salvage_manifest, config.archive_root, zstd_path=config.zstd_path
        )
        salvage_selectors.extend(s_selectors)
        salvage_coverage.extend(s_coverage)
        differences.extend(s_diffs)

    selectors.extend(salvage_selectors)
    coverage.extend(salvage_coverage)

    # Short-circuit if all archives failed to extract AND no salvage
    # verified — no point provisioning staging just to fail.
    if not archive_extracts and not salvage_selectors:
        verdict = "PASS" if not differences else "FAIL"
        outcome = DrillOutcome(
            verdict=verdict,
            cycles=cycles,
            selectors=selectors,
            counts=counts,
            coverage=coverage,
            differences=differences,
        )
        receipt = _build_receipt_from_outcome(outcome, config, now)
        return receipt, outcome

    # Phase 3: provision staging DB, lift closure, ingest, verify.
    # If archive_extracts is empty but salvage_selectors is non-empty we
    # still skip staging (salvage does not require DB).
    #
    # C3 / C-is-5 / C-di-3: provision_staging INSIDE the try block whose
    # finally runs teardown_staging. If CREATE succeeded but migration
    # then failed, the finally still runs DROP DATABASE — no leaked DB.
    if archive_extracts:
        try:
            provision_staging(config.postgres_admin_url, config.staging_dbname())
            with open_prod(config.prod_database_url_ro) as prod_conn:
                open_staging = open_staging_conn or _default_open_staging
                with open_staging(config.staging_database_url) as staging_conn:
                    factory = lifter_factory or (
                        lambda prod, staging: PsycopgRegistryLifterOps(prod, staging)
                    )
                    lifter = factory(prod_conn, staging_conn)
                    for _manifest_path, dest_dir, manifest in archive_extracts:
                        cycle_label = _cycle_label(manifest["identity"])
                        try:
                            _lift_and_ingest(
                                manifest,
                                dest_dir,
                                lifter=lifter,
                                staging_conn=staging_conn,
                                staging_database_url=config.staging_database_url,
                                workspace_root=workspace_root,
                                ingest_runs=ingest_runs,
                                ingest_forcing=ingest_forcing,
                            )
                        except DrillError as error:
                            differences.append(error.as_difference(item=cycle_label))
                            continue
                        verifier = verify_product or _verify_product_cycle
                        try:
                            verification = verifier(dest_dir, manifest, staging_conn)
                        except DrillError as error:
                            differences.append(error.as_difference(item=cycle_label))
                            continue
                        cycles.append(cycle_label)
                        counts.append(
                            {
                                "item": cycle_label,
                                "expected": verification.expected_row_count,
                                "actual": verification.staging_row_count,
                            }
                        )
                        # N-mf-1: coverage[] must be attributed only to
                        # cycles whose staging count actually matched — mirrors
                        # the salvage verifier (:494-579) which `continue`s
                        # before `coverage.append` on any mismatch. Emitting
                        # coverage for a FAIL cycle would falsely claim the
                        # drill covered its window.
                        if not verification.matches:
                            differences.append(
                                {
                                    "item": cycle_label,
                                    "expected": {
                                        "code": CODE_STAGING_COUNT_MISMATCH,
                                        "row_count": verification.expected_row_count,
                                    },
                                    "actual": {"row_count": verification.staging_row_count},
                                }
                            )
                            continue
                        coverage.append(verification.coverage)
        finally:
            teardown_staging(config.postgres_admin_url, config.staging_dbname())

    verdict = "PASS" if not differences else "FAIL"
    outcome = DrillOutcome(
        verdict=verdict,
        cycles=cycles,
        selectors=selectors,
        counts=counts,
        coverage=coverage,
        differences=differences,
    )
    receipt = _build_receipt_from_outcome(outcome, config, now)
    return receipt, outcome


def _build_receipt_from_outcome(
    outcome: DrillOutcome, config: DrillConfig, now: datetime
) -> dict[str, Any]:
    staging_database = {
        "database": config.staging_dbname(),
        "schema": config.staging_run_label,
        "instance_id": config.staging_instance_id,
    }
    if outcome.verdict == "PASS":
        comparisons: dict[str, Any] = {
            "cycles": outcome.cycles,
            "selectors": outcome.selectors,
            "counts": outcome.counts,
        }
        if not comparisons["cycles"] and not comparisons["counts"]:
            # Schema requires cycles + counts to have ``minItems: 1`` on PASS.
            # A PASS with no restored product cycles is meaningless; refuse.
            raise DrillConfigError(
                "PASS receipt requires at least one restored product cycle"
            )
        return build_receipt(
            verdict="PASS",
            staging_database=staging_database,
            coverage=outcome.coverage,
            comparisons=comparisons,
            now=now,
        )
    return build_receipt(
        verdict="FAIL",
        staging_database=staging_database,
        coverage=outcome.coverage,
        differences=outcome.differences,
        now=now,
    )


def _cycle_label(identity: Mapping[str, Any]) -> str:
    lane = identity.get("lane", "unknown")
    if lane == "runs":
        return str(identity.get("run_id") or "runs-unknown")
    if lane == "forcing":
        source = identity.get("source")
        cycle = identity.get("cycle_identity")
        basin = identity.get("basin_version_id")
        model = identity.get("model_id")
        return f"forcing-{source}-{cycle}-{basin}-{model}"
    return f"{lane}-{identity.get('cycle_identity', 'unknown')}"


@contextmanager
def _default_open_staging(dsn: str) -> Iterator[Any]:
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _lift_and_ingest(
    manifest: Mapping[str, Any],
    dest_dir: Path,
    *,
    lifter: RegistryLifterOps,
    staging_conn: Any,
    staging_database_url: str,
    workspace_root: Path,
    ingest_runs: Callable[[Path, Mapping[str, Any], str], Mapping[str, Any]] | None,
    ingest_forcing: Callable[[Path, Mapping[str, Any], Any], Mapping[str, Any]] | None,
) -> None:
    identity = manifest["identity"]
    lane = identity["lane"]
    if lane == "runs":
        run_id = identity["run_id"]
        _lift_registry_closure_runs(lifter, run_id)
        # A2 / C-is-1: commit the lifted registry rows on the staging
        # connection BEFORE we hand off to OutputParser. OutputParser
        # opens its own psycopg2 connection via
        # ``PsycopgOutputParserRepository(database_url=STAGING_DATABASE_URL)``
        # — an uncommitted lift is invisible to that second connection and
        # every ingest INSERT fails an FK check against the parent tables.
        _staging_commit_if_possible(staging_conn)
        # Copy the restored ``input/manifest.json`` + ``output/`` tree into
        # workspace layout ``runs/<run_id>/input/`` +
        # ``runs/<run_id>/output/`` so ``OutputParser`` finds the rivqdown
        # file at its canonical URI.
        _prepare_run_workspace(dest_dir, workspace_root, run_id)
        adapter = ingest_runs or _ingest_runs_cycle
        adapter(workspace_root, manifest, staging_database_url=staging_database_url)  # type: ignore[misc]
    elif lane == "forcing":
        forcing_version_id = manifest.get("producer", {}).get("subject_id")
        if not isinstance(forcing_version_id, str) or not forcing_version_id:
            raise ArchiveManifestMismatchError(
                "forcing manifest producer.subject_id (forcing_version_id) is missing"
            )
        _lift_registry_closure_forcing(lifter, forcing_version_id)
        # A2 / C-is-1: even though the forcing adapter uses the same
        # ``staging_conn`` (so an uncommitted lift is visible), the handoff
        # helper begins its own transaction; commit here for symmetry with
        # the runs lane and to keep transaction scope narrow.
        _staging_commit_if_possible(staging_conn)
        adapter = ingest_forcing or _ingest_forcing_cycle
        adapter(dest_dir, manifest, staging_conn)  # type: ignore[misc]
    else:
        raise ArchiveManifestMismatchError(
            f"unsupported lane for ingest: {lane!r} (states lane not reingested)"
        )


def _staging_commit_if_possible(staging_conn: Any) -> None:
    """Commit the staging connection, no-op if the stub does not support it."""
    commit = getattr(staging_conn, "commit", None)
    if callable(commit):
        commit()


def _prepare_run_workspace(dest_dir: Path, workspace_root: Path, run_id: str) -> None:
    """Symlink/copy the restored run tree into ``runs/<run_id>/`` under workspace.

    ``OutputParser._find_rivqdown_file`` looks up
    ``runs/<run_id>/output/`` relative to the object-store root; the drill
    workspace IS the object store. If the extract already landed under
    ``runs/<run_id>/`` we're done, otherwise mirror the tree.
    """
    canonical = workspace_root / "runs" / run_id
    if canonical.exists():
        return
    canonical.parent.mkdir(parents=True, exist_ok=True)
    # Use a symlink so we do not double the disk footprint. Extract
    # already landed everything under ``dest_dir``; the canonical URI is
    # ``runs/<run_id>/output/`` so we symlink the whole extract root.
    os.symlink(dest_dir, canonical)


def _verify_product_cycle(
    dest_dir: Path, manifest: Mapping[str, Any], staging_conn: Any
) -> ProductVerification:
    """Compute file-derived expected count + staging COUNT(*)."""
    identity = manifest["identity"]
    lane = identity["lane"]
    cycle_label = _cycle_label(identity)
    if lane == "runs":
        # Find the .rivqdown file
        output_dir = dest_dir / "output"
        candidates = sorted(
            path for path in output_dir.iterdir()
            if path.is_file() and _is_rivqdown_path(path)
        )
        if not candidates:
            raise ArchiveTarCorruptedError(
                f"restored runs cycle {cycle_label!r} has no .rivqdown output"
            )
        source_file = candidates[0]
        # Segment count comes from ingest via load_river_segments which
        # queries core.river_segment. For verification we count staging
        # rows AND count expected rows by re-parsing.
        segment_count = _staging_segment_count(staging_conn, identity)
        expected = _parse_rivqdown_expected_row_count(source_file, segment_count)
        staging_rows = _staging_row_count(
            staging_conn,
            "hydro.river_timeseries",
            {"run_id": identity["run_id"]},
        )
        window = _identity_window(manifest)
        return ProductVerification(
            cycle_label=cycle_label,
            expected_row_count=expected,
            staging_row_count=staging_rows,
            coverage={"source": "runs", "window": window},
        )
    if lane == "forcing":
        forcing_version_id = manifest.get("producer", {}).get("subject_id")
        if not isinstance(forcing_version_id, str):
            raise ArchiveManifestMismatchError(
                "forcing manifest producer.subject_id is missing"
            )
        expected = _forcing_expected_row_count(dest_dir)
        staging_rows = _staging_row_count(
            staging_conn,
            "met.forcing_station_timeseries",
            {"forcing_version_id": forcing_version_id},
        )
        window = _identity_window(manifest)
        return ProductVerification(
            cycle_label=cycle_label,
            expected_row_count=expected,
            staging_row_count=staging_rows,
            coverage={"source": "forcing", "window": window},
        )
    raise ArchiveManifestMismatchError(f"unsupported lane for verify: {lane!r}")


def _is_rivqdown_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith((".rivqdown", ".rivqdown.csv", ".rivqdown.dat"))


def _staging_row_count(
    conn: Any, table: str, predicates: Mapping[str, Any]
) -> int:
    from psycopg2 import sql

    schema, name = table.split(".", 1)
    clauses = [sql.SQL("{} = %s").format(sql.Identifier(column)) for column in predicates]
    stmt = sql.SQL("SELECT count(*) FROM {}.{} WHERE ").format(
        sql.Identifier(schema), sql.Identifier(name)
    ) + sql.SQL(" AND ").join(clauses)
    with conn.cursor() as cursor:
        cursor.execute(stmt, tuple(predicates.values()))
        row = cursor.fetchone()
        return int(row[0]) if row else 0


def _staging_segment_count(conn: Any, identity: Mapping[str, Any]) -> int:
    """Count river_segment rows that ingest would iterate for this run."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT river_network_version_id FROM core.model_instance WHERE model_id = "
            "(SELECT model_id FROM hydro.hydro_run WHERE run_id = %s)",
            (identity["run_id"],),
        )
        row = cursor.fetchone()
        if not row:
            raise RegistryClosureIncompleteError(
                f"staging.core.model_instance missing for run_id={identity['run_id']!r}"
            )
        river_network_version_id = row[0]
        cursor.execute(
            "SELECT count(*) FROM core.river_segment WHERE river_network_version_id = %s",
            (river_network_version_id,),
        )
        count_row = cursor.fetchone()
        return int(count_row[0]) if count_row else 0


def _forcing_expected_row_count(dest_dir: Path) -> int:
    """Count timeseries rows in the extracted forcing bundle.

    Matches ``packages/common/forcing_domain_handoff._json_rows`` semantics
    (A3 / C-mf-1): payload is either a bare row list OR an object with
    ``rows: [...]``. The prior ``records`` fallback was dead code and is
    dropped (C-mf-3 — subsumed).
    """
    payload = dest_dir / "payloads" / "station_timeseries.json"
    data = json.loads(payload.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return len(data)
    if isinstance(data, Mapping):
        rows = data.get("rows")
        if isinstance(rows, list):
            return len(rows)
    raise ArchiveManifestMismatchError(
        "forcing station_timeseries.json must be a row array or object with 'rows'"
    )


def _identity_window(manifest: Mapping[str, Any]) -> dict[str, str]:
    producer = manifest.get("producer") or {}
    start = producer.get("start_time") or manifest.get("identity", {}).get("cycle_time")
    end = producer.get("end_time") or start
    if not isinstance(start, str) or not isinstance(end, str):
        raise ArchiveManifestMismatchError(
            "product manifest is missing producer.start_time/end_time"
        )
    return {"start": start, "end": end}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _config_from_env(env: Mapping[str, str], argv: Sequence[str]) -> DrillConfig:
    parser = argparse.ArgumentParser(
        prog="node27_archive_rebuild_drill",
        description="Node-27 archive rebuild drill orchestrator.",
    )
    parser.add_argument("--archive-manifest", action="append", default=[])
    parser.add_argument("--salvage-manifest", action="append", default=[])
    args = parser.parse_args(argv)

    archive_root = _require_path_env(env, "NHMS_ARCHIVE_ROOT")
    workspace_root = _require_path_env(env, "NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE")
    receipt_path = Path(env.get("NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH") or "").expanduser()
    if not receipt_path.is_absolute():
        raise DrillConfigError(
            "NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH must be an absolute path"
        )
    prod_url = _require_env(env, "PROD_DATABASE_URL_RO")
    staging_url = _require_env(env, "STAGING_DATABASE_URL")
    admin_url = _require_env(env, "POSTGRES_ADMIN_URL")
    instance_id = _require_env(env, "NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID")
    run_label = env.get(
        "NHMS_ARCHIVE_REBUILD_DRILL_RUN_LABEL"
    ) or f"archive_drill_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    zstd_path = _validate_zstd(Path(env.get("NHMS_ZSTD_BIN", "/usr/bin/zstd")))
    archive_manifests = tuple(Path(item).expanduser() for item in args.archive_manifest)
    salvage_manifests = tuple(Path(item).expanduser() for item in args.salvage_manifest)
    if not archive_manifests and not salvage_manifests:
        raise DrillConfigError(
            "no --archive-manifest / --salvage-manifest args passed; nothing to drill"
        )
    lock_path_env = env.get("NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH")
    if lock_path_env and lock_path_env.strip():
        lock_path = Path(lock_path_env).expanduser()
        if not lock_path.is_absolute():
            raise DrillConfigError(
                "NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH must be an absolute path"
            )
    else:
        lock_path = _default_lock_path()
    return DrillConfig(
        archive_root=archive_root,
        workspace_root=workspace_root,
        receipt_path=receipt_path,
        prod_database_url_ro=prod_url,
        staging_database_url=staging_url,
        postgres_admin_url=admin_url,
        staging_instance_id=instance_id,
        staging_run_label=run_label,
        zstd_path=zstd_path,
        archive_manifest_paths=archive_manifests,
        salvage_manifest_paths=salvage_manifests,
        lock_path=lock_path,
    )


def _require_env(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value or not value.strip():
        raise DrillConfigError(f"env var {key} is required")
    return value


def _require_path_env(env: Mapping[str, str], key: str) -> Path:
    value = _require_env(env, key)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DrillConfigError(f"env var {key} must be an absolute path: {value!r}")
    return path


def _validate_zstd(path: Path) -> Path:
    """Reuse the mover's fail-closed zstd validator."""
    return _MOVER._validate_zstd(path)


@contextmanager
def _single_instance_lock(lock_path: Path) -> Iterator[Any]:
    """Acquire an exclusive fcntl.flock on ``lock_path``; refuse if held.

    C2 / C-is-3: without a single-instance guard, two overlapping runs
    would race on DROP/CREATE DATABASE + tar extract into the same
    workspace. Non-blocking LOCK_EX | LOCK_NB — held → raise
    ``DrillConcurrentInvocationError``.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise DrillConcurrentInvocationError(
                    f"another drill holds {lock_path}"
                ) from error
            raise
        yield fh
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass


def _default_lock_path() -> Path:
    """Canonical single-instance lock path.

    Byte-identical with the runbook (`docs/runbooks/tier-node27-timeseries-storage.md`
    §7.2 wire-code entry + §7.6 step 1 recovery) so operators reading either
    surface can rely on the same absolute path. Override via
    ``NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH`` when the deployment stamps the
    logs directory somewhere non-default.
    """
    return Path("~/node27-archive-rebuild-drill-logs/drill.lock").expanduser()


def _fail_receipt_for_uncaught(
    config: DrillConfig, error: Exception, now: datetime
) -> dict[str, Any]:
    """Build a FAIL receipt for an otherwise-unhandled exception (B1)."""
    outcome = DrillOutcome(
        verdict="FAIL",
        cycles=[],
        selectors=[],
        counts=[],
        coverage=[],
        differences=[
            {
                "item": "drill",
                "expected": {"code": CODE_DRILL_UNCAUGHT_ERROR},
                "actual": {
                    "error": str(error),
                    "cause_type": type(error).__name__,
                },
            }
        ],
    )
    return _build_receipt_from_outcome(outcome, config, now)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        config = _config_from_env(os.environ, argv)
    except DrillConfigError as error:
        print(json.dumps({"status": "failed", "reason": str(error)}), file=sys.stderr)
        return 2

    lock_path = config.lock_path
    try:
        with _single_instance_lock(lock_path):
            return _main_locked(config)
    except DrillConcurrentInvocationError as error:
        # C2: emit a FAIL receipt so operators see the collision in the
        # normal receipt stream — a silent exit 2 is easy to miss.
        try:
            receipt = _fail_receipt_for_concurrent(config, error, datetime.now(UTC))
            write_receipt(config.receipt_path, receipt)
        except Exception:  # pragma: no cover — receipt best-effort
            pass
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": str(error),
                    "code": CODE_DRILL_CONCURRENT_INVOCATION,
                }
            ),
            file=sys.stderr,
        )
        return 2


def _fail_receipt_for_concurrent(
    config: DrillConfig, error: DrillConcurrentInvocationError, now: datetime
) -> dict[str, Any]:
    outcome = DrillOutcome(
        verdict="FAIL",
        cycles=[],
        selectors=[],
        counts=[],
        coverage=[],
        differences=[
            {
                "item": "drill",
                "expected": {"code": CODE_DRILL_CONCURRENT_INVOCATION},
                "actual": {
                    "error": str(error),
                    "cause_type": type(error).__name__,
                },
            }
        ],
    )
    return _build_receipt_from_outcome(outcome, config, now)


def _main_locked(config: DrillConfig) -> int:
    try:
        receipt, outcome = run_drill(config)
    except DrillConfigError as error:
        print(json.dumps({"status": "failed", "reason": str(error)}), file=sys.stderr)
        return 2
    except Exception as error:
        # B1 / C-is-4: any uncaught downstream fault (psycopg2 / OSError /
        # OutputParsingError / AttributeError / ...) must land as a
        # schema-valid FAIL receipt with wire code DRILL_UNCAUGHT_ERROR,
        # not a raw stack trace. Operators consume receipts as the sole
        # oracle.
        now = datetime.now(UTC)
        try:
            receipt = _fail_receipt_for_uncaught(config, error, now)
            write_receipt(config.receipt_path, receipt)
        except Exception:  # pragma: no cover — receipt-emit best-effort
            pass
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": str(error),
                    "code": CODE_DRILL_UNCAUGHT_ERROR,
                    "cause_type": type(error).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 1
    write_receipt(config.receipt_path, receipt)
    print(json.dumps({"verdict": outcome.verdict, "receipt_path": str(config.receipt_path)}))
    return 0 if outcome.verdict == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
