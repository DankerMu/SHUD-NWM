"""Fakes, a fake ``systemctl`` and the workspace shared by the node-27 basin retirement suites (#2757).

Everything is in ``tmp_path``: an object store (the canonical manifest and the
succession receipts of the node-22 half), a real env file of mode 0600 with a
real lock file beside it, and a directory for the fake ``systemctl``.

The fake database is strict: its cursor knows exactly the statements written
down in this module -- not the tool's constants -- and fails on any other, so a
pattern match, a second column in the ``UPDATE`` or a write to ``core.basin``
cannot pass.  Writes happen in a per-connection working copy that only
``commit`` makes visible; ``snapshot()`` is the committed state.

The fake registry store answers the way ``PsycopgModelRegistryStore`` does: a
preflight dict with ``status`` / ``blockers`` / ``warnings``, and an operation
result dict with ``status`` (``allowed``, ``already_current``, ``blocked``) --
a blocked operation and an audit persistence failure are returned, not raised.

The fake ``systemctl`` is a script that answers ``--user show`` from a queue
(the last answer repeats) and stamps ``now`` with its own ``CLOCK_MONOTONIC``
reading, the clock the tool takes its reference from.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg2
import pytest

import scripts.node27_retire_basin as tool
from scripts.basin_retirement import autopipe, database

SUCCESSION_ID = "retire-huai-20261006"
DATABASE_URL = "postgresql://nhms_ingest_rw:s3cret-pw@127.0.0.1:55432/nhms"
UNIT = "nhms-node27-autopipe.service"
STEPS = ("exclude", "supersede", "deactivate", "verify")

HUAI = "basins_huai_v1"
TAO = "basins_tao_v1"
WEI = "basins_wei_v1"

# The statements the tool may send, written out here on purpose (see the module docstring).
BASIN_ID_SQL = "SELECT basin_id FROM core.basin_version WHERE basin_version_id = %s"
MODEL_IDS_SQL = "SELECT model_id FROM core.model_instance WHERE basin_version_id = %s ORDER BY model_id"
ACTIVE_MODEL_IDS_SQL = (
    "SELECT model_id FROM core.model_instance WHERE basin_version_id = %s AND active_flag ORDER BY model_id"
)
CANDIDATE_COUNT_SQL = (
    "SELECT count(*) FROM hydro.hydro_run WHERE basin_version_id = %s "
    "AND status IN ('succeeded','parsed','published')"
)
SUPERSEDE_SQL = (
    "UPDATE hydro.hydro_run SET status = 'superseded' WHERE basin_version_id = %s "
    "AND status IN ('succeeded','parsed','published')"
)
COPY_PREFIX = "COPY (SELECT * FROM hydro.hydro_run WHERE basin_version_id = "
COPY_SUFFIX = " AND status IN ('succeeded','parsed','published') ORDER BY run_id) TO STDOUT WITH CSV HEADER"
CANDIDATES = ("succeeded", "parsed", "published")
RUN_COLUMNS = ("run_id", "run_type", "model_id", "basin_version_id", "status", "error_message", "updated_at")

ENV_TEMPLATE = """# node-27 ingest runtime (test copy)
NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest
DATABASE_URL={database_url}
OBJECT_STORE_ROOT={object_store_root}

# Retired basins; one line, comma separated.
AUTOPIPE_EXCLUDE_BASINS={excluded}
AUTOPIPE_EXCLUDE_MODEL_IDS=
AUTOPIPE_LOCK_PATH={lock_path}
AUTOPIPE_RUN_WORKERS=2
"""

_FAKE_SYSTEMCTL = r'''
import json, os, sys, time

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "trace"), "a", encoding="utf-8") as trace:
    trace.write(" ".join(sys.argv[1:]) + "\n")
with open(os.path.join(here, "answers.json"), encoding="utf-8") as handle:
    answers = json.load(handle)
answer = answers[0]
if len(answers) > 1:
    with open(os.path.join(here, "answers.json"), "w", encoding="utf-8") as handle:
        json.dump(answers[1:], handle)
if sys.argv[1:4] != ["--user", "show", "nhms-node27-autopipe.service"] or sys.argv[4] != "-p":
    print("fake systemctl: only `--user show nhms-node27-autopipe.service -p ...` is answered", file=sys.stderr)
    sys.exit(64)
if answer.get("rc"):
    print("fake systemctl: injected failure", file=sys.stderr)
    sys.exit(int(answer["rc"]))
if "raw" in answer:
    sys.stdout.write(answer["raw"])
    sys.exit(0)
now = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
start = now if answer["start"] == "now" else int(answer["start"])
end = answer["exit"]
if end == "now":
    end = now
elif isinstance(end, str) and end.startswith("+"):
    end = start + int(end[1:])
values = {
    "ActiveState": answer["state"],
    "ExecMainStartTimestampMonotonic": start,
    "ExecMainExitTimestampMonotonic": int(end),
    "ExecMainCode": answer["code"],
    "ExecMainStatus": answer["status"],
}
for name in sys.argv[5].split(","):
    print(f"{name}={values[name]}")
'''

_FLOCK_HOLDER = r"""
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


def monotonic_us() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


def round_answer(
    *, state: str = "inactive", start: Any = "now", end: Any = "+1000", code: int = 1, status: int = 0
) -> dict[str, Any]:
    """One answer of the fake ``systemctl``: by default a round that started now and exited by itself with 0."""

    return {"state": state, "start": start, "exit": end, "code": code, "status": status}


def running_answer(start: Any = "now") -> dict[str, Any]:
    """A round in flight: the oneshot unit is ``activating`` and has no exit timestamp yet."""

    return round_answer(state="activating", start=start, end=0, code=0, status=0)


@contextlib.contextmanager
def held_flock(path: Path) -> Iterator[subprocess.Popen[str]]:
    """Hold ``flock(2)`` on ``path`` from another process, as ``flock(1)`` in the cron script does."""

    holder = subprocess.Popen(
        [sys.executable, "-c", _FLOCK_HOLDER, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        yield holder
    finally:
        release_flock(holder)


def release_flock(holder: subprocess.Popen[str]) -> None:
    if holder.poll() is None:
        holder.kill()
    holder.wait(timeout=30)
    for stream in (holder.stdin, holder.stdout):
        if stream is not None:
            stream.close()


def tree(root: Path, *, without: tuple[Path, ...] = ()) -> dict[str, tuple[str, str]]:
    """Every entry under ``root``: its mode and its content hash, link target or ``dir``.

    ``without`` names entries to leave out, with everything under them.
    """

    entries: dict[str, tuple[str, str]] = {}
    for path in sorted(root.rglob("*")):
        if any(path == left_out or left_out in path.parents for left_out in without):
            continue
        status = path.lstat()
        mode = f"{stat.S_IMODE(status.st_mode):04o}"
        if stat.S_ISLNK(status.st_mode):
            entries[str(path.relative_to(root))] = (mode, f"link:{os.readlink(path)}")
        elif stat.S_ISDIR(status.st_mode):
            entries[str(path.relative_to(root))] = (mode, "dir")
        else:
            entries[str(path.relative_to(root))] = (mode, hashlib.sha256(path.read_bytes()).hexdigest())
    return entries


class FakeDatabase:
    """The committed rows of the three tables the tool reads, and everything every connection did."""

    def __init__(self) -> None:
        self.basin_versions: dict[str, str] = {}
        self.models: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.connections: list[FakeConnection] = []
        self.audit_log: list[dict[str, Any]] = []
        # Called between the COPY and the UPDATE of a supersede transaction: a concurrent writer.
        self.before_update: Any = None

    def add_basin_version(self, basin_version_id: str, basin_id: str) -> None:
        self.basin_versions[basin_version_id] = basin_id

    def add_model(self, model_id: str, basin_version_id: str, *, active: bool) -> None:
        self.models[model_id] = {"basin_version_id": basin_version_id, "active_flag": active}

    def add_run(self, run_id: str, basin_version_id: str, status: str, **columns: Any) -> None:
        number = len(self.runs)
        self.runs[run_id] = {
            "run_id": run_id,
            "run_type": "forecast",
            "model_id": f"model-of-{run_id}",
            "basin_version_id": basin_version_id,
            "status": status,
            "error_message": "",
            "updated_at": f"2026-09-21 19:{number:02d}:00+00",
            **columns,
        }

    def connect(self, dsn: str, **kwargs: Any) -> FakeConnection:
        connection = FakeConnection(self, dsn, kwargs)
        self.connections.append(connection)
        return connection

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {"basin_versions": self.basin_versions, "models": self.models, "runs": self.runs, "audit": self.audit_log}
        )

    def statuses(self, basin_version_id: str) -> dict[str, str]:
        return {
            run_id: run["status"] for run_id, run in self.runs.items() if run["basin_version_id"] == basin_version_id
        }

    def active(self, basin_version_id: str) -> list[str]:
        return sorted(
            model_id
            for model_id, model in self.models.items()
            if model["basin_version_id"] == basin_version_id and model["active_flag"]
        )

    @property
    def statements(self) -> list[str]:
        return [statement for connection in self.connections for statement in connection.statements]

    @property
    def commits(self) -> int:
        return sum(connection.commits for connection in self.connections)


class FakeConnection:
    def __init__(self, database_: FakeDatabase, dsn: str, kwargs: dict[str, Any]) -> None:
        self.database = database_
        self.dsn = dsn
        self.kwargs = kwargs
        self.autocommit = True
        self.readonly = False
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []
        self.copies: list[str] = []
        self._working: dict[str, dict[str, Any]] | None = None

    def set_session(self, *, readonly: bool | None = None) -> None:
        self.readonly = bool(readonly)

    def cursor(self) -> FakeCursor:
        assert not self.closed, "cursor() on a closed connection"
        assert self.autocommit is False, "the tool must never run in autocommit"
        return FakeCursor(self)

    def runs(self) -> dict[str, dict[str, Any]]:
        return self.database.runs if self._working is None else self._working

    def writable_runs(self) -> dict[str, dict[str, Any]]:
        if self._working is None:
            self._working = copy.deepcopy(self.database.runs)
        return self._working

    def commit(self) -> None:
        assert not self.closed
        self.commits += 1
        if self._working is not None:
            self.database.runs = self._working
            self._working = None

    def rollback(self) -> None:
        self.rollbacks += 1
        self._working = None

    def close(self) -> None:
        self._working = None
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rowcount = -1
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def mogrify(self, sql: str, params: tuple[Any, ...]) -> bytes:
        # As psycopg2: a quoted literal per placeholder, returned as bytes.
        assert sql.count("%s") == len(params)
        for value in params:
            assert isinstance(value, str)
            sql = sql.replace("%s", "'" + value.replace("'", "''") + "'", 1)
        return sql.encode("utf-8")

    def _selected(self, basin_version_id: str) -> list[dict[str, Any]]:
        runs = self.connection.runs().values()
        rows = [run for run in runs if run["basin_version_id"] == basin_version_id and run["status"] in CANDIDATES]
        return sorted(rows, key=lambda run: run["run_id"])

    def copy_expert(self, sql: str, handle: Any) -> None:
        assert isinstance(sql, str), "copy_expert was given something that is not the rendered statement"
        self.connection.statements.append(sql)
        self.connection.copies.append(sql)
        # copy_expert binds nothing: a placeholder left in the statement would reach the server as text.
        assert "%s" not in sql and "%(" not in sql, sql
        assert sql.startswith(COPY_PREFIX + "'") and sql.endswith("'" + COPY_SUFFIX), sql
        literal = sql[len(COPY_PREFIX) + 1 : -len(COPY_SUFFIX) - 1]
        assert "'" not in literal.replace("''", "")
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(RUN_COLUMNS)
        for run in self._selected(literal.replace("''", "'")):
            writer.writerow([run[column] for column in RUN_COLUMNS])
        # As psycopg2: text to a text file, bytes to anything else.
        text = buffer.getvalue()
        handle.write(text if isinstance(handle, io.TextIOBase) else text.encode("utf-8"))

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.statements.append(sql)
        assert isinstance(params, tuple) and len(params) == 1 and isinstance(params[0], str), (sql, params)
        (basin_version_id,) = params
        database_ = self.connection.database
        if sql == BASIN_ID_SQL:
            found = database_.basin_versions.get(basin_version_id)
            self._rows = [(found,)] if found is not None else []
        elif sql in (MODEL_IDS_SQL, ACTIVE_MODEL_IDS_SQL):
            self._rows = [
                (model_id,)
                for model_id, model in sorted(database_.models.items())
                if model["basin_version_id"] == basin_version_id
                and (sql == MODEL_IDS_SQL or model["active_flag"])
            ]
        elif sql == CANDIDATE_COUNT_SQL:
            self._rows = [(len(self._selected(basin_version_id)),)]
        elif sql == SUPERSEDE_SQL:
            if self.connection.readonly:
                raise psycopg2.errors.ReadOnlySqlTransaction("cannot execute UPDATE in a read-only transaction")
            if database_.before_update is not None:
                database_.before_update()
            runs = self.connection.writable_runs()
            # A concurrent writer's committed rows are visible to the statement that follows them.
            for run_id, run in database_.runs.items():
                runs.setdefault(run_id, copy.deepcopy(run))
            changed = 0
            for run in runs.values():
                if run["basin_version_id"] == basin_version_id and run["status"] in CANDIDATES:
                    run["status"] = "superseded"  # the one column the statement sets
                    changed += 1
            self.rowcount = changed
            self._rows = []
        else:
            raise AssertionError(f"the tool sent a statement the fake database does not know: {sql}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeRegistryStore:
    """The lifecycle calls of ``PsycopgModelRegistryStore`` over the fake database's ``core.model_instance``."""

    def __init__(self, database_: FakeDatabase) -> None:
        self.database = database_
        self.created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.preflights: list[dict[str, Any]] = []
        self.operations: list[dict[str, Any]] = []
        self.preflight_blockers: dict[str, list[dict[str, str]]] = {}
        # model_id -> "blocked" | "audit_failure" | "already_current" | "raise"; anything else transitions.
        self.operation_outcomes: dict[str, str] = {}

    def factory(self, *args: Any, **kwargs: Any) -> FakeRegistryStore:
        self.created.append((args, kwargs))
        return self

    def _preflight(self, model_id: str, call: dict[str, Any], blockers: list[dict[str, str]]) -> dict[str, Any]:
        decision = call["policy_decision"]
        assert decision is not None and decision.decision == "allow", decision
        assert (decision.action_id, decision.target_type, decision.target_id) == (
            "models.deactivate",
            "model_instance",
            model_id,
        )
        return {
            "schema": "nhms.model_operation_preflight.v1",
            "operation": call["operation"],
            "action_id": decision.action_id,
            "actor_id": decision.actor_id,
            "roles": list(decision.roles),
            "status": "blocked" if blockers else "ready",
            "basin_version_id": self.database.models[model_id]["basin_version_id"],
            "model_id": model_id,
            "blockers": list(blockers),
            "warnings": [{"code": "COPIED_ROOT_EVIDENCE_MISSING", "message": "Copied-root evidence is missing."}],
            "override_missing_active": bool(call["override_missing_active"]),
            "reason": "[redacted]",
        }

    def preflight_model_operation(
        self,
        model_id: str,
        *,
        operation: str,
        policy_decision: Any = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        call = {
            "model_id": model_id,
            "operation": operation,
            "policy_decision": policy_decision,
            "override_missing_active": override_missing_active,
            "reason": reason,
        }
        self.preflights.append(call)
        return self._preflight(model_id, call, self.preflight_blockers.get(model_id, []))

    def model_lifecycle_operation(
        self,
        model_id: str,
        *,
        operation: str,
        policy_decision: Any = None,
        trusted_internal: bool = False,
        request_id: str | None = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
        cold_start_approval: Any = None,
    ) -> dict[str, Any]:
        assert trusted_internal is False, "the tool must pass its own policy decision, never trusted_internal"
        call = {
            "model_id": model_id,
            "operation": operation,
            "policy_decision": policy_decision,
            "override_missing_active": override_missing_active,
            "reason": reason,
        }
        self.operations.append(call)
        outcome = self.operation_outcomes.get(model_id)
        model = {"model_id": model_id, **self.database.models[model_id]}
        if outcome == "raise":
            raise RuntimeError("Model registry database operation failed: connection reset")
        if outcome == "blocked":
            blocker = {"code": "INVALID_TRANSITION", "message": "deactivate is not allowed from deprecated."}
            self.database.audit_log.append({"model_id": model_id, "outcome": "blocked"})
            return {
                "status": "blocked",
                "operation": operation,
                "model": model,
                "preflight": self._preflight(model_id, call, [blocker]),
                "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": 900},
            }
        if outcome == "audit_failure":
            # The shape of `_lifecycle_audit_persistence_failure_result`: returned, the mutation rolled back.
            blocker = {
                "code": "LIFECYCLE_AUDIT_PERSISTENCE_FAILED",
                "message": "Lifecycle audit evidence could not be persisted; mutation was rolled back.",
            }
            return {
                "status": "blocked",
                "operation": operation,
                "model": model,
                "previous_model": model,
                "preflight": self._preflight(model_id, call, [blocker]),
                "audit_reference": None,
            }
        row = self.database.models[model_id]
        if outcome == "already_current" or not row["active_flag"]:
            row["active_flag"] = False
            status = "already_current"
        else:
            row["active_flag"] = False
            status = "allowed"
        self.database.audit_log.append({"model_id": model_id, "outcome": status, "actor": policy_decision.actor_id})
        return {
            "status": status,
            "operation": operation,
            "model": {"model_id": model_id, **row},
            "previous_model": model,
            "preflight": self._preflight(model_id, call, []),
            "audit_reference": {
                "entity_type": "model_instance",
                "entity_id": model_id,
                "log_id": len(self.database.audit_log),
            },
        }


@dataclass
class Space:
    root: Path
    monkeypatch: pytest.MonkeyPatch
    capsys: pytest.CaptureFixture[str]
    database: FakeDatabase
    store: FakeRegistryStore
    wait_seconds: float = 30.0
    last_stderr: str = field(default="", init=False)

    @property
    def object_store(self) -> Path:
        return self.root / "store"

    @property
    def manifest(self) -> Path:
        return self.object_store / "scheduler" / "registry" / "manifest-last.json"

    @property
    def receipt_root(self) -> Path:
        return self.object_store / "scheduler" / "succession"

    @property
    def succession(self) -> Path:
        return self.receipt_root / SUCCESSION_ID

    @property
    def env_file(self) -> Path:
        return self.root / "env" / "node27-ingest.env"

    @property
    def retire_lock(self) -> Path:
        return self.root / "env" / "node27-ingest.env.retire-lock"

    @property
    def autopipe_lock(self) -> Path:
        return self.root / "locks" / "autopipe.cron.lock"

    @property
    def systemctl_directory(self) -> Path:
        return self.root / "systemctl"

    def directory(self, basin_version_id: str = HUAI) -> Path:
        return self.succession / f"retire-{basin_version_id}"

    def env_backup(self, basin_version_id: str = HUAI) -> Path:
        return self.root / "env" / f"node27-ingest.env.bak-{SUCCESSION_ID}-{basin_version_id}"

    def receipt(self, step: str, basin_version_id: str = HUAI) -> dict[str, Any]:
        return json.loads((self.directory(basin_version_id) / f"retire-{step}.json").read_text(encoding="utf-8"))

    def receipts_present(self, basin_version_id: str = HUAI) -> list[str]:
        directory = self.directory(basin_version_id)
        return [step for step in STEPS if (directory / f"retire-{step}.json").exists()]

    def failures(self, basin_version_id: str = HUAI) -> list[dict[str, Any]]:
        paths = sorted(self.directory(basin_version_id).glob("retire-failed-*.json"))
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def write_env(self, content: str, *, mode: int = 0o600) -> None:
        self.env_file.write_text(content, encoding="utf-8")
        os.chmod(self.env_file, mode)

    def env_text(self, *, excluded: str = "zhaochen_hhy,hhe", lock_path: Path | None = None) -> str:
        return ENV_TEMPLATE.format(
            database_url=DATABASE_URL,
            object_store_root=self.object_store,
            excluded=excluded,
            lock_path=lock_path or self.autopipe_lock,
        )

    def excluded(self) -> str:
        (line,) = [
            line
            for line in self.env_file.read_text(encoding="utf-8").split("\n")
            if line.startswith("AUTOPIPE_EXCLUDE_BASINS=")
        ]
        return line.partition("=")[2]

    def set_answers(self, *answers: dict[str, Any]) -> None:
        (self.systemctl_directory / "answers.json").write_text(json.dumps(list(answers)), encoding="utf-8")

    def systemctl_calls(self) -> list[str]:
        trace = self.systemctl_directory / "trace"
        return trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []

    def write_node22(self, *, kind: str = "remove_basin", removes: list[str] | None = None) -> None:
        self.succession.mkdir(parents=True, exist_ok=True)
        plan: dict[str, Any] = {"schema_version": "nhms.model_succession.plan.v1", "succession_id": SUCCESSION_ID}
        if kind == "remove_basin":
            plan.update(kind=kind, removes=removes if removes is not None else ["dg_huai_gfs", "dg_huai_ifs"])
        else:
            plan.update(kind=kind, adds=["dg_new_gfs"], provision_succession_id=SUCCESSION_ID)
        (self.succession / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        finish = {
            "schema_version": "nhms.model_succession.step_receipt.v1",
            "succession_id": SUCCESSION_ID,
            "step": "finish",
            "outcome": "completed",
            "timer_action": "started",
        }
        (self.succession / "step-finish.json").write_text(json.dumps(finish), encoding="utf-8")

    def write_manifest(self, rows: list[dict[str, Any]]) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps({"models": rows}), encoding="utf-8")

    def arguments(self, basin_version_id: str = HUAI, *, apply: bool = True, wait: float | None = None) -> list[str]:
        arguments = [
            "--succession-id", SUCCESSION_ID,
            "--basin-version-id", basin_version_id,
            "--operator-id", "danker",
            "--reason", "owner decision: the basin leaves production",
            "--env-file", str(self.env_file),
            "--autopipe-wait-seconds", str(self.wait_seconds if wait is None else wait),
        ]  # fmt: skip
        return [*arguments, "--apply"] if apply else arguments

    def run(self, basin_version_id: str = HUAI, *, apply: bool = True, wait: float | None = None) -> tuple[int, Any]:
        """``main`` with the standard command line: the exit status and the JSON report (None when refused)."""

        self.capsys.readouterr()
        status = tool.main(self.arguments(basin_version_id, apply=apply, wait=wait))
        captured = self.capsys.readouterr()
        self.last_stderr = captured.err
        return status, (json.loads(captured.out) if captured.out.strip() else None)

    def everything(self, *, without_lock: bool = True) -> dict[str, Any]:
        """The whole ``tmp_path`` tree and the committed database, for "nothing was written" comparisons.

        Left out: the fake ``systemctl``'s own queue and trace, which the fake writes, and by default the
        tool's lock file, which every run may create.
        """

        without = (self.systemctl_directory, *((self.retire_lock,) if without_lock else ()))
        return {"tree": tree(self.root, without=without), "database": self.database.snapshot()}


def manifest_row(model_id: str, basin_id: str, basin_version_id: str) -> dict[str, Any]:
    return {"model_id": model_id, "basin_id": basin_id, "basin_version_id": basin_version_id, "source_id": "gfs"}


def _seed(database_: FakeDatabase) -> None:
    database_.add_basin_version(HUAI, "basins_huai")
    database_.add_basin_version(TAO, "basins_tao")
    database_.add_basin_version(WEI, "basins_wei")
    # The basin to retire: a baseline row, two hashed direct-grid rows and one that is inactive already.
    database_.add_model("basins_huai_shud", HUAI, active=True)
    database_.add_model("dg_huai_gfs", HUAI, active=True)
    database_.add_model("dg_huai_ifs", HUAI, active=True)
    database_.add_model("dg_huai_old", HUAI, active=False)
    database_.add_model("basins_tao_shud", TAO, active=True)
    database_.add_model("dg_tao_gfs", TAO, active=True)
    database_.add_model("basins_wei_shud", WEI, active=True)
    database_.add_model("dg_wei_gfs", WEI, active=True)
    database_.add_run("huai-01", HUAI, "succeeded")
    database_.add_run("huai-02", HUAI, "parsed")
    # A field with a comma, a quote and a line break: one CSV record, not two.
    database_.add_run("huai-03", HUAI, "published", error_message='late, "retried"\nthen fine')
    database_.add_run("huai-04", HUAI, "published")
    database_.add_run("huai-05", HUAI, "failed")
    database_.add_run("huai-06", HUAI, "superseded")
    database_.add_run("huai-07", HUAI, "running")
    database_.add_run("tao-01", TAO, "published")
    database_.add_run("tao-02", TAO, "succeeded")
    database_.add_run("wei-01", WEI, "published")
    database_.add_run("wei-02", WEI, "succeeded")


@pytest.fixture
def space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Space:
    """A node-27 after the node-22 removal of ``basins_huai`` and ``basins_tao``: nothing retired yet."""

    database_ = FakeDatabase()
    _seed(database_)
    store = FakeRegistryStore(database_)
    built = Space(root=tmp_path, monkeypatch=monkeypatch, capsys=capsys, database=database_, store=store)
    for directory in ("env", "locks", "systemctl", "tmp"):
        (tmp_path / directory).mkdir()
    built.write_env(built.env_text())
    built.write_node22(removes=["dg_huai_gfs", "dg_huai_ifs", "dg_tao_gfs"])
    built.write_manifest([manifest_row("dg_wei_gfs", "basins_wei", WEI), manifest_row("dg_wei_ifs", "basins_wei", WEI)])
    script = built.systemctl_directory / "systemctl"
    script.write_text(f"#!{sys.executable}\n{_FAKE_SYSTEMCTL}", encoding="utf-8")
    script.chmod(0o755)
    built.set_answers(round_answer())

    for name in ("NHMS_AUTH_MODE", "AUTH_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(built.object_store))
    monkeypatch.setenv(tool.SYSTEMCTL_ENV, str(script))
    # Never the machine's /tmp/autopipe.cron.lock, whatever a test does to the env file.
    monkeypatch.setenv("NODE27_AUTOPIPE_LOCK_PATH", str(tmp_path / "locks" / "process-env.lock"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(psycopg2, "connect", database_.connect)
    monkeypatch.setattr(database, "PsycopgModelRegistryStore", store.factory)
    monkeypatch.setattr(autopipe, "sleep", lambda _seconds: time.sleep(0.01))
    return built
