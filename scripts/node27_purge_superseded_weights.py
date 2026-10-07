#!/usr/bin/env python
"""Delete the ``met.interp_weight`` rows of superseded models on node-27; a dry-run unless ``--apply``.

Every Direct Grid generation registers its own model and its own weight rows,
and nothing removes the previous generation's.  This command removes the
weights of every model that no protection covers.  A model's weights are
deleted only when **all** of these hold, evaluated in SQL with the server
clock (the window is ``now() - make_interval(days => --min-idle-days)``; a NULL
timestamp is never inside it):

* it is not the model of the latest displayable forecast run (``forecast``,
  ``succeeded`` / ``parsed`` / ``published``, non-null ``cycle_time``; newest
  ``cycle_time``, then highest ``run_id``) of any ``(basin_version_id,
  lower(source_id))``                                         -> ``current``
* it is not a ``model_id`` of the canonical scheduler manifest -> ``in_manifest``
* no ``hydro.hydro_run`` row of it, of any type or status, has ``cycle_time``,
  ``start_time``, ``created_at`` or ``updated_at`` inside the window -> ``recent_run``
* no ``met.forcing_version`` row of it was created inside it   -> ``recent_forcing``
* none of its weight rows was created inside it                -> ``recent_weights``
* its ``core.model_instance`` row was created before it        -> ``recently_created``

Everything else is ``purgeable``.  The rule does not look at the id prefix,
``active_flag`` or ``lifecycle_state``.

Without ``--apply``: one read-only connection, no file and no directory
created, no statement that writes; the report (server version, per-class model
and row counts, the purgeable models with their rows, the total and the largest
single model) goes to stdout.

With ``--apply``, per purgeable model in ``model_id`` order, one READ COMMITTED
transaction: ``lock_timeout`` 10 s; the advisory lock the weight writers take
(``upsert_interp_weights`` of the forcing producer store and
``_lock_interp_weight_scope`` of the forcing domain handoff) for every
``(source_id, grid_id)`` of the model; the rule again, now under the locks (a
model that is protected by now is skipped); backup and delete in ONE statement
(``COPY (DELETE ... RETURNING ...) TO STDOUT``) into ``weights-<model_id>.csv``
(exclusive create, 0600); no row of the model may be left and the CSV must hold
a row; file and directory fsynced; commit.  The backup is removed only when the
commit call was never reached.  Any failure stops the run: earlier models stay
purged, later ones are untouched.

Receipts, never rewritten, in ``<receipt-root>/purge-<utc stamp>/``:
``model-<nnnn>.json`` per model, then exactly one of ``purge-receipt.json`` and
``purge-failed.json``.

Refused before the database is opened: an empty ``--operator-id`` / ``--reason``;
``--min-idle-days`` below 21 (the production forcing retention);
``DATABASE_URL`` of the process not the one plain unquoted line of the env
file; a missing, unreadable or malformed manifest; with ``--apply``, another
instance (an exclusive lock on ``<env file>.weight-purge-lock``).

Never written: ``met.met_station``, ``core.model_instance``, ``hydro.*``,
``met.forcing_version``, any timeseries table.  No VACUUM, ANALYZE or TRUNCATE.

Run it on node-27 with the ingest environment loaded:
``cd /home/nwm/NWM && uv run --no-sync python -m scripts.node27_purge_superseded_weights``.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from packages.common import succession_receipt as succession

REPO_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_NAME = "nhms-purge-superseded-weights"
DATABASE_URL_ENV = "DATABASE_URL"
OBJECT_STORE_ROOT_ENV = "OBJECT_STORE_ROOT"
DEFAULT_ENV_FILE_KEY = "infra/env/node27-ingest.env"
DEFAULT_RECEIPT_ROOT_KEY = "scheduler/weight-purge"
MANIFEST_KEY = "scheduler/registry/manifest-last.json"
LOCK_SUFFIX = ".weight-purge-lock"
RUNBOOK = "docs/runbooks/production-ops/operating-scope.md (7.7)"
RECEIPT_SCHEMA_VERSION = "nhms.weight_purge.receipt.v1"
FAILURE_SCHEMA_VERSION = "nhms.weight_purge.failure.v1"
SUMMARY_NAME = "purge-receipt.json"
FAILURE_NAME = "purge-failed.json"
DEFAULT_MIN_IDLE_DAYS = 30
# The production forcing retention, in days: a window shorter than it could purge a model whose series still exist.
MINIMUM_IDLE_DAYS = 21
DEFAULT_PAUSE_SECONDS = 2.0
BACKUP_MODE = 0o600
NOTHING_WRITTEN = "Nothing was written."

CLASSES = ("current", "in_manifest", "recent_run", "recent_forcing", "recent_weights", "recently_created", "purgeable")
PURGEABLE = "purgeable"
# Not a class of the rule: what a skip records when the model has no weight row left at its own transaction.
NO_WEIGHT_ROWS = "no_weight_rows"

# ``COPY`` binds no parameters, so a model id is rendered as a literal: only ids of this shape are ever rendered.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Every column of ``met.interp_weight`` in table order.  The backup is written and restored with this list; the
# real-database suite compares it with ``information_schema`` so a later ``ADD COLUMN`` fails there.
WEIGHT_COLUMNS = (
    "weight_id",
    "source_id",
    "grid_id",
    "model_id",
    "station_id",
    "variable",
    "grid_cell_id",
    "weight",
    "method",
    "created_at",
    "grid_signature",
    "active_flag",
    "superseded_at",
    "grid_snapshot_id",
)

SERVER_VERSION_SQL = "SHOW server_version"
# First in every purge transaction: a writer holding a scope fails the model instead of hanging the run.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '10s'"
PAIRS_SQL = "SELECT DISTINCT source_id, grid_id FROM met.interp_weight WHERE model_id = %s"
ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
REMAINING_SQL = "SELECT count(*) FROM met.interp_weight WHERE model_id = %s"
PURGE_COPY_SQL = (
    "COPY (DELETE FROM met.interp_weight WHERE model_id = {model_id} AND (source_id, grid_id) IN ({pairs}) "
    "RETURNING {columns}) TO STDOUT WITH CSV HEADER"
)
# One statement for the whole table and for one model (the second and third parameter: NULL, or the model id
# twice).  Parameters: days, model id, model id, the manifest's model ids.  It reads ``met.interp_weight``,
# ``hydro.hydro_run``, ``met.forcing_version`` and ``core.model_instance`` and nothing else.  A comparison with a
# NULL timestamp is NULL, so it never puts a row inside the window; a model row that is missing or has no
# ``created_at`` is not provably old and stays protected.
CLASSIFY_SQL = """
WITH cutoff AS (
    SELECT now() - make_interval(days => %s) AS at
),
weights AS (
    SELECT w.model_id,
           count(*) AS row_count,
           coalesce(bool_or(w.created_at >= c.at), false) AS recent
    FROM met.interp_weight w
    CROSS JOIN cutoff c
    WHERE (%s::text IS NULL OR w.model_id = %s)
    GROUP BY w.model_id
),
latest AS (
    SELECT DISTINCT ON (h.basin_version_id, lower(h.source_id)) h.model_id
    FROM hydro.hydro_run h
    WHERE h.run_type = 'forecast'
      AND h.status IN ('succeeded', 'parsed', 'published')
      AND h.cycle_time IS NOT NULL
    ORDER BY h.basin_version_id, lower(h.source_id), h.cycle_time DESC, h.run_id DESC
),
recent_runs AS (
    SELECT DISTINCT h.model_id
    FROM hydro.hydro_run h
    CROSS JOIN cutoff c
    WHERE h.cycle_time >= c.at OR h.start_time >= c.at OR h.created_at >= c.at OR h.updated_at >= c.at
),
recent_forcing AS (
    SELECT DISTINCT f.model_id
    FROM met.forcing_version f
    CROSS JOIN cutoff c
    WHERE f.created_at >= c.at
)
SELECT w.model_id,
       w.row_count,
       CASE
           WHEN EXISTS (SELECT 1 FROM latest l WHERE l.model_id = w.model_id) THEN 'current'
           WHEN w.model_id = ANY (%s::text[]) THEN 'in_manifest'
           WHEN EXISTS (SELECT 1 FROM recent_runs r WHERE r.model_id = w.model_id) THEN 'recent_run'
           WHEN EXISTS (SELECT 1 FROM recent_forcing f WHERE f.model_id = w.model_id) THEN 'recent_forcing'
           WHEN w.recent THEN 'recent_weights'
           WHEN (m.created_at < c.at) IS NOT TRUE THEN 'recently_created'
           ELSE 'purgeable'
       END AS class
FROM weights w
CROSS JOIN cutoff c
LEFT JOIN core.model_instance m ON m.model_id = w.model_id
ORDER BY w.model_id
"""

DRY_RUN_NOTICE = (
    "DRY-RUN (no --apply): the connection is read-only, no file or directory is created and nothing is deleted. "
    "An --apply deletes the met.interp_weight rows of every purgeable model, one transaction per model, each "
    "backed up to a CSV first."
)


class PurgeRefusal(RuntimeError):
    """The run is refused before anything is written."""


class PurgeFailure(RuntimeError):
    """A model could not be purged; the run stops.  ``outcome`` says what became of that model's rows."""

    def __init__(self, message: str, *, outcome: str = "rolled_back", backup: Path | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.backup = backup


class CommitMarker:
    """Whether the commit call of a purge transaction was reached.

    The one fact that decides whether a backup may be removed: set before the
    call, so a commit that returned, raised or was interrupted all count.
    """

    def __init__(self) -> None:
        self.reached = False


@dataclass(frozen=True)
class Settings:
    operator_id: str
    reason: str
    env_file: Path
    receipt_root: Path
    object_store_root: Path
    database_url: str
    min_idle_days: int
    pause_seconds: float
    max_models: int | None

    @property
    def manifest(self) -> Path:
        return self.object_store_root / MANIFEST_KEY

    @property
    def lock_file(self) -> Path:
        return Path(f"{self.env_file}{LOCK_SUFFIX}")


@dataclass(frozen=True)
class Manifest:
    model_ids: tuple[str, ...]
    sha256: str


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def lock_key(source_id: str, grid_id: str, model_id: str) -> str:
    """The advisory lock key of one weight scope, spelled as the weight writers spell it (``source_id`` as stored)."""

    return f"met.interp_weight:{source_id}\x1f{grid_id}\x1f{model_id}"


def scrub(text: str, database_url: str) -> str:
    """``text`` without the connection string or its password, whatever put them there."""

    secrets = {database_url}
    try:
        secrets.add(parse_dsn(database_url).get("password") or "")
    except psycopg2.Error:
        pass
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--operator-id", required=True, help="Recorded in the receipts.")
    parser.add_argument("--reason", required=True, help="Why the weights are purged; recorded in the receipts.")
    parser.add_argument(
        "--env-file",
        help=f"The ingest env file whose {DATABASE_URL_ENV} line this process must carry "
        f"(default: {DEFAULT_ENV_FILE_KEY} of this checkout).",
    )
    parser.add_argument(
        "--receipt-root",
        help=f"Directory of the run directories (default: <{OBJECT_STORE_ROOT_ENV}>/{DEFAULT_RECEIPT_ROOT_KEY}).",
    )
    parser.add_argument(
        "--min-idle-days",
        type=int,
        default=DEFAULT_MIN_IDLE_DAYS,
        help=f"The idle window in days (default: %(default)s; below {MINIMUM_IDLE_DAYS} is refused).",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="Sleep between two models of an apply (default: %(default)s).",
    )
    parser.add_argument("--max-models", type=int, help="Purge at most this many models in this run.")
    parser.add_argument("--apply", action="store_true", help="Delete. Without it the run is a dry-run.")
    return parser.parse_args(argv)


def settings_from_arguments(args: argparse.Namespace) -> Settings:
    """The settings of a run.  Reads no file and opens no connection."""

    empty = [option for option in ("operator_id", "reason") if not str(getattr(args, option)).strip()]
    if empty:
        names = ", ".join(f"--{option.replace('_', '-')}" for option in empty)
        raise PurgeRefusal(f"Refused: {names} must not be empty. {NOTHING_WRITTEN}")
    if args.min_idle_days < MINIMUM_IDLE_DAYS:
        raise PurgeRefusal(
            f"Refused: --min-idle-days {args.min_idle_days} is below {MINIMUM_IDLE_DAYS}, the production forcing "
            f"retention: a model whose series still exist could be purged. {NOTHING_WRITTEN}"
        )
    if not args.pause_seconds >= 0:
        raise PurgeRefusal(f"Refused: --pause-seconds must not be negative. {NOTHING_WRITTEN}")
    if args.max_models is not None and args.max_models < 1:
        raise PurgeRefusal(f"Refused: --max-models must be at least 1. {NOTHING_WRITTEN}")
    values = {name: os.environ.get(name) or "" for name in (DATABASE_URL_ENV, OBJECT_STORE_ROOT_ENV)}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise PurgeRefusal(
            f"Refused: {', '.join(missing)} must be set (load the ingest env file, {DEFAULT_ENV_FILE_KEY}). "
            f"{NOTHING_WRITTEN}"
        )
    object_store_root = Path(values[OBJECT_STORE_ROOT_ENV])
    if not object_store_root.is_absolute():
        raise PurgeRefusal(f"Refused: {OBJECT_STORE_ROOT_ENV} must be an absolute path. {NOTHING_WRITTEN}")
    env_file = Path(args.env_file) if args.env_file else REPO_ROOT / DEFAULT_ENV_FILE_KEY
    return Settings(
        operator_id=args.operator_id.strip(),
        reason=args.reason.strip(),
        env_file=Path(os.path.abspath(env_file)),
        receipt_root=(
            Path(os.path.abspath(args.receipt_root))
            if args.receipt_root
            else object_store_root / DEFAULT_RECEIPT_ROOT_KEY
        ),
        object_store_root=object_store_root,
        database_url=values[DATABASE_URL_ENV],
        min_idle_days=args.min_idle_days,
        pause_seconds=args.pause_seconds,
        max_models=args.max_models,
    )


_DATABASE_URL_LINE = re.compile(rb"DATABASE_URL=(.*)")
_DATABASE_URL_ASSIGNMENT = re.compile(rb"\s*(export\s+)?DATABASE_URL\+?=")
_QUOTES = ("'", '"')


def check_database_binding(settings: Settings) -> None:
    """Refuse unless the env file names, in one plain line, the database this process would delete from.

    Neither value is ever put in a message.
    """

    try:
        lines = settings.env_file.read_bytes().split(b"\n")
    except OSError as error:
        raise PurgeRefusal(
            f"Refused: cannot read the env file {settings.env_file} ({error}). {NOTHING_WRITTEN}"
        ) from error
    plain = [match.group(1) for line in lines if (match := _DATABASE_URL_LINE.fullmatch(line))]
    assignments = sum(1 for line in lines if _DATABASE_URL_ASSIGNMENT.match(line))
    if len(plain) != 1 or assignments != 1:
        raise PurgeRefusal(
            f"Refused: the env file {settings.env_file} must hold exactly one line {DATABASE_URL_ENV}=<value> "
            f"(unquoted, the whole line); it has {len(plain)} such lines and {assignments} lines assigning "
            f"{DATABASE_URL_ENV} in any form. {NOTHING_WRITTEN}"
        )
    try:
        value = plain[0].decode("utf-8")
    except UnicodeDecodeError as error:
        raise PurgeRefusal(
            f"Refused: the {DATABASE_URL_ENV} line of {settings.env_file} is not UTF-8. {NOTHING_WRITTEN}"
        ) from error
    if not value or value.startswith(_QUOTES) or value.endswith(_QUOTES):
        raise PurgeRefusal(
            f"Refused: the {DATABASE_URL_ENV} line of {settings.env_file} must be {DATABASE_URL_ENV}=<value>, "
            f"non-empty and unquoted. {NOTHING_WRITTEN}"
        )
    if value != settings.database_url:
        raise PurgeRefusal(
            f"Refused: {DATABASE_URL_ENV} of this process is not the one in {settings.env_file}. The tool "
            f"deletes: it must be bound to the ingest database of that file. Load {DATABASE_URL_ENV} from it "
            f"(source it, or cut its line). {NOTHING_WRITTEN}"
        )


def read_manifest(path: Path) -> Manifest:
    """The model ids of the canonical manifest; ``ValueError`` when it does not name every one of its models."""

    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(f"the canonical manifest {path} cannot be read ({error})") from error
    try:
        document = json.loads(content)
    except ValueError as error:
        raise ValueError(f"the canonical manifest {path} is not JSON ({error})") from error
    models = document.get("models") if isinstance(document, Mapping) else None
    if not isinstance(models, list) or not models:
        raise ValueError(f"the canonical manifest {path} has no models list, or an empty one")
    model_ids: list[str] = []
    for index, row in enumerate(models):
        model_id = row.get("model_id") if isinstance(row, Mapping) else None
        if not isinstance(model_id, str) or not model_id:
            # A row whose model cannot be told is not proof that a model is out of the manifest.
            raise ValueError(f"the canonical manifest {path} has a row without a model_id (models[{index}])")
        model_ids.append(model_id)
    return Manifest(model_ids=tuple(sorted(set(model_ids))), sha256=hashlib.sha256(content).hexdigest())


def hold_instance_lock(settings: Settings) -> int:
    """Take the exclusive lock beside the env file for the whole apply; the descriptor is held until exit."""

    path = settings.lock_file
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise PurgeRefusal(f"Refused: cannot take the lock {path} ({error}). {NOTHING_WRITTEN}") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise PurgeRefusal(
            f"Refused: another instance of the weight purge is running (it holds {path}). {NOTHING_WRITTEN}"
        ) from error
    except OSError as error:
        os.close(descriptor)
        raise PurgeRefusal(f"Refused: cannot take the lock {path} ({error}). {NOTHING_WRITTEN}") from error
    return descriptor


def _database_failure(settings: Settings, error: Exception, where: str) -> PurgeFailure:
    text = scrub(f"{type(error).__name__}: {str(error).strip()}", settings.database_url)
    return PurgeFailure(f"The database refused ({where}): {text}")


def _connect(settings: Settings, *, readonly: bool) -> Any:
    """One connection, never in autocommit, READ COMMITTED; read-only for a dry-run before any statement."""

    try:
        connection = psycopg2.connect(settings.database_url, fallback_application_name=APPLICATION_NAME)
    except psycopg2.Error as error:
        raise _database_failure(settings, error, "connecting") from None
    try:
        connection.autocommit = False
        connection.set_session(isolation_level="READ COMMITTED", readonly=readonly)
    except BaseException:
        connection.close()
        raise
    return connection


def _close(connection: Any) -> None:
    # A rollback that fails must not replace what the caller raised; closing discards the transaction anyway.
    try:
        connection.rollback()
    except psycopg2.Error:
        pass
    finally:
        connection.close()


def classify(
    cursor: Any, settings: Settings, manifest: Manifest, *, model_id: str | None = None
) -> list[dict[str, Any]]:
    """``{model_id, rows, class}`` of every model that has weight rows, or of ``model_id`` alone."""

    cursor.execute(CLASSIFY_SQL, (settings.min_idle_days, model_id, model_id, list(manifest.model_ids)))
    rows = [{"model_id": str(row[0]), "rows": int(row[1]), "class": str(row[2])} for row in cursor.fetchall()]
    unknown = sorted({row["class"] for row in rows} - set(CLASSES))
    if unknown:
        raise PurgeFailure(f"The classification returned classes this tool does not know: {unknown}.")
    return rows


def _server_version(cursor: Any) -> str:
    cursor.execute(SERVER_VERSION_SQL)
    return str(cursor.fetchone()[0])


def class_counts(models: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {name: {"models": 0, "rows": 0} for name in CLASSES}
    for model in models:
        counts[model["class"]]["models"] += 1
        counts[model["class"]]["rows"] += model["rows"]
    return counts


def _purgeable(models: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    chosen = [{"model_id": m["model_id"], "rows": m["rows"]} for m in models if m["class"] == PURGEABLE]
    return sorted(chosen, key=lambda model: model["model_id"])


def _thresholds(settings: Settings) -> dict[str, Any]:
    return {
        "min_idle_days": settings.min_idle_days,
        "pause_seconds": settings.pause_seconds,
        "max_models": settings.max_models,
    }


def _manifest_record(settings: Settings, manifest: Manifest) -> dict[str, Any]:
    return {"path": str(settings.manifest), "sha256": manifest.sha256, "model_count": len(manifest.model_ids)}


def dry_run(settings: Settings, manifest: Manifest) -> tuple[int, dict[str, Any]]:
    """Report what an apply would delete.  Read-only connection; creates no file and no directory."""

    connection = _connect(settings, readonly=True)
    try:
        with connection.cursor() as cursor:
            version = _server_version(cursor)
            models = classify(cursor, settings, manifest)
    except psycopg2.Error as error:
        raise _database_failure(settings, error, "classifying") from None
    finally:
        _close(connection)
    purgeable = _purgeable(models)
    largest = max(purgeable, key=lambda model: (model["rows"], model["model_id"]), default=None)
    return 0, {
        "dry_run": True,
        "server_version": version,
        "thresholds": _thresholds(settings),
        "manifest": _manifest_record(settings, manifest),
        "classes": class_counts(models),
        "purgeable_models": purgeable,
        "total_rows": sum(model["rows"] for model in purgeable),
        "largest_model": largest,
        # An apply fails at the first of these: their ids cannot be rendered into the COPY statement.
        "model_ids_an_apply_cannot_render": [
            m["model_id"] for m in purgeable if not MODEL_ID_PATTERN.fullmatch(m["model_id"])
        ],
        "receipt_root": str(settings.receipt_root),
    }


def _purge_statement(model_id: str, pairs: Sequence[tuple[str, str]]) -> sql.Composed:
    return sql.SQL(PURGE_COPY_SQL).format(
        model_id=sql.Literal(model_id),
        pairs=sql.SQL(", ").join(
            sql.SQL("({}, {})").format(sql.Literal(source_id), sql.Literal(grid_id)) for source_id, grid_id in pairs
        ),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in WEIGHT_COLUMNS),
    )


def _backup_data_rows(path: Path) -> int:
    """The data rows of a backup, read back from the file; the header must be the column list."""

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != list(WEIGHT_COLUMNS):
            raise PurgeFailure(f"The backup {path} does not start with the column list of this tool.")
        return sum(1 for _row in reader)


def purge_model(
    connection: Any, settings: Settings, manifest: Manifest, run_directory: Path, model_id: str
) -> dict[str, Any]:
    """Purge one model in one transaction, or skip it; the record of its ``model-<nnnn>.json``.

    Raises ``PurgeFailure``; nothing of the model is deleted unless its ``outcome`` says otherwise.
    """

    if not MODEL_ID_PATTERN.fullmatch(model_id):
        # Before any statement: the id would have to be rendered into the COPY statement.
        raise PurgeFailure(
            f"model_id {model_id!r} does not match {MODEL_ID_PATTERN.pattern}; no statement was sent for it."
        )
    backup = run_directory / f"weights-{model_id}.csv"
    commit = CommitMarker()
    created = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(LOCK_TIMEOUT_SQL)
            cursor.execute(PAIRS_SQL, (model_id,))
            pairs = sorted((str(row[0]), str(row[1])) for row in cursor.fetchall())
            for source_id, grid_id in pairs:
                cursor.execute(ADVISORY_LOCK_SQL, (lock_key(source_id, grid_id, model_id),))
            # The rule again, under the locks: no writer can replace a locked scope from here to the commit.
            found = classify(cursor, settings, manifest, model_id=model_id)
            current = found[0] if found else {"rows": 0, "class": NO_WEIGHT_ROWS}
            if current["class"] != PURGEABLE or not pairs:
                connection.rollback()
                skipped_as = current["class"] if current["class"] != PURGEABLE else NO_WEIGHT_ROWS
                return {
                    "model_id": model_id,
                    "status": "skipped",
                    "rows": current["rows"],
                    "backup": None,
                    "sha256": None,
                    "class": skipped_as,
                }
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, BACKUP_MODE)
            created = True
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), BACKUP_MODE)
                # Backup and delete in one statement: the file is exactly the rows that were deleted.
                cursor.copy_expert(_purge_statement(model_id, pairs), handle)
                handle.flush()
                os.fsync(handle.fileno())
            cursor.execute(REMAINING_SQL, (model_id,))
            left = int(cursor.fetchone()[0])
            if left:
                raise PurgeFailure(
                    f"{left} met.interp_weight rows of {model_id} are left after the delete: a writer added a "
                    "scope after the locks were taken. The transaction was rolled back and the backup removed."
                )
            rows = _backup_data_rows(backup)
            if rows < 1:
                raise PurgeFailure(
                    f"The backup of {model_id} holds no row. The transaction was rolled back and the backup removed."
                )
            fsync_directory(run_directory)
        # From here on nothing may conclude "not committed".
        commit.reached = True
        try:
            connection.commit()
        except BaseException as error:  # an interrupt inside the call leaves the outcome unknown too
            text = scrub(f"{type(error).__name__}: {str(error).strip()}", settings.database_url)
            raise PurgeFailure(
                f"The COMMIT of the purge of {model_id} did not return ({text}); whether its rows were deleted is "
                f"not known. The backup {backup} was kept: a dry-run shows whether the model still has rows.",
                outcome="unknown",
                backup=backup,
            ) from None
        try:
            sha256 = succession.file_sha256(backup)
        except OSError as error:
            raise PurgeFailure(
                f"The rows of {model_id} were deleted and committed, but its backup {backup} could not be read "
                f"back ({error}). The backup was kept.",
                outcome="committed",
                backup=backup,
            ) from error
    except BaseException as error:
        if not commit.reached:
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            if created:
                # Nothing was committed: the file is not the backup of anything.
                backup.unlink(missing_ok=True)
        if isinstance(error, psycopg2.Error):
            raise _database_failure(settings, error, f"purging {model_id}") from None
        if isinstance(error, OSError):
            raise PurgeFailure(f"The backup {backup} of {model_id} could not be written ({error}).") from error
        raise
    return {
        "model_id": model_id,
        "status": "purged",
        "rows": rows,
        "backup": str(backup),
        "sha256": sha256,
        "class": PURGEABLE,
    }


def _header(settings: Settings, schema_version: str) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "operator_id": settings.operator_id,
        "reason": settings.reason,
        "thresholds": _thresholds(settings),
        "git_commit": succession.git_commit(),
    }


def _totals(done: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    purged = [record for record in done if record["status"] == "purged"]
    return {
        "models_purged": len(purged),
        "rows_purged": sum(record["rows"] for record in purged),
        "models_skipped": len(done) - len(purged),
    }


def _write_final(path: Path, receipt: dict[str, Any], key: str) -> None:
    try:
        succession.write_receipt(path, receipt)
        receipt[key] = str(path)
    except OSError as error:
        receipt[key] = f"could NOT be written: {error}"


def apply(
    settings: Settings, manifest: Manifest, *, sleep: Callable[[float], None] = time.sleep
) -> tuple[int, dict[str, Any]]:
    """Purge the purgeable models, one transaction each; stop at the first failure."""

    connection = _connect(settings, readonly=False)
    try:
        try:
            with connection.cursor() as cursor:
                version = _server_version(cursor)
                models = classify(cursor, settings, manifest)
            connection.rollback()
        except psycopg2.Error as error:
            raise _database_failure(settings, error, "classifying") from None
        purgeable = [model["model_id"] for model in _purgeable(models)]
        chosen = purgeable if settings.max_models is None else purgeable[: settings.max_models]
        not_reached = purgeable[len(chosen) :]
        run_directory = settings.receipt_root / f"purge-{utc_stamp()}"
        try:
            settings.receipt_root.mkdir(parents=True, exist_ok=True)
            run_directory.mkdir()
        except OSError as error:
            raise PurgeRefusal(
                f"Refused: the run directory {run_directory} cannot be created ({error}). {NOTHING_WRITTEN}"
            ) from error
        common = {
            "run_directory": str(run_directory),
            "server_version": version,
            "manifest_at_start": _manifest_record(settings, manifest),
            "classes_at_start": class_counts(models),
        }
        done: list[dict[str, Any]] = []
        for index, model_id in enumerate(chosen, start=1):
            try:
                if index > 1:
                    sleep(settings.pause_seconds)
                try:
                    # node-22 rewrites the manifest: what protects a model is what it says now.
                    latest = read_manifest(settings.manifest)
                except ValueError as error:
                    raise PurgeFailure(f"Stopped before {model_id}: {error}. Its rows were not touched.") from error
                record = purge_model(connection, settings, latest, run_directory, model_id)
                try:
                    succession.write_receipt(run_directory / f"model-{index:04d}.json", record)
                except OSError as error:
                    done.append(record)
                    raise PurgeFailure(
                        f"The receipt of {model_id} ({record['status']}) could not be written ({error}).",
                        outcome="committed" if record["status"] == "purged" else "rolled_back",
                        backup=Path(record["backup"]) if record["backup"] else None,
                    ) from error
                done.append(record)
            except Exception as error:  # noqa: BLE001 - every failure of a model is recorded; a kill is not caught
                known = isinstance(error, PurgeFailure)
                text = str(error) if known else f"{type(error).__name__}: {error}"
                failure = {
                    **_header(settings, FAILURE_SCHEMA_VERSION),
                    **common,
                    "outcome": "failed",
                    "failed_model": {
                        "model_id": model_id,
                        "error": scrub(text, settings.database_url),
                        "outcome": error.outcome if known else "rolled_back",
                        "backup_kept": str(error.backup) if known and error.backup else None,
                    },
                    "models": done,
                    "totals": _totals(done),
                    "models_not_attempted": [*chosen[index:], *not_reached],
                    "ways_on": [
                        "Earlier models stay purged (their receipts and backups are in this directory); later "
                        "ones are untouched.",
                        "Remove the cause and run the same command: it starts a new run directory and purged "
                        f"models are no longer candidates. Restoring a model: {RUNBOOK}.",
                    ],
                }
                _write_final(run_directory / FAILURE_NAME, failure, "failure_receipt")
                print(
                    f"Weight purge FAILED at {model_id}: {failure['failed_model']['error']}\n"
                    f"Failure receipt: {failure['failure_receipt']}",
                    file=sys.stderr,
                )
                return 1, failure
        receipt = {
            **_header(settings, RECEIPT_SCHEMA_VERSION),
            **common,
            "outcome": "completed",
            "models": done,
            "totals": _totals(done),
            "models_not_reached": not_reached,
        }
        _write_final(run_directory / SUMMARY_NAME, receipt, "receipt")
        return (0 if receipt["receipt"] == str(run_directory / SUMMARY_NAME) else 1), receipt
    finally:
        _close(connection)


def main(argv: Sequence[str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    args = _parse_args(argv)
    report: dict[str, Any]
    try:
        # Everything that needs no database, before any connection.
        settings = settings_from_arguments(args)
        check_database_binding(settings)
        try:
            manifest = read_manifest(settings.manifest)
        except ValueError as error:
            raise PurgeRefusal(f"Refused: {error}. {NOTHING_WRITTEN}") from error
        if args.apply:
            lock = hold_instance_lock(settings)
            try:
                status, report = apply(settings, manifest, sleep=sleep)
            finally:
                os.close(lock)
        else:
            print(DRY_RUN_NOTICE, file=sys.stderr, flush=True)
            status, report = dry_run(settings, manifest)
    except (PurgeRefusal, PurgeFailure) as error:
        # Before any model: nothing was deleted.
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
