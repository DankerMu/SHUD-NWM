from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    directory_identity_no_follow,
    verify_directory_no_follow,
)
from services.tile_publisher.publisher import (
    PublishError,
    TilePublisher,
    _collect_copyback_source_tree,
    _commit_qdown_copyback_batch,
    _configured_path_no_resolve,
    _CopybackRollbackEntry,
    _ForcingPackageRef,
    _ForcingPackageValidationError,
    _has_table,
    _parse_forcing_lineage,
    _paths_overlap,
    _reject_existing_symlink_components,
    _rollback_qdown_copyback_batch,
    _table_columns,
)

_REQUIRED_ENV = ("DATABASE_URL", "OBJECT_STORE_ROOT", "NHMS_OBJECT_STORE_COPYBACK_ROOT")
_COUNT_FIELDS = (
    "total_run_count",
    "forcing_version_count",
    "copyable_package_count",
    "already_present_checksum_consistent_count",
    "missing_source_count",
    "checksum_mismatch_count",
    "legacy_key_rejected_count",
    "copied_count",
    "failure_count",
)
_DISCOVER_BACKFILL_RUNS_SQL = """
    SELECT
           h.run_id,
           h.status,
           h.model_id,
           h.basin_version_id,
           h.forcing_version_id,
           h.source_id,
           h.cycle_time,
           fv.forcing_version_id AS forcing_row_forcing_version_id,
           fv.forcing_package_uri AS forcing_package_uri,
           fv.checksum AS forcing_checksum,
           fv.lineage_json AS forcing_lineage_json
    FROM hydro.hydro_run h
    LEFT JOIN met.forcing_version fv
      ON fv.forcing_version_id = h.forcing_version_id
    WHERE h.status IN ('succeeded', 'parsed', 'published')
      -- #1442: the run's identity arrives through the correlated hydro_run row,
      -- so the probe correlates on the surrogate key. Correlating on the text
      -- run identity instead would be a text fact join — forbidden, and not
      -- pushdown material either, since a join equality is not a constant.
      AND EXISTS (
          SELECT 1
          FROM hydro.river_timeseries rt
          WHERE rt.run_key = h.run_key
            -- transitional compressed-chunk pushdown aid, remove with #1342
            AND rt.variable = 'q_down'
            -- Deliberately no explicit enum cast on the literal: this statement
            -- is also parsed by the sqlite harness in
            -- tests/test_forcing_copyback_backfill.py, and PostgreSQL coerces
            -- the unknown-typed literal to the enum anyway, so the predicate is
            -- equally sargable without one.
            AND rt.variable_e = 'q_down'
            AND rt.value IS NOT NULL
      )
    ORDER BY h.run_id
"""


class BackfillError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class BackfillConfig:
    database_url: str
    object_store_root: Path | str
    copyback_root: Path | str
    object_store_prefix: str = ""
    apply: bool = False

    @classmethod
    def from_env(cls, *, apply: bool) -> BackfillConfig:
        missing = [name for name in _REQUIRED_ENV if not os.getenv(name, "").strip()]
        if missing:
            raise BackfillError(
                "CONFIG_MISSING",
                "Missing required environment variables for forcing copyback backfill.",
                details={"missing": missing},
            )
        return cls(
            database_url=os.environ["DATABASE_URL"].strip(),
            object_store_root=os.environ["OBJECT_STORE_ROOT"].strip(),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            copyback_root=os.environ["NHMS_OBJECT_STORE_COPYBACK_ROOT"].strip(),
            apply=apply,
        )


@dataclass(frozen=True)
class _Candidate:
    row: dict[str, Any]
    ref: _ForcingPackageRef


def discover_backfill_runs(session: Session) -> list[dict[str, Any]]:
    _require_backfill_schema(session)
    rows = session.execute(text(_DISCOVER_BACKFILL_RUNS_SQL)).mappings()
    return [_with_parsed_forcing_lineage(dict(row)) for row in rows]


def run_backfill(config: BackfillConfig) -> dict[str, Any]:
    object_store_root_raw = _configured_path_no_resolve(config.object_store_root)
    copyback_root_raw = _configured_path_no_resolve(config.copyback_root)
    object_store_root, object_store_identity = _verify_object_store_root(object_store_root_raw)
    _validate_copyback_root_boundary(
        copyback_root_raw=copyback_root_raw,
        object_store_root_raw=object_store_root_raw,
        object_store_root=object_store_root,
        object_store_identity=object_store_identity,
        apply=config.apply,
    )

    publisher = TilePublisher(
        workspace_root=Path.cwd(),
        object_store_root=object_store_root_raw,
        object_store_prefix=config.object_store_prefix,
    )

    try:
        engine = create_engine(config.database_url, future=True)
        with Session(engine) as session:
            _attach_sqlite_main_as_schemas(session)
            runs = discover_backfill_runs(session)
    except BackfillError:
        raise
    except SQLAlchemyError as error:
        raise BackfillError(
            "DATABASE_DISCOVERY_FAILED",
            f"Failed to discover forcing copyback candidates: {error}",
        ) from error

    report = _empty_report(
        apply=config.apply,
        object_store_root=object_store_root,
        copyback_root=copyback_root_raw,
        runs=runs,
    )

    target_store: LocalObjectStore | None = None
    copyback_root = copyback_root_raw
    if config.apply:
        try:
            copyback_root = publisher._prepare_copyback_root(
                copyback_root_raw=copyback_root_raw,
                object_store_root_raw=object_store_root_raw,
                object_store_root=object_store_root,
            )
        except PublishError as error:
            raise _backfill_error_from_publish_error(error) from error
        # Wrapped so a probe failure stays a copyback-root diagnosis: a bare
        # OSError would escape run_backfill and be caught by the CLI's generic
        # handler as BACKFILL_FAILED, degrading the error code.
        try:
            copyback_identity = directory_identity_no_follow(copyback_root)
        except (OSError, SafeFilesystemError) as error:
            raise BackfillError(
                "COPYBACK_ROOT_UNSAFE",
                "NHMS_OBJECT_STORE_COPYBACK_ROOT must be a safe directory when it already exists.",
                details={"copyback_root": str(copyback_root), "error": str(error)},
            ) from error
        _reject_same_copyback_root(
            copyback_root=copyback_root,
            object_store_root=object_store_root,
            copyback_identity=copyback_identity,
            object_store_identity=object_store_identity,
        )
        target_store = LocalObjectStore(copyback_root, object_store_prefix=config.object_store_prefix)
        report["copyback_root"] = str(copyback_root)
    if not runs:
        return _redact_output(report)

    if not config.apply:
        copyback_root, target_exists = _dry_run_copyback_root(
            copyback_root_raw=copyback_root_raw,
            object_store_root=object_store_root,
            object_store_identity=object_store_identity,
        )
        if target_exists:
            target_store = LocalObjectStore(copyback_root, object_store_prefix=config.object_store_prefix)
        report["copyback_root"] = str(copyback_root)

    _plan_or_apply_packages(
        publisher=publisher,
        runs=runs,
        target_store=target_store,
        copyback_root=copyback_root,
        apply=config.apply,
        report=report,
    )
    return _redact_output(report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill historical q_down forcing packages into NHMS_OBJECT_STORE_COPYBACK_ROOT."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy validated missing packages; omitted by default for dry-run",
    )
    args = parser.parse_args(argv)

    try:
        report = run_backfill(BackfillConfig.from_env(apply=args.apply))
    except BackfillError as error:
        _emit_error(error, stream=sys.stderr)
        return 2 if error.error_code == "CONFIG_MISSING" else 1
    except PublishError as error:
        _emit_error(_backfill_error_from_publish_error(error), stream=sys.stderr)
        return 1
    except (OSError, ObjectStoreError, SafeFilesystemError, ValueError) as error:
        _emit_error(
            BackfillError(
                "BACKFILL_FAILED",
                f"Forcing copyback backfill failed: {error}",
                details={"error_type": type(error).__name__},
            ),
            stream=sys.stderr,
        )
        return 1

    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _require_backfill_schema(session: Session) -> None:
    required_tables = (
        ("hydro", "hydro_run"),
        ("hydro", "river_timeseries"),
        ("met", "forcing_version"),
    )
    missing_tables = [
        f"{schema}.{table_name}"
        for schema, table_name in required_tables
        if not _has_table(session, schema, table_name)
    ]
    if missing_tables:
        raise BackfillError(
            "BACKFILL_SCHEMA_MISSING",
            "Required tables are missing for forcing copyback backfill.",
            details={"missing_tables": missing_tables},
        )

    # Kept in lockstep with _DISCOVER_BACKFILL_RUNS_SQL: exactly the columns that
    # statement references per relation (#1442). rt.run_id left the statement
    # when the correlated probe moved to rt.run_key = h.run_key, and rt.variable
    # stays only as the transitional pushdown aid (removed with #1342).
    required_columns = {
        ("hydro", "hydro_run"): {
            "run_id",
            "run_key",
            "status",
            "model_id",
            "basin_version_id",
            "forcing_version_id",
            "source_id",
            "cycle_time",
        },
        ("hydro", "river_timeseries"): {"run_key", "variable", "variable_e", "value"},
        ("met", "forcing_version"): {
            "forcing_version_id",
            "forcing_package_uri",
            "checksum",
            "lineage_json",
        },
    }
    missing_columns: dict[str, list[str]] = {}
    for (schema, table_name), columns in required_columns.items():
        existing = _table_columns(session, schema, table_name)
        missing = sorted(columns - existing)
        if missing:
            missing_columns[f"{schema}.{table_name}"] = missing
    if missing_columns:
        raise BackfillError(
            "BACKFILL_SCHEMA_MISSING",
            "Required columns are missing for forcing copyback backfill.",
            details={"missing_columns": missing_columns},
        )


def _attach_sqlite_main_as_schemas(session: Session) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return
    database = bind.url.database
    if not database or database == ":memory:":
        return
    connection = session.connection()
    attached = {
        str(row["name"])
        for row in connection.exec_driver_sql("PRAGMA database_list").mappings()
    }
    for schema in ("hydro", "met"):
        if schema not in attached:
            connection.exec_driver_sql(f"ATTACH DATABASE ? AS {schema}", (database,))


def _with_parsed_forcing_lineage(row: dict[str, Any]) -> dict[str, Any]:
    row["forcing_lineage"] = _parse_forcing_lineage(row.get("forcing_lineage_json"))
    return row


def _verify_object_store_root(object_store_root_raw: Path) -> tuple[Path, tuple[int, int]]:
    """Verify OBJECT_STORE_ROOT and take its filesystem identity in one place.

    The identity travels with the verified path so that no same-root call site
    re-probes the object-store side: a probe failure there must be diagnosed as
    ``OBJECT_STORE_ROOT_UNSAFE`` naming the object-store root, never as a
    copyback-root failure naming the wrong root (#1192).
    """

    try:
        object_store_root = verify_directory_no_follow(object_store_root_raw).resolve()
        return object_store_root, directory_identity_no_follow(object_store_root)
    except (OSError, SafeFilesystemError) as error:
        raise BackfillError(
            "OBJECT_STORE_ROOT_UNSAFE",
            "OBJECT_STORE_ROOT must be an existing safe directory.",
            details={"object_store_root": str(object_store_root_raw), "error": str(error)},
        ) from error


def _validate_copyback_root_boundary(
    *,
    copyback_root_raw: Path,
    object_store_root_raw: Path,
    object_store_root: Path,
    object_store_identity: tuple[int, int],
    apply: bool,
) -> None:
    _reject_existing_same_copyback_root(
        copyback_root_raw=copyback_root_raw,
        object_store_root_raw=object_store_root_raw,
        object_store_root=object_store_root,
        object_store_identity=object_store_identity,
    )
    if _paths_overlap(copyback_root_raw, object_store_root_raw) and copyback_root_raw != object_store_root_raw:
        raise BackfillError(
            "COPYBACK_ROOT_OVERLAP",
            "NHMS_OBJECT_STORE_COPYBACK_ROOT must not overlap OBJECT_STORE_ROOT.",
            details={
                "copyback_root": str(copyback_root_raw),
                "object_store_root": str(object_store_root),
            },
        )
    if not apply:
        try:
            _reject_existing_symlink_components(copyback_root_raw)
        except (OSError, SafeFilesystemError) as error:
            raise BackfillError(
                "COPYBACK_ROOT_UNSAFE",
                "NHMS_OBJECT_STORE_COPYBACK_ROOT has an unsafe existing path component.",
                details={"copyback_root": str(copyback_root_raw), "error": str(error)},
            ) from error


def _reject_existing_same_copyback_root(
    *,
    copyback_root_raw: Path,
    object_store_root_raw: Path,
    object_store_root: Path,
    object_store_identity: tuple[int, int],
) -> None:
    if copyback_root_raw == object_store_root_raw:
        # Both configured strings are equal and this runs before resolution, so
        # there is nothing to probe: the object-store identity stands for both.
        _reject_same_copyback_root(
            copyback_root=object_store_root,
            object_store_root=object_store_root,
            copyback_identity=object_store_identity,
            object_store_identity=object_store_identity,
        )
    try:
        copyback_root = verify_directory_no_follow(copyback_root_raw).resolve()
        # Inside the existing try on purpose: this pre-check returns silently
        # when the copyback root is absent or unreadable, and the probe must
        # keep that lenient posture rather than escaping the pre-check.
        copyback_identity = directory_identity_no_follow(copyback_root)
    except FileNotFoundError:
        return
    except (OSError, SafeFilesystemError):
        return
    _reject_same_copyback_root(
        copyback_root=copyback_root,
        object_store_root=object_store_root,
        copyback_identity=copyback_identity,
        object_store_identity=object_store_identity,
    )


def _dry_run_copyback_root(
    *,
    copyback_root_raw: Path,
    object_store_root: Path,
    object_store_identity: tuple[int, int],
) -> tuple[Path, bool]:
    try:
        copyback_root = verify_directory_no_follow(copyback_root_raw).resolve()
    except FileNotFoundError:
        return copyback_root_raw, False
    except (OSError, SafeFilesystemError) as error:
        raise BackfillError(
            "COPYBACK_ROOT_UNSAFE",
            "NHMS_OBJECT_STORE_COPYBACK_ROOT must be a safe directory when it already exists.",
            details={"copyback_root": str(copyback_root_raw), "error": str(error)},
        ) from error
    # A separate try: the root was just verified to exist, so any probe failure
    # here -- FileNotFoundError included -- is a real fault and keeps this
    # path's strict posture instead of degrading into "target absent".
    try:
        copyback_identity = directory_identity_no_follow(copyback_root)
    except (OSError, SafeFilesystemError) as error:
        raise BackfillError(
            "COPYBACK_ROOT_UNSAFE",
            "NHMS_OBJECT_STORE_COPYBACK_ROOT must be a safe directory when it already exists.",
            details={"copyback_root": str(copyback_root_raw), "error": str(error)},
        ) from error
    _reject_same_copyback_root(
        copyback_root=copyback_root,
        object_store_root=object_store_root,
        copyback_identity=copyback_identity,
        object_store_identity=object_store_identity,
    )
    if _paths_overlap(copyback_root, object_store_root) and copyback_root != object_store_root:
        raise BackfillError(
            "COPYBACK_ROOT_OVERLAP",
            "NHMS_OBJECT_STORE_COPYBACK_ROOT must not overlap OBJECT_STORE_ROOT.",
            details={
                "copyback_root": str(copyback_root),
                "object_store_root": str(object_store_root),
            },
        )
    return copyback_root, True


def _reject_same_copyback_root(
    *,
    copyback_root: Path,
    object_store_root: Path,
    copyback_identity: tuple[int, int],
    object_store_identity: tuple[int, int],
) -> None:
    """Refuse a copyback root that IS the object-store root.

    Sameness is filesystem identity, not the resolved path string, so an
    aliased root cannot be mistaken for a distinct root and copied across
    (#1192).  Identities are computed by the caller: each call site owns its
    probe and its failure posture explicitly.
    """

    if copyback_identity != object_store_identity:
        return
    raise BackfillError(
        "COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT must be different from OBJECT_STORE_ROOT for backfill.",
        details={
            "copyback_root": str(copyback_root),
            "object_store_root": str(object_store_root),
            "reason": "copyback_root_matches_object_store_root",
        },
    )


def _empty_report(
    *,
    apply: bool,
    object_store_root: Path,
    copyback_root: Path,
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "completed",
        "mode": "apply" if apply else "dry_run",
        "apply": apply,
        "object_store_root": str(object_store_root),
        "copyback_root": str(copyback_root),
        "packages": [],
        "failures": [],
    }
    for field in _COUNT_FIELDS:
        report[field] = 0
    report["total_run_count"] = len({str(row.get("run_id")) for row in runs if row.get("run_id") is not None})
    report["forcing_version_count"] = len(
        {
            str(row.get("forcing_row_forcing_version_id"))
            for row in runs
            if row.get("forcing_row_forcing_version_id") not in (None, "")
        }
    )
    return report


def _plan_or_apply_packages(
    *,
    publisher: TilePublisher,
    runs: list[dict[str, Any]],
    target_store: LocalObjectStore | None,
    copyback_root: Path,
    apply: bool,
    report: dict[str, Any],
) -> None:
    groups: dict[str, list[_Candidate]] = {}
    for row in runs:
        try:
            ref = publisher._forcing_package_ref_for_run(row)
        except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
            category = _classify_metadata_error(publisher, row, error)
            _record_failure(
                report,
                row=row,
                reason=str(error),
                category=category,
                object_key=None,
            )
            continue
        groups.setdefault(ref.object_key, []).append(_Candidate(row=row, ref=ref))

    for object_key in sorted(groups):
        candidates = groups[object_key]
        refs = [candidate.ref for candidate in candidates]
        rows = [candidate.row for candidate in candidates]
        package = _package_record(object_key, rows)
        report["packages"].append(package)

        checksums = {ref.checksum for ref in refs}
        if len(checksums) != 1:
            _fail_package(
                report,
                package,
                rows=rows,
                reason="Forcing package checksum differs for the same normalized forcing package key.",
                category="checksum_mismatch",
            )
            continue

        target_state = _inspect_existing_target(
            publisher=publisher,
            refs=refs,
            object_key=object_key,
            target_store=target_store,
        )
        if target_state["status"] == "already_present":
            package.update(
                {
                    "status": "already_present",
                    "file_count": target_state["file_count"],
                    "byte_count": target_state["byte_count"],
                }
            )
            report["already_present_checksum_consistent_count"] += 1
            continue
        if target_state["status"] == "failed":
            _fail_package(
                report,
                package,
                rows=rows,
                reason=target_state["reason"],
                category=target_state["category"],
            )
            continue

        source_state = _validate_source_package(publisher, refs, object_key)
        if source_state["status"] == "failed":
            _fail_package(
                report,
                package,
                rows=rows,
                reason=source_state["reason"],
                category=source_state["category"],
            )
            continue

        report["copyable_package_count"] += 1
        package.update(
            {
                "source_file_count": len(source_state["tree"].files),
                "source_byte_count": sum(size for _key, size in source_state["tree"].file_sizes),
            }
        )
        if not apply:
            package["status"] = "copyable"
            continue

        if target_store is None:
            _fail_package(
                report,
                package,
                rows=rows,
                reason="NHMS_OBJECT_STORE_COPYBACK_ROOT is not available for apply.",
                category="target_unsafe",
            )
            continue

        copy_state = _copy_package(
            publisher=publisher,
            refs=refs,
            object_key=object_key,
            target_store=target_store,
            copyback_root=copyback_root,
        )
        if copy_state["status"] == "failed":
            _fail_package(
                report,
                package,
                rows=rows,
                reason=copy_state["reason"],
                category=copy_state["category"],
            )
            continue
        package.update(
            {
                "status": "copied",
                "file_count": copy_state["file_count"],
                "byte_count": copy_state["byte_count"],
            }
        )
        report["copied_count"] += 1


def _inspect_existing_target(
    *,
    publisher: TilePublisher,
    refs: list[_ForcingPackageRef],
    object_key: str,
    target_store: LocalObjectStore | None,
) -> dict[str, Any]:
    if target_store is None:
        return {"status": "missing"}
    try:
        target_tree = _collect_copyback_source_tree(target_store, object_key)
    except FileNotFoundError:
        return {"status": "missing"}
    except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
        return {
            "status": "failed",
            "reason": str(error),
            "category": _classify_tree_error(error, context="target"),
        }
    try:
        publisher._validate_forcing_source_tree_for_refs(refs, target_tree, target_store)
    except _ForcingPackageValidationError as error:
        return {
            "status": "failed",
            "reason": str(error.original_error),
            "category": _classify_tree_error(error.original_error, context="target"),
        }
    return {
        "status": "already_present",
        "file_count": len(target_tree.files),
        "byte_count": sum(size for _key, size in target_tree.file_sizes),
    }


def _validate_source_package(
    publisher: TilePublisher,
    refs: list[_ForcingPackageRef],
    object_key: str,
) -> dict[str, Any]:
    try:
        source_tree = _collect_copyback_source_tree(publisher.object_store, object_key)
        publisher._validate_forcing_source_tree_for_refs(refs, source_tree, publisher.object_store)
    except FileNotFoundError as error:
        return {"status": "failed", "reason": str(error), "category": "missing_source"}
    except _ForcingPackageValidationError as error:
        return {
            "status": "failed",
            "reason": str(error.original_error),
            "category": _classify_tree_error(error.original_error, context="source"),
        }
    except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
        return {
            "status": "failed",
            "reason": str(error),
            "category": _classify_tree_error(error, context="source"),
        }
    return {"status": "valid", "tree": source_tree}


def _copy_package(
    *,
    publisher: TilePublisher,
    refs: list[_ForcingPackageRef],
    object_key: str,
    target_store: LocalObjectStore,
    copyback_root: Path,
) -> dict[str, Any]:
    rollback_log: list[_CopybackRollbackEntry] = []
    try:
        summary = publisher._copyback_object_tree_with_rollback(
            object_key,
            target_store,
            validate_source_tree=lambda source_tree: publisher._validate_forcing_source_tree_for_refs(
                refs, source_tree, publisher.object_store
            ),
            validate_target_tree=lambda target_tree: publisher._validate_forcing_source_tree_for_refs(
                refs, target_tree, target_store
            ),
            rollback_log=rollback_log,
        )
        _commit_qdown_copyback_batch(rollback_log, containment_root=copyback_root)
    except Exception as error:
        try:
            _rollback_qdown_copyback_batch(rollback_log, containment_root=copyback_root)
        except SafeFilesystemError as rollback_error:
            return {
                "status": "failed",
                "reason": f"{error}; rollback failed: {rollback_error}",
                "category": _classify_tree_error(error, context="target"),
            }
        original = error.original_error if isinstance(error, _ForcingPackageValidationError) else error
        return {
            "status": "failed",
            "reason": str(original),
            "category": _classify_tree_error(original, context="target"),
        }
    return {"status": "copied", **summary}


def _package_record(object_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object_key": object_key,
        "status": "planned",
        "run_ids": sorted({str(row.get("run_id")) for row in rows if row.get("run_id") not in (None, "")}),
        "forcing_version_ids": sorted(
            {
                str(row.get("forcing_version_id"))
                for row in rows
                if row.get("forcing_version_id") not in (None, "")
            }
        ),
        "references": [_reference_evidence(row) for row in rows],
    }


def _reference_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row.get("run_id"),
        "forcing_version_id": row.get("forcing_version_id"),
        "forcing_package_uri": row.get("forcing_package_uri"),
    }


def _fail_package(
    report: dict[str, Any],
    package: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    reason: str,
    category: str,
) -> None:
    package["status"] = "failed"
    package["reason"] = reason
    _record_failure(
        report,
        row=rows[0],
        reason=reason,
        category=category,
        object_key=package["object_key"],
        related_rows=rows,
    )


def _record_failure(
    report: dict[str, Any],
    *,
    row: dict[str, Any],
    reason: str,
    category: str,
    object_key: str | None,
    related_rows: list[dict[str, Any]] | None = None,
) -> None:
    normalized_category = _normalized_failure_category(category)
    failure = {
        "run_id": row.get("run_id"),
        "forcing_version_id": row.get("forcing_version_id"),
        "forcing_package_uri": row.get("forcing_package_uri"),
        "reason": reason,
        "category": normalized_category,
    }
    if object_key is not None:
        failure["object_key"] = object_key
    if related_rows:
        failure["related_run_ids"] = sorted(
            {str(item.get("run_id")) for item in related_rows if item.get("run_id") not in (None, "")}
        )
        failure["related_forcing_version_ids"] = sorted(
            {
                str(item.get("forcing_version_id"))
                for item in related_rows
                if item.get("forcing_version_id") not in (None, "")
            }
        )
        failure["references"] = [_reference_evidence(item) for item in related_rows]
    report["failures"].append(failure)
    report["failure_count"] += 1
    if normalized_category == "missing_source":
        report["missing_source_count"] += 1
    elif normalized_category == "checksum_mismatch":
        report["checksum_mismatch_count"] += 1
    elif normalized_category == "legacy_key_rejected":
        report["legacy_key_rejected_count"] += 1


def _normalized_failure_category(category: str) -> str:
    if category in {"missing_source", "checksum_mismatch", "legacy_key_rejected"}:
        return category
    return "failed"


def _classify_metadata_error(
    publisher: TilePublisher,
    row: dict[str, Any],
    error: BaseException,
) -> str:
    if _is_legacy_forcing_key(publisher.object_store, row):
        return "legacy_key_rejected"
    return _classify_tree_error(error, context="metadata")


def _classify_tree_error(error: BaseException, *, context: str) -> str:
    message = str(error).lower()
    if "checksum" in message and ("mismatch" in message or "does not match" in message or "differs" in message):
        return "checksum_mismatch"
    if context == "source" and (
        isinstance(error, FileNotFoundError)
        or "is missing" in message
        or "no such file" in message
        or "not found" in message
    ):
        return "missing_source"
    return "target_unsafe" if context == "target" else "failed"


def _is_legacy_forcing_key(object_store: LocalObjectStore, row: dict[str, Any]) -> bool:
    package_uri = row.get("forcing_package_uri")
    forcing_version_id = str(row.get("forcing_version_id") or "").strip()
    if not isinstance(package_uri, str) or not package_uri.strip() or not forcing_version_id:
        return False
    try:
        key = object_store.normalize_key(package_uri).rstrip("/")
    except ValueError:
        return False
    parts = PurePosixPath(key).parts
    return len(parts) == 2 and parts[0] == "forcing" and parts[1] == forcing_version_id


def _emit_error(error: BackfillError, *, stream: Any) -> None:
    payload = {
        "status": "failed",
        "error_code": error.error_code,
        "message": error.message,
    }
    if error.details:
        payload["details"] = error.details
    json.dump(_redact_output(payload), stream, sort_keys=True)
    stream.write("\n")


def _backfill_error_from_publish_error(error: PublishError) -> BackfillError:
    return BackfillError(
        error.error_code,
        error.message,
        details=dict(error.details),
    )


def _redact_output(payload: Any) -> Any:
    return redact_payload(payload)


if __name__ == "__main__":
    raise SystemExit(main())
