#!/usr/bin/env python3
"""Disposable Linux smoke: real private files, fake process/HTTP boundaries only.

Run with python3 -B governance_smoke.py --private-parent /home/nwm.
Never executes a subprocess, systemctl, Docker, or a network request. The only
production seam replaced is the private frozen PGDATA identity, with the actual
identity of this smoke's owned directory. No executor CLI/env bypass is added.

Same-tool stage/unstage coverage is retained but is not cross-version proof.
Handoff cases load hash-verified original f24 bytes to produce staged state,
then consume it with the changed executor and identical ownership arguments.
Git/import fakes bind revision plus path and refuse wrong origins.

Oracle-only --original-executor selects the hash-verified f24 producer; it is
not a production governance_stage argument.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

F24_COMMIT = "f24c3fb37be392300052c0d0ebf0ee35366a4599"
F24_STAGE_SHA256 = "67bffe238cdf294e89255fa3c8b05303d0a09e2867271d013673f315727c811c"
RETAINED = "1a32ebb7b536873e6403f3faeb6eb8d83ef24d32"
TARGET = "415cbd1e9d0eee39ba0dfb623a586b02cbb340f2"
F24_STAGE_GIT = (
    F24_COMMIT
    + ":openspec/changes/timeseries-narrow-store-expand-contract/"
    + "receipts/issue-1987-execution-tools/governance_stage.py.txt"
)
WRAPPERS = (
    "scripts/node27_resource_governance_once.sh",
    "scripts/node27_resource_governance.py",
)
PAYLOADS = {
    "a8db554d6402bec642e9a05627eae64b2b79aec3": "smoke old active source\n",
    RETAINED: "smoke retained 1a32 runtime\n",
    TARGET: "smoke target 415 source\n",
}


def load(path, name="governance_under_smoke"):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


def refuse(gov, operation, code):
    try:
        operation()
    except gov.Refusal as exc:
        assert str(exc) == code, (str(exc), code)
    else:
        raise AssertionError("expected refusal: " + code)


def original_stage_bytes(path=None):
    if path is not None:
        data = Path(path).read_bytes()
    else:
        receipt = Path(__file__).resolve().parents[2] / (
            "timeseries-narrow-store-expand-contract/receipts/issue-1987-execution-tools/governance_stage.py.txt"
        )
        if receipt.is_file():
            data = receipt.read_bytes()
        else:
            data = subprocess.check_output(["git", "show", F24_STAGE_GIT])
    assert digest(data) == F24_STAGE_SHA256, "F24_STAGE_HASH_MISMATCH"
    return data


def load_original(private_parent, path=None):
    data = original_stage_bytes(path)
    directory = Path(tempfile.mkdtemp(prefix="issue1987-f24-stage-", dir=private_parent))
    destination = directory / "governance_stage.py"
    destination.write_bytes(data)
    destination.chmod(0o700)
    module = load(destination, "governance_original_f24")
    assert module.NEW == RETAINED and module.OLD == "a8db554d6402bec642e9a05627eae64b2b79aec3"
    return module


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return b'{"status":"ok"}'


class Boundary:
    def __init__(self, gov, args, active, enabled, failure, *, heads, blobs):
        self.g = gov
        self.a = args
        self.active = active
        self.enabled = enabled
        self.failure = failure
        self.heads = dict(heads)
        self.blobs = dict(blobs)
        self.container_drift = False
        self.invocation = 0
        self.commands = []
        self.loaded_pin = False
        self.phases = []
        self.replace = os.replace
        self.pin = Path(args.unit_root) / (gov.SERVICE + ".d") / gov.PIN
        self.units = (gov.SERVICE, gov.TIMER, "nhms-display-api.service")
        self.installed_env = Path(args.env_file).with_name("installed-service.env")
        self.environment_mode = "nonempty"
        self.bus_fault = None
        self.missing_common = False
        self.runner_busy = False
        self.bus_units = {
            gov.SERVICE: "/org/freedesktop/systemd1/unit/smoke_governance",
            "nhms-display-api.service": "/org/freedesktop/systemd1/unit/smoke_display",
        }

    def observe_replace(self, source, destination):
        self.replace(source, destination)
        if Path(destination) == Path(self.a.state_dir) / "state.json":
            self.phases.append(json.loads(Path(destination).read_text())["phase"])

    def open(self, request, timeout):
        assert request.full_url in ("http://127.0.0.1:8080/health", "http://127.0.0.1:8081/health")
        assert timeout == 10
        return Response()

    def cutover(self, sha):
        self.heads[self.a.active_root] = sha
        write_checkout(Path(self.a.active_root), sha)

    def receipt(self):
        pg = Path(self.a.pgdata_root)
        info, fs = pg.stat(), os.statvfs(pg)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        data = {
            "schema_version": "nhms.node27_resource_governance.audit.v1",
            "status": "completed",
            "execution_mode": "read_only_audit",
            "recommendations": [],
            "started_at": now,
            "finished_at": now,
            "paths": {"repo_root": self.a.active_root, "pgdata_root": str(pg)},
            "working_set": {
                "working_set_filesystem": {
                    "status": "ok",
                    "path": str(pg),
                    "blockers": [],
                    "device_identity": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}:{fs.f_fsid}",
                }
            },
            "safety": {"destructive_actions_enabled": False},
        }
        if self.failure == "binding":
            data["working_set"]["working_set_filesystem"]["device_identity"] = "wrong-device"
        if self.failure == "stale_payload":
            data["started_at"] = data["finished_at"] = "2000-01-01T00:00:00+00:00"
        if self.failure == "audit_critical" and not self.pin.exists():
            data["recommendations"] = [{"code": "AUTOVACUUM_OUTPUT_STALLED", "severity": "critical"}]
        Path(self.a.log_root, f"resource-governance-{self.invocation}.json").write_text(json.dumps(data))
        if self.failure == "multiple_receipts":
            Path(self.a.log_root, f"resource-governance-{self.invocation}-extra.json").write_text(json.dumps(data))

    def systemctl(self, argv):
        g, a = self.g, self.a
        if argv == ["list-unit-files", "nhms-*", "--no-legend", "--no-pager"]:
            return "\n".join(unit + " enabled" for unit in self.units)
        if argv == ["daemon-reload"]:
            if self.failure == "interrupt_after_remove" and not self.pin.exists():
                raise g.Refusal("INTERRUPTED")
            self.loaded_pin = self.pin.exists()
            return ""
        if len(argv) == 2 and argv[0] in ("start", "stop"):
            action, unit = argv
            if unit == g.TIMER:
                self.active = "active" if action == "start" else "inactive"
                if action == "stop" and self.failure == "config_drift":
                    self.installed_env.write_bytes(b"foreign configuration replacement\n")
                return ""
            assert unit == g.SERVICE and action == "start", argv
            self.invocation += 1
            if self.failure == "foreign":
                self.pin.write_bytes(b"foreign replacement\n")
            if self.failure not in ("old_receipt_only", "no_receipt"):
                self.receipt()
            return ""
        if len(argv) >= 3 and argv[0] == "show" and argv[1] in self.units:
            unit = argv[1]
            values = {
                "Transient": "no",
                "FragmentPath": str(Path(a.unit_root, unit)),
                "DropInPaths": str(self.pin) if unit == g.SERVICE and self.loaded_pin else "",
                "LoadState": "loaded",
                "UnitFileState": self.enabled,
                "ActiveState": self.active if unit == g.TIMER else "inactive",
                "MainPID": "0",
                "ControlPID": "0",
                "Result": "success",
                "ExecMainStatus": "0",
                "InvocationID": str(self.invocation),
                "ExecMainStartTimestampMonotonic": str(self.invocation * 100),
                "ExecMainExitTimestampMonotonic": str(self.invocation * 100 + 1),
            }
            if self.runner_busy and unit == g.SERVICE:
                values.update(ActiveState="active", MainPID="1")
            if self.failure in ("service", "foreign") and self.invocation:
                values.update(Result="exit-code", ExecMainStatus="1")
            properties = [p.removeprefix("--property=") for p in argv[2:]]
            assert all(p.startswith("--property=") for p in argv[2:]), argv
            assert "EnvironmentFiles" not in properties or unit.endswith(".service"), argv
            assert all(p in values or p == "EnvironmentFiles" for p in properties), argv
            if unit == "nhms-display-api.service" and self.environment_mode == "nonempty":
                values["EnvironmentFiles"] = str(self.installed_env) + " (ignore_errors=no)"
            if self.missing_common:
                del values["DropInPaths"]
            # Empty EnvironmentFiles is omitted by real systemctl 249; timers
            # do not have this service property at all.
            return "\n".join(p + "=" + values[p] for p in properties if p in values)
        raise AssertionError("unknown systemctl command: " + repr(argv))

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        g, a = self.g, self.a
        if argv[:2] == ["systemctl", "--user"]:
            text = self.systemctl(argv[2:])
        elif argv[:3] == ["busctl", "--user", "call"]:
            assert argv[3:8] == [
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "GetUnit",
                "s",
            ], argv
            assert len(argv) == 9 and argv[8] in self.bus_units, argv
            text = 'o "' + self.bus_units[argv[8]] + '"'
            if self.bus_fault == "resolution":
                text = 's "not-an-object-path"'
        elif argv[:3] == ["busctl", "--user", "get-property"]:
            assert len(argv) == 7 and argv[3] == "org.freedesktop.systemd1", argv
            assert argv[4] in self.bus_units.values(), argv
            assert argv[5:] == ["org.freedesktop.systemd1.Service", "EnvironmentFiles"], argv
            if self.bus_fault in ("unknown", "failed"):
                error = b"Unknown interface or property" if self.bus_fault == "unknown" else b"Query failed"
                return subprocess.CompletedProcess(argv, 1, b"", error)
            text = {
                None: "a(sb) 0",
                "resolution": "a(sb) 0",
                "nonempty": 'a(sb) 1 "/unexpected.env" false',
                "wrong_type": "as 0",
            }[self.bus_fault]
        elif argv == ["journalctl", "--user", "-u", g.SERVICE, "-n", "100", "--no-pager"]:
            text = "old failed audit (fake system boundary)"
        elif argv[:2] == ["git", "-C"] and argv[2] in (a.active_root, a.runtime_root):
            root = argv[2]
            if root not in self.heads:
                raise AssertionError("unbound git root: " + root)
            if argv[3:] == ["rev-parse", "HEAD"]:
                text = self.heads[root]
            elif argv[3:] == ["status", "--porcelain", "--untracked-files=no"]:
                text = ""
            elif len(argv) == 5 and argv[3] == "show":
                revision, relative = argv[4].split(":", 1)
                if revision != self.heads[root] or (revision, relative) not in self.blobs:
                    return subprocess.CompletedProcess(argv, 1, b"", b"wrong origin or revision")
                text = self.blobs[(revision, relative)]
            else:
                raise AssertionError(argv)
        elif argv[0] in [str(Path(root, ".venv/bin/python")) for root in (a.active_root, a.runtime_root)]:
            cwd = kwargs["cwd"]
            assert len(argv) == 3 and argv[1] == "-c" and "IMPORT_ORIGIN_OK" in argv[2]
            assert cwd in (a.active_root, a.runtime_root)
            assert argv[0] == str(Path(cwd, ".venv/bin/python"))
            match = re.search(r"'([0-9a-f]{40}):' \+ relative", argv[2])
            assert match is not None, "probe SHA literal missing"
            probe = match.group(1)
            if probe != g.NEW or probe != self.heads[cwd]:
                return subprocess.CompletedProcess(argv, 1, b"", b"import origin mismatch")
            text = "IMPORT_ORIGIN_OK"
        elif argv[:2] == ["/bin/bash", "-c"]:
            assert len(argv) == 5 and argv[3:] == ["governance-env", a.env_file]
            keys = (
                "NODE27_GOVERNANCE_REPO_ROOT",
                "NODE27_RESOURCE_GOVERNANCE_REPO_ROOT",
                "NODE27_GOVERNANCE_PGDATA_ROOT",
                "NODE27_RESOURCE_GOVERNANCE_LOG_ROOT",
                "NODE27_RESOURCE_GOVERNANCE_LOCK_PATH",
                "NODE27_RESOURCE_GOVERNANCE_SUMMARY_PATH",
                "NODE27_RESOURCE_GOVERNANCE_REPO",
                "REPO",
                "ENV_FILE",
                "PYTHONPATH",
            )
            values = dict.fromkeys(keys)
            values.update(
                NODE27_GOVERNANCE_REPO_ROOT=a.active_root,
                NODE27_GOVERNANCE_PGDATA_ROOT=a.pgdata_root,
                NODE27_RESOURCE_GOVERNANCE_LOG_ROOT=a.log_root,
                NODE27_RESOURCE_GOVERNANCE_LOCK_PATH=a.runner_lock,
            )
            text = json.dumps(values)
        elif argv[:5] == ["docker", "inspect", "--type=container", "--format", "{{json .Mounts}}"]:
            assert argv[5:] == [a.source_id]
            text = json.dumps(
                [{"Destination": a.container_pgdata, "Type": "bind", "Source": a.pgdata_root, "RW": True}]
            )
        elif argv == [
            "docker",
            "inspect",
            "--type=container",
            "--format",
            "{{json .Id}} {{json .Image}} {{json .State.Running}}",
            a.source_name,
        ]:
            text = json.dumps(a.source_id) + " " + json.dumps(a.source_image) + " true"
        elif argv == ["docker", "exec", a.source_id, "stat", "-Lc", "%d:%i:%u:%g:%a", "--", a.container_pgdata]:
            info = Path(a.pgdata_root).stat()
            text = f"{info.st_dev}:{info.st_ino}:{info.st_uid}:{info.st_gid}:{stat.S_IMODE(info.st_mode):o}"
            if self.container_drift:
                text = "0:0:0:0:700"
        elif argv in (["ss", "-H", "-ltnp", "sport = :8080"], ["ss", "-H", "-ltnp", "sport = :8081"]):
            text = f'LISTEN users:(("smoke",pid={os.getpid()},fd=1))'
        else:
            raise AssertionError("unknown command/target: " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, text.encode(), b"")


def write_checkout(root, sha):
    payload = PAYLOADS[sha]
    for relative in WRAPPERS:
        target = root / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_text(payload)
        target.chmod(0o700)
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    python.write_text("smoke system-boundary fixture\n")
    python.chmod(0o700)


def blobs_for(*shas):
    return {(sha, relative): PAYLOADS[sha] for sha in shas for relative in WRAPPERS}


def prepare_tree(g, root):
    for relative in (
        "runtime/.venv/bin",
        "runtime/scripts",
        "runtime/infra/systemd",
        "active/.venv/bin",
        "active/scripts",
        "active/infra/systemd",
        "units",
        "logs",
        "shared/pgdata",
    ):
        (root / relative).mkdir(parents=True, mode=0o700, exist_ok=True)
    for checkout in ("runtime", "active"):
        (root / checkout / "infra/systemd" / g.SERVICE).write_text("[Service]\nType=oneshot\n")
    for unit in (g.SERVICE, g.TIMER, "nhms-display-api.service"):
        (root / "units" / unit).write_text("[Service]\nType=oneshot\n")
    for name in ("env", "hold"):
        (root / name).write_text("private unchanged fixture\n")
        (root / name).chmod(0o600)
    (root / "shared").chmod(0o777)
    a = g.parser().parse_args(["stage", "--state-dir", str(root / "state"), "--runtime-root", str(root / "runtime")])
    for key, relative in {
        "active_root": "active",
        "unit_root": "units",
        "env_file": "env",
        "hold_state": "hold",
        "pgdata_root": "shared/pgdata",
        "log_root": "logs",
        "runner_lock": "logs/runner.lock",
    }.items():
        setattr(a, key, str(root / relative))
    a.uid, a.gid = os.getuid(), os.getgid()
    return a


def bound(module, boundary, identity):
    patches = (
        patch.object(module, "_PGDATA_IDENTITY", identity),
        patch.object(module.subprocess, "run", boundary.run),
        patch.object(module.urllib.request, "build_opener", return_value=boundary),
        patch.object(module.os, "replace", boundary.observe_replace),
    )
    return patches[0], patches[1], patches[2], patches[3]


def systemctl_user(commands):
    return [cmd[2:] for cmd in commands if cmd[:2] == ["systemctl", "--user"]]


def close(executor):
    executor.log.close()
    executor.lock.close()


def scenario(g, root, active, enabled, failure):
    a = prepare_tree(g, root)
    write_checkout(Path(a.runtime_root), g.NEW)
    write_checkout(Path(a.active_root), g.OLD)
    info = Path(a.pgdata_root).stat()
    identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    boundary = Boundary(
        g,
        a,
        active,
        enabled,
        failure,
        heads={a.runtime_root: g.NEW, a.active_root: g.OLD},
        blobs=blobs_for(g.OLD, g.NEW),
    )
    boundary.installed_env.write_bytes(b"INSTALLED_ROUTE=original\n")
    boundary.installed_env.chmod(0o600)
    if failure == "old_receipt_only":
        boundary.receipt()  # A real pre-existing name must not count as fresh.
        old_receipt = Path(a.log_root, "resource-governance-0.json")
        old_receipt_bytes = old_receipt.read_bytes()
    before = {p: p.read_bytes() for p in (root / "env", root / "hold", *list((root / "units").iterdir()))}
    pg, run, opener, replace = bound(g, boundary, identity)
    with pg, run, opener, replace:
        e = g.Executor(a)
        try:
            if failure is None and active == "active":
                boundary.environment_mode = "omitted"
                e.config()  # Exact observed display-service empty-array omission.
                for fault, code in (
                    ("resolution", "SYSTEMD_UNIT_RESOLUTION_INVALID"),
                    ("nonempty", "ENVIRONMENT_FILES_EMPTY_UNPROVEN"),
                    ("wrong_type", "ENVIRONMENT_FILES_EMPTY_UNPROVEN"),
                    ("unknown", "COMMAND_FAILED"),
                    ("failed", "COMMAND_FAILED"),
                ):
                    boundary.bus_fault = fault
                    refuse(g, e.config, code)
                boundary.bus_fault = None
                boundary.missing_common = True
                refuse(g, e.config, "SYSTEMD_PROPERTY_MISSING")
                boundary.missing_common = False
                boundary.environment_mode = "nonempty"
                boundary.installed_env.chmod(0o666)
                refuse(g, e.config, "WORLD_WRITABLE_PATH")
                boundary.installed_env.chmod(0o600)
            refuse(g, lambda: g.safe(root / "shared", directory=True), "WORLD_WRITABLE_PATH")
            e.source()  # Same real shared ancestor is legal only for observation.
            boundary.container_drift = True
            refuse(g, e.source, "PRIMARY_PGDATA_CONTAINER_MISMATCH")
            boundary.container_drift = False
            link = root / "pg-link"
            link.symlink_to(a.pgdata_root, target_is_directory=True)
            original = a.pgdata_root
            a.pgdata_root = str(link)
            refuse(g, e.source, "SYMLINK_PATH")
            a.pgdata_root = original
            with patch.object(g, "_PGDATA_IDENTITY", (0, 0, 0, 0, 0)):
                refuse(g, e.source, "PRIMARY_PGDATA_IDENTITY_MISMATCH")
            if failure:
                code = {
                    "binding": "AUDIT_DEVICE_BINDING_FAILED",
                    "service": "REAL_SERVICE_FAILED",
                    "foreign": "REAL_SERVICE_FAILED",
                    "old_receipt_only": "FRESH_AUDIT_MISSING_OR_AMBIGUOUS",
                    "no_receipt": "FRESH_AUDIT_MISSING_OR_AMBIGUOUS",
                    "multiple_receipts": "FRESH_AUDIT_MISSING_OR_AMBIGUOUS",
                    "stale_payload": "AUDIT_TIMESTAMP_INVALID",
                    "config_drift": "OTHER_CONFIG_DRIFT",
                }[failure]
                refuse(g, e.execute, code)
                assert ("timer_stopped" if failure == "config_drift" else "stage_starting") in boundary.phases
                if failure == "old_receipt_only":
                    assert old_receipt.read_bytes() == old_receipt_bytes
                expected = "rollback_required" if failure == "foreign" else "rolled_back"
                assert e.state["phase"] == expected and e.state["outcome"] == "FAIL", e.state
                if failure == "foreign":
                    assert e.pin.read_bytes() == b"foreign replacement\n"
                    assert boundary.active == "inactive"  # Fail closed, not a false timer-restored claim.
                    a.operation = "recover"
                    refuse(g, e.execute, "PIN_NOT_OWNED")
                    assert e.pin.read_bytes() == b"foreign replacement\n"
                else:
                    assert not e.pin.exists()
                    assert boundary.active == active
                assert "rollback_pending" in boundary.phases
            else:
                # Real newly created receipts retain automatic filesystem
                # mtimes; no clock patch, utime, or artificial delay is used.
                e.execute()
                assert e.state["phase"] == "staged" and e.state["outcome"] == "PASS"
                assert e.pin.read_bytes() == e.content and boundary.active == active
                boundary.cutover(g.NEW)
                # Reopen durable state across the authorized active-checkout cutover.
                close(e)
                a.operation = "unstage"
                e = g.Executor(a)
                e.execute()
                assert e.state["phase"] == "unstaged" and e.state["outcome"] == "PASS"
                assert not e.pin.exists() and boundary.active == active
                assert boundary.phases == [
                    "prepared",
                    "timer_stopped",
                    "install_pending",
                    "stage_starting",
                    "stage_verified",
                    "staged",
                    "unstage_admitted",
                    "unstage_prepared",
                    "unstage_remove_pending",
                    "unstage_starting",
                    "unstage_verified",
                    "unstaged",
                ]
            assert boundary.enabled == enabled
            assert all(p.read_bytes() == data for p, data in before.items())
            assert boundary.installed_env.read_bytes() == (
                b"foreign configuration replacement\n" if failure == "config_drift" else b"INSTALLED_ROUTE=original\n"
            )
            assert not any(
                cmd[:3] == ["systemctl", "--user", action]
                for cmd in boundary.commands
                for action in ("enable", "disable", "mask", "reset-failed")
            )
            return {"kind": "same_tool", "timer": [active, enabled], "failure": failure, "phases": boundary.phases}
        finally:
            close(e)
            (root / "shared").chmod(0o700)


def stage_original(original, changed, root):
    assert original.NEW == RETAINED and changed.NEW == TARGET
    assert original.OLD == changed.OLD == "a8db554d6402bec642e9a05627eae64b2b79aec3"
    a = prepare_tree(original, root)
    write_checkout(Path(a.runtime_root), RETAINED)
    write_checkout(Path(a.active_root), original.OLD)
    info = Path(a.pgdata_root).stat()
    identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    boundary = Boundary(
        original,
        a,
        "active",
        "enabled",
        None,
        heads={a.runtime_root: RETAINED, a.active_root: original.OLD},
        blobs=blobs_for(original.OLD, RETAINED, TARGET),
    )
    boundary.installed_env.write_bytes(b"INSTALLED_ROUTE=original\n")
    boundary.installed_env.chmod(0o600)
    wrong = boundary.run(
        ["git", "-C", a.runtime_root, "show", TARGET + ":scripts/node27_resource_governance.py"],
        cwd=a.runtime_root,
    )
    assert wrong.returncode == 1
    pg, run, opener, replace = bound(original, boundary, identity)
    with pg, run, opener, replace:
        producer = original.Executor(a)
        try:
            producer.execute()
            assert producer.state["phase"] == "staged" and producer.state["outcome"] == "PASS"
            pin = producer.pin.read_bytes()
            assert pin == producer.content
            snapshot = json.loads((Path(a.state_dir) / "state.json").read_text())
            assert snapshot["identity"] == producer.identity
            assert snapshot["pin_digest"] == original.digest(pin)
            assert snapshot["identity"]["runtime_root"] == a.runtime_root
            assert RETAINED not in json.dumps(snapshot["identity"])
            assert TARGET not in json.dumps(snapshot["identity"])
        finally:
            close(producer)
    return a, boundary, identity, snapshot, pin


def consume(changed, a, boundary, identity, operation):
    a.operation = operation
    boundary.g = changed
    pg, run, opener, replace = bound(changed, boundary, identity)
    with pg, run, opener, replace:
        executor = changed.Executor(a)
        try:
            executor.execute()
            return json.loads((Path(a.state_dir) / "state.json").read_text()), None
        except changed.Refusal as exc:
            if executor.state and executor.state.get("phase") not in ("rolled_back", "staged", "unstaged"):
                executor.save(executor.state["phase"], outcome="FAIL", failure=str(exc))
            return json.loads((Path(a.state_dir) / "state.json").read_text()), exc
        finally:
            close(executor)


def handoff(original, changed, root, case):
    a, boundary, identity, snapshot, pin = stage_original(original, changed, root)
    before = pin
    identity_before = json.loads(json.dumps(snapshot["identity"]))
    pin_digest = snapshot["pin_digest"]
    result = {"kind": "handoff", "case": case, "producer_phase": "staged"}

    if case == "pre_cutover":
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "GIT_HEAD_MISMATCH"
        assert Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).read_bytes() == before
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["phase"] == "staged" and current["identity"] == identity_before
        assert current["pin_digest"] == pin_digest
        result.update(refusal="GIT_HEAD_MISMATCH", phase=current["phase"])
        return result

    boundary.cutover(TARGET)
    assert boundary.heads[a.runtime_root] == RETAINED
    assert boundary.heads[a.active_root] == TARGET

    if case == "active_runner":
        boundary.runner_busy = True
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "RUNNER_NOT_IDLE"
        assert Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).read_bytes() == before
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["phase"] == "staged" and current["identity"] == identity_before
        result.update(refusal="RUNNER_NOT_IDLE", phase=current["phase"])
        return result

    if case == "foreign_pin":
        Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).write_bytes(b"foreign replacement\n")
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "OWNED_DROPIN_DRIFT"
        assert Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).read_bytes() == b"foreign replacement\n"
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["identity"] == identity_before and current["pin_digest"] == pin_digest
        result.update(refusal="OWNED_DROPIN_DRIFT", phase=current["phase"])
        return result

    if case == "foreign_env":
        Path(a.env_file).write_bytes(b"foreign environment replacement\n")
        Path(a.env_file).chmod(0o600)
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "ENVIRONMENT_DRIFT"
        assert Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).read_bytes() == before
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["identity"] == identity_before
        result.update(refusal="ENVIRONMENT_DRIFT", phase=current["phase"])
        return result

    if case == "foreign_unit":
        Path(a.unit_root, changed.SERVICE).write_bytes(b"[Service]\nType=oneshot\nforeign-unit\n")
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "OTHER_CONFIG_DRIFT"
        assert Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).read_bytes() == before
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["identity"] == identity_before
        result.update(refusal="OTHER_CONFIG_DRIFT", phase=current["phase"])
        return result

    if case == "interrupt_after_remove":
        boundary.failure = "interrupt_after_remove"
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "INTERRUPTED"
        assert not Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).exists()
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["phase"] == "unstage_remove_pending"
        assert current["identity"] == identity_before and current["pin_digest"] == pin_digest
        assert current["direction"] == "unstage"
        boundary.failure = None
        recovered, recovered_error = consume(changed, a, boundary, identity, "recover")
        assert recovered_error is None
        assert recovered["phase"] == "unstaged" and recovered["outcome"] == "PASS"
        assert recovered["identity"] == identity_before
        assert not Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).exists()
        actions = systemctl_user(boundary.commands)
        starts_service = [i for i, item in enumerate(actions) if item == ["start", changed.SERVICE]]
        starts_timer = [i for i, item in enumerate(actions) if item == ["start", changed.TIMER]]
        assert starts_service[-1] < starts_timer[-1]
        result.update(interrupted_phase="unstage_remove_pending", recovered_phase="unstaged")
        return result

    if case == "audit_critical":
        boundary.failure = "audit_critical"
        state, error = consume(changed, a, boundary, identity, "unstage")
        assert isinstance(error, changed.Refusal) and str(error) == "AUDIT_CRITICAL"
        assert not Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).exists()
        current = json.loads((Path(a.state_dir) / "state.json").read_text())
        assert current["phase"] == "unstage_starting" and current["identity"] == identity_before
        assert current.get("outcome") != "PASS"
        actions = systemctl_user(boundary.commands)
        last_stop = max(i for i, item in enumerate(actions) if item == ["stop", changed.TIMER])
        assert ["start", changed.TIMER] not in actions[last_stop:]
        assert boundary.active == "inactive"
        boundary.failure = None
        recovered, recovered_error = consume(changed, a, boundary, identity, "recover")
        assert recovered_error is None
        assert recovered["phase"] == "unstaged" and recovered["outcome"] == "PASS"
        assert recovered["identity"] == identity_before
        actions = systemctl_user(boundary.commands)
        starts_service = [i for i, item in enumerate(actions) if item == ["start", changed.SERVICE]]
        starts_timer = [i for i, item in enumerate(actions) if item == ["start", changed.TIMER]]
        assert starts_service[-1] < starts_timer[-1]
        result.update(
            refusal="AUDIT_CRITICAL",
            failed_phase="unstage_starting",
            recovered_phase="unstaged",
            critical="AUTOVACUUM_OUTPUT_STALLED",
        )
        return result

    state, error = consume(changed, a, boundary, identity, "unstage")
    assert error is None
    assert state["phase"] == "unstaged" and state["outcome"] == "PASS"
    assert state["identity"] == identity_before and state["pin_digest"] == pin_digest
    assert not Path(a.unit_root, changed.SERVICE + ".d", changed.PIN).exists()
    assert boundary.heads[a.runtime_root] == RETAINED
    assert "unstage_verified" in boundary.phases
    assert boundary.phases.index("unstage_verified") < boundary.phases.index("unstaged")
    actions = systemctl_user(boundary.commands)
    starts_service = [i for i, item in enumerate(actions) if item == ["start", changed.SERVICE]]
    starts_timer = [i for i, item in enumerate(actions) if item == ["start", changed.TIMER]]
    assert starts_service[-1] < starts_timer[-1], actions
    result.update(phase="unstaged", audit_before_timer=True, retained_runtime=RETAINED, active=TARGET)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", type=Path, default=Path(__file__).with_name("governance_stage.py"))
    parser.add_argument("--private-parent", type=Path, required=True)
    parser.add_argument(
        "--original-executor",
        type=Path,
        help="Oracle-only hash-verified original f24 governance_stage.py; not a production CLI flag",
    )
    args = parser.parse_args()
    assert Path("/proc/self/stat").exists(), "Linux /proc required; no services are contacted"
    os.umask(0o077)
    g = load(args.executor)
    assert g.NEW == TARGET and g.OLD == "a8db554d6402bec642e9a05627eae64b2b79aec3"
    g.safe(args.private_parent, directory=True)
    original = load_original(args.private_parent, args.original_executor)
    same_tool = []
    for active, enabled, failure in (
        ("active", "enabled", None),
        ("inactive", "disabled", None),
        ("active", "enabled-runtime", "service"),
        ("inactive", "static", "binding"),
        ("active", "enabled", "foreign"),
        ("active", "enabled", "old_receipt_only"),
        ("active", "enabled", "no_receipt"),
        ("active", "enabled", "multiple_receipts"),
        ("active", "enabled", "stale_payload"),
        ("active", "enabled", "config_drift"),
    ):
        with tempfile.TemporaryDirectory(prefix="issue1987-governance-smoke-", dir=args.private_parent) as directory:
            same_tool.append(scenario(g, Path(directory), active, enabled, failure))
    handoff_cases = []
    for case in (
        "pre_cutover",
        "active_runner",
        "foreign_pin",
        "foreign_env",
        "foreign_unit",
        "interrupt_after_remove",
        "audit_critical",
        "happy",
    ):
        with tempfile.TemporaryDirectory(prefix="issue1987-governance-handoff-", dir=args.private_parent) as directory:
            handoff_cases.append(handoff(original, g, Path(directory), case))
    print(
        json.dumps(
            {
                "surface": "governance_system_boundary_fakes_real_private_files",
                "same_tool": same_tool,
                "original_state_new_active_handoff": handoff_cases,
                "original_executor_sha256": F24_STAGE_SHA256,
                "changed_executor_sha256": digest(Path(args.executor).read_bytes()),
                "retained": RETAINED,
                "target": TARGET,
                "old": g.OLD,
                "real_services_exercised": False,
                "database_exercised": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
