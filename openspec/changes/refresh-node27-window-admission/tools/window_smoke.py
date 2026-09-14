#!/usr/bin/env python3
"""Focused disposable executor scenario. No production execution or provisioning.

Parent setup: provision a fresh container from IMAGE below, label
nhms.i8.disposable=true, anonymous volumes ONLY, publish 5432 on a unique
127.0.0.1 port other than 5432/55432. Create an empty i8_1987_<suffix>
database there. Set I8_REHEARSAL_DSN privately (never argv). Retain exact OLD
and NEW clean checkouts and Python 3.11.15 with their installed dependencies.
Run once per case, with a DIFFERENT fresh database and --state each time:
  python window_smoke.py --oracle PARENT/receipts/2026-09-12-i8-rollback/rehearse_river_rollback.py.txt \
    --old-repo OLD --new-repo NEW --container nwm-i8-1987-UNIQUE \
    --state PRIVATE_ABSOLUTE_NEW_DIR --case happy \
    --original-new-repo ORIGINAL_1A32
Cases: happy, stop, session, fence, do-before-ledger, rename, source, restart, unit-config.
Happy also drives the real main/CLI admission refusals and admitted emergency paths,
including window's nested recovery protection and historical-ledger pending-set
admission at both prepare and the migration worker; only external boundaries are simulated.
Oracle-only --original-new-repo is a retained 1a32 checkout for original-f24
pending-set negatives. It is not a production window_execute flag. Changed
runtime remains 415; original f24 negatives stay original-source/target qualified.
The harness owns no container lifecycle and never deletes a database/volume.
All SQL, catalog OIDs, ledger, parser rows and reader values are real. Only
systemd, process inspection, git selection and HTTP transport are simulated.
Keep private state + stdout as evidence. No PASS until the entire case ends.

Focused typed-unit oracle needs no database:
  python window_smoke.py --case unit-config --state PRIVATE_ABSOLUTE_NEW_DIR
Expected-original-red uses only a private baseline copy:
  git show 46786b04b:openspec/changes/refresh-node27-window-admission/tools/window_execute.py \
    > PRIVATE/original-window_execute.py
  python window_smoke.py --case unit-config --state PRIVATE_ABSOLUTE_NEW_DIR \
    --original-executor PRIVATE/original-window_execute.py

Small real write-budget entrypoint, AFTER copying the actual LH-YLJ artifact
and its complete authoritative run/model/segment metadata into the isolated
expanded DB, with succeeded/parsed_at NULL/narrow routing:
  python window_smoke.py --case write-budget --container nwm-i8-1987-UNIQUE \
    --new-repo NEW --state PRIVATE_ABSOLUTE_NEW_DIR --fixture-config PRIVATE.json
PRIVATE.json uses executor parse_run_id/artifacts_root/object_store_prefix keys.
The receipt measures actual parser DB-write end-to-end wall (not read/QC alone).
Do not use the synthetic 12-row measurement as the 512232-row budget.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import FunctionType, SimpleNamespace

IMAGE = "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e"

RETIRED_LEDGER_VERSIONS = (
    "000007_flood.sql",
    "000015_flood_return_period_identity_indexes.sql",
    "000017_return_period_max_over_window_identity.sql",
    "000020_valid_time_discovery_indexes.sql",
    "000031_search_discovery_return_period_performance.sql",
    "000034_return_period_run_quality_materialization.sql",
    "000036_run_product_quality_explicit_source.sql",
)


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


w = load(Path(__file__).with_name("window_execute.py"), "window_executor")


def guard(args):
    from psycopg2.extensions import parse_dsn

    dsn = os.environ["I8_REHEARSAL_DSN"]
    identity = parse_dsn(dsn)
    w.require(set(identity) <= {"host", "port", "dbname", "user", "password", "sslmode"}, "DSN_OPTIONS")
    w.require(
        identity.get("host") == "127.0.0.1" and identity.get("port") not in {None, "5432", "55432"}, "ISOLATED_PORT"
    )
    w.require(w.re.fullmatch(r"i8_1987_[a-z0-9_]+", identity.get("dbname", "")), "ISOLATED_DATABASE")
    w.require(w.re.fullmatch(r"nwm-i8-1987-[a-z0-9-]+", args.container), "ISOLATED_CONTAINER")
    raw = subprocess.check_output(["docker", "inspect", args.container])
    container = json.loads(raw)[0]
    w.require(container["Image"] == IMAGE and container["State"]["Running"], "EXACT_RUNNING_IMAGE")
    w.require(container["Config"].get("Labels", {}).get("nhms.i8.disposable") == "true", "OWNED_DISPOSABLE")
    w.require(container["Mounts"] and all(m["Type"] == "volume" for m in container["Mounts"]), "NO_BIND_MOUNTS")
    w.require(container["HostConfig"]["NetworkMode"] != "host", "NO_HOST_NETWORK")
    w.require(
        container["NetworkSettings"]["Ports"].get("5432/tcp")
        == [{"HostIp": "127.0.0.1", "HostPort": identity["port"]}],
        "PORT_BINDING",
    )
    for key in list(os.environ):
        if key.startswith("PG"):
            del os.environ[key]
    os.environ.update(DATABASE_URL=dsn, PGOPTIONS="-c lock_timeout=5s -c statement_timeout=120s")
    return dsn, identity


def worker(args):
    _, identity = guard(args)
    # Private in-memory binding only, never exposed by the production executor.
    w._WORKER_DATABASE = identity["dbname"]
    w._WORKER_PORT = identity["port"]
    w._WORKER_USERS = dict.fromkeys(("parse", "read", "admin"), identity["user"])
    w.worker(SimpleNamespace(repo=args.repo, sha=args.sha, state=args.state, action=args.action))


class BoundaryExecutor(w.Executor):
    """Real executor state/recovery/SQL with disposable external boundaries."""

    def __init__(self, args, dsn, *, cli_args=None):
        super().__init__(cli_args or SimpleNamespace(state=args.state, command="recover", go="Danker"))
        self.fixture = args
        self.dsn = dsn
        self.selected = w.OLD
        self.units = {name: dict(unit) for name, unit in self.s.get("units", {}).items()}
        self.inject = None
        self.starts = 0
        self.system_actions = []
        self.stop_failures = 0
        self.reset_systemd_fixture()

    def reset_systemd_fixture(self):
        self.bus_paths = {name: self.bus_path(name) for name in self.units}
        self.bus_semantics = {
            name: (
                [
                    [
                        "/bin/bash",
                        ["/bin/bash", "/fixture/" + name.removesuffix(".service") + ".sh"],
                        False,
                    ],
                    [
                        "/usr/bin/env",
                        ["/usr/bin/env", "FIXTURE_SECOND=1", "/fixture/second-command"],
                        True,
                    ],
                ]
                if name == w.DISPLAY
                else [
                    [
                        "/bin/bash",
                        ["/bin/bash", "/fixture/" + name.removesuffix(".service") + ".sh"],
                        False,
                    ]
                ]
                if name.endswith(".service")
                else [
                    ["OnCalendar", "*-*-* *:00/30:00"],
                    ["OnCalendar", "Mon *-*-* 04:00:00"],
                ]
                if name == w.TIMERS[0]
                else [["OnCalendar", "*-*-* 01:00:00"]]
            )
            for name in self.units
        }
        self.bus_tick = 0
        self.show_tick = 0
        self.bus_fault = None

    def sql(self, query):
        import psycopg2

        with contextlib.closing(psycopg2.connect(self.dsn)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout='5s'; SET statement_timeout='120s';")
                cur.execute(query)
                if self.inject == "rename" and query.startswith(
                    "BEGIN; ALTER TABLE hydro.river_timeseries RENAME TO river_timeseries_narrow_rollback; "
                ):
                    self.crash_boundary()
                return (
                    "\n".join(
                        json.dumps(row[0], default=str) if isinstance(row[0], (list, dict)) else str(row[0])
                        for row in cur.fetchall()
                    )
                    if cur.description
                    else ""
                )

    def bus_path(self, name):
        return w._SYSTEMD_UNIT_PREFIX + "".join(character if character.isalnum() else "_" for character in name)

    def unit(self, name):
        w.require(name in self.units, "UNKNOWN_UNIT")
        value = dict(self.units[name])
        self.show_tick += 1
        if name.endswith(".service"):
            entries = []
            for path, argv, ignore_errors in self.bus_semantics[name]:
                entries.append(
                    "{ path="
                    + path
                    + " ; argv[]="
                    + " ".join(argv)
                    + " ; ignore_errors="
                    + ("yes" if ignore_errors else "no")
                    + " ; start_time="
                    + str(self.show_tick)
                    + " ; stop_time="
                    + str(self.show_tick + 1)
                    + " ; pid="
                    + str(1000 + self.show_tick)
                    + " ; code=exited ; status=0 }"
                )
            value["ExecStart"] = " ; ".join(entries)
        else:
            value["TimersCalendar"] = " ; ".join(
                "{ " + base + "=" + expression + " ; next_elapse=" + str(self.show_tick) + " }"
                for base, expression in self.bus_semantics[name]
            )
        return value

    def bus_property(self, name, property_name):
        self.bus_tick += 1
        if property_name == "ExecStart":
            value = {
                "type": w._EXEC_START_SIGNATURE,
                "data": [
                    [
                        path,
                        list(argv),
                        ignore_errors,
                        self.bus_tick,
                        self.bus_tick + 1,
                        self.bus_tick + 2,
                        self.bus_tick + 3,
                        self.bus_tick + 4,
                        self.bus_tick + 5,
                        self.bus_tick + 6,
                    ]
                    for path, argv, ignore_errors in self.bus_semantics[name]
                ],
            }
            if self.bus_fault == "signature":
                value["type"] = "a(ss)"
            elif self.bus_fault == "envelope":
                value["extra"] = True
            elif self.bus_fault == "exec-arity":
                value["data"][0] = value["data"][0][:-1]
            elif self.bus_fault == "exec-bool-metadata":
                value["data"][0][3] = True
            elif self.bus_fault == "exec-argv-nonstring":
                value["data"][0][1][0] = 1
            elif self.bus_fault == "exec-ignore-errors-int":
                value["data"][0][2] = 1
            return value
        value = {
            "type": w._TIMERS_CALENDAR_SIGNATURE,
            "data": [
                [base, expression, self.bus_tick + index]
                for index, (base, expression) in enumerate(self.bus_semantics[name])
            ],
        }
        if self.bus_fault == "signature":
            value["type"] = "a(ss)"
        elif self.bus_fault == "envelope":
            value["extra"] = True
        elif self.bus_fault == "timer-arity":
            value["data"][0] = value["data"][0][:-1]
        elif self.bus_fault == "timer-bool-next":
            value["data"][0][2] = True
        return value

    def system(self, action, names):
        self.system_actions.append({"action": action, "units": list(names)})
        w.require(action in {"start", "stop"} and set(names) <= set(self.units), "UNKNOWN_SYSTEM_COMMAND")
        if action == "stop" and self.stop_failures:
            self.stop_failures -= 1
            raise w.Refusal("INJECTED_STOP_FAILURE")
        if self.inject == "stop" and action == "stop":
            raise w.Refusal("INJECTED_STOP_FAILURE")
        for name in names:
            self.units[name].update(
                ActiveState="active" if action == "start" else "inactive",
                SubState="running" if action == "start" else "dead",
                MainPID="1" if action == "start" else "0",
            )
            if action == "start":
                self.starts += 1
                self.units[name]["ExecMainStartTimestampMonotonic"] = str(self.starts)
        if self.inject == "restart" and action == "start" and w.DISPLAY in names:
            self.inject = None
            self.crash_boundary()

    def bus_run(self, argv):
        if argv[:4] == ["busctl", "--user", "--json=short", "call"]:
            w.require(
                argv[4:9] == [w._SYSTEMD, w._SYSTEMD_MANAGER_PATH, w._SYSTEMD + ".Manager", "GetUnit", "s"]
                and len(argv) == 10
                and argv[9] in self.bus_paths,
                "UNKNOWN_BUSCTL_BOUNDARY",
            )
            if self.bus_fault == "resolution":
                return json.dumps({"type": "s", "data": ["not-an-object-path"]}).encode()
            return json.dumps({"type": "o", "data": [self.bus_paths[argv[9]]]}).encode()
        if argv[:4] != ["busctl", "--user", "--json=short", "get-property"]:
            return None
        w.require(
            len(argv) == 8 and argv[4] == w._SYSTEMD and argv[5] in self.bus_paths.values(),
            "UNKNOWN_BUSCTL_BOUNDARY",
        )
        names = [name for name, path in self.bus_paths.items() if path == argv[5]]
        w.require(len(names) == 1, "AMBIGUOUS_BUS_UNIT")
        name = names[0]
        interface, property_name = argv[6:]
        if name.endswith(".service"):
            w.require(
                (interface, property_name) == (w._SYSTEMD + ".Service", "ExecStart"),
                "BUS_PROPERTY_INTERFACE_MISMATCH",
            )
        else:
            w.require(
                (interface, property_name) == (w._SYSTEMD + ".Timer", "TimersCalendar"),
                "BUS_PROPERTY_INTERFACE_MISMATCH",
            )
        return json.dumps(self.bus_property(name, property_name)).encode()

    def run(self, argv, **kwargs):
        bus = self.bus_run(argv)
        if bus is not None:
            return bus
        if argv[:6] == ["docker", "exec", "-i", "nhms-db", "psql", "-X"]:
            from psycopg2.extensions import parse_dsn

            identity = parse_dsn(self.dsn)
            w.require(argv[6:12] == ["-U", "nhms", "-d", "nhms", "-v", "ON_ERROR_STOP=1"], "UNKNOWN_PSQL_BOUNDARY")
            command = list(argv)
            command[3], command[7], command[9] = self.fixture.container, identity["user"], identity["dbname"]
            proc = subprocess.run(command, input=kwargs["data"], capture_output=True, timeout=150)
            self.seq += 1
            self.save()
            self.save_file(f"role-audit-{self.seq}.stderr", proc.stderr)
            w.require(proc.returncode == 0, "REAL_ROLE_AUDIT_FAILED")
            return proc.stdout
        if argv == ["ss", "-Hlnpt", "sport = :8080"]:
            return b"" if self.units[w.DISPLAY]["ActiveState"] == "inactive" else b"listener"
        if argv == ["systemctl", "--user", "start", "--no-block", w.AUTO]:
            self.system("start", [w.AUTO])
            return b""
        raise w.Refusal("UNKNOWN_BOUNDARY_COMMAND")

    def git(self, *args, repo=None):
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return self.selected
        if args == ("rev-parse", "refs/heads/old^{commit}"):
            return w.OLD
        if args == ("switch", "old"):
            self.selected = w.OLD
            if self.inject == "source":
                self.inject = None
                self.crash_boundary()
            return ""
        raise w.Refusal("UNKNOWN_GIT_BOUNDARY")

    def crash_boundary(self):
        self.save_file(
            "system-boundary.json",
            json.dumps({"units": self.units, "selected": self.selected, "starts": self.starts}).encode(),
        )
        os.kill(os.getpid(), w.signal.SIGKILL)

    def proxy_binding(self):
        return {"boundary": "isolated-no-proxy"}

    def public_probe(self, stopped=False):
        active = self.units[w.DISPLAY]["ActiveState"] == "active"
        w.require(active != stopped, "PUBLIC_PROXY_FENCE_OR_RESTORE_FAILED")
        self.save(public_probe={"status": 200 if active else 502, "at": w.now()})

    def no_listener(self):
        w.require(self.inject != "fence", "INJECTED_FENCE_FAILURE")
        w.require(self.units[w.DISPLAY]["ActiveState"] == "inactive", "8080_LISTENER_PRESENT")
        self.public_probe(stopped=True)

    def yd(self):
        return {"boundary": "unchanged-yd"}

    def http(self, path, port=8080):
        w.require(path == self.c["api_health_path"] and port == 8080, "UNKNOWN_HTTP_BOUNDARY")
        w.require(self.units[w.DISPLAY]["ActiveState"] == "active", "DISPLAY_NOT_RUNNING")
        return {"status": 200}, b"{}"

    def source_process(self, unit):
        w.require(unit in {w.DISPLAY, w.AUTO} and self.units[unit]["ActiveState"] == "active", "NO_PROCESS")
        w.require(self.selected == (w.OLD if self.recovering else w.NEW), "RUNTIME_SHA_MISMATCH")
        return {"unit": unit, "source": self.selected, "execution": self.units[unit]["ExecMainStartTimestampMonotonic"]}

    def worker(self, action, *, repo=None, sha=None, timeout=150):
        sha = sha or w.NEW
        repo = self.fixture.old_repo if sha == w.OLD else self.fixture.new_repo
        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--container",
            self.fixture.container,
            "--state",
            str(self.root),
            "--repo",
            repo,
            "--sha",
            sha,
            "--action",
            action,
        ]
        proc = subprocess.run(argv, cwd=repo, env=os.environ.copy(), capture_output=True, timeout=timeout)
        self.seq += 1
        self.save()
        self.save_file(f"worker-{self.seq}.stderr", proc.stderr)
        self.save_file(f"worker-{self.seq}.stdout", proc.stdout)
        w.require(proc.returncode == 0, "REAL_WORKER_FAILED")
        return json.loads(proc.stdout)


def scenario(args):
    import psycopg2

    dsn, identity = guard(args)
    root = Path(args.state).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    w._PROCESS_INVENTORY_ROOT = root / "proc-boundary"
    w._PROCESS_INVENTORY_ROOT.mkdir()
    for repo, sha in ((args.old_repo, w.OLD), (args.new_repo, w.NEW)):
        w.require(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == sha, "SOURCE_SHA"
        )
    if args.original_new_repo:
        w.require(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.original_new_repo, text=True).strip()
            == "1a32ebb7b536873e6403f3faeb6eb8d83ef24d32",
            "ORIGINAL_NEW_SOURCE_SHA",
        )
    sys.path.insert(0, args.new_repo)
    from packages.common import migrate
    from tests.integration_helpers import apply_migrations_from_zero
    from tests.test_river_ts_dual_write_integration import _seed_authority, _write_rivqdown

    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regnamespace('hydro'),to_regclass('public.schema_migrations'),"
                "current_setting('server_version')"
            )
            row = cur.fetchone()
            w.require(row[:2] == (None, None) and row[2].startswith("15.2"), "FRESH_PG152_REQUIRED")
    apply_migrations_from_zero(dsn, through="000058")
    retired = RETIRED_LEDGER_VERSIONS
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for version in retired:
                cur.execute(
                    "INSERT INTO public.schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
                    (version,),
                )
                w.require(
                    not (Path(args.new_repo) / "db/migrations" / version).exists()
                    and not (Path(args.old_repo) / "db/migrations" / version).exists(),
                    "RETIRED_SQL_FILE_PRESENT",
                )
            cur.execute("SELECT version FROM public.schema_migrations ORDER BY version")
            seeded = [row[0] for row in cur.fetchall()]
    w.require(all(version in seeded for version in retired), "HISTORICAL_LEDGER_NOT_SEEDED")
    original_ledger = list(seeded)
    artifacts = root / "artifacts"
    artifacts.mkdir()
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        _seed_authority(conn, output_uri="s3://nhms/runs/run_dual_write/output/")
    _write_rivqdown(artifacts)
    # Fresh current OLD parser/reader, not the historical oracle's OLD constant.
    command = [
        sys.executable,
        args.oracle,
        "--worker",
        "--repo",
        args.old_repo,
        "--sha",
        w.OLD,
        "--action",
        "parse",
        "--run",
        "run_dual_write",
        "--artifacts",
        str(artifacts),
    ]
    parsed = subprocess.run(command, cwd=args.old_repo, capture_output=True, timeout=180)
    w.private_write(root / "old-parse.stderr", parsed.stderr)
    w.require(parsed.returncode == 0, "ACTUAL_OLD_PARSE")
    request = dict(
        basin_version_id="bv1",
        segment_id="seg-1",
        river_network_version_id="rnv1",
        issue_time="2026-06-01T00:00:00Z",
        variables=["q_down"],
        scenarios=["sc"],
        model_id="m1",
    )
    config = dict(
        repo=args.old_repo,
        old_sha=w.OLD,
        new_sha=w.NEW,
        staged_new_repo=args.new_repo,
        parse_run_id="i8_narrow_run",
        artifacts_root=str(artifacts),
        object_store_prefix="s3://nhms",
        reads={
            kind: {"request": dict(request, run_id=run)}
            for kind, run in (("legacy", "run_dual_write"), ("narrow", "i8_narrow_run"))
        },
        lock_files=[],
        yd_pids=[],
        authorized_timers=[w.TIMERS[0], w.TIMERS[1]],
        api_health_path="/health",
        yd_health_path="/health",
        public_base_url="https://fixture.invalid",
        proxy={
            "master_pid": os.getpid(),
            "files": {str(root / "proxy.conf"): w.digest(b"immutable admitted fixture\n")},
        },
    )
    w.private_write(root / "config.json", json.dumps(config).encode())
    files = {}
    for name in ("HOLD-state.json", "resume-approved", "foreign.conf", "proxy.conf"):
        path = root / name
        w.private_write(path, b"immutable admitted fixture\n")
        files[str(path)] = {"sha256": w.digest(path.read_bytes())}
    units = {
        name: dict(
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            Result="success",
            MainPID="1",
            ControlGroup="",
            ExecMainStartTimestampMonotonic="0",
        )
        for name in w.TIMERS + w.SERVICES
    }
    # One authorized timer was inactive at admission and must never be started.
    units[w.TIMERS[1]]["ActiveState"] = "inactive"
    w.private_write(
        root / "state.json",
        json.dumps(
            dict(
                units=units,
                files=files,
                old_branch="old",
                fence_epochs=[],
                unit_config_snapshot=w.UNIT_CONFIG_SNAPSHOT,
            )
        ).encode(),
    )
    e = BoundaryExecutor(args, dsn)
    for name, unit in e.s["units"].items():
        unit.update(e.stable_unit_config(name))
    e.save(units=e.s["units"])
    roles = (Path(args.new_repo) / "db/roles/node27_write_roles.sql").read_bytes()
    e.run(
        [
            "docker",
            "exec",
            "-i",
            "nhms-db",
            "psql",
            "-X",
            "-U",
            "nhms",
            "-d",
            "nhms",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "do_roles=on",
            "-v",
            "do_ownership=on",
            "-v",
            "do_audit=on",
            "-v",
            "strict_audit=on",
        ],
        data=b"SET lock_timeout='5s'; SET statement_timeout='120s';\n" + roles,
    )
    e.audit_roles()
    # Compare the real catalog JSON producer with an independent native OID
    # read, just as prepare compares its result with measured integer 24541.
    # A JSON string OID must fail here, not merely agree with other strings.
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 'hydro.river_timeseries'::regclass::oid")
            admitted_oid = cur.fetchone()[0]
    admission_catalog = e.catalog()
    w.require(
        type(admitted_oid) is int
        and len(admission_catalog) == 1
        and admission_catalog[0]["relname"] == "river_timeseries"
        and admission_catalog[0]["oid"] == admitted_oid,
        "CATALOG_NUMERIC_ADMISSION_MISMATCH",
    )
    e.save(
        old_oid=admission_catalog[0]["oid"],
        ledger_before=e.ledger(),
        legacy_read=e.worker("legacy", sha=w.OLD),
        legacy_catalog=e.table_metadata("hydro", "river_timeseries"),
        forcing_catalog=e.table_metadata("met", "forcing_station_timeseries"),
    )
    if args.case == "happy":
        historical_ledger_admission(e)
        late_prepare_admission(e)
    before = e.catalog(), e.ledger()
    session = psycopg2.connect(dsn) if args.case == "session" else None
    e.inject = args.case if args.case in {"stop", "fence"} else None
    try:
        try:
            e.freeze()
        except w.Refusal:
            w.require(args.case in {"stop", "session", "fence"}, "UNEXPECTED_FREEZE_REFUSAL")
            w.require((e.catalog(), e.ledger()) == before, "DDL_BEFORE_DRAIN")
        else:
            w.require(args.case not in {"stop", "session", "fence"}, "UNSAFE_DRAIN_ACCEPTED")
    finally:
        if session:
            session.close()
    e.inject = None
    e.freeze()
    if args.case not in {"stop", "session", "fence"}:
        migration = Path(args.new_repo) / "db/migrations" / w.EXPAND
        if args.case == "do-before-ledger":
            statements = migrate.split_sql_statements(migration.read_text())
            w.require(len(statements) == 1, "SINGLE_DO_REQUIRED")
            e.sql(statements[0])
            w.require(e.ledger() == before[1], "DO_INSERTED_LEDGER")
        else:
            migrated = e.worker("migrate")
            w.require(migrated.get("file") == w.EXPAND, "CHANGED_WORKER_EXPAND_NOT_RECORDED")
            e.save_file(
                "historical-ledger-changed-worker-positive.json",
                json.dumps(
                    {
                        "site": "worker",
                        "pending": "historical",
                        "result": "accepted-and-committed",
                        "file": migrated.get("file"),
                        "ledger_after": e.ledger(),
                    },
                    default=str,
                ).encode(),
            )
        if args.case in {"happy", "rename", "source", "restart"}:
            w.require(
                e.ledger() == sorted(original_ledger + [w.EXPAND]),
                "EXPAND_DID_NOT_PRESERVE_HISTORICAL_LEDGER",
            )
        else:
            w.require(e.ledger() == original_ledger, "PRE_COMMIT_LEDGER_NOT_ORIGINAL_L")
        e.validate_expand()
        e.sql(
            "INSERT INTO hydro.hydro_run (run_id,run_type,scenario_id,model_id,basin_version_id,"
            "cycle_time,start_time,end_time,status,run_manifest_uri,output_uri) VALUES "
            "('i8_narrow_run','forecast','sc','m1','bv1','2026-06-01','2026-06-01',"
            "'2026-06-01 03:00+00','succeeded','s3://m','s3://nhms/runs/i8_narrow_run/output/')"
        )
        source = artifacts / "runs/run_dual_write/output/demo.rivqdown"
        target = artifacts / "runs/i8_narrow_run/output/demo.rivqdown"
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())
        if args.case == "happy":
            entrypoint_admission(e)
        started = time.monotonic()
        result = e.worker("parse")
        e.save(parse_write_wall_seconds=time.monotonic() - started, parse=result)
        w.require(result["rows_written"] == 12, "REAL_FORMAT_ROWS")
        facts = e.rows(
            "SELECT run_key,basin_version_key,river_network_version_key,river_segment_key,"
            "lead_time_hours,value FROM hydro.river_timeseries ORDER BY valid_time,river_segment_key"
        )
        expected = [
            dict(
                run_key=4002,
                basin_version_key=5001,
                river_network_version_key=6001,
                river_segment_key=7000 + segment,
                lead_time_hours=hour,
                value=float((hour + 1) * segment),
            )
            for hour in range(3)
            for segment in range(1, 5)
        ]
        w.require(facts == expected, "D12_INDEPENDENT_REAL_FORMAT_VALUES")
        e.worker("narrow")
        w.require(e.worker("legacy")["response_sha256"] == e.s["legacy_read"]["response_sha256"], "LEGACY_CHANGED")
        if args.case == "happy":
            e.recovering = False
            e.selected = w.NEW
            e.start_runtime()
            e.ingress_audit()
            cache_boundary(e)
    expected_ledger = (
        sorted(original_ledger + [w.EXPAND])
        if args.case in {"happy", "rename", "source", "restart"}
        else list(original_ledger)
    )
    w.require(e.ledger() == expected_ledger, "PRE_RECOVERY_LEDGER_NOT_TRANSITION")
    ledger = e.ledger()
    w.require(ledger == expected_ledger, "CAPTURED_LEDGER_NOT_EXPECTED")
    narrow_oid = e.s.get("narrow_oid")
    retained = (
        e.rows("SELECT * FROM hydro.river_timeseries ORDER BY valid_time,river_segment_key") if narrow_oid else None
    )
    e = reopen(e)
    e.inject = args.case if args.case in {"rename", "source", "restart"} else None
    if args.case in {"rename", "source", "restart"}:
        child = os.fork()
        if child == 0:
            try:
                e.recover()
            finally:
                os._exit(70)
        _, status = os.waitpid(child, 0)
        w.require(os.WIFSIGNALED(status) and os.WTERMSIG(status) == w.signal.SIGKILL, "HARD_INTERRUPTION_NOT_EXERCISED")
        external = json.loads((root / "system-boundary.json").read_text())
        e.units, e.selected, e.starts = external["units"], external["selected"], external["starts"]
        e = reopen(e)
    else:
        e.recover()
    for _ in range(2):
        w.require(e.ledger() == expected_ledger, "D12_DELETED_HISTORICAL_OR_EXPAND_LEDGER")
        prior_epochs = json.loads(json.dumps(e.s["fence_epochs"]))
        e = reopen(e)
        e.recover()
        e.immutable()
        w.require(e.ledger() == ledger, "LEDGER_CHANGED")
        catalog = {r["relname"]: r["oid"] for r in e.catalog()}
        w.require(
            type(e.s["old_oid"]) is int and catalog["river_timeseries"] == e.s["old_oid"] == admitted_oid,
            "OLD_OID_LOST",
        )
        if narrow_oid:
            w.require(
                type(e.s["narrow_oid"]) is int and catalog["river_timeseries_narrow_rollback"] == narrow_oid,
                "RETAINED_OID_LOST",
            )
            w.require(
                e.rows("SELECT * FROM hydro.river_timeseries_narrow_rollback ORDER BY valid_time,river_segment_key")
                == retained,
                "RETAINED_ROWS_CHANGED",
            )
        for index, epoch in enumerate(prior_epochs):
            w.require(e.s["fence_epochs"][index]["start"] == epoch["start"], "FENCE_START_OVERWRITTEN")
            if epoch.get("validated_restart"):
                w.require(e.s["fence_epochs"][index] == epoch, "CLOSED_EPOCH_CHANGED")
        w.require(e.s["restored_timers"] == [w.TIMERS[0]], "TIMER_AUTHORIZATION")
    if args.case == "happy":
        e.sql("ALTER TABLE hydro.river_timeseries RENAME TO river_timeseries_unknown_smoke;")
        unknown = e.rows("SELECT oid,relname FROM pg_class WHERE relnamespace='hydro'::regnamespace ORDER BY oid")
        starts = e.starts
        try:
            e.recover()
        except w.Refusal as error:
            w.require(str(error) == "CANONICAL_ABSENT_UNKNOWN_RECOVERY", "WRONG_UNKNOWN_RECOVERY_REFUSAL")
        else:
            raise w.Refusal("UNKNOWN_RECOVERY_ACCEPTED")
        w.require(
            e.starts == starts
            and e.ledger() == ledger
            and e.rows("SELECT oid,relname FROM pg_class WHERE relnamespace='hydro'::regnamespace ORDER BY oid")
            == unknown,
            "UNKNOWN_RECOVERY_MUTATED_DATA_OR_STARTED",
        )
        # Undo only this fixture-owned rename, never an inferred recovery repair.
        e.sql("ALTER TABLE hydro.river_timeseries_unknown_smoke RENAME TO river_timeseries;")
        e.recover()
        e.immutable()
    e.save(smoke_case=args.case, smoke_result="PASS", scope="disposable executor recovery, not production")
    print(
        json.dumps(
            {
                "case": args.case,
                "result": "PASS",
                "state": str(root),
                "old_oid": e.s["old_oid"],
                "narrow_oid": narrow_oid,
                "fence_epochs": e.s["fence_epochs"],
            },
            default=str,
        )
    )


def unit_config_oracle(args):
    """Exercise typed immutable comparison without a database or systemd host."""

    root = Path(args.state).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    reports = []

    def fixture(name, *, raw_snapshot=False, phase="PREPARED"):
        case_root = root / name
        case_root.mkdir(mode=0o700)
        repo = case_root / "repo"
        config = {"repo": str(repo), "staged_new_repo": str(repo / "staged")}
        config_bytes = json.dumps(config).encode()
        w.private_write(case_root / "config.json", config_bytes)
        protected = case_root / "protected.conf"
        w.private_write(protected, b"stable protected fixture\n")
        units = {
            unit: {
                "LoadState": "loaded",
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "MainPID": "1",
                "ControlGroup": "",
                "UnitFileState": "enabled",
                "FragmentPath": str(repo / "units" / unit),
                "DropInPaths": "",
                "WorkingDirectory": str(repo),
                "ExecStart": "",
                "Environment": "FIXTURE_ENV=stable",
                "EnvironmentFiles": "/fixture/stable.env (ignore_errors=no)",
                "TimeoutStartUSec": "1min",
                "TimersCalendar": "",
                "ExecMainStartTimestampMonotonic": "0",
                "ExecMainStatus": "0",
                "ConditionResult": "yes",
            }
            for unit in w.TIMERS + w.SERVICES
        }
        state = {
            "phase": phase,
            "units": units,
            "files": {str(protected): {"sha256": w.digest(protected.read_bytes())}},
            "old_branch": "old",
            "fence_epochs": [],
            "unit_config_snapshot": w.UNIT_CONFIG_SNAPSHOT,
            "config_sha256": w.digest(config_bytes),
            "driver_sha256": w.digest(Path(w.__file__).read_bytes()),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "ledger_before": ["fixture"],
        }
        w.private_write(case_root / "state.json", json.dumps(state, sort_keys=True).encode())
        executor = BoundaryExecutor(SimpleNamespace(state=str(case_root)), "unused")
        snapshot = {unit: executor.unit(unit) for unit in units}
        if not raw_snapshot:
            for unit, record in snapshot.items():
                record.update(executor.stable_unit_config(unit))
        executor.save(units=snapshot)
        return executor, case_root, protected

    def close(executor):
        os.close(executor.lock)

    def expect_immutable_refusal(name, expected, mutate):
        executor, _, protected = fixture(name)
        try:
            mutate(executor, protected)
            try:
                executor.immutable()
            except w.Refusal as error:
                w.require(str(error) == expected, "UNIT_CONFIG_ORACLE_WRONG_REFUSAL")
            else:
                raise w.Refusal("UNIT_CONFIG_ORACLE_ACCEPTED_DRIFT")
            reports.append({"case": name, "check": expected})
        finally:
            close(executor)

    executor, _, _ = fixture("runtime-metadata")
    try:
        executor.immutable()
        reports.append({"case": "runtime-metadata", "result": "pass"})
    finally:
        close(executor)

    def command_path(executor, _):
        executor.bus_semantics[w.DISPLAY][0][0] = "/fixture/changed-command"

    def command_argv(executor, _):
        executor.bus_semantics[w.DISPLAY][1][1][-1] = "/fixture/changed-argv"

    def command_ignore_errors(executor, _):
        executor.bus_semantics[w.DISPLAY][1][2] = False

    def command_order(executor, _):
        executor.bus_semantics[w.DISPLAY].reverse()

    def calendar_expression(executor, _):
        executor.bus_semantics[w.TIMERS[0]][1][1] = "Tue *-*-* 05:00:00"

    def calendar_base(executor, _):
        executor.bus_semantics[w.TIMERS[0]][1][0] = "OnStartupSec"

    def calendar_order(executor, _):
        executor.bus_semantics[w.TIMERS[0]].reverse()

    for name, mutate in (
        ("exec-path", command_path),
        ("exec-complete-argv", command_argv),
        ("exec-ignore-errors", command_ignore_errors),
        ("exec-command-order", command_order),
        ("calendar-expression", calendar_expression),
        ("calendar-base", calendar_base),
        ("calendar-order", calendar_order),
    ):
        expect_immutable_refusal(name, "UNIT_CONFIG_CHANGED", mutate)

    def property_drift(key, value):
        def mutate(executor, _):
            executor.units[w.DISPLAY][key] = value

        return mutate

    for name, key, value in (
        ("environment", "Environment", "FIXTURE_ENV=changed"),
        ("environment-files", "EnvironmentFiles", "/fixture/changed.env (ignore_errors=no)"),
        ("environment-files-ignore-errors", "EnvironmentFiles", "/fixture/stable.env (ignore_errors=yes)"),
        ("fragment-path", "FragmentPath", "/fixture/changed.service"),
        ("drop-in-paths", "DropInPaths", "/fixture/changed.conf"),
        ("working-directory", "WorkingDirectory", "/fixture/changed-root"),
        ("unit-file-state", "UnitFileState", "disabled"),
        ("timeout", "TimeoutStartUSec", "2min"),
    ):
        expect_immutable_refusal(name, "UNIT_CONFIG_CHANGED", property_drift(key, value))

    def protected_file(_, path):
        w.private_write(path, b"changed protected fixture\n")

    expect_immutable_refusal("protected-file", "UNIT_ENV_HOLD_OR_FOREIGN_FILE_CHANGED", protected_file)

    def bus_fault(fault):
        def mutate(executor, _):
            executor.bus_fault = fault

        return mutate

    for fault, expected in (
        ("resolution", "SYSTEMD_UNIT_RESOLUTION_INVALID"),
        ("signature", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("envelope", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("timer-arity", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("timer-bool-next", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("exec-arity", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("exec-bool-metadata", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("exec-argv-nonstring", "SYSTEMD_TYPED_PROPERTY_INVALID"),
        ("exec-ignore-errors-int", "SYSTEMD_TYPED_PROPERTY_INVALID"),
    ):
        expect_immutable_refusal("malformed-" + fault, expected, bus_fault(fault))

    def invoke_main(case_root, command, factory):
        original_executor, original_argv = w.Executor, sys.argv
        w.Executor = factory
        sys.argv = [str(Path(w.__file__).resolve()), command, "--state", str(case_root), "--go", "Danker"]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                returncode = w.main()
        finally:
            w.Executor, sys.argv = original_executor, original_argv
        return returncode, json.loads(output.getvalue())

    def old_snapshot_refusal(name, command, phase):
        executor, case_root, _ = fixture(name, phase=phase)
        state_path = case_root / "state.json"
        old = json.loads(state_path.read_text())
        del old["unit_config_snapshot"]
        old["phase"] = phase
        w.private_write(state_path, json.dumps(old, sort_keys=True).encode())
        before = state_path.read_bytes()
        close(executor)
        instances = []

        def factory(cli_args):
            instance = BoundaryExecutor(SimpleNamespace(state=str(case_root)), "unused", cli_args=cli_args)
            instances.append(instance)
            return instance

        returncode, receipt = invoke_main(case_root, command, factory)
        for instance in instances:
            close(instance)
        w.require(
            returncode == 1
            and receipt["check"] == "FRESH_TYPED_UNIT_SNAPSHOT_REQUIRED"
            and receipt["phase"] == "INITIALIZATION_REFUSED"
            and not instances
            and state_path.read_bytes() == before,
            "OLD_UNIT_SNAPSHOT_REWRITTEN_OR_ACCEPTED",
        )
        reports.append({"case": name, "check": receipt["check"], "state_unchanged": True})

    old_snapshot_refusal("old-prepared", "window", "PREPARED")
    old_snapshot_refusal("old-failed", "recover", "FORWARD_FAILED_RECOVERY_REQUIRED")

    class AdmissionExecutor(BoundaryExecutor):
        def __init__(self, fixture_args, dsn, *, cli_args=None):
            super().__init__(fixture_args, dsn, cli_args=cli_args)
            self.window_worker_calls = []

        def ledger(self):
            return list(self.s["ledger_before"])

        def worker(self, action, **_):
            self.window_worker_calls.append(action)
            raise w.Refusal("UNIT_CONFIG_WINDOW_GATE_PASSED")

    def window_gate(name, expected, mutate=None, expect_worker=False):
        executor, case_root, protected = fixture(name)
        try:
            if mutate is not None:
                mutate(executor, protected)
            external_units = json.loads(json.dumps(executor.units))
            semantics = json.loads(json.dumps(executor.bus_semantics))
            state_path = case_root / "state.json"
            before = state_path.read_bytes()
        finally:
            close(executor)
        instances = []

        def factory(cli_args):
            instance = AdmissionExecutor(SimpleNamespace(state=str(case_root)), "unused", cli_args=cli_args)
            instance.units = json.loads(json.dumps(external_units))
            instance.reset_systemd_fixture()
            instance.bus_semantics = json.loads(json.dumps(semantics))
            instances.append(instance)
            return instance

        returncode, receipt = invoke_main(case_root, "window", factory)
        for instance in instances:
            close(instance)
        actions = instances[0].system_actions if instances else []
        worker_calls = instances[0].window_worker_calls if instances else []
        w.require(
            returncode == 1
            and receipt["check"] == expected
            and actions == []
            and worker_calls == (["preparse"] if expect_worker else [])
            and state_path.read_bytes() == before,
            "WINDOW_UNIT_CONFIG_GATE_MUTATED",
        )
        reports.append(
            {
                "case": name,
                "check": expected,
                "worker_calls": worker_calls,
                "state_unchanged": True,
                "system_actions": actions,
            }
        )

    window_gate("window-runtime-metadata", "UNIT_CONFIG_WINDOW_GATE_PASSED", expect_worker=True)
    window_gate("window-path-drift", "UNIT_CONFIG_CHANGED", command_path)

    if args.original_executor:
        original = Path(args.original_executor).resolve()
        w.require(original.is_file(), "ORIGINAL_EXECUTOR_REQUIRED")
        baseline = load(original, "window_executor_original_unit_config")
        executor, _, _ = fixture("original-volatile-red", raw_snapshot=True)
        try:
            try:
                baseline.Executor.immutable(executor)
            except BaseException as error:
                w.require(str(error) == "UNIT_CONFIG_CHANGED", "ORIGINAL_VOLATILE_WRONG_REFUSAL")
            else:
                raise w.Refusal("ORIGINAL_VOLATILE_METADATA_ACCEPTED")
            reports.append({"case": "original-volatile-red", "check": "UNIT_CONFIG_CHANGED"})
        finally:
            close(executor)

    receipt = {
        "case": "unit-config",
        "result": "PASS",
        "state": str(root),
        "reports": reports,
        "scope": "typed unit comparator and pre-T0 admission only; no database or production operation",
    }
    w.private_write(root / "unit-config-oracle.json", json.dumps(receipt, sort_keys=True).encode())
    print(json.dumps(receipt, sort_keys=True))


def historical_ledger_admission(e):
    """Actual prepare and migrate admission against a ledger-only retired catalog."""
    original_executor, original_safe_path, original_argv, original_getuid = (
        w.Executor,
        w.safe_path,
        sys.argv,
        os.getuid,
    )
    f24_execute = Path(__file__).resolve().parents[2] / (
        "timeseries-narrow-store-expand-contract/receipts/issue-1987-execution-tools/window_execute.py.txt"
    )
    if f24_execute.is_file():
        f24_bytes = f24_execute.read_bytes()
    else:
        f24_bytes = subprocess.check_output(
            [
                "git",
                "show",
                "f24c3fb37be392300052c0d0ebf0ee35366a4599:openspec/changes/"
                "timeseries-narrow-store-expand-contract/receipts/issue-1987-execution-tools/"
                "window_execute.py.txt",
            ]
        )
        f24_execute = e.root / "f24-window_execute.py"
        w.private_write(f24_execute, f24_bytes)
    w.require(
        w.digest(f24_bytes) == "75450451c063e8731d66fd02c64cb73a52e96fa785b0d4e434653698be13e83b",
        "F24_EXECUTE_HASH_MISMATCH",
    )
    f24 = load(f24_execute, "window_executor_f24")
    w.require(f24.NEW == "1a32ebb7b536873e6403f3faeb6eb8d83ef24d32", "F24_NEW_NOT_RETAINED")
    w.require(bool(e.fixture.original_new_repo), "ORIGINAL_NEW_REPO_REQUIRED")
    changed = Path(w.__file__).read_bytes()
    before_catalog, before_ledger = e.catalog(), e.ledger()
    w.require(
        all(version in before_ledger for version in RETIRED_LEDGER_VERSIONS) and w.EXPAND not in before_ledger,
        "HISTORICAL_LEDGER_NOT_VISIBLE",
    )
    extra_current = "000058_hot_timeseries_chunk_interval_3d.sql"
    w.require(
        extra_current in before_ledger and extra_current not in RETIRED_LEDGER_VERSIONS, "EXTRA_CURRENT_NOT_APPLIED"
    )
    extra_row = e.rows("SELECT version, applied_at FROM public.schema_migrations WHERE version='" + extra_current + "'")
    w.require(len(extra_row) == 1, "EXTRA_CURRENT_ROW_MISSING")
    reports = []
    extra_removed = False

    def restore_extra_row():
        nonlocal extra_removed
        applied_at = str(extra_row[0]["applied_at"])
        current = e.rows(
            "SELECT version, applied_at FROM public.schema_migrations WHERE version='" + extra_current + "'"
        )
        if current != extra_row:
            e.sql("DELETE FROM public.schema_migrations WHERE version='" + extra_current + "'")
            e.sql(
                "INSERT INTO public.schema_migrations (version, applied_at) VALUES ('"
                + extra_current
                + "', TIMESTAMPTZ '"
                + applied_at
                + "')"
            )
        extra_removed = False
        w.require(
            e.rows("SELECT version, applied_at FROM public.schema_migrations WHERE version='" + extra_current + "'")
            == extra_row,
            "EXTRA_CURRENT_ROW_NOT_RESTORED",
        )

    def worker_migrate(module, *, repo=None, sha=None):
        repo = str(Path(repo or e.fixture.new_repo).resolve())
        sha = sha or module.NEW
        python = Path(repo) / ".venv/bin/python"
        w.require(python.is_file() and os.access(python, os.X_OK), "PRIVATE_PYTHON_MISSING")
        source = Path(module.__file__).resolve()
        wrapper = e.root / ("historical-worker-wrapper-" + w.digest(str(source).encode())[:12] + ".py")
        if not wrapper.exists():
            w.private_write(
                wrapper,
                (
                    "import importlib.machinery\n"
                    "import importlib.util\n"
                    "import os\n"
                    "import sys\n"
                    "from psycopg2.extensions import parse_dsn\n"
                    "source = " + repr(str(source)) + "\n"
                    "loader = importlib.machinery.SourceFileLoader('historical_worker_under_test', source)\n"
                    "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
                    "mod = importlib.util.module_from_spec(spec)\n"
                    "sys.modules[loader.name] = mod\n"
                    "loader.exec_module(mod)\n"
                    "identity = parse_dsn(os.environ['DATABASE_URL'])\n"
                    "mod._WORKER_DATABASE = identity['dbname']\n"
                    "mod._WORKER_PORT = identity['port']\n"
                    "mod._WORKER_USERS = dict.fromkeys(('parse', 'read', 'admin'), identity['user'])\n"
                    "sys.argv = [source, *sys.argv[1:]]\n"
                    "raise SystemExit(mod.main())\n"
                ).encode(),
            )
        argv = [
            str(python),
            str(wrapper),
            "worker",
            "--state",
            str(e.root),
            "--action",
            "migrate",
            "--repo",
            repo,
            "--sha",
            sha,
        ]
        env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update(
            DATABASE_URL=e.dsn,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
        )
        proc = subprocess.run(argv, cwd=repo, env=env, capture_output=True, timeout=150)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        e.seq += 1
        e.save()
        stdout_name = f"historical-worker-{e.seq}.stdout"
        stderr_name = f"historical-worker-{e.seq}.stderr"
        e.save_file(stdout_name, stdout.encode())
        e.save_file(stderr_name, stderr.encode())
        payload = json.loads(stdout) if stdout.strip() else {}
        evidence = {
            "python": str(python),
            "cwd": repo,
            "sha": sha,
            "source": str(source),
            "wrapper": str(wrapper),
            "stdout": stdout_name,
            "stderr": stderr_name,
            "returncode": proc.returncode,
        }
        return proc.returncode, payload, stderr, evidence

    def require_unchanged(code):
        w.require(e.catalog() == before_catalog and e.ledger() == before_ledger, code)

    def require_worker_pending_refusal(rc, payload, stderr, code):
        w.require(
            rc == 1
            and payload.get("error_type") == "Refusal"
            and "PENDING_NOT_EXACT_000059" in stderr
            and "DSN_DESTINATION_MISMATCH" not in stderr
            and "LIVE_DB_IDENTITY" not in stderr
            and "MODULE_ORIGIN_MISMATCH" not in stderr
            and "WORKER_SHA" not in stderr
            and "CREDENTIAL_ROLE_MISMATCH" not in stderr,
            code,
        )

    try:
        e.sql("DELETE FROM public.schema_migrations WHERE version='" + extra_current + "'")
        extra_removed = True
        extra_ledger = e.ledger()
        w.require(extra_current not in extra_ledger and w.EXPAND not in extra_ledger, "EXTRA_PENDING_NOT_CREATED")
        rc, payload, stderr, evidence = worker_migrate(w)
        require_worker_pending_refusal(rc, payload, stderr, "WORKER_EXTRA_PENDING_ACCEPTED")
        w.require(e.catalog() == before_catalog and e.ledger() == extra_ledger, "WORKER_EXTRA_PENDING_SIDE_EFFECT")
        reports.append({"site": "worker", "pending": "extra", "returncode": rc, "payload": payload, "child": evidence})
        restore_extra_row()
        e.sql("INSERT INTO public.schema_migrations (version) VALUES ('" + w.EXPAND + "')")
        rc, payload, stderr, evidence = worker_migrate(w)
        require_worker_pending_refusal(rc, payload, stderr, "WORKER_ZERO_PENDING_ACCEPTED")
        w.require(
            e.catalog() == before_catalog and e.ledger() == sorted(before_ledger + [w.EXPAND]),
            "WORKER_ZERO_PENDING_SIDE_EFFECT",
        )
        reports.append({"site": "worker", "pending": "zero", "returncode": rc, "payload": payload, "child": evidence})
        e.sql("DELETE FROM public.schema_migrations WHERE version='" + w.EXPAND + "'")
        w.require(e.ledger() == before_ledger, "ZERO_PENDING_LEDGER_NOT_RESTORED")
        rc, payload, stderr, evidence = worker_migrate(f24, repo=e.fixture.original_new_repo, sha=f24.NEW)
        require_worker_pending_refusal(rc, payload, stderr, "F24_WORKER_SHOULD_REFUSE_HISTORICAL")
        require_unchanged("F24_WORKER_HISTORICAL_SIDE_EFFECT")
        reports.append(
            {"site": "worker-f24", "pending": "historical", "returncode": rc, "payload": payload, "child": evidence}
        )
    finally:
        restore_extra_row()
        if w.EXPAND in e.ledger() and e.catalog() == before_catalog:
            e.sql("DELETE FROM public.schema_migrations WHERE version='" + w.EXPAND + "'")

    assets = e.root / "historical-prepare-assets"
    assets.mkdir(mode=0o700)
    hold = "/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold/state.json"
    paths = {hold: assets / "state.json", str(Path(hold).parent / "resume-approved"): assets / "resume-approved"}
    w.private_write(paths[hold], b'{"phase":"lifted-and-fences-removed"}')
    w.private_write(paths[str(Path(hold).parent / "resume-approved")], b"fixture approval\n")
    config = json.loads(json.dumps(e.c))
    config.update(
        repo="/home/nwm/NWM",
        restore_branch="new",
        restore_ref="refs/remotes/fixture/new",
        env_files=[str(assets / "fixture.env")],
        protected_files=list(paths),
        lock_files=sorted(w.LOCKS),
        admission={},
    )
    w.private_write(assets / "fixture.env", b"# fixture-only installed environment\n")
    for key in ("go", "d12", "capacity", "artifacts", "governance"):
        path = assets / (key + ".evidence")
        data = ("fixture boundary evidence: " + key).encode()
        w.private_write(path, data)
        config["admission"][key] = {"path": str(path), "sha256": w.digest(data)}
    config_path = e.root / "historical-prepare-config.json"
    w.private_write(config_path, json.dumps(config).encode())
    original_config = json.loads(json.dumps(config))
    original_config.update(new_sha=f24.NEW, staged_new_repo=e.fixture.original_new_repo)
    original_config_path = e.root / "historical-prepare-original-config.json"
    w.private_write(original_config_path, json.dumps(original_config).encode())
    external_units = {}
    for name in w.TIMERS + w.SERVICES:
        fragment = assets / name
        w.private_write(fragment, ("# fixture installed unit: " + name + "\n").encode())
        unit = dict.fromkeys(w.PROPS, "")
        unit.update(
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            Result="success",
            MainPID="1",
            UnitFileState="enabled",
            FragmentPath=str(fragment),
            WorkingDirectory=config["repo"],
            ExecStart=config["repo"] + "/fixture-command",
            ExecMainStatus="0",
            ExecMainStartTimestampMonotonic="0",
        )
        if name == "nhms-node27-timeseries-compression.service":
            unit["TimeoutStartUSec"] = "1h 5min 40s"
        external_units[name] = unit
    fixture_oid = int(e.sql("SELECT 'hydro.river_timeseries'::regclass::oid"))
    prepare = original_executor.prepare
    replacements = sum(type(value) is int and value == 24541 for value in prepare.__code__.co_consts)
    w.require(replacements == 1, "FROZEN_PREPARE_OID_BINDING_CHANGED")
    constants = tuple(
        fixture_oid if type(value) is int and value == 24541 else value for value in prepare.__code__.co_consts
    )
    bound_prepare = FunctionType(
        prepare.__code__.replace(co_consts=constants),
        prepare.__globals__,
        prepare.__name__,
        prepare.__defaults__,
        prepare.__closure__,
    )
    f24_prepare = f24.Executor.prepare
    f24_replacements = sum(type(value) is int and value == 24541 for value in f24_prepare.__code__.co_consts)
    w.require(f24_replacements == 1, "F24_PREPARE_OID_BINDING_CHANGED")
    f24_bound_prepare = FunctionType(
        f24_prepare.__code__.replace(
            co_consts=tuple(
                fixture_oid if type(value) is int and value == 24541 else value
                for value in f24_prepare.__code__.co_consts
            )
        ),
        f24_prepare.__globals__,
        f24_prepare.__name__,
        f24_prepare.__defaults__,
        f24_prepare.__closure__,
    )
    lock_paths = {path: str(assets / ("lock-" + str(index))) for index, path in enumerate(sorted(w.LOCKS))}

    class HistoricalPrepareExecutor(BoundaryExecutor):
        def run(self, argv, **kwargs):
            if argv == ["docker", "inspect", "nhms-db"]:
                return json.dumps(
                    [
                        {
                            "Id": "5cfa71472de87f926d1fd7c085edc3ff6ab8cea445e899be3b2676bb2d607e5f",
                            "State": {"Running": True},
                            "Image": IMAGE,
                            "Mounts": [
                                {
                                    "Source": "/data/GHDC/nhms-primary/pgdata",
                                    "Destination": "/home/postgres/pgdata/data",
                                }
                            ],
                        }
                    ]
                ).encode()
            if argv in (
                ["systemctl", "--user", "list-unit-files", "--no-pager", "--no-legend"],
                ["systemctl", "--user", "list-units", "--all", "--no-pager", "--no-legend"],
                ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
            ):
                return b""
            if argv == ["docker", "exec", "nhms-db", "pg_dump", "-U", "nhms", "-d", "nhms", "--schema-only"]:
                raise w.Refusal("PREPARE_REACHED_POST_PENDING_DUMP")
            return super().run(argv, **kwargs)

        def git(self, *args, repo=None):
            expected_new = self.c["new_sha"]
            staged = self.c["staged_new_repo"]
            if repo is not None:
                w.require(str(repo) == staged, "UNKNOWN_STAGED_GIT_BOUNDARY")
                if args == ("rev-parse", "HEAD"):
                    return expected_new
                if args == ("status", "--porcelain=v1", "--untracked-files=all"):
                    return ""
                raise w.Refusal("UNKNOWN_STAGED_GIT_BOUNDARY")
            if args == ("symbolic-ref", "--short", "HEAD"):
                return "old"
            if args in (
                ("rev-parse", "--verify", "refs/heads/new^{commit}"),
                ("rev-parse", "--verify", "refs/remotes/fixture/new^{commit}"),
            ):
                return expected_new
            return super().git(*args, repo=repo)

        def drain(self):
            original = self.c["lock_files"]
            self.c["lock_files"] = [lock_paths[path] for path in original]
            try:
                return super().drain()
            finally:
                self.c["lock_files"] = original

        def audit_roles(self):
            original = self.repo
            self.repo = Path(self.fixture.old_repo)
            try:
                return super().audit_roles()
            finally:
                self.repo = original

        def public_probe(self, stopped=False):
            self.save(public_probe={"status": 200, "at": w.now()})

        def proxy_binding(self):
            return {"boundary": "isolated-no-proxy"}

    class ChangedHistoricalPrepareExecutor(HistoricalPrepareExecutor):
        prepare = bound_prepare

    class F24HistoricalPrepareExecutor(HistoricalPrepareExecutor):
        prepare = f24_bound_prepare

    def safe_boundary(path, private=False):
        return original_safe_path(paths.get(str(path), path), private)

    saved_env = {key: os.environ.get(key) for key in ("DATABASE_URL", "I8_INGEST_DSN", "I8_DISPLAY_DSN")}

    def invoke_prepare(state_name, executor_cls, module, *, extra_pending=False, insert_expand=False):
        nonlocal extra_removed
        root = e.root / ("historical-prepare-" + state_name)
        instances = []

        def factory(cli_args):
            fixture = SimpleNamespace(**{**vars(e.fixture), "state": str(root)})
            instance = executor_cls(fixture, e.dsn, cli_args=cli_args)
            instance.units = json.loads(json.dumps(external_units))
            instance.reset_systemd_fixture()
            instances.append(instance)
            return instance

        saved_module_executor = module.Executor
        saved_module_safe_path = module.safe_path
        saved_module_argv = sys.argv
        module.Executor = factory
        module.safe_path = safe_boundary
        os.getuid = lambda: 1005
        selected_config = config_path if module is w else original_config_path
        sys.argv = [
            str(Path(module.__file__).resolve()),
            "prepare",
            "--state",
            str(root),
            "--config",
            str(selected_config),
        ]
        os.environ.update({key: e.dsn for key in saved_env})
        output = io.StringIO()
        try:
            if extra_pending:
                e.sql("DELETE FROM public.schema_migrations WHERE version='" + extra_current + "'")
                extra_removed = True
            if insert_expand:
                e.sql("INSERT INTO public.schema_migrations (version) VALUES ('" + w.EXPAND + "')")
            with contextlib.redirect_stdout(output):
                rc = module.main()
        finally:
            for instance in instances:
                os.close(instance.lock)
            if extra_pending:
                restore_extra_row()
            if insert_expand and w.EXPAND in e.ledger():
                e.sql("DELETE FROM public.schema_migrations WHERE version='" + w.EXPAND + "'")
            module.Executor = saved_module_executor
            module.safe_path = saved_module_safe_path
            sys.argv = saved_module_argv
            os.getuid = original_getuid
            w.Executor, w.safe_path, sys.argv, os.getuid = (
                original_executor,
                original_safe_path,
                original_argv,
                original_getuid,
            )
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        receipt = json.loads(output.getvalue()) if output.getvalue().strip() else {}
        e.save_file("historical-prepare-" + state_name + ".stdout", output.getvalue().encode())
        return rc, receipt

    try:
        rc, receipt = invoke_prepare("extra", ChangedHistoricalPrepareExecutor, w, extra_pending=True)
        w.require(
            rc == 1 and receipt.get("check") == "PENDING_MIGRATIONS_NOT_EXACT",
            "PREPARE_EXTRA_PENDING_ACCEPTED",
        )
        require_unchanged("PREPARE_EXTRA_PENDING_SIDE_EFFECT")
        reports.append({"site": "prepare", "pending": "extra", "returncode": rc, "receipt": receipt})
        rc, receipt = invoke_prepare("zero", ChangedHistoricalPrepareExecutor, w, insert_expand=True)
        w.require(
            rc == 1 and receipt.get("check") == "PENDING_MIGRATIONS_NOT_EXACT",
            "PREPARE_ZERO_PENDING_ACCEPTED",
        )
        require_unchanged("PREPARE_ZERO_PENDING_SIDE_EFFECT")
        reports.append({"site": "prepare", "pending": "zero", "returncode": rc, "receipt": receipt})
        rc, receipt = invoke_prepare("f24-historical", F24HistoricalPrepareExecutor, f24)
        w.require(
            rc == 1 and receipt.get("check") == "PENDING_MIGRATIONS_NOT_EXACT",
            "F24_PREPARE_SHOULD_REFUSE_HISTORICAL",
        )
        require_unchanged("F24_PREPARE_HISTORICAL_SIDE_EFFECT")
        reports.append({"site": "prepare-f24", "pending": "historical", "returncode": rc, "receipt": receipt})
        rc, receipt = invoke_prepare("historical", ChangedHistoricalPrepareExecutor, w)
        w.require(
            rc == 1 and receipt.get("check") == "PREPARE_REACHED_POST_PENDING_DUMP",
            "PREPARE_HISTORICAL_NOT_ADMITTED",
        )
        require_unchanged("PREPARE_HISTORICAL_SIDE_EFFECT")
        reports.append({"site": "prepare", "pending": "historical", "returncode": rc, "receipt": receipt})

    finally:
        restore_extra_row()
        w.Executor, w.safe_path, sys.argv, os.getuid = (
            original_executor,
            original_safe_path,
            original_argv,
            original_getuid,
        )

    e.save_file(
        "historical-ledger-admission.json",
        json.dumps(
            {
                "retired": list(RETIRED_LEDGER_VERSIONS),
                "ledger_before": before_ledger,
                "f24_execute_sha256": w.digest(f24_bytes),
                "changed_execute_sha256": w.digest(changed),
                "changed_worker_positive": "deferred-to-happy-path-real-e.worker(migrate)",
                "worker_protocol": (
                    "Fresh selected-repo .venv/bin/python child; cwd=repo; argv worker "
                    "--state/--action migrate/--repo/--sha; DATABASE_URL env-only; "
                    "no PYTHONPATH/PYTHONHOME; private wrapper only patches "
                    "_WORKER_DATABASE/_WORKER_PORT/_WORKER_USERS."
                ),
                "cases": reports,
                "limitation": (
                    "Actual prepare control flow with fixture expected-OID substitution, "
                    "not byte-identical execution. Worker identity gates remain production constants. "
                    "Changed-worker historical accept is the later real committed expand on this catalog, "
                    "not an apply_migration interceptor."
                ),
            },
            default=str,
        ).encode(),
    )

    w.require(e.catalog() == before_catalog and e.ledger() == before_ledger, "HISTORICAL_ORACLE_LEFT_RESIDUE")


def late_prepare_admission(e):
    """Real prepare/save/run failure, then disk-reconstructed main invocations."""
    original_executor, original_safe_path, original_argv = w.Executor, w.safe_path, sys.argv
    root, assets = e.root / "late-prepare", e.root / "late-prepare-assets"
    assets.mkdir(mode=0o700)
    hold = "/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold/state.json"
    paths = {hold: assets / "state.json", str(Path(hold).parent / "resume-approved"): assets / "resume-approved"}
    w.private_write(paths[hold], b'{"phase":"lifted-and-fences-removed"}')
    w.private_write(paths[str(Path(hold).parent / "resume-approved")], b"fixture approval\n")
    config = json.loads(json.dumps(e.c))
    config.update(
        repo="/home/nwm/NWM",
        restore_branch="new",
        restore_ref="refs/remotes/fixture/new",
        env_files=[str(assets / "fixture.env")],
        protected_files=list(paths),
        lock_files=sorted(w.LOCKS),
        admission={},
    )
    w.private_write(assets / "fixture.env", b"# fixture-only installed environment\n")
    for key in ("go", "d12", "capacity", "artifacts", "governance"):
        path = assets / (key + ".evidence")
        data = ("fixture boundary evidence: " + key).encode()
        w.private_write(path, data)
        config["admission"][key] = {"path": str(path), "sha256": w.digest(data)}
    config_path = e.root / "late-prepare-config.json"
    w.private_write(config_path, json.dumps(config).encode())
    external_units = {}
    for name in w.TIMERS + w.SERVICES:
        fragment = assets / name
        w.private_write(fragment, ("# fixture installed unit: " + name + "\n").encode())
        unit = dict.fromkeys(w.PROPS, "")
        unit.update(
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            Result="success",
            MainPID="1",
            UnitFileState="enabled",
            FragmentPath=str(fragment),
            WorkingDirectory=config["repo"],
            ExecStart=config["repo"] + "/fixture-command",
            ExecMainStatus="0",
            ExecMainStartTimestampMonotonic="0",
        )
        if name == "nhms-node27-timeseries-compression.service":
            unit["TimeoutStartUSec"] = "1h 5min 40s"
        external_units[name] = unit
    before_units = json.loads(json.dumps(external_units))
    before_catalog, before_ledger = e.catalog(), e.ledger()
    fixture_oid = int(e.sql("SELECT 'hydro.river_timeseries'::regclass::oid"))
    w.require(
        len(before_catalog) == 1 and before_catalog[0]["oid"] == fixture_oid,
        "LATE_PREPARE_REAL_OID_REQUIRED",
    )
    prepare = original_executor.prepare
    replacements = sum(type(value) is int and value == 24541 for value in prepare.__code__.co_consts)
    w.require(replacements == 1, "FROZEN_PREPARE_OID_BINDING_CHANGED")
    constants = tuple(
        fixture_oid if type(value) is int and value == 24541 else value for value in prepare.__code__.co_consts
    )
    bound_prepare = FunctionType(
        prepare.__code__.replace(co_consts=constants),
        prepare.__globals__,
        prepare.__name__,
        prepare.__defaults__,
        prepare.__closure__,
    )
    binding = {
        "original_expected_oid": 24541,
        "fixture_expected_oid": fixture_oid,
        "transformed_constant_count": replacements,
        "source_sha256": w.digest(Path(w.__file__).read_bytes()),
        "limitation": (
            "Actual prepare control flow with fixture expected-OID substitution, not byte-identical execution."
        ),
    }
    lock_paths = {path: str(assets / ("lock-" + str(index))) for index, path in enumerate(sorted(w.LOCKS))}

    class LatePrepareExecutor(BoundaryExecutor):
        prepare = bound_prepare

        def run(self, argv, **kwargs):
            if argv == ["docker", "inspect", "nhms-db"]:
                return json.dumps(
                    [
                        {
                            "Id": "5cfa71472de87f926d1fd7c085edc3ff6ab8cea445e899be3b2676bb2d607e5f",
                            "State": {"Running": True},
                            "Image": IMAGE,
                            "Mounts": [
                                {
                                    "Source": "/data/GHDC/nhms-primary/pgdata",
                                    "Destination": "/home/postgres/pgdata/data",
                                }
                            ],
                        }
                    ]
                ).encode()
            if argv in (
                ["systemctl", "--user", "list-unit-files", "--no-pager", "--no-legend"],
                ["systemctl", "--user", "list-units", "--all", "--no-pager", "--no-legend"],
                ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
            ):
                return b""
            if argv == ["docker", "exec", "nhms-db", "pg_dump", "-U", "nhms", "-d", "nhms", "--schema-only"]:
                # Only the external pg_dump process is substituted. The real
                # executor.run persists child/returncode and raises COMMAND_FAILED.
                self.dump_invocations += 1
                return original_executor.run(
                    self,
                    [sys.executable, "-c", "raise SystemExit(23)"],
                    cwd=self.fixture.old_repo,
                    **kwargs,
                )
            return super().run(argv, **kwargs)

        def git(self, *args, repo=None):
            if repo is not None:
                w.require(str(repo) == self.fixture.new_repo, "UNKNOWN_STAGED_GIT_BOUNDARY")
                if args == ("rev-parse", "HEAD"):
                    return w.NEW
                if args == ("status", "--porcelain=v1", "--untracked-files=all"):
                    return ""
                raise w.Refusal("UNKNOWN_STAGED_GIT_BOUNDARY")
            if args == ("symbolic-ref", "--short", "HEAD"):
                return "old"
            if args in (
                ("rev-parse", "--verify", "refs/heads/new^{commit}"),
                ("rev-parse", "--verify", "refs/remotes/fixture/new^{commit}"),
            ):
                return w.NEW
            return super().git(*args, repo=repo)

        def drain(self):
            original = self.c["lock_files"]
            self.c["lock_files"] = [lock_paths[path] for path in original]
            try:
                return super().drain()
            finally:
                self.c["lock_files"] = original

        def audit_roles(self):
            original = self.repo
            self.repo = Path(self.fixture.old_repo)
            try:
                return super().audit_roles()
            finally:
                self.repo = original

    def safe_boundary(path, private=False):
        return original_safe_path(paths.get(str(path), path), private)

    reports = []
    saved_env = {key: os.environ.get(key) for key in ("DATABASE_URL", "I8_INGEST_DSN", "I8_DISPLAY_DSN")}

    def invoke(command, go):
        instances = []

        def factory(cli_args):
            fixture = SimpleNamespace(**{**vars(e.fixture), "state": str(root)})
            instance = LatePrepareExecutor(fixture, e.dsn, cli_args=cli_args)
            instance.units = external_units
            instance.reset_systemd_fixture()
            instance.dump_invocations = 0
            instances.append(instance)
            return instance

        w.Executor = factory
        sys.argv = [str(Path(w.__file__).resolve()), command, "--state", str(root)]
        if command == "prepare":
            sys.argv += ["--config", str(config_path)]
        if go:
            sys.argv += ["--go", "Danker"]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                rc = w.main()
        finally:
            for instance in instances:
                os.close(instance.lock)
        name = command + ("-go" if go else "-no-go")
        e.save_file("late-prepare-" + name + ".stdout", output.getvalue().encode())
        receipt = json.loads(output.getvalue())
        actions = instances[0].system_actions if instances else []
        record = {
            "case": name,
            "returncode": rc,
            "receipt": receipt,
            "system_actions": actions,
            "dump_invocations": instances[0].dump_invocations if instances else 0,
            "serving_unchanged": external_units == before_units,
            "catalog_unchanged": e.catalog() == before_catalog,
            "ledger_unchanged": e.ledger() == before_ledger,
        }
        reports.append(record)
        e.save_file("late-prepare-admission.json", json.dumps({"binding": binding, "cases": reports}).encode())
        return record

    try:
        w.safe_path = safe_boundary
        os.environ.update({key: e.dsn for key in saved_env})
        result = invoke("prepare", False)
        w.require(
            result["returncode"] == 1
            and result["receipt"]["check"] == "COMMAND_FAILED"
            and result["dump_invocations"] == 1
            and not result["system_actions"],
            "POST_SNAPSHOT_PG_DUMP_FAILURE_NOT_REACHED",
        )
        persisted = (root / "state.json").read_bytes()
        snapshot = json.loads(persisted)
        w.require(
            all(
                key in snapshot
                for key in (
                    "old_oid",
                    "units",
                    "files",
                    "ledger_before",
                    "old_branch",
                    "fence_epochs",
                    "unit_config_snapshot",
                )
            )
            and snapshot.get("unit_config_snapshot") == w.UNIT_CONFIG_SNAPSHOT
            and "legacy_read" not in snapshot
            and snapshot.get("child_pid") is None
            and snapshot.get("last_command_rc") == 23,
            "REAL_LATE_PREPARE_SNAPSHOT_REQUIRED",
        )
        e.save_file("late-prepare-persisted.json", persisted)
        e.save_file("late-prepare-pg_dump-failure.txt", (root / "failure-private.txt").read_bytes())
        for command, go, expected in (
            ("recover", False, "RECOVERY_GO_REQUIRED"),
            ("status", False, None),
            ("recover", True, "PREPARATION_INCOMPLETE_NO_AUTOMATIC_RECOVERY"),
        ):
            result = invoke(command, go)
            w.require(
                not result["system_actions"]
                and result["serving_unchanged"]
                and result["catalog_unchanged"]
                and result["ledger_unchanged"],
                "LATE_PREPARE_RECOVERY_MUTATED_SERVING",
            )
            receipt = result["receipt"]
            w.require(
                result["returncode"] == (0 if command == "status" else 1)
                and receipt.get("check") == expected
                and receipt.get("inspection_only")
                and "recovery_command" not in receipt
                and not receipt.get("phase", "").startswith("BLOCKED_FENCE"),
                "LATE_PREPARE_REFUSAL_OR_GUIDANCE",
            )
    finally:
        w.Executor, w.safe_path, sys.argv = original_executor, original_safe_path, original_argv
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def entrypoint_admission(e):
    """Drive the real CLI parser/main with the existing external boundaries."""
    original_executor, original_argv = w.Executor, sys.argv
    config_bytes = (e.root / "config.json").read_bytes()
    admitted = json.loads(json.dumps(e.s))
    admitted.update(
        phase="PREPARED",
        config_sha256=w.digest(config_bytes),
        driver_sha256=w.digest(Path(w.__file__).read_bytes()),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        ledger_before=e.ledger(),
    )
    before_catalog, before_ledger = e.catalog(), e.ledger()
    reports = []

    def invoke(name, state, *, command="recover", go=True, stop_failures=0, owner_refusal=False):
        root = Path("/") if owner_refusal else e.root / ("entrypoint-" + name)
        if owner_refusal:
            w.require(root.stat().st_uid != os.getuid(), "ROOT_OWNERSHIP_FIXTURE_REQUIRED")
        elif command != "prepare":
            root.mkdir(mode=0o700)
            w.private_write(root / "config.json", config_bytes)
            if state is not None:
                w.private_write(root / "state.json", json.dumps(state).encode())
        instances = []
        external_units = json.loads(json.dumps(e.s["units"]))

        def factory(cli_args):
            fixture = SimpleNamespace(**{**vars(e.fixture), "state": str(root)})
            instance = BoundaryExecutor(fixture, e.dsn, cli_args=cli_args)
            instance.units = json.loads(json.dumps(external_units))
            instance.reset_systemd_fixture()
            instance.stop_failures = stop_failures
            instances.append(instance)
            return instance

        w.Executor = factory
        sys.argv = [str(Path(w.__file__).resolve()), command, "--state", str(root)]
        if go:
            sys.argv += ["--go", "Danker"]
        if command == "prepare":
            sys.argv += ["--config", str(e.root / "config.json")]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                rc = w.main()
        finally:
            w.Executor, sys.argv = original_executor, original_argv
            for instance in instances:
                os.close(instance.lock)
        raw = output.getvalue()
        e.save_file("entrypoint-" + name + ".stdout", raw.encode())
        receipt = json.loads(raw)
        actions = instances[0].system_actions if instances else []
        final_units = instances[0].units if instances else external_units
        if stop_failures:
            w.require(
                rc == 1
                and receipt["check"] == "INJECTED_STOP_FAILURE"
                and receipt["phase"] == "BLOCKED_FENCED"
                and any(row["action"] == "stop" and w.DISPLAY in row["units"] for row in actions)
                and final_units[w.DISPLAY]["ActiveState"] == "inactive",
                "ADMITTED_ENTRYPOINT_FAILURE_NOT_FENCED",
            )
        else:
            w.require(not actions and final_units == external_units, "INELIGIBLE_ENTRYPOINT_STOPPED_SERVICES")
            w.require(not receipt.get("phase", "").startswith("BLOCKED_FENCE"), "REFUSAL_CLAIMED_FENCING")
            w.require("recovery_command" not in receipt and receipt.get("inspection_only"), "UNSAFE_RECOVERY_GUIDANCE")
        w.require(e.catalog() == before_catalog and e.ledger() == before_ledger, "ENTRYPOINT_CHANGED_DATABASE")
        reports.append({"case": name, "returncode": rc, "receipt": receipt, "system_actions": actions})
        return rc, receipt

    partial = {
        "phase": "PREPARATION_FAILED",
        "fence_epochs": [],
        "unit_config_snapshot": w.UNIT_CONFIG_SNAPSHOT,
    }
    for name, state in (("config-only", None), ("failed-prepare", partial)):
        for go in (False, True):
            rc, receipt = invoke(name + ("-go" if go else "-no-go"), state, go=go)
            expected = "PREPARATION_INCOMPLETE_NO_AUTOMATIC_RECOVERY" if go else "RECOVERY_GO_REQUIRED"
            w.require(rc == 1 and receipt["check"] == expected, "WRONG_INELIGIBLE_RECOVERY_REFUSAL")
        rc, _ = invoke(name + "-status", state, command="status", go=False)
        w.require(rc == 0, "INCOMPLETE_STATUS_FAILED")
    rc, receipt = invoke("phase-only-admission", {**partial, "phase": "FENCED_DRAINED", "_recovery_active": True})
    w.require(
        rc == 1 and receipt["check"] == "PREPARATION_INCOMPLETE_NO_AUTOMATIC_RECOVERY",
        "PERSISTED_LABEL_ADMITTED_RECOVERY",
    )
    rc, receipt = invoke("complete-no-go", admitted, go=False)
    w.require(rc == 1 and receipt["check"] == "RECOVERY_GO_REQUIRED", "COMPLETE_NO_GO_ACCEPTED")
    rc, receipt = invoke("owned-child-running", {**admitted, "child_pid": os.getpid()})
    w.require(
        rc == 1 and receipt["check"] == "OWNED_COMMAND_STILL_RUNNING_INSPECT_SUPERVISOR",
        "OWNED_CHILD_RECOVERY_ACCEPTED",
    )
    rc, receipt = invoke("ownership-refusal", None, owner_refusal=True)
    w.require(rc == 1 and receipt["check"] == "PRIVATE_STATE_REQUIRED", "WRONG_CONSTRUCTOR_REFUSAL")
    rc, receipt = invoke("actual-failed-prepare", None, command="prepare")
    w.require(rc == 1 and receipt["check"] == "FROZEN_IDENTITY_REQUIRED", "WRONG_PREPARE_REFUSAL")
    rc, receipt = invoke("window-no-go", admitted, command="window", go=False)
    w.require(rc == 1 and receipt["check"] == "PREPARED_SIGNED_GO_REQUIRED", "WINDOW_NO_GO_ACCEPTED")
    invoke("admitted-recovery-failure", admitted, stop_failures=1)
    # Actual window preparse reads the real unparsed fixture. Its first timer
    # stop fails; nested admitted recover's stop fails too; emergency must fence.
    invoke("nested-window-recovery-failure", admitted, command="window", stop_failures=2)
    e.save_file("entrypoint-admission.json", json.dumps(reports, sort_keys=True).encode())


def reopen(e):
    """Discard executor memory; retain only the external simulated system."""
    units, selected, starts = e.units, e.selected, e.starts
    args, dsn = e.fixture, e.dsn
    os.close(e.lock)
    restored = BoundaryExecutor(args, dsn)
    restored.units, restored.selected, restored.starts = units, selected, starts
    restored.reset_systemd_fixture()
    return restored


def cache_boundary(e):
    """Actual two-tier owner + API header adapter, fake HTTP transport only."""
    import psycopg2
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from apps.api.routes.hydro_display import _mvt_response
    from services.tiles import mvt

    engine = create_engine("postgresql+psycopg2://", creator=lambda: psycopg2.connect(e.dsn))
    os.environ["NHMS_MVT_FILE_CACHE_DIR"] = str(e.root / "tile-files")
    generated = []
    mode = "db"
    target_pinned = False
    cycle = "2026-06-01T00:00:00Z"
    original_http = e.http
    e.c["read_guards"] = [{"path": "/api/guard", "status": 422}]

    def transport(path, port=8080):
        w.require(port == 8080, "CACHE_TRANSPORT_PORT")
        body = b"{}"
        headers = {}
        status = 200
        if path == "/api/v1/layers/discharge/cycles?source=gfs":
            body = json.dumps({"data": {"default_cycle": cycle, "cycles": [{"cycle_time": cycle}]}}).encode()
        elif path.startswith("/api/v1/layers/discharge/valid-times?"):
            body = json.dumps({"data": {"valid_times": [cycle]}}).encode()
        elif path == "/api/guard":
            status = 422
        elif path.startswith("/api/v1/tiles/hydro-national/"):
            tile = mvt.TileInput(
                layer_id="hydro:q_down",
                source_id="i8_narrow_run",
                source_version=mode + str(target_pinned) + ("-pinned" if "/gfs/" in path else "-default"),
                valid_time=cycle,
                z=4,
                x=12,
                y=6,
            )
            key = mvt.cache_key(tile)
            with Session(engine) as session:
                targeted = ("/gfs/" in path) == target_pinned
                if mode == "db" and targeted:
                    seeded = mvt.build_raw_tile_response(session, tile, b"retained-cache")
                    w.require(seeded.cache_status in {"miss", "hit"}, "DB_CACHE_SEED")
                    w.require(mvt._read_cache(session, tile, key) is not None, "REAL_DB_CACHE_REQUIRED")
                elif mode == "file" and targeted:
                    w.require(mvt._write_file_cache(key, b"retained-file-cache"), "FILE_CACHE_SEED")
                    w.require(mvt._read_cache(session, tile, key) is None, "FILE_CASE_DB_NOT_EMPTY")
                cached = mvt.read_cached_tile_response(session, tile)
                if cached is None:
                    # Real PostGIS encoding of the actual NEW narrow facts.
                    raw = session.execute(
                        text(
                            "SELECT ST_AsMVT(q,'hydro',4096,'geom') FROM ("
                            "SELECT value, ST_SetSRID(ST_MakePoint(river_segment_key % 4,"
                            "lead_time_hours),3857) AS geom "
                            "FROM hydro.river_timeseries WHERE run_key=(SELECT run_key FROM hydro.hydro_run "
                            "WHERE run_id='i8_narrow_run')) q"
                        )
                    ).scalar_one()
                    w.require(raw, "REAL_SQL_GENERATION_EMPTY")
                    generated.append(path)
                    cached = mvt.build_raw_tile_response(session, tile, bytes(raw))
                response = _mvt_response(cached)
                body, headers = response.body, dict(response.headers)
        else:
            w.require(
                path in {"/api/v1/layers", "/api/v1/tiles/river-network-national/5/25/12.pbf"},
                "UNKNOWN_CACHE_TRANSPORT",
            )
        record = dict(
            path=path,
            port=port,
            status=status,
            headers=headers,
            bytes=len(body),
            sha256=w.digest(body),
            seconds=0,
            at=w.now(),
        )
        e.seq += 1
        e.save_file(f"cache-response-{e.seq}.json", json.dumps(record).encode())
        return record, body

    e.http = transport
    try:
        for mode in ("db", "file"):
            for target_pinned in (False, True):
                before = len(generated)
                try:
                    e.api_proof()
                except w.Refusal as error:
                    w.require(str(error) == "NO_UNCACHED_REAL_PIN_GENERATION_PROOF", "WRONG_CACHE_REFUSAL")
                else:
                    raise w.Refusal("CACHED_HIT_ACCEPTED_AS_GENERATION")
                w.require(len(generated) - before == int(target_pinned), "CACHED_REQUEST_GENERATED_SQL")
        generated.clear()
        mode = "generation"
        proof = e.api_proof()
        w.require(
            len(generated) == 2 and "/gfs/" not in generated[0] and "/gfs/" in generated[1],
            "BOTH_GENERATION_PATHS_REQUIRED",
        )
        e.save(cache_boundary_proof=proof, cache_boundary_sql_paths=generated)
    finally:
        e.http = original_http
        engine.dispose()


def write_budget(args):
    """Fixture provisioner supplies real metadata/artifacts in the isolated DB."""
    import psycopg2

    dsn, identity = guard(args)
    config = json.loads(w.safe_path(args.fixture_config, True).read_text())
    w.require(
        config["parse_run_id"] == "fcst_gfs_2026081912_dg_945b6f0bbf63c5314d47df9481892229",
        "ACTUAL_SMALL_RUN_REQUIRED",
    )
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,parsed_at,timeseries_store FROM hydro.hydro_run WHERE run_id=%s",
                (config["parse_run_id"],),
            )
            w.require(cur.fetchone() == ("succeeded", None, "narrow"), "PROVISIONED_UNPARSED_REAL_FIXTURE_REQUIRED")
    root = Path(args.state).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    w.private_write(root / "config.json", json.dumps(config).encode())
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--container",
        args.container,
        "--state",
        str(root),
        "--repo",
        args.new_repo,
        "--sha",
        w.NEW,
        "--action",
        "parse",
    ]
    started = time.monotonic()
    result = subprocess.run(command, cwd=args.new_repo, capture_output=True, timeout=180)
    elapsed = time.monotonic() - started
    w.private_write(root / "parse.stderr", result.stderr)
    w.require(result.returncode == 0, "ACTUAL_SMALL_DB_WRITE_FAILED")
    parsed = json.loads(result.stdout)
    w.require(parsed["rows_written"] == 512232, "SMALL_REAL_ROW_COUNT")
    receipt = dict(
        result="PASS",
        scope="actual small parser DB-write wall, includes startup/read/QC",
        seconds=elapsed,
        rows_written=512232,
        database=identity["dbname"],
    )
    w.private_write(root / "write-budget.json", json.dumps(receipt).encode())
    print(json.dumps(receipt))


def arguments():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--worker", action="store_true")
    for name in (
        "oracle",
        "old-repo",
        "new-repo",
        "container",
        "state",
        "repo",
        "sha",
        "action",
        "fixture-config",
        "original-new-repo",
    ):
        p.add_argument("--" + name)
    p.add_argument("--original-executor")
    p.add_argument(
        "--case",
        choices=(
            "happy",
            "stop",
            "session",
            "fence",
            "do-before-ledger",
            "rename",
            "source",
            "restart",
            "write-budget",
            "unit-config",
        ),
    )
    args = p.parse_args()
    if args.worker:
        required = ("container", "state", "repo", "sha", "action")
    elif args.case == "unit-config":
        required = ("state",)
    elif args.case == "write-budget":
        required = ("container", "state", "new_repo", "fixture_config")
    else:
        required = ("oracle", "old_repo", "new_repo", "container", "state", "case")
        if args.case == "happy":
            required = required + ("original_new_repo",)
    if args.original_executor and args.case != "unit-config":
        p.error("--original-executor requires --case unit-config")
    for name in required:
        if not getattr(args, name):
            p.error("missing --" + name.replace("_", "-"))
    return args


if __name__ == "__main__":
    os.umask(0o077)
    args = arguments()
    try:
        if args.worker:
            worker(args)
        elif args.case == "unit-config":
            unit_config_oracle(args)
        elif args.case == "write-budget":
            write_budget(args)
        else:
            scenario(args)
    except BaseException as error:
        print(
            json.dumps(
                {
                    "result": "FAIL",
                    "error_type": type(error).__name__,
                    "check": str(error) if isinstance(error, w.Refusal) else None,
                }
            )
        )
        raise SystemExit(1)
