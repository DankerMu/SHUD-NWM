"""#3376: ``scripts/node27_integration_pg.py`` driven as a subprocess against a fake ``docker``.

The fake is the only ``docker`` on ``PATH``: it logs each argument vector as one JSON
line, answers from a table keyed on the leading arguments, keeps the ``nhms-it-db``
container's existence in a small state file, and fails any call it does not know.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "node27_integration_pg.py"
IMAGE = "sha256:" + "ad39c4fbc5c4" + "0" * 48 + "640e"
SHORT_IMAGE = "ad39c4fbc5c4"
LABEL_KEY = "nhms.integration-pg"
SENTINEL = ".nhms-it-pg.json"
ENV_FILE = "postgres.env"
PRODUCTION_INSPECT = ["inspect", "nhms-db", "--format", "{{.Image}}"]
SETTINGS = [
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
]
IMAGE_PASSWD = "root:x:0:0:root:/root:/bin/bash\npostgres:x:1000:1000::/home/postgres:/bin/sh\n"
IMAGE_GROUP = "root:x:0:\npostgres:x:1000:\n"

FAKE_DOCKER = r"""
import json
import os
import sys
from pathlib import Path

base = Path(os.environ["FAKE_DOCKER_DIR"])
argv = sys.argv[1:]
with open(base / "calls.jsonl", "a") as log:
    log.write(json.dumps(argv) + "\n")
config = json.loads((base / "config.json").read_text())
state_path = base / "state.json"
state = json.loads(state_path.read_text())

keys = ("inspect nhms-db", "inspect nhms-it-db", "run --rm", "run -d", "exec", "rm")
key = next((k for k in keys if argv[: len(k.split())] == k.split()), None)
if key is None:
    sys.stderr.write("fake docker: unexpected call\n")
    sys.exit(99)


def label_of(args):
    for index, arg in enumerate(args[:-1]):
        if arg == "--label" and args[index + 1].startswith("nhms.integration-pg="):
            return args[index + 1].split("=", 1)[1]
    return None


def save():
    state_path.write_text(json.dumps(state))


if key == "run --rm" and config.get("listing_dir"):
    listing = sorted(os.listdir(config["listing_dir"]))
    with open(base / "listings.jsonl", "a") as handle:
        handle.write(json.dumps(listing) + "\n")

override = config["responses"].get(key)
if override is not None:
    if override.get("effect") == "create":
        state["container"] = {"running": False, "label": label_of(argv)}
        save()
    sys.stdout.write(override.get("stdout", ""))
    sys.stderr.write(override.get("stderr", ""))
    sys.exit(override.get("code", 0))

if key == "inspect nhms-db":
    print(config["image"])
elif key == "inspect nhms-it-db":
    container = state["container"]
    if container is None:
        sys.stderr.write("Error: No such object: nhms-it-db\n")
        sys.exit(1)
    labels = {"nhms.integration-pg": container["label"]} if container["label"] is not None else None
    print(("true" if container["running"] else "false") + " " + json.dumps(labels))
elif key == "run --rm":
    sys.stdout.write(config["passwd"] + "\n--GROUP--\n" + config["group"])
elif key == "run -d":
    state["container"] = {"running": True, "label": label_of(argv)}
    save()
    print("f" * 64)
elif key == "exec":
    print("C.UTF-8|UTF8")
elif key == "rm":
    state["container"] = None
    save()
"""

CHILD = r"""
import fcntl
import json
import os
import sys

out, lock = sys.argv[1], sys.argv[2]
descriptor = os.open(lock, os.O_RDWR)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    available = True
except BlockingIOError:
    available = False
with open(out, "w") as handle:
    json.dump({"env": dict(os.environ), "exclusive_available": available}, handle)
sys.exit(7)
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fake = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.fake.mkdir()
        self.bin.mkdir()
        (self.fake / "docker.py").write_text(FAKE_DOCKER)
        wrapper = self.bin / "docker"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.fake / "docker.py"}" "$@"\n')
        wrapper.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        self.root = home / "nhms-it-pg"
        self.label = str(home.resolve() / "nhms-it-pg")
        self.lock = home / "nhms-it-pg.lock"
        self.configure()
        self.set_container(None)

    def configure(self, responses: dict | None = None, listing_dir: Path | None = None) -> None:
        config = {
            "image": IMAGE,
            "passwd": IMAGE_PASSWD,
            "group": IMAGE_GROUP,
            "responses": responses or {},
            "listing_dir": str(listing_dir) if listing_dir else None,
        }
        (self.fake / "config.json").write_text(json.dumps(config))

    def set_container(self, container: dict | None) -> None:
        (self.fake / "state.json").write_text(json.dumps({"container": container}))

    def container(self) -> dict | None:
        return json.loads((self.fake / "state.json").read_text())["container"]

    def tool(
        self, *args: str, extra_env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {"PATH": str(self.bin), "FAKE_DOCKER_DIR": str(self.fake), "PYTHONUNBUFFERED": "1"}
        env.update(extra_env or {})
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env,
            cwd=cwd or self.tmp,
            capture_output=True,
            text=True,
            timeout=60,
            umask=0o022,
            check=False,
        )
        assert "Traceback" not in result.stderr, result.stderr
        return result

    def start(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.tool("start", "--root", str(self.root), *extra)

    def calls(self) -> list[list[str]]:
        path = self.fake / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def calls_text(self) -> str:
        path = self.fake / "calls.jsonl"
        return path.read_text() if path.exists() else ""

    def runs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:2] == ["run", "-d"]]

    def removals(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:1] == ["rm"]]

    def listings(self) -> list[list[str]]:
        path = self.fake / "listings.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def password(self) -> str:
        for line in (self.root / ENV_FILE).read_text().splitlines():
            if line.startswith("POSTGRES_PASSWORD="):
                return line.split("=", 1)[1]
        raise AssertionError("no password line")


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    for call in harness.calls():
        if "nhms-db" in call:
            assert call == PRODUCTION_INSPECT, call


@contextmanager
def held(path: Path, operation: int) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def snapshot(path: Path) -> dict[str, bytes | str] | None:
    if not os.path.lexists(path):
        return None
    tree: dict[str, bytes | str] = {}
    for current, directories, files in os.walk(path):
        for name in directories + files:
            full = Path(current) / name
            relative = str(full.relative_to(path))
            tree[relative] = "<dir>" if full.is_dir() else full.read_bytes()
    return tree


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg == flag]


# --- 4.1 start, happy path ---------------------------------------------------------


def test_start_creates_the_instance_from_the_production_image(harness: Harness) -> None:
    harness.configure(listing_dir=harness.root)
    result = harness.start()

    assert result.returncode == 0, result.stderr
    root = harness.label
    assert result.stdout.splitlines() == [
        f"started: nhms-it-db on 127.0.0.1:55442, image {SHORT_IMAGE}, directory {root}"
    ]
    [run] = harness.runs()
    uid, gid = os.getuid(), os.getgid()
    assert pairs(run, "--name") == ["nhms-it-db"]
    assert pairs(run, "--label") == [f"{LABEL_KEY}={root}"]
    assert pairs(run, "--user") == [f"{uid}:{gid}"]
    assert pairs(run, "-p") == ["127.0.0.1:55442:5432"]
    assert pairs(run, "--env-file") == [f"{root}/{ENV_FILE}"]
    assert pairs(run, "-v") == [
        f"{root}/pgdata:/home/postgres/pgdata/data:rw",
        f"{root}/passwd:/etc/passwd:ro",
        f"{root}/group:/etc/group:ro",
    ]
    assert pairs(run, "--memory") == ["8g"]
    assert pairs(run, "--cpus") == ["8"]
    assert pairs(run, "--shm-size") == ["1g"]
    assert pairs(run, "--log-opt") == ["max-size=20m", "max-file=2"]
    assert run[run.index(IMAGE) + 1] == "postgres"
    assert pairs(run, "-c") == SETTINGS
    assert not any(arg.startswith("--restart") for arg in run)
    assert not any(arg in {"-e", "--env"} or arg.startswith("--env=") for arg in run)

    sentinel = json.loads((harness.root / SENTINEL).read_text())
    assert sentinel == {"container": "nhms-it-db", "port": 55442, "image": IMAGE}
    assert mode(harness.root) == 0o700
    assert mode(harness.root / "pgdata") == 0o700
    assert mode(harness.root / ENV_FILE) == 0o600
    env_lines = (harness.root / ENV_FILE).read_text().splitlines()
    assert env_lines[0] == "POSTGRES_USER=postgres"
    assert env_lines[2:] == ["POSTGRES_DB=postgres", "PGDATA=/home/postgres/pgdata/data", "TIMESCALEDB_TELEMETRY=off"]
    password = harness.password()
    assert len(password) == 48 and int(password, 16) >= 0

    assert f"postgres:x:{uid}:{gid}::/home/postgres:/bin/sh" in (harness.root / "passwd").read_text().splitlines()
    assert "root:x:0:0:root:/root:/bin/bash" in (harness.root / "passwd").read_text().splitlines()
    assert f"postgres:x:{gid}:" in (harness.root / "group").read_text().splitlines()

    [dump] = [call for call in harness.calls() if call[:2] == ["run", "--rm"]]
    assert dump[:7] == ["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", IMAGE]
    probes = [call for call in harness.calls() if call[:1] == ["exec"]]
    assert probes and probes[0][:4] == ["exec", "nhms-it-db", "sh", "-c"]
    assert "-h 127.0.0.1" in probes[0][4]
    assert harness.listings() == [[SENTINEL, "pgdata"]]


# --- 4.2 secrecy ---------------------------------------------------------------------


def test_password_never_reaches_an_argument_list_or_the_output(harness: Harness) -> None:
    started = harness.start()
    assert started.returncode == 0, started.stderr
    password = harness.password()
    outputs = [started.stdout, started.stderr]

    ran, _ = _run_child(harness)
    assert ran.returncode == 7
    status = harness.tool("status", "--root", str(harness.root))
    assert status.returncode == 0
    stopped = harness.tool("stop", "--root", str(harness.root))
    assert stopped.returncode == 0, stopped.stderr
    outputs += [ran.stdout, ran.stderr, status.stdout, status.stderr, stopped.stdout, stopped.stderr]

    assert password not in harness.calls_text()
    assert all(password not in text for text in outputs)
    assert all("postgresql://" not in text for text in outputs)


# --- 4.3 refusals of start -------------------------------------------------------------


def _foreign_directory(harness: Harness) -> list[str]:
    harness.root.mkdir()
    (harness.root / "notes.txt").write_text("not the tool's")
    (harness.root / "sub").mkdir()
    (harness.root / "sub" / "data.bin").write_bytes(b"\x00\x01")
    return ["start", "--root", str(harness.root)]


def _production_inspect_fails(harness: Harness) -> list[str]:
    harness.configure({"inspect nhms-db": {"code": 1, "stderr": "Error: No such object: nhms-db\n"}})
    return ["start", "--root", str(harness.root)]


def _foreign_label(harness: Harness) -> list[str]:
    harness.set_container({"running": True, "label": str(harness.tmp / "elsewhere")})
    return ["start", "--root", str(harness.root)]


def _running_without_sentinel(harness: Harness) -> list[str]:
    harness.set_container({"running": True, "label": harness.label})
    return ["start", "--root", str(harness.root)]


REFUSALS = {
    "production-port": lambda h: ["start", "--root", str(h.root), "--port", "55432"],
    "port-out-of-range": lambda h: ["start", "--root", str(h.root), "--port", "80"],
    "relative-root": lambda h: ["start", "--root", "home/nhms-it-pg"],
    "foreign-directory": _foreign_directory,
    "production-inspect-fails": _production_inspect_fails,
    "foreign-label": _foreign_label,
    "running-without-sentinel": _running_without_sentinel,
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_start_refusals_change_nothing(harness: Harness, case: str) -> None:
    args = REFUSALS[case](harness)
    before = snapshot(harness.root)
    container = harness.container()

    result = harness.tool(*args)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stderr.startswith("node27_integration_pg: ")
    assert harness.runs() == []
    assert harness.removals() == []
    assert snapshot(harness.root) == before
    assert harness.container() == container


def test_start_refuses_while_the_lock_is_held(harness: Harness) -> None:
    with held(harness.lock, fcntl.LOCK_SH):
        result = harness.start()

    assert result.returncode == 3, result.stderr
    assert "lock" in result.stderr
    assert harness.runs() == []
    assert snapshot(harness.root) is None


# --- 4.4 idempotence and stale state -------------------------------------------------------


def test_start_twice_is_idempotent(harness: Harness) -> None:
    assert harness.start().returncode == 0
    with held(harness.lock, fcntl.LOCK_SH):
        again = harness.start()

    assert again.returncode == 0, again.stderr
    assert again.stdout.startswith("already running: nhms-it-db on 127.0.0.1:55442")
    assert len(harness.runs()) == 1


def test_start_after_reboot_replaces_the_stale_instance(harness: Harness) -> None:
    assert harness.start().returncode == 0
    old_password = harness.password()
    (harness.root / "pgdata" / "PG_VERSION").write_text("15\n")
    harness.set_container({"running": False, "label": harness.label})

    result = harness.start()

    assert result.returncode == 0, result.stderr
    calls = harness.calls()
    rm_at = calls.index(["rm", "-f", "nhms-it-db"])
    assert any(call[:2] == ["run", "-d"] for call in calls[rm_at + 1 :])
    assert not (harness.root / "pgdata" / "PG_VERSION").exists()
    assert harness.password() != old_password
    assert harness.container() == {"running": True, "label": harness.label}


# --- 4.5 failure cleanup ------------------------------------------------------------------------


FAILURES = {
    "passwd-dump-fails": ({"run --rm": {"code": 125, "stderr": "dump failed\n"}}, (), False),
    "docker-run-fails": (
        {
            "run -d": {
                "code": 125,
                "stderr": "Bind for 127.0.0.1:55442 failed: port is already allocated\n",
                "effect": "create",
            }
        },
        (),
        True,
    ),
    "never-ready": ({"exec": {"code": 2, "stderr": "psql: connection refused\n"}}, ("--ready-timeout", "0.3"), True),
    "wrong-collation": ({"exec": {"stdout": "en_US.utf8|UTF8\n"}}, (), True),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_start_failure_removes_what_it_created(harness: Harness, case: str) -> None:
    responses, extra, container_created = FAILURES[case]
    harness.configure(responses, listing_dir=harness.root)

    result = harness.start(*extra)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stderr.strip()
    assert not os.path.lexists(harness.root)
    assert (harness.removals() == [["rm", "-f", "nhms-it-db"]]) is container_created
    assert harness.container() is None
    assert harness.listings() == [[SENTINEL, "pgdata"]]
    if case == "docker-run-fails":
        assert "port is already allocated" in result.stderr


# --- 4.6 stop ---------------------------------------------------------------------------------------


def test_stop_refuses_while_a_run_holds_the_lock_unless_forced(harness: Harness) -> None:
    assert harness.start().returncode == 0
    before = snapshot(harness.root)

    with held(harness.lock, fcntl.LOCK_SH):
        refused = harness.tool("stop", "--root", str(harness.root))
        assert refused.returncode == 3
        assert harness.removals() == []
        assert snapshot(harness.root) == before

        forced = harness.tool("stop", "--root", str(harness.root), "--force")

    assert forced.returncode == 0, forced.stderr
    assert harness.removals() == [["rm", "-f", "nhms-it-db"]]
    assert not os.path.lexists(harness.root)


def test_stop_with_the_lock_free_removes_container_and_directory(harness: Harness) -> None:
    assert harness.start().returncode == 0

    result = harness.tool("stop", "--root", str(harness.root))

    assert result.returncode == 0, result.stderr
    assert harness.removals() == [["rm", "-f", "nhms-it-db"]]
    assert harness.container() is None
    assert not os.path.lexists(harness.root)


def test_stop_leaves_a_directory_without_the_sentinel(harness: Harness) -> None:
    _foreign_directory(harness)
    before = snapshot(harness.root)

    result = harness.tool("stop", "--root", str(harness.root))

    assert result.returncode == 1
    assert SENTINEL in result.stderr
    assert snapshot(harness.root) == before


def test_stop_refuses_a_container_labelled_for_another_directory(harness: Harness) -> None:
    harness.set_container({"running": True, "label": str(harness.tmp / "elsewhere")})

    result = harness.tool("stop", "--root", str(harness.root))

    assert result.returncode == 1
    assert harness.removals() == []
    assert harness.container() == {"running": True, "label": str(harness.tmp / "elsewhere")}


def test_stop_does_not_follow_a_symbolic_link_root(harness: Harness) -> None:
    target = harness.tmp / "target"
    target.mkdir()
    (target / SENTINEL).write_text("{}")
    (target / "data.txt").write_text("keep")
    harness.root.symlink_to(target)
    before = snapshot(target)

    result = harness.tool("stop", "--root", str(harness.root))

    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert snapshot(target) == before
    assert harness.root.is_symlink()


def test_stop_does_not_follow_links_inside_the_root(harness: Harness) -> None:
    assert harness.start().returncode == 0
    outside = harness.tmp / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (harness.root / "pgdata" / "escape").symlink_to(outside)

    result = harness.tool("stop", "--root", str(harness.root))

    assert result.returncode == 0, result.stderr
    assert not os.path.lexists(harness.root)
    assert (outside / "keep.txt").read_text() == "keep"


def test_stop_with_nothing_there_says_so(harness: Harness) -> None:
    result = harness.tool("stop", "--root", str(harness.root))

    assert result.returncode == 0
    assert result.stdout.startswith("nothing to stop")
    assert harness.removals() == []


# --- 4.7 run ----------------------------------------------------------------------------------------


def _run_child(harness: Harness, extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, Path]:
    child = harness.tmp / "child.py"
    child.write_text(CHILD)
    out = harness.tmp / "out.json"
    result = harness.tool(
        "run", "--root", str(harness.root), "--", sys.executable, str(child), str(out), str(harness.lock),
        extra_env=extra_env,
    )  # fmt: skip
    return result, out


def test_run_hands_the_url_to_the_command_and_holds_the_lock(harness: Harness) -> None:
    assert harness.start().returncode == 0

    result, out = _run_child(
        harness, {"NHMS_ALLOW_DATABASE_URL_INTEGRATION": "1", "NHMS_RUN_INTEGRATION": "0", "PYTHONUNBUFFERED": "1"}
    )

    assert result.returncode == 7
    assert result.stdout == ""
    assert result.stderr == ""
    seen = json.loads(out.read_text())
    env = seen["env"]
    assert env["NHMS_RUN_INTEGRATION"] == "1"
    assert "NHMS_ALLOW_DATABASE_URL_INTEGRATION" not in sorted(env)
    url = urlsplit(env["NHMS_INTEGRATION_DATABASE_URL"])
    assert (url.scheme, url.hostname, url.port, url.username, url.path) == (
        "postgresql",
        "127.0.0.1",
        55442,
        "postgres",
        "/postgres",
    )
    assert url.password == harness.password()
    assert seen["exclusive_available"] is False


@pytest.mark.parametrize("instance", ["absent", "container-stopped"])
def test_run_without_a_running_instance_does_not_execute(harness: Harness, instance: str) -> None:
    if instance == "container-stopped":
        assert harness.start().returncode == 0
        harness.set_container({"running": False, "label": harness.label})

    result, out = _run_child(harness)

    assert result.returncode == 1
    assert "start" in result.stderr
    assert not out.exists()


def test_run_refuses_while_start_or_stop_holds_the_lock(harness: Harness) -> None:
    assert harness.start().returncode == 0

    with held(harness.lock, fcntl.LOCK_EX):
        result, out = _run_child(harness)

    assert result.returncode == 3
    assert not out.exists()


# --- 4.8 status ---------------------------------------------------------------------------------------


def test_status_reports_running_stopped_and_absent(harness: Harness) -> None:
    absent = harness.tool("status", "--root", str(harness.root))
    assert (absent.returncode, absent.stdout.split(":")[0]) == (3, "absent")

    assert harness.start().returncode == 0
    password = harness.password()
    running = harness.tool("status", "--root", str(harness.root))
    assert running.returncode == 0
    assert running.stdout.splitlines() == [
        f"running: nhms-it-db on 127.0.0.1:55442, image {SHORT_IMAGE}, directory {harness.label}"
    ]

    harness.set_container({"running": False, "label": harness.label})
    stopped = harness.tool("status", "--root", str(harness.root))
    assert (stopped.returncode, stopped.stdout.split(":")[0]) == (3, "stopped")

    for result in (absent, running, stopped):
        assert password not in result.stdout + result.stderr
        assert "postgresql://" not in result.stdout + result.stderr
