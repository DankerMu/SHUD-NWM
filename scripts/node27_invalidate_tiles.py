#!/usr/bin/env python3
"""Scoped MVT tile invalidation for the six-basin replay (#1164 change 2, design D6.2).

After the replayed runs are re-ingested on node-27, the tiles rendered from the
OLD data must go.  ``map.tile_cache`` has no basin dimension, so the finest
admissible knife is ``(source_id, valid_time inside the replay window)``:

* ``source_id`` -- for hydro layers this column carries the RUN id
  (``apps/api/routes/hydro_display.py:313-321`` passes ``source_id=run_id``), so
  the scope is given either as explicit ``--source-id`` values or harvested from
  a replacement receipt's ``rows[].run_id`` (``--from-replacement-receipt``,
  REPEATABLE -- the replay writes one receipt per source per phase and the run-id
  sets are unioned; a receipt whose ``outcome`` is not ``completed`` is refused
  because it names only the cycles that finished).  Nothing is ever matched by
  prefix or wildcard.
* ``valid_time`` -- inclusive window; rows with a NULL ``valid_time`` (the
  river-network / station layers) are never in scope.

Both cache tiers must go together, because either one alone still serves the
stale tile: the DB row (``services/tiles/mvt.py:_read_cache``) and the file cache
entry ``<NHMS_MVT_FILE_CACHE_DIR>/<key[:2]>/<key>.pbf``
(``services/tiles/mvt.py:_file_cache_path``).  Order is file first, DB second,
and a key whose file leg could not be completed keeps BOTH tiers: that leaves the
row re-runnable instead of stranding an unreachable stale file.  Such keys make
the run ``incomplete`` (exit 3) with the keys named in the receipt.

The file-cache ROOT itself is prechecked three ways before anything is read from
the database: it must exist and be a directory.  A confirmed-absent root, a root
that is not a directory, and a root that cannot be probed are all refusals -- a
mistyped ``--file-cache-dir`` would otherwise make every per-key probe report
``absent`` and delete the DB rows while the stale files keep being served.

Dry run is the default and writes nothing at all: it reports the candidate rows
and their file-cache probe verdicts, and still publishes a receipt.

Exit codes: ``0`` completed (or dry run), ``2`` refused before any mutation,
``3`` incomplete -- some keys were deliberately left intact, ``4`` failed AFTER
a mutation could have started (file-cache entries were unlinked, or a DELETE
scope was staged for this transaction, and the delete/commit or the receipt
write did not complete).  Exit 4 always echoes a receipt on stdout naming every
unlinked entry, and writes it to ``--receipt-path`` when that is still possible.

A SIGNAL interrupt (Ctrl-C / SIGINT) does NOT exit 4: the interrupt is re-raised
after the receipt is written and echoed, so the process ends with the signal's
own default (130 for SIGINT) and prints a traceback.  The receipt -- on stdout
and at ``--receipt-path``, ``failure.reason`` ``interrupted_after_file_unlink``
(or ``interrupted_delete_scope_uncertain`` when no file cache entry was
unlinked) -- is the authoritative record of what was mutated, not the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)

SCHEMA_VERSION = "nhms.replay_tile_invalidation.v1"
FILE_CACHE_DIR_ENV = "NHMS_MVT_FILE_CACHE_DIR"
DATABASE_URL_ENV = "DATABASE_URL"

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 120_000
LOCK_TIMEOUT_MS = 5_000

MAX_RECEIPT_ENTRIES = 20_000
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_SOURCE_IDS = 4096

PROBE_PRESENT = "present"
PROBE_ABSENT = "absent"
PROBE_UNDETERMINABLE = "undeterminable"

_CACHE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")

TABLE_PRESENT_SQL = "SELECT to_regclass('map.tile_cache')"
CANDIDATE_SQL = """
SELECT cache_key, layer_id, z, x, y, source_id, valid_time
FROM map.tile_cache
WHERE source_id = ANY(%(source_ids)s)
  AND valid_time IS NOT NULL
  AND valid_time >= %(window_start)s
  AND valid_time <= %(window_end)s
ORDER BY cache_key
"""
DELETE_SQL = "DELETE FROM map.tile_cache WHERE cache_key = ANY(%(cache_keys)s)"


class TileInvalidationRefused(RuntimeError):
    """Refused before any mutation: exit 2."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": "refused", "refusal_reason": self.reason, **self.details}


class TileInvalidationIncomplete(RuntimeError):
    """Some keys were deliberately left intact; the receipt names them: exit 3."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        super().__init__("incomplete")
        self.receipt: dict[str, Any] = dict(receipt)


class TileInvalidationPartialMutation(RuntimeError):
    """Something was already mutated and the run could not finish: exit 4.

    The two reachable shapes are a DB delete/commit that failed after file-cache
    entries were unlinked, and a receipt write that failed after the delete was
    committed.  Both carry the receipt -- including the unlinked entries -- so a
    bare traceback can never be the only record of a partial mutation.
    """

    def __init__(
        self,
        reason: str,
        *,
        receipt: Mapping[str, Any],
        receipt_written: bool,
        detail: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt: dict[str, Any] = dict(receipt)
        self.receipt_written = bool(receipt_written)
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": "failed_partial_mutation",
            "failure_reason": self.reason,
            "receipt_written": self.receipt_written,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# file cache
# ---------------------------------------------------------------------------


def file_cache_path(root: Path | None, cache_key: str) -> Path | None:
    """Mirror of ``services.tiles.mvt._file_cache_path`` (same layout, no follow)."""

    if root is None or not _CACHE_KEY_PATTERN.fullmatch(cache_key):
        return None
    return root / cache_key[:2] / f"{cache_key}.pbf"


def probe_file_cache_root(root: Path | None) -> dict[str, Any]:
    """Three-way precheck of the configured file-cache root.

    ``present`` only when the root exists AND is a directory.  Anything else is
    a refusal upstream: with a wrong root every per-key probe answers ``absent``
    and the run would delete the DB rows while the real files keep serving
    stale tiles (round-1 C-F4).
    """

    if root is None:
        return {"path": None, "status": PROBE_ABSENT, "detail": "file cache not configured"}
    try:
        metadata = stat_no_follow(root)
    except FileNotFoundError:
        return {"path": str(root), "status": PROBE_ABSENT, "detail": "file cache root does not exist"}
    except (OSError, SafeFilesystemError) as error:
        return {"path": str(root), "status": PROBE_UNDETERMINABLE, "detail": f"stat failed: {error}"}
    if not stat.S_ISDIR(metadata.st_mode):
        return {"path": str(root), "status": PROBE_ABSENT, "detail": "file cache root is not a directory"}
    return {"path": str(root), "status": PROBE_PRESENT, "detail": None}


def probe_file_cache(root: Path | None, cache_key: str) -> dict[str, Any]:
    """Three-way probe: an unreadable entry is never a completed negative."""

    path = file_cache_path(root, cache_key)
    if root is None:
        return {"path": None, "status": PROBE_ABSENT, "detail": "file cache not configured"}
    if path is None:
        return {"path": None, "status": PROBE_ABSENT, "detail": "cache_key is not a file-cache key"}
    try:
        metadata = stat_no_follow(path)
    except FileNotFoundError:
        return {"path": str(path), "status": PROBE_ABSENT, "detail": None}
    except (OSError, SafeFilesystemError) as error:
        return {"path": str(path), "status": PROBE_UNDETERMINABLE, "detail": f"stat failed: {error}"}
    if not stat.S_ISREG(metadata.st_mode):
        return {"path": str(path), "status": PROBE_UNDETERMINABLE, "detail": "not a regular file"}
    return {"path": str(path), "status": PROBE_PRESENT, "detail": None}


def _unlink_file_cache(path: Path) -> tuple[bool, str | None]:
    try:
        os.unlink(path)  # noqa: PTH108 - no-follow removal of the exact entry
    except FileNotFoundError:
        return True, None
    except OSError as error:
        return False, str(error)
    return True, None


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


def _connect_default(database_url: str) -> Any:
    import psycopg2  # type: ignore[import-untyped]

    return psycopg2.connect(database_url, connect_timeout=CONNECT_TIMEOUT_SECONDS)


def _candidate_rows(
    cursor: Any,
    *,
    source_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    cursor.execute(
        CANDIDATE_SQL,
        {
            "source_ids": list(source_ids),
            "window_start": window_start,
            "window_end": window_end,
        },
    )
    rows = cursor.fetchall() or []
    resolved: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            values = dict(row)
        else:
            values = dict(zip(("cache_key", "layer_id", "z", "x", "y", "source_id", "valid_time"), row, strict=False))
        resolved.append(values)
    return resolved


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def invalidate_tiles(
    *,
    source_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    receipt_path: Path,
    file_cache_dir: Path | None,
    execute: bool,
    database_url: str,
    connect: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    # Resolved at call time (not as a default argument) so the connection
    # boundary can be substituted module-wide, CLI entry point included.
    connect = connect or _connect_default
    started_at = datetime.now(tz=UTC)
    resolved_sources = sorted({str(source_id).strip() for source_id in source_ids if str(source_id).strip()})
    if not resolved_sources:
        raise TileInvalidationRefused("source_ids_missing")
    if len(resolved_sources) > MAX_SOURCE_IDS:
        raise TileInvalidationRefused(
            "source_ids_limit_exceeded",
            {"count": len(resolved_sources), "limit": MAX_SOURCE_IDS},
        )
    if window_end < window_start:
        raise TileInvalidationRefused(
            "window_inverted",
            {"window_start": _format_time(window_start), "window_end": _format_time(window_end)},
        )
    if not database_url:
        raise TileInvalidationRefused("database_url_missing", {"env": DATABASE_URL_ENV})
    if file_cache_dir is not None:
        root_probe = probe_file_cache_root(file_cache_dir)
        if root_probe["status"] != PROBE_PRESENT:
            raise TileInvalidationRefused(
                "file_cache_dir_absent"
                if root_probe["status"] == PROBE_ABSENT
                else "file_cache_dir_undeterminable",
                {"file_cache_dir": str(file_cache_dir), "probe": root_probe},
            )

    unlinked_paths: list[str] = []
    candidates: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    # Bound OUTSIDE the transaction block: the failure arms below read it to
    # decide whether a DB mutation may already be in flight, and a deployment
    # with no file cache reaches those arms with nothing unlinked (round-3 C3-3).
    deletable_keys: list[str] = []
    deleted_rows: int | None = 0

    def mutation_may_be_in_flight() -> bool:
        """True when this run could already have changed something.

        Either file-cache entries are gone, or a DELETE scope was staged for a
        transaction that is being torn down with an unknown fate (round-3 C3-3).
        A DRY RUN never qualifies: it stages a scope for the report and issues
        nothing, so its failures stay bare re-raises rather than a receipt
        claiming a mutation that could not have happened.
        """

        return bool(unlinked_paths) or bool(execute and deletable_keys)

    def partial_mutation(
        reason: str,
        *,
        error: BaseException,
        rows_deleted: int | None,
    ) -> TileInvalidationPartialMutation:
        """Receipt-first record of a mutation that already happened."""

        receipt = _receipt_payload(
            outcome="failed_partial_mutation",
            executed=execute,
            started_at=started_at,
            resolved_sources=resolved_sources,
            window_start=window_start,
            window_end=window_end,
            file_cache_dir=file_cache_dir,
            candidates=candidates,
            entries=entries,
            blocked=blocked,
            deleted_rows=rows_deleted,
            unlinked_paths=unlinked_paths,
            failure={"reason": reason, "detail": str(error)},
        )
        written, write_error = _try_write_receipt(receipt_path, receipt)
        return TileInvalidationPartialMutation(
            reason,
            receipt=receipt,
            receipt_written=written,
            detail=str(error) if written else f"{error}; receipt write failed: {write_error}",
        )

    connection = connect(database_url)
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cursor.execute(f"SET lock_timeout = {LOCK_TIMEOUT_MS}")
                cursor.execute(TABLE_PRESENT_SQL)
                table = cursor.fetchone()
                if table is None or (table[0] if not isinstance(table, Mapping) else table.get("to_regclass")) is None:
                    raise TileInvalidationRefused("tile_cache_table_absent")
                candidates = _candidate_rows(
                    cursor,
                    source_ids=resolved_sources,
                    window_start=window_start,
                    window_end=window_end,
                )

                for row in candidates:
                    cache_key = str(row.get("cache_key") or "")
                    probe = probe_file_cache(file_cache_dir, cache_key)
                    entry = {
                        "cache_key": cache_key,
                        "layer_id": row.get("layer_id"),
                        "source_id": row.get("source_id"),
                        "valid_time": _iso_value(row.get("valid_time")),
                        "tile": [row.get("z"), row.get("x"), row.get("y")],
                        "file_cache": probe,
                        "file_cache_action": "planned" if execute else "none",
                        "row_action": "planned" if execute else "none",
                    }
                    if probe["status"] == PROBE_UNDETERMINABLE:
                        # Fail closed: leave BOTH tiers so the key stays re-runnable.
                        entry["file_cache_action"] = "blocked"
                        entry["row_action"] = "blocked"
                        blocked.append(entry)
                        entries.append(entry)
                        continue
                    if execute:
                        if probe["status"] == PROBE_PRESENT:
                            removed, detail = _unlink_file_cache(Path(str(probe["path"])))
                            if not removed:
                                entry["file_cache_action"] = "blocked"
                                entry["row_action"] = "blocked"
                                entry["file_cache"] = {**probe, "detail": detail}
                                blocked.append(entry)
                                entries.append(entry)
                                continue
                            entry["file_cache_action"] = "deleted"
                            unlinked_paths.append(str(probe["path"]))
                        else:
                            entry["file_cache_action"] = "absent"
                    deletable_keys.append(cache_key)
                    entries.append(entry)

                deleted_rows = 0
                if execute and deletable_keys:
                    cursor.execute(DELETE_SQL, {"cache_keys": deletable_keys})
                    deleted_rows = int(getattr(cursor, "rowcount", 0) or 0)
        except Exception as error:
            _rollback(connection)
            if not mutation_may_be_in_flight():
                raise
            # Something may already be in flight: file-cache entries are gone,
            # or a DELETE scope was staged/issued for this transaction.  Emit a
            # receipt naming it instead of dying with a bare traceback.  The
            # DELETE never committed, so zero rows is a fact here.
            raise partial_mutation(
                "db_delete_failed_after_file_unlink" if unlinked_paths else "db_delete_failed",
                error=error,
                rows_deleted=0,
            ) from error
        if execute:
            # The commit is its OWN failure class: once it has been issued the
            # transaction's fate is genuinely unknown (the server may have
            # committed and lost the connection before answering).  Reporting
            # `deleted_rows: 0` here would state a negative nobody verified.
            try:
                connection.commit()
            except Exception as error:
                _rollback(connection)
                if not mutation_may_be_in_flight():
                    raise
                raise partial_mutation(
                    "db_commit_uncertain_after_file_unlink" if unlinked_paths else "db_commit_uncertain",
                    error=error,
                    rows_deleted=None,
                ) from error
    except TileInvalidationPartialMutation:
        raise
    except BaseException as error:
        # KeyboardInterrupt / SystemExit in the middle of the unlink loop escape
        # every `except Exception` above; without this arm the files are gone and
        # a bare traceback is the only record (round-2 C2-6).  A signal that
        # arrives with a DELETE scope staged and nothing unlinked (a deployment
        # with no file cache) is just as uncertain, so it gets a receipt too
        # (round-3 C3-3) -- under a reason that does not claim an unlink.
        _rollback(connection)
        if not mutation_may_be_in_flight():
            raise
        failure = partial_mutation(
            "interrupted_after_file_unlink" if unlinked_paths else "interrupted_delete_scope_uncertain",
            error=error,
            rows_deleted=None,
        )
        print(json.dumps(failure.receipt, ensure_ascii=False, sort_keys=True, default=str))
        raise
    finally:
        _close(connection)

    for entry in entries:
        if entry["row_action"] == "planned" and execute:
            entry["row_action"] = "deleted"

    receipt = _receipt_payload(
        outcome="incomplete" if blocked else "completed",
        executed=execute,
        started_at=started_at,
        resolved_sources=resolved_sources,
        window_start=window_start,
        window_end=window_end,
        file_cache_dir=file_cache_dir,
        candidates=candidates,
        entries=entries,
        blocked=blocked,
        deleted_rows=deleted_rows,
        unlinked_paths=unlinked_paths,
        failure=None,
    )
    written, write_error = _try_write_receipt(receipt_path, receipt)
    if not written:
        if unlinked_paths or deleted_rows:
            # The delete is committed; the only remaining duty is to surface the
            # record, which main() echoes on stdout under exit 4.
            raise TileInvalidationPartialMutation(
                "receipt_write_failed_after_commit",
                receipt=receipt,
                receipt_written=False,
                detail=write_error,
            )
        raise TileInvalidationRefused(
            "receipt_write_failed",
            {"path": str(receipt_path), "error": write_error},
        )
    if blocked:
        raise TileInvalidationIncomplete(receipt)
    return receipt


def _receipt_payload(
    *,
    outcome: str,
    executed: bool,
    started_at: datetime,
    resolved_sources: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    file_cache_dir: Path | None,
    candidates: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    blocked: Sequence[Mapping[str, Any]],
    deleted_rows: int | None,
    unlinked_paths: Sequence[str],
    failure: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "executed": bool(executed),
        "started_at": _format_time(started_at),
        "finished_at": _format_time(datetime.now(tz=UTC)),
        "scope": {
            "source_ids": list(resolved_sources),
            "window_start": _format_time(window_start),
            "window_end": _format_time(window_end),
            "file_cache_dir": str(file_cache_dir) if file_cache_dir is not None else None,
        },
        "totals": {
            "candidate_rows": len(candidates),
            "deleted_rows": deleted_rows,
            "file_cache_deleted": sum(1 for entry in entries if entry["file_cache_action"] == "deleted"),
            "file_cache_absent": sum(1 for entry in entries if entry["file_cache"]["status"] == PROBE_ABSENT),
            "file_cache_present": sum(1 for entry in entries if entry["file_cache"]["status"] == PROBE_PRESENT),
            "file_cache_undeterminable": sum(
                1 for entry in entries if entry["file_cache"]["status"] == PROBE_UNDETERMINABLE
            ),
            "blocked_keys": len(blocked),
        },
        "blocked_cache_keys": [entry["cache_key"] for entry in blocked][:MAX_RECEIPT_ENTRIES],
        # Every list in this receipt is bounded, so every list says whether it
        # was cut: the runbook treats the unlinked-path list as the authoritative
        # record of what was removed (round-2 C2-3).
        "blocked_cache_keys_truncated": len(blocked) > MAX_RECEIPT_ENTRIES,
        "unlinked_file_cache_paths": list(unlinked_paths)[:MAX_RECEIPT_ENTRIES],
        "unlinked_file_cache_paths_truncated": len(unlinked_paths) > MAX_RECEIPT_ENTRIES,
        "failure": dict(failure) if failure is not None else None,
        "entries": list(entries)[:MAX_RECEIPT_ENTRIES],
        "entries_truncated": len(entries) > MAX_RECEIPT_ENTRIES,
    }


def _try_write_receipt(path: Path, receipt: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Never raise: the receipt is the record of a mutation that already happened."""

    try:
        _write_receipt(path, receipt)
    except (OSError, SafeFilesystemError) as error:
        return False, str(error)
    return True, None


def _rollback(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:  # pragma: no cover - best effort on a broken connection
        pass


def _close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:  # pragma: no cover - best effort on a broken connection
        pass


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"
    ensure_directory_no_follow(path.parent)
    atomic_write_bytes_no_follow(path, content, mode=0o600)


def _iso_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _format_time(value)
    return str(value)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(token: str) -> datetime:
    text = str(token).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise TileInvalidationRefused("time_token_invalid", {"token": token}) from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


REPLACEMENT_RECEIPT_SCHEMA_VERSION = "nhms.production_replay_replacement.v1"
#: The only replacement outcome whose run-id set is the WHOLE replayed scope; an
#: ``in_progress``/``halted`` receipt names the cycles that finished before the
#: interruption and would silently narrow the invalidation (round-2 C2-1).
REPLACEMENT_RECEIPT_COMPLETED_OUTCOME = "completed"


def source_ids_from_replacement_receipts(paths: Sequence[Path]) -> list[str]:
    """Union of the replaced run ids across every replacement receipt.

    The replay produces one receipt per source per phase (four in total for the
    six-basin window), and every one of them names a disjoint slice of the
    replayed runs: invalidating with a single receipt leaves the rest of the
    replayed run ids serving tiles rendered from the pre-replay data.
    """

    resolved: set[str] = set()
    for path in paths:
        resolved.update(source_ids_from_replacement_receipt(path))
    return sorted(resolved)


def source_ids_from_replacement_receipt(path: Path) -> list[str]:
    """Harvest the exact replaced run ids from a replacement receipt."""

    try:
        content = read_bytes_limited_no_follow(path, max_bytes=MAX_RECEIPT_BYTES)
    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
        raise TileInvalidationRefused("replacement_receipt_unreadable", {"path": str(path)}) from error
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TileInvalidationRefused("replacement_receipt_unreadable", {"path": str(path)}) from error
    if not isinstance(payload, Mapping) or payload.get("schema_version") != REPLACEMENT_RECEIPT_SCHEMA_VERSION:
        raise TileInvalidationRefused("replacement_receipt_schema_unsupported", {"path": str(path)})
    outcome = str(payload.get("outcome") or "")
    if outcome != REPLACEMENT_RECEIPT_COMPLETED_OUTCOME:
        raise TileInvalidationRefused(
            "replacement_receipt_not_completed",
            {"path": str(path), "outcome": outcome or None},
        )
    run_ids = sorted(
        {
            str(row.get("run_id"))
            for row in payload.get("rows") or []
            if isinstance(row, Mapping) and str(row.get("run_id") or "").strip()
        }
    )
    if not run_ids:
        raise TileInvalidationRefused("replacement_receipt_has_no_rows", {"path": str(path)})
    return run_ids


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        dest="source_ids",
        help="Exact map.tile_cache.source_id value (repeatable); for hydro layers this is the run id.",
    )
    parser.add_argument(
        "--from-replacement-receipt",
        action="append",
        type=Path,
        default=[],
        dest="from_replacement_receipts",
        help=(
            "Harvest the source ids from a replacement receipt's rows[].run_id (repeatable; "
            "the run-id sets are unioned, so one invocation covers every phase's receipt)."
        ),
    )
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    parser.add_argument(
        "--file-cache-dir",
        type=Path,
        default=None,
        help=f"Defaults to ${FILE_CACHE_DIR_ENV}; pass --no-file-cache when the deployment has none.",
    )
    parser.add_argument(
        "--no-file-cache",
        action="store_true",
        help="Declare that this deployment serves no file cache (otherwise an unset dir is refused).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the deletion; without it nothing is deleted.",
    )
    return parser


def _resolve_file_cache_dir(args: argparse.Namespace) -> Path | None:
    if args.no_file_cache:
        return None
    candidate = args.file_cache_dir
    if candidate is None:
        raw = os.getenv(FILE_CACHE_DIR_ENV, "").strip()
        candidate = Path(raw) if raw else None
    if candidate is None:
        # Refuse rather than silently skip a tier that still serves stale tiles.
        raise TileInvalidationRefused("file_cache_dir_unset", {"env": FILE_CACHE_DIR_ENV})
    return candidate.expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        source_ids = list(args.source_ids)
        source_ids.extend(source_ids_from_replacement_receipts(list(args.from_replacement_receipts)))
        receipt = invalidate_tiles(
            source_ids=source_ids,
            window_start=parse_time(args.window_start),
            window_end=parse_time(args.window_end),
            receipt_path=args.receipt_path,
            file_cache_dir=_resolve_file_cache_dir(args),
            execute=bool(args.execute),
            database_url=os.getenv(DATABASE_URL_ENV, ""),
        )
    except TileInvalidationIncomplete as error:
        print(json.dumps(error.receipt, ensure_ascii=False, sort_keys=True, default=str))
        return 3
    except TileInvalidationPartialMutation as error:
        # Declared exit code, receipt on stdout: a partial mutation must never
        # surface as a bare traceback under exit 1.
        print(json.dumps(error.receipt, ensure_ascii=False, sort_keys=True, default=str))
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr)
        return 4
    except TileInvalidationRefused as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
