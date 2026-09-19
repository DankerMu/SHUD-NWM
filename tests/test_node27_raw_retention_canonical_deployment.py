"""node-27 split raw-retention deployment (#2360, design D4-D6).

The canonical lane runs in the SYSTEM unit `nhms-node27-canonical-retention`
as the copyback root's owner (frd_muziyao, uid 1103); raw + precip-cache stay
in the nwm user unit; both alert on failure. These tests read the committed
unit files as text (the pattern of the sibling unit suites) and drive the root
installer only on paths that need no root: the refusal before any write, and
the env rendering it applies to the nwm env.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_UNITS = ROOT / "infra" / "systemd" / "system"
CANONICAL_SERVICE = SYSTEM_UNITS / "nhms-node27-canonical-retention.service"
CANONICAL_TIMER = SYSTEM_UNITS / "nhms-node27-canonical-retention.timer"
SYSTEM_ALERT_TEMPLATE = SYSTEM_UNITS / "nhms-node27-system-unit-failure-alert@.service"
USER_SERVICE = ROOT / "infra" / "systemd" / "nhms-node27-raw-retention.service"
USER_TIMER = ROOT / "infra" / "systemd" / "nhms-node27-raw-retention.timer"
USER_ALERT_TEMPLATE = ROOT / "infra" / "systemd" / "nhms-node27-unit-failure-alert@.service"
INSTALLER = ROOT / "scripts" / "node27_canonical_retention_install.sh"
ALERT_HANDLER = ROOT / "scripts" / "node27_unit_failure_alert_once.sh"


def _directives(path: Path) -> list[str]:
    """Non-comment, non-blank lines, so prose that names a directive never matches."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _section(path: Path, name: str) -> list[str]:
    lines = _directives(path)
    start = lines.index(f"[{name}]")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    return lines[start + 1 : end]


# --- unit files ---------------------------------------------------------------


def test_the_canonical_system_unit_runs_the_wrapper_as_the_copyback_root_owner() -> None:
    service = _section(CANONICAL_SERVICE, "Service")

    for directive in (
        "Type=oneshot",
        "User=frd_muziyao",
        "WorkingDirectory=/home/nwm/NWM",
        "ExecStart=/home/nwm/NWM/scripts/node27_raw_retention_once.sh",
        "Environment=NODE27_RAW_RETENTION_ENV_FILE=/etc/nhms/node27-canonical-retention.env",
        "Environment=NODE27_RAW_RETENTION_BOOTSTRAP_LOG=/var/log/nhms-node27-canonical-retention/bootstrap.log",
        "LogsDirectory=nhms-node27-canonical-retention",
        "LogsDirectoryMode=0755",
        "RuntimeDirectory=nhms-node27-canonical-retention",
        "StandardOutput=journal",
        "StandardError=journal",
        "TimeoutStartSec=0",
    ):
        assert directive in service, directive
    # Nothing may point the system unit at nwm's user-unit log or env paths.
    assert not [line for line in service if "node27-raw-retention-logs" in line]
    assert not [line for line in service if "infra/env/node27-raw-retention.env" in line]
    assert "OnFailure=nhms-node27-system-unit-failure-alert@%n.service" in _section(CANONICAL_SERVICE, "Unit")


def test_the_canonical_timer_ticks_with_the_nwm_unit_and_is_installable() -> None:
    timer = _section(CANONICAL_TIMER, "Timer")

    assert "OnCalendar=*-*-* 03:35:00 UTC" in timer
    # One cutoff date for a cycle's mirror and its PNGs: same tick as the nwm unit.
    assert [line for line in timer if line.startswith("OnCalendar=")] == [
        line for line in _section(USER_TIMER, "Timer") if line.startswith("OnCalendar=")
    ]
    assert "Persistent=true" in timer
    assert "Unit=nhms-node27-canonical-retention.service" in timer
    # `systemctl enable --now` in the installer needs an [Install] target.
    assert _section(CANONICAL_TIMER, "Install") == ["WantedBy=timers.target"]


def test_the_system_alert_template_runs_the_user_handler_on_the_system_journal() -> None:
    service = _section(SYSTEM_ALERT_TEMPLATE, "Service")

    for directive in (
        "Type=oneshot",
        "User=nwm",
        "SupplementaryGroups=systemd-journal",
        "WorkingDirectory=/home/nwm/NWM",
        # Leading `-`: a missing env file leaves the handler on its soft exit.
        "EnvironmentFile=-/home/nwm/NWM/infra/env/node27-frontier-alert.env",
        "Environment=NHMS_UNIT_FAILURE_JOURNAL_SCOPE=system",
        "ExecStart=/home/nwm/NWM/scripts/node27_unit_failure_alert_once.sh %i",
        "TimeoutStartSec=120",
    ):
        assert directive in service, directive
    # `%h` in a system unit is not nwm's home; the path must be absolute.
    assert not [line for line in service if "%h" in line]
    # Same handler as the user template.
    user_exec = [line for line in _section(USER_ALERT_TEMPLATE, "Service") if line.startswith("ExecStart=")]
    assert user_exec == [line for line in service if line.startswith("ExecStart=")]
    assert os.access(ALERT_HANDLER, os.X_OK)


def test_the_nwm_raw_retention_unit_alerts_on_failure() -> None:
    assert "OnFailure=nhms-node27-unit-failure-alert@%n.service" in _section(USER_SERVICE, "Unit")


def test_the_installer_installs_exactly_the_committed_system_units() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    committed = sorted(path.name for path in SYSTEM_UNITS.iterdir())

    assert committed == [
        "nhms-node27-canonical-retention.service",
        "nhms-node27-canonical-retention.timer",
        "nhms-node27-system-unit-failure-alert@.service",
    ]
    for name in committed:
        assert f"  {name}\n" in text, name
    # The installer's env / log / flock paths are the ones the unit names.
    assert 'readonly TARGET_ENV="$ENV_DIR/node27-canonical-retention.env"' in text
    assert "readonly ENV_DIR=/etc/nhms" in text
    assert "readonly LOG_DIR=/var/log/nhms-node27-canonical-retention" in text
    assert "readonly RUN_LOCK=/run/nhms-node27-canonical-retention/raw-retention.lock" in text
    assert os.access(INSTALLER, os.X_OK)


def test_the_installer_runs_the_nwm_venv_python_only_as_the_unit_user() -> None:
    """Root never executes the nwm-writable venv interpreter."""
    calls = [line for line in _directives(INSTALLER) if "/.venv/bin/python" in line]

    assert len(calls) >= 2, calls
    for line in calls:
        assert 'runuser -u "$UNIT_USER" -- ' in line.split("/.venv/bin/python")[0], line


# --- installer: refusal before any write --------------------------------------

# Every command the installer could write or change state with. Each stub
# records its name and fails, so a test run proves by the empty record that the
# refusal came first -- whatever uid runs the suite.
_MUTATING_COMMANDS = ("install", "mkdir", "mktemp", "chown", "chmod", "mv", "systemctl", "runuser", "rm")


def _stub_bin(tmp_path: Path, *, uid: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "mutations.txt"
    for name in _MUTATING_COMMANDS:
        stub = bin_dir / name
        stub.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{record}"\nexit 97\n', encoding="utf-8")
        stub.chmod(0o755)
    fake_id = bin_dir / "id"
    fake_id.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$#" -eq 1 ] && [ "$1" = "-u" ]; then echo {uid}; exit 0; fi\n'
        'echo "id: no such user" >&2\nexit 1\n',
        encoding="utf-8",
    )
    fake_id.chmod(0o755)
    return bin_dir, record


def _run_installer(bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_installer_refuses_a_non_root_caller_before_any_write(tmp_path: Path) -> None:
    bin_dir, record = _stub_bin(tmp_path, uid="1005")

    result = _run_installer(bin_dir)

    assert result.returncode != 0
    assert "REFUSED: must run as root" in result.stderr
    assert not record.exists(), record.read_text(encoding="utf-8")


def test_the_installer_refuses_when_the_unit_user_is_missing_before_any_write(tmp_path: Path) -> None:
    """Root caller, but `id -u frd_muziyao` fails: the uid precondition stops it."""
    bin_dir, record = _stub_bin(tmp_path, uid="0")

    result = _run_installer(bin_dir)

    assert result.returncode != 0
    assert "REFUSED: user frd_muziyao does not exist" in result.stderr
    assert not record.exists(), record.read_text(encoding="utf-8")


def test_the_installer_refuses_for_real_as_non_root(tmp_path: Path) -> None:
    """No stubs: the real `id` on a non-root account."""
    if os.geteuid() == 0:
        pytest.skip("the suite runs as root; the stubbed rows above cover the refusal")
    result = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True, timeout=60)

    assert result.returncode == 1
    assert "REFUSED: must run as root" in result.stderr


# The lock and source-env checks name fixed node-27 paths, so they are driven
# as the functions `check_preconditions` calls, on tmp paths, with `stat`
# stubbed (owner 1103 needs root; macOS `stat` has no `-c`).


def _run_check(
    tmp_path: Path, function: str, target: Path, *, owner: str = "1103", mode: str = "600"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, record = _stub_bin(tmp_path, uid="0")
    fake_stat = bin_dir / "stat"
    fake_stat.write_text(
        f'#!/usr/bin/env bash\ncase "$2" in %u) echo {owner} ;; %a) echo {mode} ;; *) exit 2 ;; esac\n',
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; "$2" "$3"', "_", str(INSTALLER), function, str(target)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, record


def _lock_file(tmp_path: Path) -> Path:
    lock = tmp_path / ".nhms-copyback-batch.lock"
    lock.write_text("", encoding="utf-8")
    return lock


def _nwm_env(tmp_path: Path, text: str) -> Path:
    env = tmp_path / "node27-raw-retention.env"
    env.write_text(text, encoding="utf-8")
    return env


_GOOD_NWM_ENV = (
    "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\n"
    "NODE27_RAW_RETENTION_LANES=raw,precip-cache\n"
    "# NODE27_RAW_RETENTION_LANES=canonical\n"
)


def test_the_preconditions_run_the_lock_and_env_checks_on_the_fixed_paths_before_any_write() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    body = text[text.index("check_preconditions() {") : text.index("write_env() {")]

    assert (
        body.index('check_copyback_lock "$COPYBACK_LOCK"')
        < body.index('check_source_env "$SOURCE_ENV"')
        < body.index("runuser")
    )
    assert text.index("  check_preconditions\n") < text.index("  write_env\n")


def test_a_valid_lock_and_nwm_env_pass_their_checks(tmp_path: Path) -> None:
    (tmp_path / "lock").mkdir()
    lock_result, lock_record = _run_check(tmp_path / "lock", "check_copyback_lock", _lock_file(tmp_path))
    assert lock_result.returncode == 0, lock_result.stderr
    assert not lock_record.exists()

    for index, text in enumerate(
        (
            _GOOD_NWM_ENV,
            "export NODE27_RAW_RETENTION_LANES='raw'\n"
            'NODE27_RAW_RETENTION_OBJECT_STORE_ROOT="/home/ghdc/nwm/object-store"\n',
            'export NODE27_RAW_RETENTION_LANES="raw,precip-cache"\n'
            "  export NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\n",
        )
    ):
        case = tmp_path / f"env-{index}"
        case.mkdir()
        result, record = _run_check(case, "check_source_env", _nwm_env(case, text))
        assert result.returncode == 0, (text, result.stderr)
        assert not record.exists()


@pytest.mark.parametrize(
    ("owner", "mode", "message"),
    [
        ("1005", "600", "REFUSED: copyback lock is not owned by uid 1103"),
        ("1103", "660", "REFUSED: copyback lock mode is not 600"),
    ],
)
def test_the_installer_refuses_an_unsafe_copyback_lock(tmp_path: Path, owner: str, mode: str, message: str) -> None:
    result, record = _run_check(tmp_path, "check_copyback_lock", _lock_file(tmp_path), owner=owner, mode=mode)

    assert result.returncode != 0
    assert message in result.stderr
    assert not record.exists(), record.read_text(encoding="utf-8")


def test_the_installer_refuses_a_missing_copyback_lock(tmp_path: Path) -> None:
    result, record = _run_check(tmp_path, "check_copyback_lock", tmp_path / ".nhms-copyback-batch.lock")

    assert result.returncode != 0
    assert "is missing or not a regular file" in result.stderr
    assert not record.exists(), record.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "mode", "message"),
    [
        (_GOOD_NWM_ENV, "644", "mode is not 600"),
        # No LANES line: the nwm unit would keep all three lanes.
        (
            "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\n",
            "600",
            "must set NODE27_RAW_RETENTION_LANES on exactly one line",
        ),
        # Two LANES lines: refused rather than guessing which one wins.
        (
            _GOOD_NWM_ENV + "NODE27_RAW_RETENTION_LANES=raw\n",
            "600",
            "must set NODE27_RAW_RETENTION_LANES on exactly one line",
        ),
        (
            "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\n"
            'export NODE27_RAW_RETENTION_LANES="raw,canonical"\n',
            "600",
            "may name only raw / precip-cache",
        ),
        *(
            (
                "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\n" + line,
                "600",
                "must be a comma list with no spaces",
            )
            for line in (
                "NODE27_RAW_RETENTION_LANES=raw, precip-cache\n",
                "export NODE27_RAW_RETENTION_LANES=raw, precip-cache\n",
                'NODE27_RAW_RETENTION_LANES="raw, precip-cache"\n',
            )
        ),
        (
            "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\nNODE27_RAW_RETENTION_LANES=,\n",
            "600",
            "names no lane",
        ),
        (
            "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store/\n"
            "NODE27_RAW_RETENTION_LANES=raw,precip-cache\n",
            "600",
            "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT is not /home/ghdc/nwm/object-store",
        ),
        (
            "NODE27_RAW_RETENTION_LANES=raw,precip-cache\n",
            "600",
            "must set NODE27_RAW_RETENTION_OBJECT_STORE_ROOT on exactly one line",
        ),
    ],
    ids=[
        "mode-644",
        "lanes-missing",
        "lanes-twice",
        "lanes-canonical",
        "lanes-space",
        "lanes-space-export",
        "lanes-space-quoted",
        "lanes-empty",
        "root-mismatch",
        "root-missing",
    ],
)
def test_the_installer_refuses_an_unfit_nwm_env(tmp_path: Path, text: str, mode: str, message: str) -> None:
    result, record = _run_check(tmp_path, "check_source_env", _nwm_env(tmp_path, text), mode=mode)

    assert result.returncode != 0
    assert "REFUSED: source env " in result.stderr
    assert message in result.stderr
    assert not record.exists(), record.read_text(encoding="utf-8")


# --- installer: generated env --------------------------------------------------


def _render(source_env: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; render_canonical_env "$2"', "_", str(INSTALLER), str(source_env)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assignments(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, _, value = stripped.partition("=")
        pairs.append((key, value))
    return pairs


def test_the_generated_env_drops_per_unit_keys_and_sets_the_canonical_lane(tmp_path: Path) -> None:
    source = tmp_path / "node27-raw-retention.env"
    source.write_text(
        "NODE27_RAW_RETENTION_REPO=/home/nwm/NWM\n"
        "NODE27_DISPLAY_WATERMARK_DATABASE_URL=postgresql://nhms_display_ro:s3cret@127.0.0.1:55432/nhms\n"
        "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store\n"
        "NODE27_RAW_RETENTION_LOG_ROOT=/home/nwm/node27-raw-retention-logs\n"
        "NODE27_RAW_RETENTION_LOCK_PATH=/tmp/node27-raw-retention.lock\n"
        "export NODE27_RAW_RETENTION_LOG_FILE=/home/nwm/node27-raw-retention-logs/raw-retention.log\n"
        "  NODE27_RAW_RETENTION_SUMMARY_PATH=/home/nwm/summary.json\n"
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG=/home/nwm/node27-raw-retention.log\n"
        "NODE27_RAW_RETENTION_LANES=raw,precip-cache\n"
        "NODE27_RAW_RETENTION_SOURCES=GFS,IFS\n"
        "NODE27_RAW_RETENTION_DAYS=14\n"
        "# NODE27_RAW_RETENTION_LANES=commented\n"
        "NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt",  # no trailing newline
        encoding="utf-8",
    )

    result = _render(source)

    assert result.returncode == 0, result.stderr
    pairs = _assignments(result.stdout)
    keys = [key for key, _ in pairs]
    assert len(keys) == len(set(keys)), f"duplicated keys: {keys}"
    assert dict(pairs) == {
        "NODE27_RAW_RETENTION_REPO": "/home/nwm/NWM",
        "NODE27_DISPLAY_WATERMARK_DATABASE_URL": "postgresql://nhms_display_ro:s3cret@127.0.0.1:55432/nhms",
        "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT": "/home/ghdc/nwm/object-store",
        "NODE27_RAW_RETENTION_SOURCES": "GFS,IFS",
        "NODE27_RAW_RETENTION_DAYS": "14",
        "NHMS_MVT_FILE_CACHE_DIR": "/home/nwm/.cache/nhms/mvt",
        "NODE27_RAW_RETENTION_LANES": "canonical",
        "NODE27_RAW_RETENTION_LOG_ROOT": "/var/log/nhms-node27-canonical-retention",
        "NODE27_RAW_RETENTION_LOCK_PATH": "/run/nhms-node27-canonical-retention/raw-retention.lock",
    }
    # The shell reading it (the wrapper does `set -a; . env`) sees the same.
    sourced = subprocess.run(
        [
            "bash",
            "-c",
            'set -a; . /dev/stdin; printf "%s|%s|%s\\n" "$NODE27_RAW_RETENTION_LANES" '
            '"$NODE27_RAW_RETENTION_LOG_ROOT" "${NODE27_RAW_RETENTION_BOOTSTRAP_LOG-unset}"',
        ],
        input=result.stdout,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert sourced.stdout.strip() == "canonical|/var/log/nhms-node27-canonical-retention|unset"


def test_the_generated_env_survives_a_source_that_is_only_per_unit_keys(tmp_path: Path) -> None:
    """`grep -v` exits 1 when it filters every line; that is not an error."""
    source = tmp_path / "node27-raw-retention.env"
    source.write_text("NODE27_RAW_RETENTION_LANES=raw\n", encoding="utf-8")

    result = _render(source)

    assert result.returncode == 0, result.stderr
    assert _assignments(result.stdout) == [
        ("NODE27_RAW_RETENTION_LANES", "canonical"),
        ("NODE27_RAW_RETENTION_LOG_ROOT", "/var/log/nhms-node27-canonical-retention"),
        ("NODE27_RAW_RETENTION_LOCK_PATH", "/run/nhms-node27-canonical-retention/raw-retention.lock"),
    ]


def test_an_unreadable_source_env_fails_the_render(tmp_path: Path) -> None:
    result = _render(tmp_path / "missing.env")

    assert result.returncode != 0
    assert "NODE27_RAW_RETENTION_LANES=canonical" not in result.stdout
