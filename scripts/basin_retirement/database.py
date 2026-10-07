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
    "AND status IN ('succeeded','parsed','published') RETURNING run_id"
)
# First in the supersede transaction: an ingest holding a row lock fails the step instead of hanging it.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '10s'"


class CommitOutcomeUnknown(StepFailure):
    """The commit call did not return: whether the runs were superseded is not known."""


class CommitMarker:
    """Whether the commit call of a supersede transaction was reached.

    The one fact that decides whether the run backup may be removed: set
    before the call, so a commit that returned, raised or was interrupted, and
    everything after it, all count as reached.
    """

    def __init__(self) -> None:
        self.reached = False


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
    """Row count, per-status counts and the ``run_id`` of every row of a run backup, read back from the file."""

    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise StepFailure(f"The run backup {path} cannot be read back ({error}).") from error
    if not rows or "status" not in rows[0] or "run_id" not in rows[0]:
        raise StepFailure(f"The run backup {path} has no CSV header with a run_id and a status column.")
    status_column, id_column = rows[0].index("status"), rows[0].index("run_id")
    try:
        statuses = Counter(row[status_column] for row in rows[1:])
        run_ids = [row[id_column] for row in rows[1:]]
    except IndexError as error:
        raise StepFailure(f"The run backup {path} has a row shorter than its header.") from error
    unexpected = sorted(set(statuses) - set(CANDIDATE_STATUSES))
    if unexpected:
        raise StepFailure(f"The run backup {path} holds rows in {unexpected}, which this tool does not supersede.")
    if len(set(run_ids)) != len(run_ids):
        raise StepFailure(f"The run backup {path} holds a run_id more than once.")
    return {
        "row_count": len(rows) - 1,
        "status_counts": {status: statuses.get(status, 0) for status in CANDIDATE_STATUSES},
        "run_ids": run_ids,
    }


def _differing_ids(backed_up: set[str], updated: list[str]) -> str | None:
    """What differs between the rows of the backup and the rows the update returned, or None."""

    changed = set(updated)
    if changed == backed_up and len(updated) == len(backed_up):
        return None
    only_backup, only_update = sorted(backed_up - changed), sorted(changed - backed_up)
    return (
        f"the update returned {len(updated)} rows and the backup holds {len(backed_up)}; in the backup only: "
        f"{only_backup[:10]} ({len(only_backup)}), updated only: {only_update[:10]} ({len(only_update)})"
    )


def backup_and_supersede(
    database_url: str,
    basin_version_id: str,
    *,
    backup: BinaryIO,
    backup_path: Path,
    commit: CommitMarker | None,
) -> dict[str, Any]:
    """Back the candidate runs up to ``backup`` and set them ``superseded``, in one transaction.

    The backup is flushed and fsynced, and so is its directory, before the
    update.  The rows the update returns must be exactly the rows of the
    backup: the two statements see separate snapshots, so equal counts are not
    enough.  With a ``commit`` marker the transaction is committed, the marker
    being set before the commit call; without one the same statements are
    rolled back.  Removing the backup file is the caller's part, and it
    depends on the marker alone.
    """

    try:
        with _connection(database_url, readonly=False) as connection:
            with connection.cursor() as cursor:
                cursor.execute(LOCK_TIMEOUT_SQL)
                statement = cursor.mogrify(BACKUP_COPY_SQL, (basin_version_id,))
                cursor.copy_expert(statement.decode("utf-8") if isinstance(statement, bytes) else statement, backup)
                backup.flush()
                os.fsync(backup.fileno())
                fsync_directory(backup_path.parent)
                facts = backup_facts(backup_path)
                run_ids = facts.pop("run_ids")
                cursor.execute(SUPERSEDE_SQL, (basin_version_id,))
                updated = [str(row[0]) for row in cursor.fetchall()]
                differing = _differing_ids(set(run_ids), updated)
                if differing:
                    raise StepFailure(
                        f"The rows the update changed are not the rows of the backup: {differing}. A run changed "
                        "status between the two statements. The transaction was rolled back."
                    )
                cursor.execute(CANDIDATE_COUNT_SQL, (basin_version_id,))
                left = int(cursor.fetchone()[0])
                if left:
                    raise StepFailure(
                        f"{left} runs of {basin_version_id} are still in {list(CANDIDATE_STATUSES)} after the "
                        "update. The transaction was rolled back."
                    )
            if commit is not None:
                # From here on nothing may conclude "not committed".
                commit.reached = True
                try:
                    connection.commit()
                except BaseException as error:  # an interrupt inside the call leaves the outcome unknown too
                    raise CommitOutcomeUnknown(
                        f"The COMMIT of the supersede transaction did not return ({type(error).__name__}: "
                        f"{str(error).strip()}); whether the runs were superseded is not known. The backup "
                        f"{backup_path} was kept: run the same command, which decides from the table."
                    ) from error
    except psycopg2.Error as error:
        raise _database_failure(error) from error
    return {**facts, "updated_row_count": len(updated), "committed": commit is not None}
