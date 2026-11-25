"""#3231: ``scripts/ops/display-c1-receipt.sh`` is the node-27 display receipt (C1).

Contract: requirement "The display deployment receipt observes the running unit"
of capability ``runtime-service-role-boundary``
(``openspec/changes/display-c1-receipt-on-systemd``).

The script runs as a copy inside a temporary git checkout (one real commit, so
the reflog exists; its time is pinned through ``GIT_COMMITTER_DATE``). The host
commands it reads -- ``systemctl``, ``ps``, ``ss``, ``curl`` -- are fakes on
``PATH`` that log every call outside the checkout; ``date +%s`` is faked too, so
every time the script compares is an integer the test chose. The fakes, their
logs, the two storage roots, ``HOME`` and ``TMPDIR`` all live outside the
checkout, and every run asserts that the checkout (``.git/index`` included), the
storage roots, ``HOME`` and ``TMPDIR`` are byte-identical afterwards.

The pattern follows the start-wrapper harness in ``tests/test_start_display_api.py``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.git_fixture_env import run_fixture_git

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_RELATIVE = "scripts/ops/display-c1-receipt.sh"
UNIT = "nhms-display-api.service"

CLAIMS = (
    "unit_active",
    "process_of_checkout",
    "checkout_clean",
    "process_loaded_head",
    "listener_of_unit",
    "health",
    "runtime_role",
    "slurm_routes_absent",
    "no_slurm_client",
    "storage_not_writable",
)
HEADER_KEYS = (
    "repo_root",
    "checkout_head",
    "main_pid",
    "port",
    "process_started",
    "head_moved",
    "env_modified",
    "object_store_root",
    "published_artifact_root",
)
# The requirement's set for "An inactive unit fails every claim that needs the process".
PROCESS_CLAIMS = frozenset(
    {
        "unit_active",
        "process_of_checkout",
        "process_loaded_head",
        "listener_of_unit",
        "health",
        "runtime_role",
        "slurm_routes_absent",
    }
)
# Without a port nothing can be listened to or requested.
PORT_CLAIMS = frozenset({"listener_of_unit", "health", "runtime_role", "slurm_routes_absent"})
SLURM_COMMANDS = ("sbatch", "scancel", "squeue", "scontrol", "sacct", "srun", "sinfo")
SLURM_PATHS = ("/etc/slurm", "/etc/slurm-llnl", "/run/munge/munge.socket.2")

# Measured on node-27 (2026-11-25): the main process and its two workers hold the port.
MAIN_PID = "1028018"
WORKER_PIDS = ("1028026", "1028027")
PORT = "8080"
SS_LINE = (
    "LISTEN 0      2048   127.0.0.1:8080 0.0.0.0:* "
    'users:(("python",pid=1028027,fd=4),("python",pid=1028026,fd=4),("python",pid=1028018,fd=4))'
)
RUNTIME_CONFIG_BODY = (
    '{"request_id":"r-1","status":"ok","data":{"service_role":"display_readonly",'
    '"control_mutations_enabled":false,"slurm_routes_enabled":false,'
    '"queue_depth_mode":"display_readonly_unavailable","display_readonly":true}}'
)
SLURM_NOT_FOUND_BODY = '{"detail":"Not found"}'

# The fixed clock: the checkout's HEAD moved at HEAD_MOVED, display.env was last
# changed at ENV_MODIFIED, the process started at PROCESS_STARTED, the receipt runs at NOW.
NOW = 1_795_612_300
HEAD_MOVED = NOW - 10_000
ENV_MODIFIED = NOW - 20_000
PROCESS_STARTED = NOW - 5_000

SECRET = "s3cr3t-c1-receipt-token"

BASH = "/bin/bash" if Path("/bin/bash").is_file() else (shutil.which("bash") or "bash")
SEPARATOR = "\x1f"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _log_line(name: str) -> str:
    return f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >>\"$FAKE_LOG_DIR/{name}.log\"\n"


FAKE_SYSTEMCTL = (
    "#!/bin/sh\n"
    + _log_line("systemctl")
    + r"""for arg do
  case "$arg" in
    is-active)
      printf '%s\n' "$FAKE_UNIT_STATE"
      [ "$FAKE_UNIT_STATE" = active ]
      exit $?
      ;;
    show)
      printf '%s\n' "$FAKE_MAIN_PID"
      exit 0
      ;;
  esac
done
exit 1
"""
)

FAKE_PS = (
    "#!/bin/sh\n"
    + _log_line("ps")
    + r"""field=
pid=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) field=$2; shift 2 ;;
    -p) pid=$2; shift 2 ;;
    *) shift ;;
  esac
done
case "$field" in
  args=)
    [ "$pid" = "$FAKE_MAIN_PID" ] || exit 1
    printf '%s\n' "$FAKE_MAIN_ARGS"
    ;;
  etimes=)
    [ "$pid" = "$FAKE_MAIN_PID" ] || exit 1
    printf '  %s\n' "$FAKE_ETIMES"
    ;;
  ppid=)
    for entry in $FAKE_PPIDS; do
      case "$entry" in
        "$pid":*) printf '  %s\n' "${entry#*:}"; exit 0 ;;
      esac
    done
    exit 1
    ;;
esac
exit 0
"""
)

FAKE_SS = (
    "#!/bin/sh\n"
    + _log_line("ss")
    + r"""[ -n "$FAKE_SS_OUTPUT" ] && printf '%s\n' "$FAKE_SS_OUTPUT"
exit "${FAKE_SS_RC:-0}"
"""
)

FAKE_CURL = (
    "#!/bin/sh\n"
    + _log_line("curl")
    + r"""fmt=
url=
prev=
for arg do
  [ "$prev" = "-w" ] && fmt=$arg
  prev=$arg
  case "$arg" in http://*) url=$arg ;; esac
done
case "$url" in
  */health) [ "${url%/api/v1/slurm/health}" = "$url" ] && which=HEALTH || which=SLURM ;;
  */api/v1/runtime/config) which=CONFIG ;;
  *) exit 6 ;;
esac
eval "code=\$FAKE_${which}_CODE"
eval "body=\$FAKE_${which}_BODY"
[ "$code" = none ] && exit 7
printf '%s' "$body"
out=$(printf '%s' "$fmt" | sed "s/%{http_code}/$code/")
printf "$out"
exit 0
"""
)


def _fake_date(real_date: str) -> str:
    return f"""#!/bin/sh
if [ "$#" = 1 ] && [ "$1" = "+%s" ]; then
  printf '%s\\n' "$FAKE_NOW"
  exit 0
fi
exec {real_date} "$@"
"""


def _host_slurm_client() -> list[str]:
    present = [name for name in SLURM_COMMANDS if shutil.which(name)]
    present.extend(path for path in SLURM_PATHS if os.path.lexists(path))
    return present


@dataclass
class ReceiptRun:
    completed: subprocess.CompletedProcess[str]
    header: dict[str, str]
    claims: dict[str, str]
    verdict: str
    calls: dict[str, list[list[str]]]

    @property
    def failing(self) -> set[str]:
        return {claim for claim, value in self.claims.items() if value.startswith("FAIL")}


@dataclass
class Harness:
    tmp: Path
    repo: Path
    fake_bin: Path
    extra_bin: Path
    log_dir: Path
    home: Path
    tmpdir: Path
    object_store: Path
    published: Path
    knobs: dict[str, str] = field(default_factory=dict)

    @property
    def env_file(self) -> Path:
        return self.repo / "infra/env/display.env"

    def main_args(self, root: Path | None = None) -> str:
        return (
            f"{root or self.repo}/.venv/bin/python -m uvicorn apps.api.main:app "
            f"--host 127.0.0.1 --port {PORT} --workers 2"
        )

    def write_env(self, lines: list[str], *, mtime: int = ENV_MODIFIED) -> None:
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(self.env_file, (mtime, mtime))

    def default_env_lines(self) -> list[str]:
        return [
            f"DATABASE_URL=postgresql://nhms_display_ro:{SECRET}@db.internal.example:5432/nhms",
            "NHMS_SERVICE_ROLE=display_readonly",
            f"OBJECT_STORE_ROOT={self.object_store}",
            f"NHMS_PUBLISHED_ARTIFACT_ROOT={self.published}",
        ]

    def set_started(self, started: int) -> None:
        self.knobs["FAKE_ETIMES"] = str(NOW - started)

    def git(self, *args: str, date: int = HEAD_MOVED) -> None:
        stamp = f"{date} +0000"
        run_fixture_git(
            self.repo,
            *args,
            env_overlay={
                "GIT_AUTHOR_NAME": "fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                "GIT_AUTHOR_DATE": stamp,
                "GIT_COMMITTER_DATE": stamp,
            },
            capture_output=True,
        )

    def head(self) -> str:
        return run_fixture_git(self.repo, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()

    def snapshot(self) -> dict[str, tuple[str, int, str]]:
        state: dict[str, tuple[str, int, str]] = {}
        for base in (self.repo, self.object_store, self.published, self.home, self.tmpdir):
            for path in [base, *sorted(base.rglob("*"))]:
                info = path.lstat()
                key = str(path)
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISLNK(info.st_mode):
                    state[key] = ("link", mode, os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    state[key] = ("dir", mode, "")
                else:
                    state[key] = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
        return state

    def run(
        self,
        *,
        script: Path | None = None,
        args: tuple[str, ...] = (),
        env_mode: int | None = None,
    ) -> ReceiptRun:
        before = self.snapshot()
        if env_mode is not None:
            self.env_file.chmod(env_mode)
        try:
            env = {
                "PATH": os.pathsep.join([str(self.extra_bin), str(self.fake_bin), os.environ.get("PATH", "")]),
                "HOME": str(self.home),
                "TMPDIR": str(self.tmpdir),
                "LC_ALL": "C",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "FAKE_LOG_DIR": str(self.log_dir),
                **self.knobs,
            }
            completed = subprocess.run(
                [BASH, str(script or self.repo / SCRIPT_RELATIVE), *args],
                cwd=self.tmp,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            if env_mode is not None:
                self.env_file.chmod(0o644)
        after = self.snapshot()
        assert after == before, "the receipt changed the checkout, a storage root, HOME or TMPDIR"
        run = _parse(completed, self._calls())
        _assert_read_only_calls(run)
        return run

    def _calls(self) -> dict[str, list[list[str]]]:
        calls: dict[str, list[list[str]]] = {}
        for name in ("systemctl", "ps", "ss", "curl"):
            log = self.log_dir / f"{name}.log"
            calls[name] = (
                [line.split(SEPARATOR)[:-1] for line in log.read_text(encoding="utf-8").splitlines()]
                if log.exists()
                else []
            )
        return calls


def _parse(completed: subprocess.CompletedProcess[str], calls: dict[str, list[list[str]]]) -> ReceiptRun:
    lines = completed.stdout.splitlines()
    assert len(lines) == len(HEADER_KEYS) + len(CLAIMS) + 1, completed.stdout + completed.stderr
    header: dict[str, str] = {}
    for key, line in zip(HEADER_KEYS, lines[: len(HEADER_KEYS)], strict=True):
        name, separator, value = line.partition("=")
        assert (name, separator) == (key, "="), line
        header[key] = value
    claims: dict[str, str] = {}
    for claim, line in zip(CLAIMS, lines[len(HEADER_KEYS) : -1], strict=True):
        name, separator, value = line.partition("=")
        assert (name, separator) == (claim, "="), line
        assert value == "ok" or re.fullmatch(r"FAIL \S.*", value), line
        claims[claim] = value
    return ReceiptRun(completed=completed, header=header, claims=claims, verdict=lines[-1], calls=calls)


def _assert_read_only_calls(run: ReceiptRun) -> None:
    """The requirement's "changes nothing": only `show` / `is-active`, only GET."""
    for call in run.calls["systemctl"]:
        assert call in (
            ["--user", "is-active", UNIT],
            ["--user", "show", UNIT, "-p", "MainPID", "--value"],
        ), call
    for call in run.calls["curl"]:
        assert "--max-time" in call, call
        forbidden = {"-X", "--request", "-d", "-F", "-T", "-I", "-o", "--output", "--head", "--form", "--upload-file"}
        assert not forbidden.intersection(call), call
        assert not any(arg.startswith("--data") for arg in call), call
        urls = [arg for arg in call if arg.startswith("http")]
        assert len(urls) == 1 and urls[0].startswith("http://127.0.0.1:"), call
    for call in run.calls["ss"]:
        assert call[0] == "-Hltnp" and len(call) == 2, call
    for call in run.calls["ps"]:
        assert len(call) == 4 and call[0] == "-o" and call[2] == "-p", call
        assert call[1] in ("args=", "etimes=", "ppid="), call


def _assert_claims(run: ReceiptRun, failing: set[str] | frozenset[str]) -> None:
    detail = run.completed.stdout + run.completed.stderr
    assert set(run.claims) == set(CLAIMS)
    assert run.failing == set(failing), detail
    if failing:
        assert run.verdict == "C1=FAIL", detail
        assert run.completed.returncode != 0, detail
    else:
        assert run.verdict == "C1=PASS", detail
        assert run.completed.returncode == 0, detail


@pytest.fixture(scope="module")
def shared_fake_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fakes are stateless (each run logs to its own directory), so one copy per module."""
    fake_bin = tmp_path_factory.mktemp("c1-fake-bin")
    _write_executable(fake_bin / "systemctl", FAKE_SYSTEMCTL)
    _write_executable(fake_bin / "ps", FAKE_PS)
    _write_executable(fake_bin / "ss", FAKE_SS)
    _write_executable(fake_bin / "curl", FAKE_CURL)
    real_date = shutil.which("date")
    assert real_date is not None
    _write_executable(fake_bin / "date", _fake_date(real_date))
    return fake_bin


@pytest.fixture
def harness(tmp_path: Path, shared_fake_bin: Path) -> Iterator[Harness]:
    present = _host_slurm_client()
    if present:
        pytest.skip(f"this host has a Slurm client ({', '.join(present)}): no_slurm_client cannot be ok here")
    tmp = tmp_path
    repo = tmp / "repo"
    h = Harness(
        tmp=tmp,
        repo=repo,
        fake_bin=shared_fake_bin,
        extra_bin=tmp / "extra-bin",
        log_dir=tmp / "calls",
        home=tmp / "home",
        tmpdir=tmp / "tmpdir",
        object_store=tmp / "object-store",
        published=tmp / "published",
    )
    for directory in (h.extra_bin, h.log_dir, h.home, h.tmpdir, h.object_store, h.published, repo / "infra/env"):
        directory.mkdir(parents=True)
    (repo / "scripts/ops").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / SCRIPT_RELATIVE, repo / SCRIPT_RELATIVE)
    (repo / SCRIPT_RELATIVE).chmod(0o755)
    (repo / "README.md").write_text("fixture checkout\n", encoding="utf-8")
    (repo / ".gitignore").write_text("infra/env/display.env\n", encoding="utf-8")
    h.git("init", "-q")
    h.git("add", "-A")
    h.git("commit", "-q", "-m", "fixture")
    # A stat change on a tracked file leaves the index stale: a `git status` that
    # may take optional locks would rewrite .git/index, which the snapshot catches.
    os.utime(repo / "README.md", (HEAD_MOVED + 1, HEAD_MOVED + 1))
    h.write_env(h.default_env_lines())

    for root in (h.object_store, h.published):
        root.chmod(0o555)
    if any(os.access(root, os.W_OK) for root in (h.object_store, h.published)):
        for root in (h.object_store, h.published):
            root.chmod(0o755)
        pytest.skip("platform ACL/root behavior still reports a 0555 directory writable")

    h.knobs.update(
        {
            "FAKE_NOW": str(NOW),
            "FAKE_UNIT_STATE": "active",
            "FAKE_MAIN_PID": MAIN_PID,
            "FAKE_MAIN_ARGS": h.main_args(),
            "FAKE_PPIDS": " ".join(f"{pid}:{MAIN_PID}" for pid in WORKER_PIDS),
            "FAKE_SS_OUTPUT": SS_LINE,
            "FAKE_SS_RC": "0",
            "FAKE_HEALTH_CODE": "200",
            "FAKE_HEALTH_BODY": '{"status":"ok","service":"nhms-api","version":"0.1.0"}',
            "FAKE_CONFIG_CODE": "200",
            "FAKE_CONFIG_BODY": RUNTIME_CONFIG_BODY,
            "FAKE_SLURM_CODE": "404",
            "FAKE_SLURM_BODY": SLURM_NOT_FOUND_BODY,
        }
    )
    h.set_started(PROCESS_STARTED)
    try:
        yield h
    finally:
        for root in (h.object_store, h.published):
            if root.exists():
                root.chmod(0o755)


def _expected_urls() -> list[str]:
    return [
        f"http://127.0.0.1:{PORT}/health",
        f"http://127.0.0.1:{PORT}/api/v1/runtime/config",
        f"http://127.0.0.1:{PORT}/api/v1/slurm/health",
    ]


def _urls(run: ReceiptRun) -> list[str]:
    return [arg for call in run.calls["curl"] for arg in call if arg.startswith("http")]


# -- Scenario: A healthy display unit passes ----------------------------------------


def test_a_healthy_display_unit_passes(harness: Harness) -> None:
    run = harness.run()

    _assert_claims(run, set())
    assert run.header == {
        "repo_root": str(harness.repo),
        "checkout_head": harness.head(),
        "main_pid": MAIN_PID,
        "port": PORT,
        "process_started": str(PROCESS_STARTED),
        "head_moved": str(HEAD_MOVED),
        "env_modified": str(ENV_MODIFIED),
        "object_store_root": str(harness.object_store),
        "published_artifact_root": str(harness.published),
    }
    assert set(run.claims.values()) == {"ok"}
    assert _urls(run) == _expected_urls()
    assert run.calls["ss"] == [["-Hltnp", f"sport = :{PORT}"]]
    assert sorted(call[1] for call in run.calls["systemctl"]) == ["is-active", "show"]
    assert run.completed.stderr == ""


def test_the_root_given_as_first_argument_replaces_the_scripts_own_checkout(harness: Harness) -> None:
    """D5: run from another checkout (here the real one) against the fixture checkout."""
    run = harness.run(script=REPO_ROOT / SCRIPT_RELATIVE, args=(str(harness.repo),))

    _assert_claims(run, set())
    assert run.header["repo_root"] == str(harness.repo)
    assert run.header["checkout_head"] == harness.head()


# -- Scenarios: a process older than the checkout or its env file ---------------------


@pytest.mark.parametrize(
    ("started", "passes"),
    [
        pytest.param(HEAD_MOVED - 1, False, id="head-moved-after-start"),
        pytest.param(HEAD_MOVED, False, id="head-moved-in-the-same-second"),
        # `now - etimes` can be one second late: start 100.9, HEAD move 100.95, run at
        # 201.1 derives 101. One second after the move is therefore not proof.
        pytest.param(HEAD_MOVED + 1, False, id="started-one-second-after-head-moved"),
        pytest.param(HEAD_MOVED + 2, True, id="started-two-seconds-after-head-moved"),
    ],
)
def test_a_process_older_than_the_checkout_does_not_pass(harness: Harness, started: int, passes: bool) -> None:
    harness.set_started(started)

    run = harness.run()

    _assert_claims(run, set() if passes else {"process_loaded_head"})
    assert run.header["process_started"] == str(started)


def test_a_head_move_after_the_process_started_does_not_pass(harness: Harness) -> None:
    harness.set_started(HEAD_MOVED + 100)
    (harness.repo / "CHANGELOG").write_text("later\n", encoding="utf-8")
    harness.git("add", "CHANGELOG")
    harness.git("commit", "-q", "-m", "later", date=HEAD_MOVED + 200)

    run = harness.run()

    _assert_claims(run, {"process_loaded_head"})
    assert run.header["head_moved"] == str(HEAD_MOVED + 200)
    assert run.header["checkout_head"] == harness.head()


@pytest.mark.parametrize(
    ("env_modified", "passes"),
    [
        pytest.param(PROCESS_STARTED + 1, False, id="env-modified-after-start"),
        pytest.param(PROCESS_STARTED, False, id="env-modified-in-the-same-second"),
        pytest.param(PROCESS_STARTED - 1, False, id="started-one-second-after-env-modified"),
        pytest.param(PROCESS_STARTED - 2, True, id="started-two-seconds-after-env-modified"),
    ],
)
def test_a_process_older_than_its_env_file_does_not_pass(
    harness: Harness, env_modified: int, passes: bool
) -> None:
    harness.write_env(harness.default_env_lines(), mtime=env_modified)

    run = harness.run()

    _assert_claims(run, set() if passes else {"process_loaded_head"})
    assert run.header["env_modified"] == str(env_modified)


def test_an_empty_reflog_fails_process_loaded_head(harness: Harness) -> None:
    shutil.rmtree(harness.repo / ".git/logs")

    run = harness.run()

    _assert_claims(run, {"process_loaded_head"})
    assert run.header["head_moved"] == ""
    assert "reflog" in run.claims["process_loaded_head"]


def test_an_unreadable_process_start_fails_process_loaded_head(harness: Harness) -> None:
    harness.knobs["FAKE_ETIMES"] = ""

    run = harness.run()

    _assert_claims(run, {"process_loaded_head"})
    assert run.header["process_started"] == ""


def test_a_missing_env_file_fails_its_time_and_its_storage_roots(harness: Harness) -> None:
    """The one input two claims read: its time (process_loaded_head) and its keys."""
    harness.env_file.unlink()

    run = harness.run()

    _assert_claims(run, {"process_loaded_head", "storage_not_writable"})
    assert run.header["env_modified"] == ""
    assert run.header["object_store_root"] == ""


# -- Scenario: A modified checkout does not pass ----------------------------------------


@pytest.mark.parametrize("change", ["modified", "staged", "deleted"])
def test_a_modified_checkout_does_not_pass(harness: Harness, change: str) -> None:
    readme = harness.repo / "README.md"
    if change == "modified":
        readme.write_text("edited after the commit\n", encoding="utf-8")
    elif change == "staged":
        (harness.repo / "NEW.md").write_text("staged\n", encoding="utf-8")
        harness.git("add", "NEW.md")
    else:
        readme.unlink()

    run = harness.run()

    _assert_claims(run, {"checkout_clean"})


def test_an_untracked_file_alone_does_not_fail_checkout_clean(harness: Harness) -> None:
    (harness.repo / "artifacts").mkdir()
    (harness.repo / "artifacts/evidence.txt").write_text("untracked\n", encoding="utf-8")

    run = harness.run()

    _assert_claims(run, set())


def test_a_root_that_is_not_a_checkout_is_not_clean(harness: Harness) -> None:
    shutil.rmtree(harness.repo / ".git")

    run = harness.run()

    _assert_claims(run, {"checkout_clean", "process_loaded_head"})
    assert run.header["checkout_head"] == ""


# -- Scenario: A foreign listener does not pass ------------------------------------------


FOREIGN_LINE = 'LISTEN 0      511    127.0.0.1:8080 0.0.0.0:* users:(("nginx",pid=4242,fd=6))'


@pytest.mark.parametrize(
    ("ss_output", "ss_rc", "ppids"),
    [
        pytest.param(FOREIGN_LINE, "0", None, id="foreign-process-only"),
        pytest.param(f"{SS_LINE}\n{FOREIGN_LINE}", "0", None, id="unit-and-a-foreign-line"),
        pytest.param(
            SS_LINE.replace("pid=1028027", "pid=4243"),
            "0",
            f"1028026:{MAIN_PID} 4243:1",
            id="a-pid-that-is-not-a-child",
        ),
        pytest.param("LISTEN 0      2048   127.0.0.1:8080 0.0.0.0:*", "0", None, id="line-names-no-process"),
        pytest.param(f"{SS_LINE}\nLISTEN 0 4096 [::1]:8080 [::]:*", "0", None, id="second-line-names-no-process"),
        pytest.param("", "0", None, id="nothing-listens"),
        pytest.param(SS_LINE, "1", None, id="listener-query-fails"),
    ],
)
def test_a_foreign_listener_does_not_pass(
    harness: Harness, ss_output: str, ss_rc: str, ppids: str | None
) -> None:
    harness.knobs["FAKE_SS_OUTPUT"] = ss_output
    harness.knobs["FAKE_SS_RC"] = ss_rc
    if ppids is not None:
        harness.knobs["FAKE_PPIDS"] = ppids

    run = harness.run()

    # Whatever the three requests answer (here: all as the table says).
    _assert_claims(run, {"listener_of_unit"})
    assert _urls(run) == _expected_urls()


# -- health -------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["500", "503", "none"])
def test_an_unhealthy_or_silent_health_endpoint_fails_health(harness: Harness, code: str) -> None:
    harness.knobs["FAKE_HEALTH_CODE"] = code

    run = harness.run()

    _assert_claims(run, {"health"})


# -- Scenario: A role other than display_readonly does not pass -----------------------------


def _config_body(**fields: str) -> str:
    data = {
        "service_role": '"display_readonly"',
        "control_mutations_enabled": "false",
        "slurm_routes_enabled": "false",
        "queue_depth_mode": '"display_readonly_unavailable"',
        "display_readonly": "true",
    }
    data.update(fields)
    inner = ",".join(f'"{key}":{value}' for key, value in data.items())
    return '{"request_id":"r-2","status":"ok","data":{' + inner + "}}"


def test_the_config_body_builder_reproduces_the_measured_answer() -> None:
    assert _config_body().replace('"r-2"', '"r-1"') == RUNTIME_CONFIG_BODY


@pytest.mark.parametrize(
    ("code", "body"),
    [
        pytest.param("200", _config_body(service_role='"compute_control"'), id="other-service-role"),
        pytest.param("200", _config_body(service_role='"dev_monolith"'), id="dev-monolith-role"),
        pytest.param("200", _config_body(display_readonly="false"), id="display-readonly-false"),
        pytest.param("200", _config_body(slurm_routes_enabled="true"), id="slurm-routes-enabled"),
        pytest.param("200", _config_body(control_mutations_enabled="true"), id="control-mutations-enabled"),
        pytest.param(
            "200",
            '{"request_id":"r-3","status":"ok","data":{"queue_depth_mode":"display_readonly_unavailable",'
            '"control_mutations_enabled":false,"slurm_routes_enabled":false,"display_readonly":true}}',
            id="service-role-missing-decoy-only",
        ),
        pytest.param("500", RUNTIME_CONFIG_BODY, id="non-200-with-the-right-body"),
        pytest.param("none", RUNTIME_CONFIG_BODY, id="no-answer"),
    ],
)
def test_a_role_other_than_display_readonly_does_not_pass(harness: Harness, code: str, body: str) -> None:
    harness.knobs["FAKE_CONFIG_CODE"] = code
    harness.knobs["FAKE_CONFIG_BODY"] = body

    run = harness.run()

    _assert_claims(run, {"runtime_role"})


# -- Scenario: A reachable Slurm route does not pass --------------------------------------


@pytest.mark.parametrize("code", ["200", "401", "403", "500", "none"])
def test_a_reachable_slurm_route_does_not_pass(harness: Harness, code: str) -> None:
    harness.knobs["FAKE_SLURM_CODE"] = code
    harness.knobs["FAKE_SLURM_BODY"] = '{"status":"ok"}'

    run = harness.run()

    _assert_claims(run, {"slurm_routes_absent"})


# -- Scenario: A Slurm client on the host does not pass -------------------------------------


@pytest.mark.parametrize("command", SLURM_COMMANDS)
def test_a_slurm_client_on_path_does_not_pass(harness: Harness, command: str) -> None:
    _write_executable(harness.extra_bin / command, "#!/bin/sh\nexit 0\n")

    run = harness.run()

    _assert_claims(run, {"no_slurm_client"})
    assert command in run.claims["no_slurm_client"]


@pytest.mark.parametrize("path", SLURM_PATHS)
def test_the_script_checks_each_slurm_host_path_for_existence(path: str) -> None:
    """The three absolute paths cannot be created in a test; the script names each."""
    text = (REPO_ROOT / SCRIPT_RELATIVE).read_text(encoding="utf-8")
    pattern = rf'^\s*\[ -e {re.escape(path)} \] && found="\$found {re.escape(path)}"$'
    assert re.search(pattern, text, flags=re.MULTILINE), path


# -- Scenario: A writable storage root does not pass ----------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "object-store-writable",
        "published-writable",
        "object-store-key-missing",
        "published-key-missing",
        "object-store-key-empty",
        "published-key-empty",
        "object-store-missing-directory",
        "published-relative-path",
        "last-assignment-is-writable",
    ],
)
def test_a_writable_or_unnamed_storage_root_does_not_pass(harness: Harness, variant: str) -> None:
    lines = harness.default_env_lines()
    writable = harness.tmp / "writable-root"
    writable.mkdir()
    if variant == "object-store-writable":
        harness.object_store.chmod(0o755)
    elif variant == "published-writable":
        harness.published.chmod(0o755)
    elif variant == "object-store-key-missing":
        lines = [line for line in lines if not line.startswith("OBJECT_STORE_ROOT=")]
    elif variant == "published-key-missing":
        lines = [line for line in lines if not line.startswith("NHMS_PUBLISHED_ARTIFACT_ROOT=")]
    elif variant == "object-store-key-empty":
        lines.append("OBJECT_STORE_ROOT=")
    elif variant == "published-key-empty":
        lines.append('NHMS_PUBLISHED_ARTIFACT_ROOT=""')
    elif variant == "object-store-missing-directory":
        lines.append(f"OBJECT_STORE_ROOT={harness.tmp / 'no-such-root'}")
    elif variant == "published-relative-path":
        lines.append("NHMS_PUBLISHED_ARTIFACT_ROOT=published")
    else:
        lines.append(f"OBJECT_STORE_ROOT={writable}")
    harness.write_env(lines)

    run = harness.run()

    _assert_claims(run, {"storage_not_writable"})


def test_an_unreadable_env_file_fails_storage_not_writable(harness: Harness) -> None:
    harness.env_file.chmod(0o000)
    readable = os.access(harness.env_file, os.R_OK)
    harness.env_file.chmod(0o644)
    if readable:
        pytest.skip("platform ACL/root behavior still reports a 0000 file readable")

    run = harness.run(env_mode=0o000)

    _assert_claims(run, {"storage_not_writable"})
    assert run.header["object_store_root"] == ""
    assert run.header["env_modified"] == str(ENV_MODIFIED)


def test_storage_keys_are_matched_exactly_last_assignment_wins_quotes_stripped(harness: Harness) -> None:
    writable = harness.tmp / "writable-root"
    writable.mkdir()
    harness.write_env(
        [
            f"OBJECT_STORE_ROOT={writable}",
            f"# OBJECT_STORE_ROOT={SECRET}-comment",
            f"XOBJECT_STORE_ROOT={SECRET}-prefix",
            f"OBJECT_STORE_ROOT_OLD={SECRET}-suffix",
            f'  export OBJECT_STORE_ROOT="{harness.object_store}"',
            f"NHMS_PUBLISHED_ARTIFACT_ROOT='{harness.published}'",
        ]
    )

    run = harness.run()

    _assert_claims(run, set())
    assert run.header["object_store_root"] == str(harness.object_store)
    assert run.header["published_artifact_root"] == str(harness.published)
    assert SECRET not in run.completed.stdout + run.completed.stderr


# -- Scenario: An inactive unit fails every claim that needs the process -----------------------


@pytest.mark.parametrize(
    ("unit_state", "main_pid"),
    [
        pytest.param("inactive", MAIN_PID, id="inactive-with-a-stale-pid"),
        pytest.param("failed", "0", id="failed"),
        pytest.param("active", "0", id="active-without-main-process"),
        pytest.param("active", "", id="active-main-pid-unreadable"),
        pytest.param("", "", id="systemctl-answers-nothing"),
    ],
)
def test_an_inactive_unit_fails_every_claim_that_needs_the_process(
    harness: Harness, unit_state: str, main_pid: str
) -> None:
    harness.knobs["FAKE_UNIT_STATE"] = unit_state
    harness.knobs["FAKE_MAIN_PID"] = main_pid

    run = harness.run()

    _assert_claims(run, PROCESS_CLAIMS)
    assert run.header["main_pid"] == ""
    assert run.header["port"] == ""
    # No request to a guessed port, no listener query, no process read.
    assert run.calls["curl"] == []
    assert run.calls["ss"] == []
    assert run.calls["ps"] == []


# -- process_of_checkout and the port ----------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    ["another-checkout", "a-sibling-root-with-the-same-prefix", "another-app", "a-system-python"],
)
def test_a_main_process_that_is_not_this_checkouts_uvicorn_fails_process_of_checkout(
    harness: Harness, variant: str
) -> None:
    args = harness.main_args()
    if variant == "another-checkout":
        args = harness.main_args(harness.tmp / "elsewhere")
    elif variant == "a-sibling-root-with-the-same-prefix":
        args = harness.main_args(Path(f"{harness.repo}-old"))
    elif variant == "another-app":
        args = args.replace("apps.api.main:app", "apps.api.other:app")
    else:
        args = args.replace(f"{harness.repo}/.venv/bin/python", "/usr/bin/python3")
    harness.knobs["FAKE_MAIN_ARGS"] = args

    run = harness.run()

    _assert_claims(run, {"process_of_checkout"})
    assert run.header["port"] == PORT


@pytest.mark.parametrize(
    "args_suffix",
    [
        pytest.param("--host 127.0.0.1 --workers 2", id="no-port"),
        pytest.param("--host 127.0.0.1 --port", id="port-without-value"),
        pytest.param("--host 127.0.0.1 --port http", id="port-not-a-number"),
        pytest.param("--host 127.0.0.1 --port 70000", id="port-out-of-range"),
        pytest.param(None, id="command-line-unreadable"),
    ],
)
def test_no_port_fails_every_claim_that_needs_it_without_a_request(
    harness: Harness, args_suffix: str | None
) -> None:
    """The requirement's "no port" input: nothing can be listened to or requested."""
    harness.knobs["FAKE_MAIN_ARGS"] = (
        "" if args_suffix is None else f"{harness.repo}/.venv/bin/python -m uvicorn apps.api.main:app {args_suffix}"
    )

    run = harness.run()

    _assert_claims(run, {"process_of_checkout"} | PORT_CLAIMS)
    assert run.header["port"] == ""
    assert run.calls["curl"] == []
    assert run.calls["ss"] == []


def test_the_port_comes_from_the_command_line_not_the_env_file(harness: Harness) -> None:
    harness.knobs["FAKE_MAIN_ARGS"] = harness.main_args().replace(f"--port {PORT}", "--port=18080")
    harness.knobs["FAKE_SS_OUTPUT"] = SS_LINE.replace(":8080", ":18080")
    harness.write_env([*harness.default_env_lines(), "NHMS_DISPLAY_API_PORT=8080"])

    run = harness.run()

    _assert_claims(run, set())
    assert run.header["port"] == "18080"
    assert run.calls["ss"] == [["-Hltnp", "sport = :18080"]]
    assert all(url.startswith("http://127.0.0.1:18080/") for url in _urls(run))


# -- Scenario: The receipt changes nothing ---------------------------------------------------


@pytest.mark.parametrize("outcome", ["passing", "failing"])
def test_the_receipt_changes_nothing(harness: Harness, outcome: str) -> None:
    if outcome == "failing":
        harness.set_started(HEAD_MOVED)
        harness.knobs["FAKE_SLURM_CODE"] = "200"
        (harness.repo / "README.md").write_text("edited\n", encoding="utf-8")
    index_before = (harness.repo / ".git/index").read_bytes()

    run = harness.run()

    _assert_claims(
        run,
        set() if outcome == "passing" else {"process_loaded_head", "slurm_routes_absent", "checkout_clean"},
    )
    # `Harness.run` already compared the whole tree; the index is named here explicitly.
    assert (harness.repo / ".git/index").read_bytes() == index_before
    verbs = {call[1] for call in run.calls["systemctl"]}
    assert verbs == {"show", "is-active"}
    assert len(run.calls["curl"]) == 3
    assert list(harness.tmpdir.iterdir()) == []


# -- No value of display.env reaches the output except the two roots ----------------------------


def test_no_secret_of_the_env_file_reaches_stdout_or_stderr(harness: Harness) -> None:
    harness.write_env(
        [
            *harness.default_env_lines(),
            f"AWS_SECRET_ACCESS_KEY={SECRET}-aws",
            f"NHMS_DISPLAY_API_PORT={SECRET}-port",
            f"NHMS_PUBLISHED_ARTIFACT_HOST_ROOT={SECRET}-host-root",
            f"OBJECT_STORE_ROOT_BACKUP={SECRET}-backup",
            f"{SECRET}-a-line-without-assignment",
            f"export {SECRET}-exported=1",
        ]
    )

    passing = harness.run()
    _assert_claims(passing, set())

    # Also when the claims that read the file fail.
    harness.published.chmod(0o755)
    failing = harness.run()
    _assert_claims(failing, {"storage_not_writable"})

    for run in (passing, failing):
        assert SECRET not in run.completed.stdout
        assert SECRET not in run.completed.stderr
