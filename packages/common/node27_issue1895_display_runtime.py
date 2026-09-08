"""C1 exact-SHA display runtime receipt owner and PASS-only binder."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from packages.common.evidence_io import BoundedEvidenceError, read_bounded_bytes_with_identity_no_follow
from packages.common.node27_issue1895_c14 import (
    C1_DISPLAY_UNIT,
    C1_HEALTH_PATH,
    C1_RUNTIME_CONFIG_PATH,
    C1_SERVICE_ROLE,
)
from packages.common.node27_issue1895_http import close_response, open_local_get, read_bounded_json_body
from packages.common.node27_issue1895_private_receipt import (
    assert_bracket,
    assert_timestamp_order,
    private_parent_facts,
    publish_private_receipt,
    read_private_receipt,
    refuse,
    require_matching_shas,
    require_sha,
    utc_now,
)
from packages.common.node27_issue1895_process import DISPLAY_API_UNIT, resolve_display_api_pid
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.node27_issue1895_watermark import parse_systemctl_show
from packages.common.safe_fs import SafeFilesystemError, stat_no_follow

DISPLAY_PORT_KEY = "NHMS_DISPLAY_API_PORT"
DISPLAY_ENV_PATH = Path("/home/nwm/NWM/infra/env/display.env")
DISPLAY_ENV_RELATIVE = "infra/env/display.env"
DISPLAY_ENV_MAX_BYTES = 65_536
HTTP_TIMEOUT_SECONDS = 10
HTTP_BODY_LIMIT = 65_536
SYSTEMCTL_ARGV = (
    "/usr/bin/systemctl",
    "--user",
    "show",
    DISPLAY_API_UNIT,
    "--property=MainPID",
    "--property=ActiveState",
    "--property=ControlGroup",
    "--property=Id",
)
PORT_RE = re.compile(r"^(?:[1-9][0-9]{0,4})$")
ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
EXPECTED_RUNTIME = {
    "service_role": C1_SERVICE_ROLE,
    "control_mutations_enabled": False,
    "slurm_routes_enabled": False,
    "queue_depth_mode": "display_readonly_unavailable",
    "display_readonly": True,
}
REQUIRED_KEYS = (
    "artifact",
    "schema_version",
    "status",
    "head_sha",
    "reviewed_sha",
    "started_at",
    "ended_at",
    "generated_at",
    "display_env",
    "port",
    "origin",
    "unit",
    "main_pid",
    "control_group_sha256",
    "checks",
)
ARTIFACT = "nhms-issue1895-c1-display-runtime"
SCHEMA_VERSION = "1.0"


def _error(message: str, code: str) -> None:
    refuse(message, code, stage="c1")


def _strict_port(value: object) -> int:
    text = str(value or "")
    if PORT_RE.fullmatch(text) is None:
        _error("display API port is not a canonical decimal", "C1_PORT_INVALID")
    port = int(text)
    if not 1 <= port <= 65535:
        _error("display API port is out of range", "C1_PORT_INVALID")
    return port


def parse_display_port(text: str) -> int:
    """Read exactly one unquoted scalar port assignment; never source display.env."""

    if not isinstance(text, str):
        _error("display env is not text", "C1_DISPLAY_ENV_INVALID")
    values: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        match = ASSIGNMENT_RE.fullmatch(raw.rstrip("\r"))
        if match is None:
            continue
        key, value = match.groups()
        if key in seen:
            _error("display env contains duplicate assignment", "C1_DISPLAY_ENV_DUPLICATE")
        seen.add(key)
        if key != DISPLAY_PORT_KEY:
            continue
        if value != value.strip() or not value or any(token in value for token in ("'", '"', "$", "`", "\\", "#")):
            _error("display API port assignment is shell-ambiguous", "C1_PORT_INVALID")
        values.append(value)
    if len(values) > 1:
        _error("display env contains duplicate port assignment", "C1_DISPLAY_ENV_DUPLICATE")
    return 8080 if not values else _strict_port(values[0])


def read_display_port(
    display_env: Path,
    *,
    expected_display_env: Path = DISPLAY_ENV_PATH,
) -> tuple[int, str]:
    """Read only the explicitly pinned private display.env without sourcing it.

    Production callers retain the fixed node-27 path. Tests may inject a private
    temporary path through ``expected_display_env`` without widening the CLI.
    """

    if display_env != expected_display_env or not display_env.is_absolute() or display_env.name != "display.env":
        _error("display env path is not the explicitly pinned private identity", "C1_DISPLAY_ENV_PATH")
    try:
        raw, identity = read_bounded_bytes_with_identity_no_follow(
            display_env,
            max_bytes=DISPLAY_ENV_MAX_BYTES,
            label="C1 display.env",
        )
    except BoundedEvidenceError:
        _error("display env is unreadable", "C1_DISPLAY_ENV_UNREADABLE")
        raise AssertionError("unreachable")
    try:
        info = stat_no_follow(display_env)
    except (OSError, SafeFilesystemError):
        _error("display env is unreadable", "C1_DISPLAY_ENV_UNREADABLE")
        raise AssertionError("unreachable")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        _error("display env is not an euid-owned regular input", "C1_DISPLAY_ENV_INVALID")
    if (info.st_mode & 0o777) != 0o600:
        _error("display env must be private mode-0600", "C1_DISPLAY_ENV_INVALID")
    if int(info.st_dev) != identity.device or int(info.st_ino) != identity.inode or int(info.st_size) != identity.size:
        _error("display env changed while opening", "C1_DISPLAY_ENV_INVALID")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _error("display env is not UTF-8", "C1_DISPLAY_ENV_INVALID")
    return parse_display_port(text), DISPLAY_ENV_RELATIVE


def local_origin(port: int) -> str:
    return f"http://127.0.0.1:{_strict_port(str(port))}"


def _parse_systemctl_result(result: Any) -> dict[str, str]:
    if int(getattr(result, "returncode", 1)) != 0:
        _error("systemctl show failed", "C1_SYSTEMCTL_FAILED")
    output = getattr(result, "stdout", "")
    if not isinstance(output, str) or len(output.encode("utf-8")) > 65_536:
        _error("systemctl show output is invalid", "C1_SYSTEMCTL_INVALID")
    facts = parse_systemctl_show(output)
    if set(facts) != {"MainPID", "ActiveState", "ControlGroup", "Id"}:
        _error("systemctl show fields are incomplete", "C1_SYSTEMCTL_INVALID")
    if facts.get("Id") != C1_DISPLAY_UNIT or facts.get("ActiveState") != "active":
        _error("display unit is not active under its expected Id", "C1_SYSTEMCTL_INVALID")
    return facts


def systemd_show(
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, str]:
    try:
        result = run(
            SYSTEMCTL_ARGV,
            check=False,
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        _error("systemctl show is unavailable", "C1_SYSTEMCTL_FAILED")
        raise AssertionError("unreachable")
    return _parse_systemctl_result(result)


def proc_cgroup(pid: int) -> str:
    """Read the fixed proc cgroup pseudo-file through one bounded no-follow FD.

    proc pseudo-files are not private mode-0600 receipts, so they intentionally
    do not use the private-receipt reader.  The lstat/open/fstat/path-restat
    sequence instead binds the systemd MainPID to the exact proc entry observed.
    """

    if pid <= 0:
        _error("display unit MainPID is invalid", "C1_PID_INVALID")
    path = Path("/proc") / str(pid) / "cgroup"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd: int | None = None
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            _error("display process cgroup is not a regular proc entry", "C1_CGROUP_UNREADABLE")
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_dev) != int(before.st_dev)
            or int(opened.st_ino) != int(before.st_ino)
        ):
            _error("display process cgroup changed while opening", "C1_CGROUP_UNREADABLE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(4096, 65_537 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > 65_536:
                _error("display process cgroup exceeds the byte bound", "C1_CGROUP_UNREADABLE")
            chunks.append(chunk)
        after = os.fstat(fd)
        after_path = os.lstat(path)
        if (
            int(after.st_dev) != int(opened.st_dev)
            or int(after.st_ino) != int(opened.st_ino)
            or stat.S_ISLNK(after_path.st_mode)
            or int(after_path.st_dev) != int(opened.st_dev)
            or int(after_path.st_ino) != int(opened.st_ino)
        ):
            _error("display process cgroup changed while reading", "C1_CGROUP_UNREADABLE")
        raw = b"".join(chunks)
    except Issue1895ReadinessError:
        raise
    except OSError:
        _error("display process cgroup is unreadable", "C1_CGROUP_UNREADABLE")
        raise AssertionError("unreachable")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if not raw:
        _error("display process cgroup is invalid", "C1_CGROUP_UNREADABLE")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _error("display process cgroup is invalid", "C1_CGROUP_UNREADABLE")
        raise AssertionError("unreachable")


def _require_mapping(value: object, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("C1 HTTP payload is not an object", code)
    return value


def _get_json(*, origin: str, path: str, opener: Any | None = None) -> tuple[int, Mapping[str, Any]]:
    request, response = open_local_get(
        url=f"{origin}{path}",
        opener=opener,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
        stage="c1",
    )
    del request
    try:
        status, _body, payload = read_bounded_json_body(response, body_limit=HTTP_BODY_LIMIT, stage="c1")
    finally:
        close_response(response)
    return status, _require_mapping(payload, code="C1_HTTP_JSON")


def fetch_expected_404(*, origin: str, path: str, opener: Any | None = None) -> int:
    """Bounded GET primitive for an expected 404 without weakening 2xx callers."""

    from packages.common.node27_issue1895_http import fetch_local_expected_status

    try:
        status, _body = fetch_local_expected_status(
            url=f"{origin}{path}",
            expected_status=404,
            opener=opener,
            timeout_seconds=HTTP_TIMEOUT_SECONDS,
            body_limit=HTTP_BODY_LIMIT,
            stage="c1",
        )
    except Issue1895ReadinessError as error:
        if error.code in {"API_EXPECTED_STATUS_INVALID", "API_REQUEST_FAILED"}:
            _error("slurm health status is not the required 404", "C1_SLURM_STATUS")
        raise
    return status


def observe_display_runtime(
    *,
    display_env: Path = DISPLAY_ENV_PATH,
    head_sha: str,
    reviewed_sha: str,
    receipt_path: Path,
    opener: Any | None = None,
    run_systemctl: Callable[..., Any] = subprocess.run,
    read_cgroup: Callable[[int], str] = proc_cgroup,
    now: Callable[[], str] = utc_now,
    expected_display_env: Path = DISPLAY_ENV_PATH,
) -> dict[str, Any]:
    """Produce the C1 PASS receipt only after all process and HTTP facts agree."""

    expected_sha = require_matching_shas(head_sha, reviewed_sha, stage="c1")
    started_at = now()
    port, env_identity = read_display_port(display_env, expected_display_env=expected_display_env)
    origin = local_origin(port)
    show = systemd_show(run=run_systemctl)
    pid = resolve_display_api_pid(show, read_cgroup=read_cgroup)
    cgroup = str(show["ControlGroup"])
    health_status, health = _get_json(origin=origin, path=C1_HEALTH_PATH, opener=opener)
    if health.get("status") != "ok":
        _error("health payload status is not ok", "C1_HEALTH_INVALID")
    runtime_status, runtime = _get_json(origin=origin, path=C1_RUNTIME_CONFIG_PATH, opener=opener)
    if runtime.get("status") != "ok":
        _error("runtime config envelope status is not ok", "C1_RUNTIME_INVALID")
    data = _require_mapping(runtime.get("data"), code="C1_RUNTIME_INVALID")
    if dict(data) != EXPECTED_RUNTIME:
        _error("runtime config data differs from display readonly contract", "C1_RUNTIME_INVALID")
    slurm_status = fetch_expected_404(origin=origin, path="/api/v1/slurm/health", opener=opener)
    ended_at = now()
    document: dict[str, Any] = {
        "artifact": ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "head_sha": expected_sha,
        "reviewed_sha": expected_sha,
        "started_at": started_at,
        "ended_at": ended_at,
        "generated_at": ended_at,
        "display_env": env_identity,
        "port": port,
        "origin": origin,
        "unit": C1_DISPLAY_UNIT,
        "main_pid": pid,
        "control_group_sha256": hashlib.sha256(cgroup.encode("utf-8")).hexdigest(),
        "checks": {
            "systemd_active": True,
            "main_pid_cgroup_bound": True,
            "health_status": health_status,
            "health_status_ok": True,
            "runtime_status": runtime_status,
            "runtime_exact": True,
            "slurm_health_status": slurm_status,
        },
    }
    validate_c1_receipt(document)
    publish_private_receipt(receipt_path, document, code_prefix="C1_RECEIPT", stage="c1")
    return document


def validate_c1_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != set(REQUIRED_KEYS):
        _error("C1 receipt keys are not closed", "C1_RECEIPT_KEYS")
    if document.get("artifact") != ARTIFACT or document.get("schema_version") != SCHEMA_VERSION:
        _error("C1 receipt artifact/schema differs", "C1_RECEIPT_SCHEMA")
    if document.get("status") != "PASS":
        _error("C1 receipt is not PASS", "C1_RECEIPT_STATUS")
    require_matching_shas(document.get("head_sha"), document.get("reviewed_sha"), stage="c1")
    assert_timestamp_order(document, stage="c1")
    if document.get("display_env") != DISPLAY_ENV_RELATIVE:
        _error("C1 display env provenance is not redacted relative identity", "C1_RECEIPT_ENV")
    port = document.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or local_origin(port) != document.get("origin"):
        _error("C1 port/origin fields differ", "C1_RECEIPT_ORIGIN")
    if document.get("unit") != C1_DISPLAY_UNIT:
        _error("C1 unit differs", "C1_RECEIPT_UNIT")
    pid = document.get("main_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        _error("C1 MainPID is invalid", "C1_RECEIPT_PID")
    digest = str(document.get("control_group_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        _error("C1 cgroup digest is invalid", "C1_RECEIPT_CGROUP")
    checks = document.get("checks")
    expected_checks = {
        "systemd_active": True,
        "main_pid_cgroup_bound": True,
        "health_status": 200,
        "health_status_ok": True,
        "runtime_status": 200,
        "runtime_exact": True,
        "slurm_health_status": 404,
    }
    if not isinstance(checks, Mapping) or dict(checks) != expected_checks:
        _error("C1 receipt checks differ", "C1_RECEIPT_CHECKS")
    return dict(document)


def bind_c1_receipt(
    receipt_path: Path,
    *,
    reviewed_sha: str,
    expected_port: int,
    cmd_start: str,
    cmd_end: str,
) -> dict[str, Any]:
    expected_sha = require_sha(reviewed_sha, label="reviewed_sha", stage="c1")
    raw, document, info = read_private_receipt(receipt_path, code_prefix="C1_RECEIPT", stage="c1")
    del raw
    private_parent_facts(receipt_path, code_prefix="C1_RECEIPT")
    validated = validate_c1_receipt(document)
    if validated["head_sha"] != expected_sha or validated["reviewed_sha"] != expected_sha:
        _error("C1 receipt SHA does not bind reviewed SHA", "C1_BIND_SHA")
    if validated["port"] != _strict_port(str(expected_port)) or validated["origin"] != local_origin(expected_port):
        _error("C1 receipt port/origin does not bind expected display env", "C1_BIND_ORIGIN")
    if validated["unit"] != C1_DISPLAY_UNIT:
        _error("C1 receipt unit does not bind expected unit", "C1_BIND_UNIT")
    assert_bracket(info=info, document=validated, cmd_start=cmd_start, cmd_end=cmd_end, stage="c1")
    return validated


__all__ = (
    "ARTIFACT",
    "DISPLAY_ENV_PATH",
    "DISPLAY_ENV_RELATIVE",
    "DISPLAY_PORT_KEY",
    "EXPECTED_RUNTIME",
    "HTTP_BODY_LIMIT",
    "HTTP_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "SYSTEMCTL_ARGV",
    "bind_c1_receipt",
    "fetch_expected_404",
    "local_origin",
    "observe_display_runtime",
    "parse_display_port",
    "read_display_port",
    "validate_c1_receipt",
)
