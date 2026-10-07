"""Every statement the basin retirement tool sends to the node-27 database.

Part of ``scripts/node27_retire_basin.py`` (the entry point).  The tool writes
one column of one table itself: ``status`` of ``hydro.hydro_run``, in one
transaction with the backup of the rows it changes.  ``core.model_instance``
is written only through the model lifecycle operation of the registry store
(``scripts/basin_retirement/steps.py``); ``core.basin``,
``core.basin_version`` and ``updated_at`` of a run are never written.

Rows are selected by exact ``basin_version_id``, never by a pattern: the
``model_id`` of a direct-grid row is a hash that does not hold the basin name.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import psycopg2

from packages.common.model_registry import PsycopgModelRegistryStore
from scripts.basin_retirement.model import StepFailure, fsync_directory

APPLICATION_NAME = "nhms-retire-basin"
# The statuses of a run that is, or can still become, a displayed product.
CANDIDATE_STATUSES = ("succeeded", "parsed", "published")

BASIN_ID_SQL = "SELECT basin_id FROM core.basin_version WHERE basin_version_id = %s"
MODEL_IDS_SQL = "SELECT model_id FROM core.model_instance WHERE basin_version_id = %s ORDER BY model_id"
ACTIVE_MODEL_IDS_SQL = (
    "SELECT model_id FROM core.model_instance WHERE basin_version_id = %s AND active_flag ORDER BY model_id"
)
CANDIDATE_COUNT_SQL = (
    "SELECT count(*) FROM hydro.hydro_run WHERE basin_version_id = %s "
    "AND status IN ('succeeded','parsed','published')"
)
# ``copy_expert`` binds no parameters: the one placeholder is rendered by ``cursor.mogrify``.
BACKUP_COPY_SQL = (
    "COPY (SELECT * FROM hydro.hydro_run WHERE basin_version_id = %s "
    "AND status IN ('succeeded','parsed','published') ORDER BY run_id) TO STDOUT WITH CSV HEADER"
)
SUPERSEDE_SQL = (
    "UPDATE hydro.hydro_run SET status = 'superseded' WHERE basin_version_id = %s "
    "AND status IN ('succeeded','parsed','published')"
)


class CommitOutcomeUnknown(StepFailure):
    """The ``COMMIT`` itself failed: whether the runs were superseded is not known, so the backup is kept."""


def registry_store(database_url: str) -> Any:
    """The store whose lifecycle operation deactivates the model rows."""

    return PsycopgModelRegistryStore(database_url, application_name=APPLICATION_NAME)


def _database_failure(error: Exception) -> StepFailure:
    return StepFailure(f"The database refused: {type(error).__name__}: {str(error).strip()}")


@contextmanager
def _connection(database_url: str, *, readonly: bool) -> Iterator[Any]:
    """One connection, never in autocommit; whatever is not committed by the caller is rolled back."""

    connection = psycopg2.connect(database_url, fallback_application_name=APPLICATION_NAME)
    try:
        connection.autocommit = False
        if readonly:
            connection.set_session(readonly=True)
        yield connection
    finally:
        # A rollback that fails must not replace what the caller raised; closing discards the transaction anyway.
        try:
            connection.rollback()
        except psycopg2.Error:
            pass
        finally:
            connection.close()


def _read(database_url: str, sql: str, basin_version_id: str) -> list[tuple[Any, ...]]:
    try:
        with _connection(database_url, readonly=True) as connection, connection.cursor() as cursor:
            cursor.execute(sql, (basin_version_id,))
            return list(cursor.fetchall())
    except psycopg2.Error as error:
        raise _database_failure(error) from error


def basin_id_of(database_url: str, basin_version_id: str) -> str | None:
    """``basin_id`` of the ``core.basin_version`` row, or None when there is no such row."""

    rows = _read(database_url, BASIN_ID_SQL, basin_version_id)
    return str(rows[0][0]) if rows else None


def model_ids(database_url: str, basin_version_id: str) -> list[str]:
    """Every ``core.model_instance`` row of the basin version, active or not."""

    return [str(row[0]) for row in _read(database_url, MODEL_IDS_SQL, basin_version_id)]


def active_model_ids(database_url: str, basin_version_id: str) -> list[str]:
    """The active rows of the basin version: baseline and ``dg_*`` alike."""

    return [str(row[0]) for row in _read(database_url, ACTIVE_MODEL_IDS_SQL, basin_version_id)]


def candidate_run_count(database_url: str, basin_version_id: str) -> int:
    """How many runs of the basin version are in ``succeeded``, ``parsed`` or ``published``."""

    return int(_read(database_url, CANDIDATE_COUNT_SQL, basin_version_id)[0][0])


def backup_facts(path: Path) -> dict[str, Any]:
    """Row count and per-status counts of a run backup, read back from the file itself."""

    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise StepFailure(f"The run backup {path} cannot be read back ({error}).") from error
    if not rows or "status" not in rows[0]:
        raise StepFailure(f"The run backup {path} has no CSV header with a status column.")
    column = rows[0].index("status")
    try:
        statuses = Counter(row[column] for row in rows[1:])
    except IndexError as error:
        raise StepFailure(f"The run backup {path} has a row shorter than its header.") from error
    unexpected = sorted(set(statuses) - set(CANDIDATE_STATUSES))
    if unexpected:
        raise StepFailure(f"The run backup {path} holds rows in {unexpected}, which this tool does not supersede.")
    return {
        "row_count": len(rows) - 1,
        "status_counts": {status: statuses.get(status, 0) for status in CANDIDATE_STATUSES},
    }


def backup_and_supersede(
    database_url: str, basin_version_id: str, *, backup: BinaryIO, backup_path: Path, commit: bool
) -> dict[str, Any]:
    """Back the candidate runs up to ``backup`` and set them ``superseded``, in one transaction.

    The backup is flushed and fsynced, and so is its directory, before the
    update; the transaction is committed only when ``commit``, and otherwise
    rolled back after the same statements.  Any failure before the commit
    rolls back; removing the backup file is the caller's part.
    """

    commit_attempted = False
    try:
        with _connection(database_url, readonly=False) as connection:
            with connection.cursor() as cursor:
                statement = cursor.mogrify(BACKUP_COPY_SQL, (basin_version_id,))
                cursor.copy_expert(statement.decode("utf-8") if isinstance(statement, bytes) else statement, backup)
                backup.flush()
                os.fsync(backup.fileno())
                fsync_directory(backup_path.parent)
                facts = backup_facts(backup_path)
                cursor.execute(SUPERSEDE_SQL, (basin_version_id,))
                updated = int(cursor.rowcount)
                if updated != facts["row_count"]:
                    raise StepFailure(
                        f"The update changed {updated} rows of hydro.hydro_run but the backup holds "
                        f"{facts['row_count']}: a run changed status between the two statements. The transaction "
                        "was rolled back."
                    )
                cursor.execute(CANDIDATE_COUNT_SQL, (basin_version_id,))
                left = int(cursor.fetchone()[0])
                if left:
                    raise StepFailure(
                        f"{left} runs of {basin_version_id} are still in {list(CANDIDATE_STATUSES)} after the "
                        "update. The transaction was rolled back."
                    )
            if commit:
                # From here on nothing may conclude "not committed": an interrupt counts as unknown too.
                commit_attempted = True
                connection.commit()
    except BaseException as error:
        if commit_attempted:
            raise CommitOutcomeUnknown(
                f"The COMMIT of the supersede transaction did not return ({type(error).__name__}: "
                f"{str(error).strip()}); whether the runs were superseded is not known, so the backup "
                f"{backup_path} was kept."
            ) from error
        if isinstance(error, psycopg2.Error):
            raise _database_failure(error) from error
        raise
    return {**facts, "updated_row_count": updated, "committed": commit}
