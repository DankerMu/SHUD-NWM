#!/usr/bin/env python3
"""Throwaway PostgreSQL instance for integration tests on node-27 (#3376).

Integration tests create and drop one database per test. On the production
postmaster that races autovacuum's launcher, so on node-27 they run against
``nhms-it-db``: a container started from the production container's image id,
on a loopback port that is not 55432, with its data in an instance directory
the tool marks with a sentinel.

    start   create the instance (idempotent while it runs)
    run     execute a command with the integration opt-in and the instance URL
    status  print absent / stopped / running
    stop    remove the container and the instance directory

The superuser password lives only in ``<root>/postgres.env`` (mode 0600); it
is never placed in an argument list and never printed. Standard library only;
``docker`` is found on ``PATH``.

Exit codes: 0 success (status: running) · 1 refused or failed · 2 usage ·
3 the instance lock is held (start, stop, run) or the instance is not running
(status) · for ``run``, the command's own code once it has been executed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

CONTAINER = "nhms-it-db"
LABEL_KEY = "nhms.integration-pg"
PRODUCTION_CONTAINER = "nhms-db"
PRODUCTION_PORT = 55432
DEFAULT_PORT = 55442
DEFAULT_ROOT = "/home/nwm/nhms-it-pg"
SENTINEL = ".nhms-it-pg.json"
ENV_FILE = "postgres.env"
CONTAINER_PGDATA = "/home/postgres/pgdata/data"
EXPECTED_LOCALE = "C.UTF-8|UTF8"
READY_TIMEOUT_SEC = 120.0
DOCKER_TIMEOUT_SEC = 120
PASSWD_GROUP_SEPARATOR = "\n--GROUP--\n"
SETTINGS = (
    "shared_preload_libraries=timescaledb",
    "max_connections=100",
    "max_locks_per_transaction=512",
    "max_worker_processes=59",
    "timescaledb.max_background_workers=16",
    "timezone=Etc/UTC",
    "log_statement=ddl",
    "fsync=off",
    "synchronous_commit=off",
    "full_page_writes=off",
)
DUMP_SCRIPT = "cat /etc/passwd; printf '\\n--GROUP--\\n'; cat /etc/group"
# Over TCP on purpose: the entrypoint's temporary init server listens only on the Unix socket.
PROBE_SCRIPT = (
    'PGPASSWORD="$POSTGRES_PASSWORD" PGCONNECT_TIMEOUT=5 exec psql -X -At -h 127.0.0.1 -p 5432 '
    '-U "$POSTGRES_USER" -d postgres -c '
    "\"SELECT datcollate || '|' || pg_encoding_to_char(encoding) FROM pg_database WHERE datname = 'template1'\""
)

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_NOT_RUNNING = 3


class Refusal(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


# --- docker -----------------------------------------------------------------


def _docker(*args: str, timeout: float = DOCKER_TIMEOUT_SEC) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("docker")
    if binary is None:
        raise Refusal("docker not found on PATH")
    try:
        return subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise Refusal(f"docker {args[0]} did not answer within {timeout:g} s") from None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no message"


def _production_image() -> str:
    result = _docker("inspect", PRODUCTION_CONTAINER, "--format", "{{.Image}}")
    image = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        detail = _last_line(result.stderr)
        raise Refusal(f"cannot read the image id of {PRODUCTION_CONTAINER} ({detail}); nothing created")
    return image


def _instance_container() -> tuple[bool, str | None] | None:
    """(running, label value) of ``nhms-it-db``, or None when it does not exist."""
    result = _docker("inspect", CONTAINER, "--format", "{{.State.Running}} {{json .Config.Labels}}")
    if result.returncode:
        if "no such" in result.stderr.lower():
            return None
        raise Refusal(f"docker inspect {CONTAINER} failed ({_last_line(result.stderr)})")
    try:
        running, labels_json = result.stdout.strip().split(" ", 1)
        labels = json.loads(labels_json) or {}
    except ValueError:
        raise Refusal(f"unreadable docker inspect answer for {CONTAINER}") from None
    return running == "true", labels.get(LABEL_KEY)


# --- the two removal functions ------------------------------------------------


def remove_container(root: Path) -> bool:
    """Remove ``nhms-it-db`` if it carries the label for ``root``; True when removed."""
    container = _instance_container()
    if container is None:
        return False
    _, label = container
    if label != str(root):
        raise Refusal(f"{CONTAINER} does not carry the label {LABEL_KEY}={root}; not this instance's, left in place")
    result = _docker("rm", "-f", CONTAINER)
    if result.returncode:
        raise Refusal(f"docker rm -f {CONTAINER} failed ({_last_line(result.stderr)})")
    return True


def remove_directory(root: Path) -> bool:
    """Remove the instance directory if it carries the sentinel; True when removed."""
    try:
        mode = os.lstat(root).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise Refusal(f"{root} is a symbolic link; left in place")
    if not stat.S_ISDIR(mode):
        raise Refusal(f"{root} is not a directory; left in place")
    try:
        sentinel_mode = os.lstat(root / SENTINEL).st_mode
    except FileNotFoundError:
        raise Refusal(f"{root} has no {SENTINEL}; it is not an instance directory, left in place") from None
    if not stat.S_ISREG(sentinel_mode):
        raise Refusal(f"{root / SENTINEL} is not a regular file; {root} left in place")
    shutil.rmtree(root)
    return True


# --- shared helpers -------------------------------------------------------------


def _resolve_root(raw: str) -> Path:
    if not os.path.isabs(raw):
        raise Refusal(f"--root must be an absolute path, got {raw!r}")
    normalized = Path(os.path.normpath(raw))
    if normalized.parent == normalized:
        raise Refusal("--root must not be the file system root")
    # The parent is resolved for a stable label; the root itself is not, so a symbolic link stays visible.
    return normalized.parent.resolve() / normalized.name


def _lock_path(root: Path) -> Path:
    return root.parent / (root.name + ".lock")


def _try_lock(root: Path, operation: int) -> int | None:
    """Open the lock file and take ``operation`` without blocking; None when it is held."""
    descriptor = os.open(_lock_path(root), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _has_sentinel(root: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(root / SENTINEL).st_mode)
    except FileNotFoundError:
        return False


def _read_sentinel(root: Path) -> dict:
    try:
        sentinel = json.loads((root / SENTINEL).read_text())
        int(sentinel["port"])
        str(sentinel["image"])
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal(f"{root / SENTINEL} is missing or unreadable") from None
    return sentinel


def _short_image(image: str) -> str:
    return image.removeprefix("sha256:")[:12]


def _write_new(path: Path, text: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(text)


# --- start ----------------------------------------------------------------------


def _assess(root: Path) -> str:
    """Steps 3 and 4: 'running', or a plan of removals ('fresh', 'container', 'directory', 'both')."""
    container = _instance_container()
    remove_old_container = False
    if container is not None:
        running, label = container
        if label != str(root):
            raise Refusal(f"{CONTAINER} exists without the label {LABEL_KEY}={root}; refused, nothing changed")
        if running:
            if _has_sentinel(root) and (root / ENV_FILE).is_file():
                return "running"
            raise Refusal(f"{CONTAINER} is running but {root} lacks its sentinel or env file; refused, run stop first")
        remove_old_container = True
    remove_old_directory = False
    try:
        mode = os.lstat(root).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refusal(f"{root} is not a plain directory; refused, left untouched")
        if any(root.iterdir()):
            if not _has_sentinel(root):
                raise Refusal(f"{root} is not empty and has no {SENTINEL}; refused, left untouched")
            remove_old_directory = True
    return {
        (False, False): "fresh",
        (True, False): "container",
        (False, True): "directory",
        (True, True): "both",
    }[(remove_old_container, remove_old_directory)]


def _rewrite_accounts(dump: str, uid: int, gid: int) -> tuple[str, str]:
    parts = dump.split(PASSWD_GROUP_SEPARATOR)
    if len(parts) != 2:
        raise Refusal("unexpected answer when reading the image's /etc/passwd and /etc/group")
    passwd_rows = [line.split(":") for line in parts[0].splitlines() if line]
    group_rows = [line.split(":") for line in parts[1].splitlines() if line]
    passwd_hits = group_hits = 0
    for row in passwd_rows:
        if row[0] == "postgres" and len(row) >= 4:
            row[2:4] = [str(uid), str(gid)]
            passwd_hits += 1
    for row in group_rows:
        if row[0] == "postgres" and len(row) >= 3:
            row[2] = str(gid)
            group_hits += 1
    if passwd_hits != 1 or group_hits != 1:
        raise Refusal("the image's /etc/passwd or /etc/group has no single postgres row")
    return (
        "\n".join(":".join(row) for row in passwd_rows) + "\n",
        "\n".join(":".join(row) for row in group_rows) + "\n",
    )


def _wait_ready(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = _docker("exec", CONTAINER, "sh", "-c", PROBE_SCRIPT, timeout=30)
        if result.returncode == 0:
            reply = result.stdout.strip()
            if reply != EXPECTED_LOCALE:
                raise Refusal(f"template1 collation and encoding are {reply[:80]!r}, expected {EXPECTED_LOCALE!r}")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal(f"{CONTAINER} did not accept TCP connections within {timeout:g} s")
        time.sleep(min(1.0, remaining))


def _create(root: Path, port: int, image: str, ready_timeout: float) -> None:
    uid, gid = os.getuid(), os.getgid()
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    _write_new(root / SENTINEL, json.dumps({"container": CONTAINER, "port": port, "image": image}) + "\n", 0o644)
    (root / "pgdata").mkdir(mode=0o700)
    os.chmod(root / "pgdata", 0o700)
    dump = _docker("run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-c", DUMP_SCRIPT)
    if dump.returncode:
        raise Refusal(f"reading the image's /etc/passwd failed ({_last_line(dump.stderr)})")
    passwd, group = _rewrite_accounts(dump.stdout, uid, gid)
    _write_new(root / "passwd", passwd, 0o644)
    _write_new(root / "group", group, 0o644)
    password = secrets.token_hex(24)
    _write_new(
        root / ENV_FILE,
        "POSTGRES_USER=postgres\n"
        f"POSTGRES_PASSWORD={password}\n"
        "POSTGRES_DB=postgres\n"
        f"PGDATA={CONTAINER_PGDATA}\n"
        "TIMESCALEDB_TELEMETRY=off\n",
        0o600,
    )
    settings: list[str] = []
    for setting in SETTINGS:
        settings += ["-c", setting]
    result = _docker(
        "run", "-d",
        "--name", CONTAINER,
        "--label", f"{LABEL_KEY}={root}",
        "--user", f"{uid}:{gid}",
        "--env-file", str(root / ENV_FILE),
        "-p", f"127.0.0.1:{port}:5432",
        "-v", f"{root / 'pgdata'}:{CONTAINER_PGDATA}:rw",
        "-v", f"{root / 'passwd'}:/etc/passwd:ro",
        "-v", f"{root / 'group'}:/etc/group:ro",
        "--memory", "8g", "--cpus", "8", "--shm-size", "1g",
        "--log-opt", "max-size=20m", "--log-opt", "max-file=2",
        image, "postgres", *settings,
    )  # fmt: skip
    if result.returncode:
        raise Refusal(f"docker run failed ({_last_line(result.stderr)})")
    _wait_ready(ready_timeout)


def _cleanup_after_failure(root: Path) -> None:
    for remove in (remove_container, remove_directory):
        try:
            remove(root)
        except (Refusal, OSError) as error:
            print(f"node27_integration_pg: cleanup: {_describe(error)}", file=sys.stderr)


def cmd_start(args: argparse.Namespace) -> int:
    if args.port == PRODUCTION_PORT or not 1024 <= args.port <= 65535:
        raise Refusal(f"port {args.port} refused: it must be in 1024..65535 and not {PRODUCTION_PORT}")
    root = _resolve_root(args.root)
    image = _production_image()
    if _assess(root) == "running":
        return _report_running(root, "already running")
    lock = _try_lock(root, fcntl.LOCK_EX)
    if lock is None:
        raise Refusal(
            "the instance lock is held (a run is alive, or a start or stop is at work); nothing changed", EXIT_BUSY
        )
    try:
        plan = _assess(root)
        if plan == "running":
            return _report_running(root, "already running")
        if plan in {"container", "both"}:
            remove_container(root)
        if plan in {"directory", "both"}:
            remove_directory(root)
        try:
            _create(root, args.port, image, args.ready_timeout)
        except BaseException:
            _cleanup_after_failure(root)
            raise
        return _report_running(root, "started")
    finally:
        os.close(lock)


def _report_running(root: Path, verb: str) -> int:
    sentinel = _read_sentinel(root)
    image = _short_image(sentinel["image"])
    print(f"{verb}: {CONTAINER} on 127.0.0.1:{sentinel['port']}, image {image}, directory {root}")
    return 0


# --- run --------------------------------------------------------------------------


def _read_password(root: Path) -> str:
    try:
        lines = (root / ENV_FILE).read_text().splitlines()
    except OSError:
        raise Refusal(f"{root / ENV_FILE} is missing or unreadable") from None
    for line in lines:
        key, _, value = line.partition("=")
        if key == "POSTGRES_PASSWORD" and re.fullmatch(r"[0-9a-f]+", value):
            return value
    raise Refusal(f"{root / ENV_FILE} has no usable POSTGRES_PASSWORD line")


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise Refusal("run needs a command after --", EXIT_USAGE)
    root = _resolve_root(args.root)
    if not _has_sentinel(root) or not (root / ENV_FILE).is_file():
        raise Refusal(f"no instance at {root}; run start first")
    lock = _try_lock(root, fcntl.LOCK_SH)
    if lock is None:
        raise Refusal("the instance lock is held exclusively (a start or stop is at work); command not run", EXIT_BUSY)
    container = _instance_container()
    if container is None or not container[0] or container[1] != str(root):
        raise Refusal(f"{CONTAINER} is not running for {root}; run start first")
    port = int(_read_sentinel(root)["port"])
    password = _read_password(root)
    # The command inherits the shared lock, so stop refuses for as long as it and its children live.
    os.set_inheritable(lock, True)
    env = dict(os.environ)
    env.pop("NHMS_ALLOW_DATABASE_URL_INTEGRATION", None)
    env["NHMS_RUN_INTEGRATION"] = "1"
    env["NHMS_INTEGRATION_DATABASE_URL"] = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        os.execvpe(command[0], command, env)
    except OSError as error:
        raise Refusal(f"cannot execute {command[0]!r}: {error.strerror}", 127) from None
    return 0


# --- status and stop -------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    container = _instance_container()
    ours = container is not None and container[1] == str(root)
    if ours and container[0] and _has_sentinel(root):
        sentinel = _read_sentinel(root)
        print(
            f"running: {CONTAINER} on 127.0.0.1:{sentinel['port']}, "
            f"image {_short_image(sentinel['image'])}, directory {root}"
        )
        return 0
    if ours or os.path.lexists(root):
        print(f"stopped: {CONTAINER} is not running for {root}")
        return EXIT_NOT_RUNNING
    foreign = f" ({CONTAINER} exists for another directory)" if container is not None else ""
    print(f"absent: no instance at {root}{foreign}")
    return EXIT_NOT_RUNNING


def cmd_stop(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    lock = _try_lock(root, fcntl.LOCK_EX)
    if lock is None:
        if not args.force:
            raise Refusal(
                "the instance lock is held (a run is alive, or a start is at work); nothing removed; --force overrides",
                EXIT_BUSY,
            )
        print("node27_integration_pg: lock held; going on without it (--force)", file=sys.stderr)
    try:
        failures: list[str] = []
        removed: list[str] = []
        try:
            if remove_container(root):
                removed.append(f"container {CONTAINER}")
        except Refusal as error:
            failures.append(str(error))
        try:
            if remove_directory(root):
                removed.append(f"directory {root}")
        except Refusal as error:
            failures.append(str(error))
    finally:
        if lock is not None:
            os.close(lock)
    for failure in failures:
        print(f"node27_integration_pg: {failure}", file=sys.stderr)
    if removed:
        print("removed: " + ", ".join(removed))
    elif not failures:
        print(f"nothing to stop: no {CONTAINER} and no directory at {root}")
    return EXIT_FAILED if failures else 0


# --- entry point --------------------------------------------------------------------


def _describe(error: BaseException) -> str:
    if isinstance(error, OSError):
        target = f" ({error.filename})" if error.filename else ""
        return f"{error.strerror or type(error).__name__}{target}"
    return str(error)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node27_integration_pg.py",
        description="Throwaway PostgreSQL instance for integration tests on node-27 (#3376).",
        epilog="Exit codes: 0 ok; 1 refused or failed; 2 usage; 3 lock held, or (status) not running.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    root_help = f"instance directory, absolute (default {DEFAULT_ROOT})"

    start = sub.add_parser("start", help="start the instance from the image of nhms-db")
    start.add_argument("--root", default=DEFAULT_ROOT, help=root_help)
    start.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"loopback port (default {DEFAULT_PORT})")
    start.add_argument(
        "--ready-timeout",
        type=float,
        default=READY_TIMEOUT_SEC,
        help=f"readiness deadline in seconds (default {READY_TIMEOUT_SEC:g}); exists for the tool's tests",
    )
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="remove the container and the instance directory")
    stop.add_argument("--root", default=DEFAULT_ROOT, help=root_help)
    stop.add_argument("--force", action="store_true", help="go on when the instance lock is held")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="print absent / stopped / running (exit 0 only when running)")
    status.add_argument("--root", default=DEFAULT_ROOT, help=root_help)
    status.set_defaults(func=cmd_status)

    run = sub.add_parser(
        "run",
        help="run a command with NHMS_RUN_INTEGRATION=1 and NHMS_INTEGRATION_DATABASE_URL of the instance",
        usage="%(prog)s [--root DIR] -- command ...",
    )
    run.add_argument("--root", default=DEFAULT_ROOT, help=root_help)
    run.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except Refusal as error:
        print(f"node27_integration_pg: {error}", file=sys.stderr)
        return error.code
    except OSError as error:
        print(f"node27_integration_pg: {_describe(error)}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
