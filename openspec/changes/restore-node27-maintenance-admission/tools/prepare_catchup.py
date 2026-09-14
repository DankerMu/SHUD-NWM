#!/usr/bin/env python3
"""Incident-only #2349 private paired-budget helper.

prepare / check / cleanup only. This tool never launches a DB runner, never
starts or stops a service or timer, never masks units, and never deletes a
lock. Main owns timer stop/restore and the distinct transient unit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path

OLD = "a8db554d6402bec642e9a05627eae64b2b79aec3"
UID = 1005
STATE_PARENT = Path("/home/nwm/.local/state")
PRODUCTION = Path("/home/nwm/NWM")
COMPRESSION_ENV = PRODUCTION / "infra/env/node27-timeseries-compression.env"
COLD_ENV = PRODUCTION / "infra/env/node27-cold-residency.env"
PREFLIGHT = PRODUCTION / "scripts/node27_timeseries_budget_preflight.py"
PYTHON = PRODUCTION / ".venv/bin/python"
GOVERNANCE_STATE = STATE_PARENT / "issue1987-governance-f24c3fb37/state.json"
GOVERNANCE_RUNTIME = "/home/nwm/NWM-governance-reviewed-1a32ebb7"
PIN_NAME = "70-issue1987-governance-reviewed-source.conf"
GOVERNANCE_SERVICE = "nhms-node27-resource-governance.service"
RETENTION_RECEIPT = Path("/home/nwm/node27-timeseries-retention-logs/retention-20260913T051532Z.json")
RETENTION_SHA = "8d4db2890731c666afa462e39ab080e87c6cb52acd4965112d94518dd77fb9e2"
SCHEDULED_RECEIPT = PRODUCTION / ".nhms-issue1069-live/scheduled-receipt.json"
SCHEDULED_SHA = "a3df33cf1d9a421ecb3e16d0f0ad3184cf11c62cdab8c7764face3e20ddd0ae3"
COMPRESSION_TIMER = "nhms-node27-timeseries-compression.timer"
RETENTION_TIMER = "nhms-node27-timeseries-retention.timer"
COMPRESSION_SERVICE = "nhms-node27-timeseries-compression.service"
RETENTION_SERVICE = "nhms-node27-timeseries-retention.service"
REPLAY_SERVICE = "nhms-node27-timeseries-compression-replay.service"
TIMERS = (COMPRESSION_TIMER, RETENTION_TIMER)
MAINTENANCE_SERVICES = (COMPRESSION_SERVICE, RETENTION_SERVICE, REPLAY_SERVICE)
ROOT_NAME_RE = re.compile(r"^issue2349-catchup-[A-Za-z0-9-]{1,64}$")
UNIT_NAME_RE = re.compile(r"^nhms-issue2349-catchup-[A-Za-z0-9-]{1,64}\.service$")
ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
UNIT_SHOW_RE = re.compile(r"^[A-Za-z0-9:_.\\-]+\.(service|timer)$")
COMPRESSION_BOUND_KEY = "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND"
COMPRESSION_STATEMENT_KEY = "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS"
COMPRESSION_WRAPPER_KEY = "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS"
COMPRESSION_SERVICE_KEY = "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS"
COLD_SERVICE_KEY = "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS"
COLD_WRAPPER_KEY = "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS"
COLD_STATEMENT_KEY = "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS"
DATABASE_URL_KEY = "DATABASE_URL"
ORIGINAL_ASSEMBLY = "3900,3901,7842,3600000,3600000,4"
PRIVATE_ASSEMBLY = "6300,3901,10242,6000000,3600000,1"
STATEMENT_MS = 6000000
WRAPPER_SECONDS = 6300
COLD_WRAPPER_SECONDS = 3901
SERVICE_SECONDS = 10242
BOUND = 1
MAX_ENV_BYTES = 64 * 1024
MAX_ENV_LINES = 512
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_PIN_BYTES = 64 * 1024
MAX_COMMAND_BYTES = 1024 * 1024
COMMAND_TIMEOUT = 30
PREFLIGHT_TIMEOUT = 60
SCHEMA = "issue2349.catchup.helper.v1"
MANIFEST_NAME = "manifest.json"
PRIVATE_COMPRESSION_NAME = "node27-timeseries-compression.env"
PRIVATE_COLD_NAME = "node27-cold-residency.env"
PRIVATE_RECEIPT_NAME = "scheduled-receipt.json"
SHOW_COMMON_PROPS = ("Id", "LoadState", "ActiveState", "SubState")
SHOW_SERVICE_PROPS = SHOW_COMMON_PROPS + ("MainPID",)


class Refusal(Exception):
    def __init__(self, code, path=None):
        super().__init__(code)
        self.path = _public_path(path) if path is not None else None


def _public_path(path):
    return re.sub(r"[^A-Za-z0-9/_.-]", "?", str(path))[:240]


def require(ok, code, path=None):
    if not ok:
        raise Refusal(code, path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe(path, *, directory=False, private=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "UNSAFE_PATH", path)
    require("\x00" not in str(path), "UNSAFE_PATH", path)
    chain = (path, *path.parents)
    for parent in reversed(chain):
        try:
            info = parent.lstat()
        except OSError:
            raise Refusal("PATH_STAT_FAILED", parent) from None
        require(not stat.S_ISLNK(info.st_mode), "SYMLINK_PATH", parent)
        require(info.st_uid in (0, os.getuid()), "FOREIGN_PATH_OWNER", parent)
        require(not info.st_mode & 0o002, "WORLD_WRITABLE_PATH", parent)
        if info.st_mode & 0o020:
            require(
                info.st_uid == os.getuid() and info.st_gid == os.getgid(),
                "FOREIGN_GROUP_WRITABLE_PATH",
                parent,
            )
            try:
                group = grp.getgrgid(info.st_gid)
                explicit = [pwd.getpwnam(name).pw_uid for name in group.gr_mem]
                primary = [entry.pw_uid for entry in pwd.getpwall() if entry.pw_gid == info.st_gid]
                attributes = os.listxattr(parent, follow_symlinks=False)
            except (OSError, KeyError, AttributeError):
                raise Refusal("GROUP_WRITE_AUTHORITY_UNAVAILABLE", parent) from None
            require(
                bool(primary) and all(uid == os.getuid() for uid in (*explicit, *primary)),
                "MULTIUSER_GROUP_WRITABLE_PATH",
                parent,
            )
            require(
                not {"system.posix_acl_access", "system.posix_acl_default", "security.NTACL"}.intersection(attributes),
                "GROUP_WRITABLE_PATH_ACL",
                parent,
            )
        if parent == path:
            require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "PATH_TYPE", path)
            if private:
                require(
                    info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
                    "PRIVATE_MODE",
                    path,
                )
    return path


def identity_of(info):
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
    }


def read_regular(path, *, max_bytes, private=False):
    path = safe(path, private=private)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode), "PATH_TYPE", path)
        require(info.st_size <= max_bytes, "SOURCE_TOO_LARGE", path)
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(8 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        require(
            len(raw) == info.st_size
            and not os.read(fd, 1)
            and (info.st_dev, info.st_ino, info.st_size) == (after.st_dev, after.st_ino, after.st_size),
            "SOURCE_CHANGED_DURING_READ",
            path,
        )
        return bytes(raw), identity_of(info)
    finally:
        os.close(fd)


def exclusive_write(path, data, *, mode=0o600, parent_private=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "UNSAFE_PATH", path)
    parent = safe(path.parent, directory=True, private=parent_private)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    dir_fd = os.open(parent, flags)
    file_fd = None
    try:
        file_fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=dir_fd,
        )
        os.fchmod(file_fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.fsync(dir_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(dir_fd)
    return safe(path, private=mode == 0o600)


def unlink_exact(path, recorded):
    path = Path(path)
    require(
        path.is_absolute() and path.name in (PRIVATE_COMPRESSION_NAME, PRIVATE_COLD_NAME), "CLEANUP_PATH_REFUSED", path
    )
    parent = safe(path.parent, directory=True, private=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    dir_fd = os.open(parent, flags)
    try:
        try:
            info = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise Refusal("PRIVATE_ENV_MISSING", path) from None
        require(not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode), "PATH_TYPE", path)
        require(
            info.st_dev == recorded["dev"]
            and info.st_ino == recorded["ino"]
            and info.st_uid == recorded["uid"]
            and stat.S_IMODE(info.st_mode) == recorded["mode"]
            and info.st_size == recorded["size"],
            "PRIVATE_ENV_IDENTITY_MISMATCH",
            path,
        )
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
        try:
            payload = bytearray()
            while len(payload) <= recorded["size"]:
                chunk = os.read(fd, min(8 * 1024, recorded["size"] + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            require(digest(bytes(payload)) == recorded["sha256"], "PRIVATE_ENV_HASH_MISMATCH", path)
        finally:
            os.close(fd)
        os.unlink(path.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def command_env():
    env = {
        key: os.environ[key]
        for key in ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG")
        if key in os.environ
    }
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run(argv, *, timeout=COMMAND_TIMEOUT, cwd=None):
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=cwd,
            env=command_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise Refusal("COMMAND_TIMEOUT") from None
    except OSError:
        raise Refusal("COMMAND_UNAVAILABLE") from None
    require(
        len(result.stdout) <= MAX_COMMAND_BYTES and len(result.stderr) <= MAX_COMMAND_BYTES, "COMMAND_OUTPUT_TOO_LARGE"
    )
    require(result.returncode == 0, "COMMAND_FAILED")
    try:
        return result.returncode, result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise Refusal("COMMAND_OUTPUT_INVALID") from None


def git(*args):
    return run(["/usr/bin/git", "-C", str(PRODUCTION), *args], cwd=str(PRODUCTION))[1].strip()


def systemctl_show(unit, properties):
    require(UNIT_SHOW_RE.fullmatch(unit) is not None, "UNIT_NAME_UNSAFE")
    _code, text = run(
        ["/usr/bin/systemctl", "--user", "show", unit, "--no-pager", *[f"--property={name}" for name in properties]]
    )
    allowed = set(properties)
    props = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in allowed:
            props[key] = value
    require(all(name in props for name in properties), "SYSTEMD_PROPERTY_MISSING", unit)
    return props


def parse_main_pid(raw):
    require(isinstance(raw, str) and re.fullmatch(r"0|[1-9][0-9]{0,9}", raw) is not None, "MAINPID_UNPARSEABLE")
    return int(raw)


def require_timer_inactive(unit):
    props = systemctl_show(unit, SHOW_COMMON_PROPS)
    require(props["LoadState"] == "loaded", "TIMER_NOT_LOADED", unit)
    require(props["ActiveState"] == "inactive", "TIMER_ACTIVE", unit)


def require_service_idle(unit):
    props = systemctl_show(unit, SHOW_SERVICE_PROPS)
    require(props["LoadState"] == "loaded", "SERVICE_NOT_LOADED", unit)
    require(props["ActiveState"] in ("inactive", "failed", "dead"), "SERVICE_NOT_IDLE", unit)
    require(parse_main_pid(props["MainPID"]) == 0, "SERVICE_MAINPID_ACTIVE", unit)


def require_transient_idle(unit):
    require(UNIT_NAME_RE.fullmatch(unit) is not None, "UNIT_NAME_UNSAFE", unit)
    props = systemctl_show(unit, SHOW_SERVICE_PROPS)
    if props["LoadState"] == "not-found":
        require(parse_main_pid(props["MainPID"]) == 0, "INCIDENT_UNIT_ACTIVE", unit)
        return
    require(props["LoadState"] in ("loaded", "not-found"), "INCIDENT_UNIT_STATE_UNKNOWN", unit)
    require(props["ActiveState"] in ("inactive", "failed", "dead"), "INCIDENT_UNIT_ACTIVE", unit)
    require(parse_main_pid(props["MainPID"]) == 0, "INCIDENT_UNIT_ACTIVE", unit)


def validate_root_path(raw):
    path = Path(raw)
    require(path.is_absolute() and ".." not in path.parts, "UNSAFE_PATH", path)
    require(path.parent == STATE_PARENT, "OUTPUT_PARENT_REFUSED", path)
    require(ROOT_NAME_RE.fullmatch(path.name) is not None, "OUTPUT_NAME_REFUSED", path)
    require(SAFE_PATH_RE.fullmatch(str(path)) is not None, "UNSAFE_PATH", path)
    return path


def split_newline(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def assignment_keys(text):
    keys = []
    seen = set()
    require("\x00" not in text and "\r" not in text, "ENV_SYNTAX")
    require(len(text.split("\n")) <= MAX_ENV_LINES, "ENV_TOO_MANY_LINES")
    for raw in text.splitlines(keepends=True):
        body, _newline = split_newline(raw)
        parsed = ASSIGNMENT_RE.fullmatch(body)
        if parsed is None:
            continue
        key = parsed.group(1)
        require(key not in seen, "ENV_DUPLICATE_KEY")
        seen.add(key)
        keys.append(key)
    return keys


def rewrite_env(text, *, replace, append):
    assignment_keys(text)
    found = set()
    database_url_line = None
    out = []
    for raw in text.splitlines(keepends=True):
        body, newline = split_newline(raw)
        parsed = ASSIGNMENT_RE.fullmatch(body)
        if parsed is None:
            out.append(raw if raw.endswith(("\n", "\r\n")) else raw + "\n")
            continue
        key, value = parsed.group(1), parsed.group(2)
        found.add(key)
        if key in append or key in (
            COMPRESSION_STATEMENT_KEY,
            COMPRESSION_WRAPPER_KEY,
            COMPRESSION_SERVICE_KEY,
            COLD_SERVICE_KEY,
        ):
            raise Refusal("UNEXPECTED_BUDGET_KEY")
        if key in replace:
            expected_old, new_value = replace[key]
            require(value == expected_old, "ORIGINAL_BUDGET_DRIFT")
            out.append(f"{key}={new_value}{newline or chr(10)}")
            continue
        line = raw if raw.endswith(("\n", "\r\n")) else raw + "\n"
        if key == DATABASE_URL_KEY:
            database_url_line = line
        out.append(line)
    for key in replace:
        require(key in found, "ORIGINAL_BUDGET_MISSING")
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for key, value in append.items():
        out.append(f"{key}={value}\n")
    rewritten = "".join(out)
    assignment_keys(rewritten)
    if database_url_line is not None:
        retained = None
        for raw in rewritten.splitlines(keepends=True):
            parsed = ASSIGNMENT_RE.fullmatch(split_newline(raw)[0])
            if parsed is not None and parsed.group(1) == DATABASE_URL_KEY:
                retained = raw if raw.endswith(("\n", "\r\n")) else raw + "\n"
                break
        require(retained == database_url_line, "DATABASE_URL_MUTATED")
    require(rewritten.encode("utf-8") != text.encode("utf-8"), "BUDGET_REWRITE_NOOP")
    return rewritten


def record_file(path, payload, ident, extra=None):
    record = {"path": str(path), "sha256": digest(payload), "identity": ident}
    if extra:
        record.update(extra)
    return record


def load_json_object(payload, *, code):
    try:
        document = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refusal(code) from None
    require(isinstance(document, dict), code)
    return document


def protected_snapshot():
    compression_bytes, compression_id = read_regular(COMPRESSION_ENV, max_bytes=MAX_ENV_BYTES, private=True)
    cold_bytes, cold_id = read_regular(COLD_ENV, max_bytes=MAX_ENV_BYTES, private=True)
    state_bytes, state_id = read_regular(GOVERNANCE_STATE, max_bytes=MAX_STATE_BYTES)
    state = load_json_object(state_bytes, code="GOVERNANCE_STATE_INVALID")
    require(state.get("phase") == "staged", "GOVERNANCE_NOT_STAGED")
    identity = state.get("identity")
    require(isinstance(identity, dict), "GOVERNANCE_IDENTITY_MISSING")
    require(identity.get("runtime_root") == GOVERNANCE_RUNTIME, "GOVERNANCE_RUNTIME_MISMATCH")
    unit_root = identity.get("unit_root")
    require(isinstance(unit_root, str) and unit_root, "GOVERNANCE_UNIT_ROOT_MISSING")
    pin_path = Path(unit_root) / (GOVERNANCE_SERVICE + ".d") / PIN_NAME
    pin_bytes, pin_id = read_regular(pin_path, max_bytes=MAX_PIN_BYTES)
    require(
        isinstance(state.get("pin_digest"), str) and digest(pin_bytes) == state["pin_digest"], "PIN_DIGEST_MISMATCH"
    )
    retention_bytes, retention_id = read_regular(RETENTION_RECEIPT, max_bytes=MAX_RECEIPT_BYTES)
    require(digest(retention_bytes) == RETENTION_SHA, "RETENTION_RECEIPT_DRIFT", RETENTION_RECEIPT)
    scheduled_bytes, scheduled_id = read_regular(SCHEDULED_RECEIPT, max_bytes=MAX_RECEIPT_BYTES)
    require(digest(scheduled_bytes) == SCHEDULED_SHA, "SCHEDULED_RECEIPT_DRIFT", SCHEDULED_RECEIPT)
    return {
        "compression": record_file(COMPRESSION_ENV, compression_bytes, compression_id),
        "cold": record_file(COLD_ENV, cold_bytes, cold_id),
        "governance_state": record_file(GOVERNANCE_STATE, state_bytes, state_id),
        "governance_pin": record_file(pin_path, pin_bytes, pin_id),
        "retention_receipt": record_file(RETENTION_RECEIPT, retention_bytes, retention_id),
        "scheduled_receipt": record_file(SCHEDULED_RECEIPT, scheduled_bytes, scheduled_id),
        "compression_bytes": compression_bytes,
        "cold_bytes": cold_bytes,
        "scheduled_bytes": scheduled_bytes,
        "pin_digest": state["pin_digest"],
    }


def require_source():
    safe(PRODUCTION, directory=True)
    require(git("rev-parse", "HEAD") == OLD, "GIT_HEAD_MISMATCH")
    require(not git("status", "--porcelain", "--untracked-files=no"), "TRACKED_RUNTIME_DIRTY")
    require(PREFLIGHT.is_file() and not PREFLIGHT.is_symlink(), "PREFLIGHT_UNAVAILABLE")
    require(PYTHON.is_file() and os.access(PYTHON, os.X_OK), "PRIVATE_PYTHON_MISSING")


def require_maintenance_idle():
    for unit in TIMERS:
        require_timer_inactive(unit)
    for unit in MAINTENANCE_SERVICES:
        require_service_idle(unit)


def preflight(compression, cold, *args):
    argv = [
        str(PYTHON),
        "-E",
        str(PREFLIGHT),
        "--compression-env",
        str(compression),
        "--cold-env",
        str(cold),
        *args,
    ]
    return run(argv, timeout=PREFLIGHT_TIMEOUT, cwd=str(PRODUCTION))[1].strip()


def require_assembly(compression, cold, expected):
    check = preflight(compression, cold, "--check")
    require(check == "", "PREFLIGHT_CHECK_OUTPUT")
    assembly = preflight(compression, cold, "--lane", "compression", "--format", "assembly")
    require(assembly == expected, "ASSEMBLY_MISMATCH")
    return assembly


def require_uid():
    require(os.getuid() == UID and os.geteuid() == UID, "WRONG_UID")


def output_payload(operation, *, outcome, error=None, error_path=None, **values):
    payload = {
        "schema_version": SCHEMA,
        "operation": operation,
        "outcome": outcome,
        "error": error,
        "error_path": error_path,
    }
    payload.update(values)
    return json.dumps(payload, sort_keys=True)


def mkdir_private(path):
    parent = safe(path.parent, directory=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    dir_fd = os.open(parent, flags)
    try:
        os.mkdir(path.name, 0o700, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except FileExistsError:
        raise Refusal("OUTPUT_EXISTS", path) from None
    finally:
        os.close(dir_fd)
    return safe(path, directory=True, private=True)


def prepare(root):
    require_uid()
    root = validate_root_path(root)
    safe(STATE_PARENT, directory=True)
    try:
        root.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise Refusal("PATH_STAT_FAILED", root) from None
    else:
        raise Refusal("OUTPUT_EXISTS", root)
    require_source()
    snapshot = protected_snapshot()
    require_assembly(COMPRESSION_ENV, COLD_ENV, ORIGINAL_ASSEMBLY)
    require_maintenance_idle()
    nonce = root.name.removeprefix("issue2349-catchup-")
    transient_unit = f"nhms-issue2349-catchup-{nonce}.service"
    require_transient_idle(transient_unit)
    compression_text = snapshot["compression_bytes"].decode("utf-8", "strict")
    cold_text = snapshot["cold_bytes"].decode("utf-8", "strict")
    private_compression = rewrite_env(
        compression_text,
        replace={COMPRESSION_BOUND_KEY: ("4", str(BOUND))},
        append={
            COMPRESSION_STATEMENT_KEY: str(STATEMENT_MS),
            COMPRESSION_WRAPPER_KEY: str(WRAPPER_SECONDS),
            COMPRESSION_SERVICE_KEY: str(SERVICE_SECONDS),
        },
    )
    private_cold = rewrite_env(
        cold_text,
        replace={},
        append={COLD_SERVICE_KEY: str(SERVICE_SECONDS)},
    )
    require(COLD_WRAPPER_KEY not in assignment_keys(private_cold), "COLD_WRAPPER_MUTATED")
    require(COLD_STATEMENT_KEY not in assignment_keys(private_cold), "COLD_STATEMENT_MUTATED")
    mkdir_private(root)
    compression_path = root / PRIVATE_COMPRESSION_NAME
    cold_path = root / PRIVATE_COLD_NAME
    receipt_path = root / PRIVATE_RECEIPT_NAME
    exclusive_write(compression_path, private_compression.encode("utf-8"), parent_private=True)
    exclusive_write(cold_path, private_cold.encode("utf-8"), parent_private=True)
    exclusive_write(receipt_path, snapshot["scheduled_bytes"], parent_private=True)
    assembly = require_assembly(compression_path, cold_path, PRIVATE_ASSEMBLY)
    compression_copy, compression_copy_id = read_regular(compression_path, max_bytes=MAX_ENV_BYTES, private=True)
    cold_copy, cold_copy_id = read_regular(cold_path, max_bytes=MAX_ENV_BYTES, private=True)
    receipt_copy, receipt_copy_id = read_regular(receipt_path, max_bytes=MAX_RECEIPT_BYTES, private=True)
    require(digest(compression_copy) == digest(private_compression.encode("utf-8")), "PRIVATE_ENV_HASH_MISMATCH")
    require(digest(cold_copy) == digest(private_cold.encode("utf-8")), "PRIVATE_ENV_HASH_MISMATCH")
    require(digest(receipt_copy) == SCHEDULED_SHA, "SCHEDULED_RECEIPT_DRIFT", receipt_path)
    later = protected_snapshot()
    for key in ("compression", "cold", "governance_state", "governance_pin", "retention_receipt", "scheduled_receipt"):
        require(later[key]["sha256"] == snapshot[key]["sha256"], "PROTECTED_HASH_DRIFT", later[key]["path"])
        require(later[key]["identity"] == snapshot[key]["identity"], "PROTECTED_IDENTITY_DRIFT", later[key]["path"])
    manifest = {
        "schema_version": "issue2349.catchup.prepare.v1",
        "issue": 2349,
        "created_at": now(),
        "root": str(root),
        "transient_unit": transient_unit,
        "source": {"repo": str(PRODUCTION), "head": OLD},
        "budget": {
            "compression_statement_timeout_ms": STATEMENT_MS,
            "compression_wrapper_wall_seconds": WRAPPER_SECONDS,
            "cold_wrapper_wall_seconds": COLD_WRAPPER_SECONDS,
            "service_wall_seconds": SERVICE_SECONDS,
            "compression_per_tick_bound": BOUND,
            "assembly": assembly,
        },
        "paths": {
            "original_compression_env": str(COMPRESSION_ENV),
            "original_cold_env": str(COLD_ENV),
            "private_compression_env": str(compression_path),
            "private_cold_env": str(cold_path),
            "scheduled_receipt": str(SCHEDULED_RECEIPT),
            "private_scheduled_receipt": str(receipt_path),
            "retention_receipt": str(RETENTION_RECEIPT),
            "governance_state": str(GOVERNANCE_STATE),
            "governance_pin": snapshot["governance_pin"]["path"],
        },
        "protected": {
            "compression_env": snapshot["compression"],
            "cold_env": snapshot["cold"],
            "scheduled_receipt": snapshot["scheduled_receipt"],
            "retention_receipt": snapshot["retention_receipt"],
            "governance_state": snapshot["governance_state"],
            "governance_pin": snapshot["governance_pin"],
        },
        "private": {
            "compression_env": record_file(compression_path, compression_copy, compression_copy_id),
            "cold_env": record_file(cold_path, cold_copy, cold_copy_id),
            "scheduled_receipt": record_file(receipt_path, receipt_copy, receipt_copy_id),
        },
    }
    payload = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    exclusive_write(root / MANIFEST_NAME, payload, parent_private=True)
    return {
        "schema_version": "issue2349.catchup.prepare.v1",
        "root": str(root),
        "transient_unit": transient_unit,
        "source_head": OLD,
        "budget": manifest["budget"],
        "paths": manifest["paths"],
        "digests": {
            "original_compression_env": snapshot["compression"]["sha256"],
            "original_cold_env": snapshot["cold"]["sha256"],
            "private_compression_env": digest(compression_copy),
            "private_cold_env": digest(cold_copy),
            "scheduled_receipt": SCHEDULED_SHA,
            "private_scheduled_receipt": digest(receipt_copy),
            "retention_receipt": RETENTION_SHA,
            "governance_state": snapshot["governance_state"]["sha256"],
            "governance_pin": snapshot["pin_digest"],
            "manifest": digest(payload),
        },
    }


def load_manifest(root):
    safe(root, directory=True, private=True)
    payload, ident = read_regular(root / MANIFEST_NAME, max_bytes=MAX_RECEIPT_BYTES, private=True)
    manifest = load_json_object(payload, code="MANIFEST_INVALID")
    require(manifest.get("schema_version") == "issue2349.catchup.prepare.v1", "MANIFEST_SCHEMA")
    require(manifest.get("root") == str(root), "MANIFEST_ROOT_MISMATCH")
    require(
        isinstance(manifest.get("source"), dict) and manifest["source"].get("head") == OLD, "MANIFEST_SOURCE_MISMATCH"
    )
    require(UNIT_NAME_RE.fullmatch(str(manifest.get("transient_unit") or "")) is not None, "MANIFEST_UNIT_INVALID")
    require(
        isinstance(manifest.get("budget"), dict) and manifest["budget"].get("assembly") == PRIVATE_ASSEMBLY,
        "MANIFEST_BUDGET_MISMATCH",
    )
    require(
        isinstance(manifest.get("protected"), dict) and isinstance(manifest.get("private"), dict), "MANIFEST_SCHEMA"
    )
    require(isinstance(manifest.get("paths"), dict), "MANIFEST_SCHEMA")
    return manifest, digest(payload), ident


def verify_record(record, *, private=False, expected_sha=None):
    require(isinstance(record, dict) and isinstance(record.get("path"), str), "MANIFEST_RECORD_INVALID")
    require(
        isinstance(record.get("sha256"), str) and isinstance(record.get("identity"), dict), "MANIFEST_RECORD_INVALID"
    )
    path = Path(record["path"])
    payload, ident = read_regular(
        path, max_bytes=max(MAX_ENV_BYTES, MAX_RECEIPT_BYTES, MAX_STATE_BYTES), private=private
    )
    require(digest(payload) == record["sha256"], "PROTECTED_HASH_DRIFT", path)
    require(ident == record["identity"], "PROTECTED_IDENTITY_DRIFT", path)
    if expected_sha is not None:
        require(record["sha256"] == expected_sha, "PROTECTED_HASH_DRIFT", path)
    return payload, ident


def verify_prepared(root):
    require_uid()
    root = validate_root_path(root)
    safe(STATE_PARENT, directory=True)
    manifest, manifest_sha, _ident = load_manifest(root)
    require_source()
    snapshot = protected_snapshot()
    for key, current in (
        ("compression_env", snapshot["compression"]),
        ("cold_env", snapshot["cold"]),
        ("scheduled_receipt", snapshot["scheduled_receipt"]),
        ("retention_receipt", snapshot["retention_receipt"]),
        ("governance_state", snapshot["governance_state"]),
        ("governance_pin", snapshot["governance_pin"]),
    ):
        recorded = manifest["protected"][key]
        require(current["sha256"] == recorded["sha256"], "PROTECTED_HASH_DRIFT", current["path"])
        require(current["identity"] == recorded["identity"], "PROTECTED_IDENTITY_DRIFT", current["path"])
        require(current["path"] == recorded["path"], "PROTECTED_PATH_DRIFT", current["path"])
    require(snapshot["retention_receipt"]["sha256"] == RETENTION_SHA, "RETENTION_RECEIPT_DRIFT")
    require(snapshot["scheduled_receipt"]["sha256"] == SCHEDULED_SHA, "SCHEDULED_RECEIPT_DRIFT")
    private = manifest["private"]
    verify_record(private["compression_env"], private=True)
    verify_record(private["cold_env"], private=True)
    verify_record(private["scheduled_receipt"], private=True, expected_sha=SCHEDULED_SHA)
    require(Path(private["compression_env"]["path"]) == root / PRIVATE_COMPRESSION_NAME, "PRIVATE_PATH_DRIFT")
    require(Path(private["cold_env"]["path"]) == root / PRIVATE_COLD_NAME, "PRIVATE_PATH_DRIFT")
    require(Path(private["scheduled_receipt"]["path"]) == root / PRIVATE_RECEIPT_NAME, "PRIVATE_PATH_DRIFT")
    require_assembly(private["compression_env"]["path"], private["cold_env"]["path"], PRIVATE_ASSEMBLY)
    require_maintenance_idle()
    require_transient_idle(manifest["transient_unit"])
    return manifest, manifest_sha, snapshot


def check(root):
    manifest, manifest_sha, snapshot = verify_prepared(root)
    return {
        "schema_version": "issue2349.catchup.check.v1",
        "root": manifest["root"],
        "transient_unit": manifest["transient_unit"],
        "source_head": OLD,
        "budget": manifest["budget"],
        "paths": manifest["paths"],
        "digests": {
            "original_compression_env": snapshot["compression"]["sha256"],
            "original_cold_env": snapshot["cold"]["sha256"],
            "private_compression_env": manifest["private"]["compression_env"]["sha256"],
            "private_cold_env": manifest["private"]["cold_env"]["sha256"],
            "scheduled_receipt": SCHEDULED_SHA,
            "private_scheduled_receipt": manifest["private"]["scheduled_receipt"]["sha256"],
            "retention_receipt": RETENTION_SHA,
            "governance_state": snapshot["governance_state"]["sha256"],
            "governance_pin": snapshot["pin_digest"],
            "manifest": manifest_sha,
        },
    }


def cleanup(root):
    manifest, manifest_sha, snapshot = verify_prepared(root)
    compression = manifest["private"]["compression_env"]
    cold = manifest["private"]["cold_env"]
    unlink_exact(
        compression["path"],
        {**compression["identity"], "sha256": compression["sha256"]},
    )
    unlink_exact(
        cold["path"],
        {**cold["identity"], "sha256": cold["sha256"]},
    )
    later = protected_snapshot()
    for key in ("compression", "cold", "governance_state", "governance_pin", "retention_receipt", "scheduled_receipt"):
        require(later[key]["sha256"] == snapshot[key]["sha256"], "PROTECTED_HASH_DRIFT", later[key]["path"])
    receipt = manifest["private"]["scheduled_receipt"]
    verify_record(receipt, private=True, expected_sha=SCHEDULED_SHA)
    load_manifest(Path(manifest["root"]))
    return {
        "schema_version": "issue2349.catchup.cleanup.v1",
        "root": manifest["root"],
        "transient_unit": manifest["transient_unit"],
        "removed": [compression["path"], cold["path"]],
        "retained": [
            str(Path(manifest["root"]) / MANIFEST_NAME),
            receipt["path"],
            str(SCHEDULED_RECEIPT),
            str(RETENTION_RECEIPT),
            str(COMPRESSION_ENV),
            str(COLD_ENV),
        ],
        "digests": {
            "original_compression_env": later["compression"]["sha256"],
            "original_cold_env": later["cold"]["sha256"],
            "scheduled_receipt": SCHEDULED_SHA,
            "private_scheduled_receipt": receipt["sha256"],
            "retention_receipt": RETENTION_SHA,
            "manifest": manifest_sha,
        },
    }


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("operation", choices=("prepare", "check", "cleanup"))
    p.add_argument(
        "--root", required=True, help="New direct child of /home/nwm/.local/state named issue2349-catchup-<nonce>"
    )
    return p


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.operation == "prepare":
            result = prepare(args.root)
            outcome = "PREPARED"
        elif args.operation == "check":
            result = check(args.root)
            outcome = "CHECK_OK"
        else:
            result = cleanup(args.root)
            outcome = "CLEANED"
    except Refusal as error:
        print(output_payload(args.operation, outcome="REFUSED", error=str(error), error_path=error.path), flush=True)
        return 1
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
        print(output_payload(args.operation, outcome="REFUSED", error="OPERATION_FAILED"), flush=True)
        return 1
    print(output_payload(args.operation, outcome=outcome, **result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
