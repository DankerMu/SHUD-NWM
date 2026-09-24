"""Run one checked-in one-shot SQL script against node-27 in ONE transaction.

The #1729 / #1480 data scripts (``scripts/ops/node27_1729_*.sql``,
``scripts/ops/node27_1480_*.sql``) back rows up to files and restore them from
files, which ``psql < file`` cannot do for several tables at once without
meta-commands a test cannot run. This runner is the single executor: the
disposable-DB tests call :func:`run_sql_file` on the very same files the
operator runs on node-27.

The file is plain SQL plus two marker comments, each applying to the ONE
``COPY`` statement that follows it (up to the first line ending in ``;``)::

    -- @copy-out NAME   COPY (...) TO STDOUT  -> <copy-dir>/NAME.copy (never overwritten)
    -- @copy-in NAME    COPY ... FROM STDIN   <- <copy-dir>/NAME.copy (must exist)

Everything between markers runs as one ``execute``. ``--set key=value`` binds
custom settings (``SELECT set_config(key, value, true)``) before the first
statement. Dry-run by default: the transaction is rolled back and the server
NOTICEs (counts, per-step timing) are printed; ``--apply`` commits. Exits 1 on
any database error, after rollback.

Usage (node-27; the owner DSN stays in the sourced private env, never in argv):
    cd /home/nwm/NWM && uv run python scripts/ops/node27_oneshot_sql.py \\
        scripts/ops/node27_1729_delete_evidence_basin.sql \\
        --dsn-env <VAR holding the owner DSN> --copy-dir /home/nwm/tmp/1729-backup-<ts> [--apply]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_MARKER = re.compile(r"^-- @(copy-out|copy-in) ([a-z0-9_.]+)\s*$")
_SETTING = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class Segment:
    """One ``execute`` unit: plain SQL, or a marked COPY bound to a copy file."""

    sql: str
    copy: str | None = None  # "copy-out" | "copy-in"
    name: str | None = None


@dataclass
class RunReport:
    committed: bool
    notices: list[str] = field(default_factory=list)
    steps: list[tuple[str, float]] = field(default_factory=list)


class ScriptFailedError(RuntimeError):
    """A database error rolled the whole run back; carries the NOTICEs seen so far."""

    def __init__(self, cause: Exception, notices: Sequence[str]) -> None:
        super().__init__(str(cause).strip())
        self.pgcode: str | None = getattr(cause, "pgcode", None)
        self.notices = list(notices)


def parse_segments(text: str) -> list[Segment]:
    """Split a script into plain chunks and marked COPY statements."""
    segments: list[Segment] = []
    plain: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        marker = _MARKER.match(lines[index])
        if marker is None:
            plain.append(lines[index])
            index += 1
            continue
        _flush(segments, plain)
        plain = []
        kind, name = marker.groups()
        statement: list[str] = []
        index += 1
        while index < len(lines):
            statement.append(lines[index])
            index += 1
            if lines[index - 1].rstrip().endswith(";"):
                break
        else:
            raise ValueError(f"@{kind} {name}: no statement ending in ';' follows the marker")
        sql = "\n".join(statement).strip().removesuffix(";").strip()
        direction = "TO STDOUT" if kind == "copy-out" else "FROM STDIN"
        if not sql.upper().startswith("COPY ") or not sql.upper().endswith(direction):
            raise ValueError(f"@{kind} {name}: the marked statement must be `COPY ... {direction};`")
        if any(segment.name == name and segment.copy == kind for segment in segments):
            raise ValueError(f"@{kind} {name}: duplicate marker")
        segments.append(Segment(sql, kind, name))
    _flush(segments, plain)
    return segments


def _code_lines(sql: str) -> list[str]:
    return [line.strip() for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")]


def _flush(segments: list[Segment], plain: list[str]) -> None:
    # A comment-only chunk is not a statement; the server would answer it empty.
    if _code_lines("\n".join(plain)):
        segments.append(Segment("\n".join(plain)))


def run_sql_file(
    sql_path: str | Path,
    *,
    database_url: str,
    copy_dir: str | Path | None = None,
    settings: Mapping[str, str] | None = None,
    apply: bool = False,
) -> RunReport:
    """Execute ``sql_path`` in one transaction; commit only when ``apply``."""
    import psycopg2

    segments = parse_segments(Path(sql_path).read_text(encoding="utf-8"))
    if any(segment.copy for segment in segments) and copy_dir is None:
        raise ValueError(f"{sql_path} has @copy markers; a copy directory is required")
    for key in settings or {}:
        if not _SETTING.fullmatch(key):
            raise ValueError(f"--set {key}: only custom `prefix.name` settings are accepted")
    report = RunReport(committed=False)
    connection = psycopg2.connect(database_url)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            for key, value in (settings or {}).items():
                cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
            for segment in segments:
                started = time.monotonic()
                if segment.copy == "copy-out":
                    with (Path(copy_dir) / f"{segment.name}.copy").open("x", encoding="utf-8") as handle:
                        cursor.copy_expert(segment.sql, handle)
                elif segment.copy == "copy-in":
                    with (Path(copy_dir) / f"{segment.name}.copy").open(encoding="utf-8") as handle:
                        cursor.copy_expert(segment.sql, handle)
                else:
                    cursor.execute(segment.sql)
                report.steps.append((_label(segment), time.monotonic() - started))
        if apply:
            connection.commit()
            report.committed = True
        else:
            connection.rollback()
    except psycopg2.Error as error:
        connection.rollback()
        raise ScriptFailedError(error, [notice.strip() for notice in connection.notices]) from error
    except BaseException:
        connection.rollback()
        raise
    finally:
        report.notices.extend(notice.strip() for notice in connection.notices)
        connection.close()
    return report


def _label(segment: Segment) -> str:
    if segment.copy:
        return f"@{segment.copy} {segment.name}"
    code = _code_lines(segment.sql)
    return (code[0] if code else "")[:80]


def _parse_settings(pairs: Sequence[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--set {pair}: expected key=value")
        settings[key] = value
    return settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--dsn-env", required=True, help="name of the environment variable holding the DSN")
    parser.add_argument("--copy-dir", type=Path, default=None)
    parser.add_argument("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--apply", action="store_true", help="commit (default: dry-run, rolled back)")
    args = parser.parse_args(argv)
    database_url = os.environ.get(args.dsn_env, "").strip()
    if not database_url:
        print(f"{args.dsn_env} is unset or empty", file=sys.stderr)
        return 2
    try:
        report = run_sql_file(
            args.sql_file,
            database_url=database_url,
            copy_dir=args.copy_dir,
            settings=_parse_settings(args.settings),
            apply=args.apply,
        )
    except ScriptFailedError as error:
        for notice in error.notices:
            print(notice, file=sys.stderr)
        print(f"ROLLED BACK: {error.pgcode} {error}", file=sys.stderr)
        return 1
    for notice in report.notices:
        print(notice)
    for label, seconds in report.steps:
        print(f"step {seconds:9.3f}s  {label}")
    print("COMMITTED" if report.committed else "ROLLED BACK (dry-run; pass --apply to commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
