#!/usr/bin/env python3
"""Reparse in-window legacy-routed river runs into the narrow store (#2382).

After the I8 re-forward every run parsed before T0 is routed ``legacy`` and its
facts live only in ``hydro.river_timeseries_legacy``. This lane re-runs the
production output parser for each such run from its SHUD ``.rivqdown``
artifact and flips the route in the SAME transaction, so readers see either
the legacy route with legacy facts or the narrow route with narrow facts --
never a narrow route without facts. Runs whose window has passed are left
``legacy`` (their facts are retention-expired anyway; the contract drops them).

Commands:
  plan    read-only inventory and estimate
  run     backfill (requires ``--go``); holds the timeseries lifecycle mutex
  verify  read-only legacy-vs-narrow value comparison for reparsed runs

Environment: ``DATABASE_URL`` (ingest write role for ``run``, any reader for
``plan``/``verify``), ``OBJECT_STORE_ROOT``, ``OBJECT_STORE_PREFIX``,
``NODE27_TIMESERIES_RETENTION_WINDOW_DAYS``. DSNs never enter argv or receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.common.node27_timeseries_lifecycle_lock import (  # noqa: E402
    LifecycleLockError,
    acquire_timeseries_lifecycle_lock,
    refuse_lifecycle_lock_env_override,
    release_timeseries_lifecycle_lock,
)

TOOL_VERSION = "node27-river-narrow-reparse-backfill/1"
GO_TOKEN = "Danker"
APPLICATION_NAME = "nhms-node27-river-narrow-reparse-backfill"
ELIGIBLE_STATUSES = ("published", "superseded")
CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MS = 600_000
LOCK_TIMEOUT_MS = 10_000
DEFAULT_MAX_FAILURES = 10
DEFAULT_MAX_DECOMPRESS_BYTES = 20 * 1024**3

EXIT_COMPLETE = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_PARTIAL = 3

CANDIDATES_SQL = """
SELECT h.run_id, h.status, h.cycle_time, h.start_time, h.end_time, rnv.segment_count
FROM hydro.hydro_run h
JOIN core.model_instance mi ON mi.model_id = h.model_id
JOIN core.river_network_version rnv ON rnv.river_network_version_id = mi.river_network_version_id
WHERE h.timeseries_store = 'legacy'
  AND h.parsed_at IS NOT NULL
  AND h.status IN %(statuses)s
  AND h.end_time > now() - make_interval(days => %(window_days)s)
  AND (%(end_time_after)s::timestamptz IS NULL OR h.end_time > %(end_time_after)s::timestamptz)
ORDER BY h.cycle_time DESC NULLS LAST, h.run_id
"""

LEGACY_ROUTE_COUNTS_SQL = """
SELECT (h.end_time > now() - make_interval(days => %(window_days)s)) AS in_window, h.status, count(*) AS runs
FROM hydro.hydro_run h
WHERE h.timeseries_store = 'legacy'
GROUP BY 1, 2
ORDER BY 1, 2
"""

COMPRESSED_OVERLAP_SQL = """
SELECT c.chunk_schema, c.chunk_name, c.range_start, c.range_end,
       COALESCE(s.before_compression_total_bytes, 0) AS before_compression_total_bytes
FROM timescaledb_information.chunks c
LEFT JOIN chunk_compression_stats('hydro.river_timeseries') s
  ON s.chunk_schema = c.chunk_schema AND s.chunk_name = c.chunk_name
WHERE c.hypertable_schema = 'hydro'
  AND c.hypertable_name = 'river_timeseries'
  AND c.is_compressed
  AND c.range_end > %(window_start)s
  AND c.range_start <= %(window_end)s
ORDER BY c.range_start
"""

LOCK_RUN_SQL = """
SELECT run_key, status, timeseries_store, parsed_at,
       end_time > now() - make_interval(days => %(window_days)s) AS in_window
FROM hydro.hydro_run
WHERE run_id = %(run_id)s
FOR UPDATE
"""

VERIFY_SQL = """
SELECT
  (SELECT count(*) FROM hydro.river_timeseries n WHERE n.run_key = %(run_key)s) AS narrow_rows,
  (SELECT count(*) FROM hydro.river_timeseries_legacy l WHERE l.run_id = %(run_id)s) AS legacy_rows,
  (SELECT count(*)
     FROM hydro.river_timeseries_legacy l
     JOIN core.river_segment s
       ON s.river_segment_id = l.river_segment_id
      AND s.river_network_version_id = l.river_network_version_id
     LEFT JOIN hydro.river_timeseries n
       ON n.run_key = %(run_key)s
      AND n.river_segment_key = s.river_segment_key
      AND n.variable_e::text = l.variable
      AND n.valid_time = l.valid_time
    WHERE l.run_id = %(run_id)s
      AND (n.value IS DISTINCT FROM l.value
           OR n.unit_e::text IS DISTINCT FROM l.unit
           OR n.quality_flag_e::text IS DISTINCT FROM l.quality_flag
           OR n.lead_time_hours IS DISTINCT FROM l.lead_time_hours)) AS mismatched_rows
"""


class RefusedError(RuntimeError):
    """Typed refusal: nothing was mutated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    database_url: str
    object_store_root: str
    object_store_prefix: str
    window_days: int
    max_flow_m3s: float = 100_000.0
    batch_size: int = 1000


@dataclass(frozen=True)
class Outcome:
    run_id: str
    disposition: str
    rows_written: int = 0
    seconds: float = 0.0
    error_code: str | None = None
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


def settings_from_env(env: Mapping[str, str]) -> Settings:
    missing = [
        name
        for name in ("DATABASE_URL", "OBJECT_STORE_ROOT", "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS")
        if not str(env.get(name, "")).strip()
    ]
    if missing:
        raise RefusedError("CONFIG_MISSING", f"missing environment: {', '.join(missing)}")
    raw_window = str(env["NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"]).strip()
    if not raw_window.isdigit() or int(raw_window) < 1:
        raise RefusedError("CONFIG_INVALID", "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS must be a positive integer")
    return Settings(
        database_url=str(env["DATABASE_URL"]).strip(),
        object_store_root=str(env["OBJECT_STORE_ROOT"]).strip(),
        object_store_prefix=str(env.get("OBJECT_STORE_PREFIX", "")).strip(),
        window_days=int(raw_window),
        # Same knobs and defaults as OutputParserConfig.from_env(), so a reparse
        # flags QC exactly like the autopipe parse would.
        max_flow_m3s=float(env.get("OUTPUT_PARSER_MAX_FLOW_M3S", "100000")),
        batch_size=int(env.get("OUTPUT_PARSER_BATCH_SIZE", "1000")),
    )


def connect(database_url: str) -> Any:
    import psycopg2

    connection = psycopg2.connect(
        database_url,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name=APPLICATION_NAME,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}",
    )
    connection.autocommit = False
    return connection


def _fetch(connection: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _iso(value: Any) -> Any:
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch_candidates(connection: Any, window_days: int, end_time_after: datetime | None) -> list[dict[str, Any]]:
    return _fetch(
        connection,
        CANDIDATES_SQL,
        {"statuses": ELIGIBLE_STATUSES, "window_days": window_days, "end_time_after": end_time_after},
    )


def fetch_route_counts(connection: Any, window_days: int) -> list[dict[str, Any]]:
    return [
        {"in_window": row["in_window"], "status": row["status"], "runs": int(row["runs"])}
        for row in _fetch(connection, LEGACY_ROUTE_COUNTS_SQL, {"window_days": window_days})
    ]


def fetch_compressed_overlap(connection: Any, candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    params = {
        "window_start": min(row["start_time"] for row in candidates),
        "window_end": max(row["end_time"] for row in candidates),
    }
    return _fetch(connection, COMPRESSED_OVERLAP_SQL, params)


def estimate_rows(candidates: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for row in candidates:
        hours = int((row["end_time"] - row["start_time"]).total_seconds() // 3600)
        total += int(row["segment_count"] or 0) * hours
    return total


def build_plan(connection: Any, settings: Settings, end_time_after: datetime | None) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
    try:
        candidates = fetch_candidates(connection, settings.window_days, end_time_after)
        overlap = fetch_compressed_overlap(connection, candidates)
        routes = fetch_route_counts(connection, settings.window_days)
    finally:
        connection.rollback()
    return {
        "tool_version": TOOL_VERSION,
        "generated_at": _now(),
        "window_days": settings.window_days,
        "end_time_after": _iso(end_time_after),
        "candidates": len(candidates),
        "candidates_by_status": dict(Counter(row["status"] for row in candidates)),
        "estimated_rows": estimate_rows(candidates),
        "newest_cycle": _iso(candidates[0]["cycle_time"]) if candidates else None,
        "oldest_cycle": _iso(candidates[-1]["cycle_time"]) if candidates else None,
        "legacy_route_counts": routes,
        "compressed_narrow_overlap": [
            {
                "chunk": f"{row['chunk_schema']}.{row['chunk_name']}",
                "range_start": _iso(row["range_start"]),
                "range_end": _iso(row["range_end"]),
                "before_compression_total_bytes": int(row["before_compression_total_bytes"]),
            }
            for row in overlap
        ],
    }


def decompress_overlap(connection: Any, overlap: Sequence[Mapping[str, Any]], max_bytes: int) -> list[str]:
    total = sum(int(row["before_compression_total_bytes"]) for row in overlap)
    if total > max_bytes:
        raise RefusedError(
            "DECOMPRESS_BUDGET_EXCEEDED",
            f"compressed narrow chunks overlapping the backfill hold {total} bytes > budget {max_bytes}",
        )
    done: list[str] = []
    for row in overlap:
        relation = f"{row['chunk_schema']}.{row['chunk_name']}"
        with connection.cursor() as cursor:
            cursor.execute("SELECT decompress_chunk(%s::regclass, if_compressed => true)", (relation,))
        connection.commit()
        done.append(relation)
    return done


def reparse_one(settings: Settings, run_id: str, *, connect_fn: Callable[[str], Any] = connect) -> Outcome:
    """One run, one transaction: lock row, flip route, parse, verify, commit."""

    from packages.common.object_store import LocalObjectStore
    from workers.output_parser.parser import (
        OutputParser,
        OutputParserConfig,
        OutputParsingError,
        PsycopgOutputParserRepository,
    )

    started = time.monotonic()
    connection = connect_fn(settings.database_url)
    try:
        rows = _fetch(connection, LOCK_RUN_SQL, {"run_id": run_id, "window_days": settings.window_days})
        disposition = _skip_disposition(rows[0] if rows else None)
        if disposition is not None:
            connection.rollback()
            return Outcome(run_id, disposition, seconds=time.monotonic() - started)
        before = rows[0]
        with connection.cursor() as cursor:
            cursor.execute("UPDATE hydro.hydro_run SET timeseries_store = 'narrow' WHERE run_id = %s", (run_id,))
        config = OutputParserConfig(
            object_store_root=settings.object_store_root,
            object_store_prefix=settings.object_store_prefix,
            max_flow_m3s=settings.max_flow_m3s,
            batch_size=settings.batch_size,
        )
        parser = OutputParser(
            config=config,
            repository=PsycopgOutputParserRepository(settings.database_url, _connection=connection),
            object_store=LocalObjectStore(config.object_store_root, config.object_store_prefix),
        )
        result = parser.parse_run(run_id)
        after = _fetch(
            connection,
            "SELECT h.status, h.timeseries_store, "
            "(SELECT count(*) FROM hydro.river_timeseries n WHERE n.run_key = h.run_key) AS narrow_rows "
            "FROM hydro.hydro_run h WHERE h.run_id = %(run_id)s",
            {"run_id": run_id},
        )[0]
        problem = _post_parse_problem(before, after, result.rows_written)
        if problem is not None:
            connection.rollback()
            return Outcome(run_id, "failed", seconds=time.monotonic() - started, error_code="VERIFY_MISMATCH",
                           error=problem)
        connection.commit()
        return Outcome(run_id, "reparsed", rows_written=result.rows_written, seconds=time.monotonic() - started)
    except Exception as error:  # every failure is recorded; the transaction never commits
        _rollback_quietly(connection)
        code = error.error_code if isinstance(error, OutputParsingError) else type(error).__name__
        return Outcome(run_id, "failed", seconds=time.monotonic() - started, error_code=code,
                       error=str(error)[:500])
    finally:
        _close_quietly(connection)


def _skip_disposition(row: Mapping[str, Any] | None) -> str | None:
    if row is None:
        return "missing"
    if row["timeseries_store"] != "legacy":
        return "already_narrow"
    if not row["in_window"]:
        return "aged_out"
    if row["status"] not in ELIGIBLE_STATUSES or row["parsed_at"] is None:
        return "ineligible"
    return None


def _post_parse_problem(before: Mapping[str, Any], after: Mapping[str, Any], rows_written: int) -> str | None:
    if rows_written <= 0:
        return "parser wrote no rows"
    if int(after["narrow_rows"]) != rows_written:
        return f"narrow rows {after['narrow_rows']} != parser rows {rows_written}"
    if after["status"] != before["status"]:
        return f"status changed {before['status']} -> {after['status']}"
    if after["timeseries_store"] != "narrow":
        return f"route is {after['timeseries_store']} after parse"
    return None


def _rollback_quietly(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:  # connection may already be gone; the server rolls back
        pass


def _close_quietly(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _worker_init() -> None:
    # The coordinator owns shutdown: an in-flight run finishes or dies by SIGKILL
    # (its transaction then rolls back); it is never interrupted half-handled.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    from packages.common.safe_fs import atomic_write_bytes_no_follow

    atomic_write_bytes_no_follow(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def execute_run(
    settings: Settings,
    *,
    receipt_dir: Path,
    concurrency: int,
    deadline: datetime | None,
    max_failures: int,
    max_decompress_bytes: int,
    limit: int | None,
    end_time_after: datetime | None,
    connect_fn: Callable[[str], Any] = connect,
    lock_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    from workers.output_parser import parser as parser_module

    receipt: dict[str, Any] = {
        "tool_version": TOOL_VERSION,
        "tool_sha256": _file_sha256(__file__),
        "parser_sha256": _file_sha256(parser_module.__file__),
        "started_at": _now(),
        "window_days": settings.window_days,
        "concurrency": concurrency,
        "deadline": _iso(deadline),
        "max_failures": max_failures,
        "limit": limit,
        "end_time_after": _iso(end_time_after),
    }
    lock_fd = acquire_timeseries_lifecycle_lock(lock_path)
    if lock_fd is None:
        raise RefusedError("LIFECYCLE_LOCK_CONTENDED", "timeseries lifecycle lock is held by another lane")
    stop = {"reason": None}

    def _request_stop(signum: int, _frame: Any) -> None:
        stop["reason"] = f"signal_{signum}"

    previous = {sig: signal.signal(sig, _request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        connection = connect_fn(settings.database_url)
        try:
            candidates = fetch_candidates(connection, settings.window_days, end_time_after)
            connection.rollback()
            if limit is not None:
                candidates = candidates[:limit]
            overlap = fetch_compressed_overlap(connection, candidates)
            connection.rollback()
            receipt["decompressed_chunks"] = decompress_overlap(connection, overlap, max_decompress_bytes)
            receipt["legacy_route_counts_before"] = fetch_route_counts(connection, settings.window_days)
            connection.rollback()
        finally:
            _close_quietly(connection)
        receipt["candidates"] = len(candidates)
        receipt["estimated_rows"] = estimate_rows(candidates)
        counts: Counter[str] = Counter()
        rows_written = 0
        jsonl = receipt_dir / "runs.jsonl"
        pending = [str(row["run_id"]) for row in candidates]
        with ProcessPoolExecutor(max_workers=concurrency, initializer=_worker_init) as pool:
            in_flight: set[Future[Outcome]] = set()
            while pending or in_flight:
                while pending and len(in_flight) < concurrency and _may_dispatch(stop, deadline, counts,
                                                                               max_failures):
                    in_flight.add(pool.submit(reparse_one, settings, pending.pop(0), connect_fn=connect_fn))
                if not in_flight:
                    break
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    outcome = future.result()
                    counts[outcome.disposition] += 1
                    rows_written += outcome.rows_written
                    _append_jsonl(jsonl, {"at": _now(), **outcome.as_json()})
        receipt["dispositions"] = dict(counts)
        receipt["rows_written"] = rows_written
        receipt["not_dispatched"] = len(pending)
        receipt["stop_reason"] = stop["reason"] or _stop_reason(deadline, counts, max_failures, pending)
        connection = connect_fn(settings.database_url)
        try:
            receipt["legacy_route_counts_after"] = fetch_route_counts(connection, settings.window_days)
            connection.rollback()
        finally:
            _close_quietly(connection)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        release_timeseries_lifecycle_lock(lock_fd)
    receipt["finished_at"] = _now()
    if counts.get("failed"):
        status = EXIT_FAILED
    elif pending:
        status = EXIT_PARTIAL
    else:
        status = EXIT_COMPLETE
    receipt["result"] = {EXIT_COMPLETE: "complete", EXIT_FAILED: "failed", EXIT_PARTIAL: "partial"}[status]
    return status, receipt


def _may_dispatch(stop: Mapping[str, Any], deadline: datetime | None, counts: Counter[str], max_failures: int) -> bool:
    if stop["reason"] is not None:
        return False
    if deadline is not None and datetime.now(UTC) >= deadline:
        return False
    return counts.get("failed", 0) < max_failures


def _stop_reason(deadline: datetime | None, counts: Counter[str], max_failures: int, pending: Sequence[str]) -> str:
    if not pending:
        return "exhausted"
    if counts.get("failed", 0) >= max_failures:
        return "failure_budget"
    if deadline is not None and datetime.now(UTC) >= deadline:
        return "deadline"
    return "unknown"


def verify_runs(settings: Settings, run_ids: Sequence[str], *, connect_fn: Callable[[str], Any] = connect) -> dict:
    connection = connect_fn(settings.database_url)
    results = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        for run_id in run_ids:
            keys = _fetch(connection, "SELECT run_key, timeseries_store FROM hydro.hydro_run WHERE run_id = %(r)s",
                          {"r": run_id})
            if not keys:
                results.append({"run_id": run_id, "verdict": "missing"})
                continue
            row = _fetch(connection, VERIFY_SQL, {"run_id": run_id, "run_key": keys[0]["run_key"]})[0]
            verdict = "pass" if (
                keys[0]["timeseries_store"] == "narrow"
                and int(row["narrow_rows"]) > 0
                and int(row["mismatched_rows"]) == 0
            ) else "fail"
            results.append({"run_id": run_id, "route": keys[0]["timeseries_store"], "verdict": verdict,
                            **{key: int(value) for key, value in row.items()}})
    finally:
        _rollback_quietly(connection)
        _close_quietly(connection)
    return {
        "tool_version": TOOL_VERSION,
        "generated_at": _now(),
        "runs": results,
        "verdicts": dict(Counter(item["verdict"] for item in results)),
    }


def _parse_time(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must carry a timezone")
    return value.astimezone(UTC)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--receipt-dir", type=Path, required=True)
        if name in ("plan", "run"):
            command.add_argument("--end-time-after", type=_parse_time, default=None)
    run = sub.choices["run"]
    run.add_argument("--go", required=True)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--deadline", type=_parse_time, default=None)
    run.add_argument("--max-failures", type=int, default=DEFAULT_MAX_FAILURES)
    run.add_argument("--max-decompress-bytes", type=int, default=DEFAULT_MAX_DECOMPRESS_BYTES)
    run.add_argument("--limit", type=int, default=None)
    verify = sub.choices["verify"]
    verify.add_argument("--run-id", action="append", default=[])
    verify.add_argument("--sample", type=int, default=0, help="sample N reparsed runs from runs.jsonl")
    return parser


def _sample_reparsed(receipt_dir: Path, sample: int) -> list[str]:
    path = receipt_dir / "runs.jsonl"
    if sample <= 0 or not path.exists():
        return []
    reparsed = [
        record["run_id"]
        for record in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
        if record.get("disposition") == "reparsed"
    ]
    if len(reparsed) <= sample:
        return reparsed
    step = len(reparsed) / sample
    return [reparsed[int(index * step)] for index in range(sample)]


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    environment = os.environ if env is None else env
    receipt_dir: Path = args.receipt_dir
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        settings = settings_from_env(environment)
        receipt_dir.mkdir(parents=True, exist_ok=True)
        if args.command == "plan":
            connection = connect(settings.database_url)
            try:
                plan = build_plan(connection, settings, args.end_time_after)
            finally:
                _close_quietly(connection)
            _write_json(receipt_dir / f"plan-{stamp}.json", plan)
            print(json.dumps(plan, sort_keys=True))
            return EXIT_COMPLETE
        if args.command == "verify":
            run_ids = list(args.run_id) + _sample_reparsed(receipt_dir, args.sample)
            if not run_ids:
                raise RefusedError("VERIFY_EMPTY", "no run ids given and no reparsed runs to sample")
            report = verify_runs(settings, run_ids)
            _write_json(receipt_dir / f"verify-{stamp}.json", report)
            print(json.dumps(report["verdicts"], sort_keys=True))
            return EXIT_COMPLETE if set(report["verdicts"]) == {"pass"} else EXIT_FAILED
        if args.go != GO_TOKEN:
            raise RefusedError("GO_MISSING", "run requires the explicit operator GO token")
        if args.concurrency < 1 or args.max_failures < 1 or (args.limit is not None and args.limit < 1):
            raise RefusedError("CONFIG_INVALID", "concurrency, max-failures and limit must be positive")
        refuse_lifecycle_lock_env_override(environment)
        status, receipt = execute_run(
            settings,
            receipt_dir=receipt_dir,
            concurrency=args.concurrency,
            deadline=args.deadline,
            max_failures=args.max_failures,
            max_decompress_bytes=args.max_decompress_bytes,
            limit=args.limit,
            end_time_after=args.end_time_after,
        )
    except (RefusedError, LifecycleLockError, ValueError) as error:
        code = error.code if isinstance(error, RefusedError) else (
            "LIFECYCLE_LOCK_UNSAFE" if isinstance(error, LifecycleLockError) else "CONFIG_INVALID"
        )
        refused = {"tool_version": TOOL_VERSION, "at": _now(), "result": "refused", "code": code,
                   "message": str(error)}
        try:
            receipt_dir.mkdir(parents=True, exist_ok=True)
            _write_json(receipt_dir / f"refused-{stamp}.json", refused)
        finally:
            print(json.dumps(refused, sort_keys=True), file=sys.stderr)
        return EXIT_REFUSED
    _write_json(receipt_dir / f"summary-{stamp}.json", receipt)
    print(json.dumps({key: receipt[key] for key in ("result", "dispositions", "rows_written", "stop_reason")},
                     sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
