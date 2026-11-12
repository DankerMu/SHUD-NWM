"""#2913: the node-22 unit-failure alert lane (template unit, handler, example).

`infra/systemd/nhms-node22-unit-failure-alert@.service` runs
`scripts/node22_unit_failure_alert_once.sh %i` with the failed unit's full
name; the handler reads 30 journal lines and hands exactly one message to
`$NHMS_FRONTIER_SENDMAIL -t -i`. Every unusable configuration exits 0 after a
`SKIPPED reason=...` line; a transport that rejects propagates its rc.

The handler is executed by path in a subprocess whose environment is built from
scratch (nothing inherited from ``os.environ`` except the system ``PATH`` tail),
with a fake ``journalctl`` and an appending fake sendmail in ``tmp_path``.
Expected values are spelled out here, never read back from the handler.

File-reading and rule helpers stay module-level so the 3.2 wiring assertions
can reuse them. Stdlib only, nothing imported from ``scripts/`` or other tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "infra/systemd/nhms-node22-unit-failure-alert@.service"
HANDLER_PATH = REPO_ROOT / "scripts/node22_unit_failure_alert_once.sh"
EXAMPLE_PATH = REPO_ROOT / "infra/env/node22-unit-failure-alert.example"
ENV_README_PATH = REPO_ROOT / "infra/env/README.md"
MANIFEST_PATH = REPO_ROOT / "infra/systemd/node22/MANIFEST.json"

TEMPLATE_DEPLOYED_NAME = "nhms-node22-unit-failure-alert@.service"
TEMPLATE_TRACKED_PATH = "infra/systemd/nhms-node22-unit-failure-alert@.service"

SECRET_ENV_FILE = "/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env"
AUTHORITY_ROOT = "/scratch/frd_muziyao/"
AUTHORITY_HEADING = "## node-22 unit → EnvironmentFile authority (#1694)"
AUTHORITY_TOKEN_PREFIX = "NWM/infra/env/"
SECRET_NAME_SUFFIXES = ("TOKEN", "SECRET", "PASS", "PASSWORD")

README_ROW = (
    "| `nhms-node22-unit-failure-alert@.service` "
    "| `NWM/infra/env/node22-unit-failure-alert.env` "
    "| `node22-unit-failure-alert.example` |"
)
TEMPLATE_DIRECTIVES = (
    ("Unit", "Description"),
    ("Service", "Type=oneshot"),
    ("Service", "WorkingDirectory=/scratch/frd_muziyao/NWM"),
    ("Service", "EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env"),
    ("Service", "ExecStart=/scratch/frd_muziyao/NWM/scripts/node22_unit_failure_alert_once.sh %i"),
    ("Service", "TimeoutStartSec=120"),
)
EXAMPLE_KEYS = (
    "NHMS_ALERT_EMAIL_TO",
    "NHMS_ALERT_EMAIL_FROM",
    "NHMS_FRONTIER_SENDMAIL",
    "NHMS_SMTP_HOST",
    "NHMS_SMTP_PORT",
    "NHMS_SMTP_USER",
    "NHMS_SMTP_PASS",
)

UNIT = "nhms-scheduler-journal-retention.service"
RECIPIENT = "node22-ops@example.invalid"
SENDER = "NHMS Node-22 Alert <alerts@example.invalid>"
JOURNAL_SENTINEL = "FAKE-JOURNALCTL-CONTEXT-LINE"
CALL_SEPARATOR = "=== FAKE-SENDMAIL-CALL ==="

_EXAMPLE_KEY_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_ENVIRONMENT_TOKEN = re.compile(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""")
_BACKTICK_TOKEN = re.compile(r"`([^`]*)`")


# --- module-level file and rule helpers ---------------------------------------


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> dict:
    return json.loads(read_text(MANIFEST_PATH))


def unit_directives(text: str) -> list[tuple[str, str]]:
    """``(section, stripped line)`` for every non-comment, non-blank, non-header line."""
    section = ""
    directives = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        directives.append((section, stripped))
    return directives


def unit_sections(text: str) -> list[str]:
    return [line.strip()[1:-1] for line in text.splitlines() if re.fullmatch(r"\[[A-Za-z]+\]", line.strip())]


def directive_value(line: str, directive: str) -> str | None:
    key, sep, value = line.partition("=")
    if not sep or key.strip() != directive:
        return None
    return value.strip()


def secret_assignment_violations(line: str) -> list[str]:
    """Secret-named keys an ``Environment=`` line assigns a non-empty value to."""
    value = directive_value(line, "Environment")
    if value is None:
        return []
    violations = []
    for token in _ENVIRONMENT_TOKEN.findall(value):
        key, sep, assigned = token.replace('"', "").replace("'", "").partition("=")
        if sep and key.endswith(SECRET_NAME_SUFFIXES) and assigned:
            violations.append(key)
    return violations


def authority_environment_files(readme_text: str) -> frozenset[str]:
    """Absolute ``NWM/infra/env/...`` paths from column 2 of the #1694 authority table only."""
    lines = readme_text.splitlines()
    paths = set()
    for line in lines[lines.index(AUTHORITY_HEADING) + 1 :]:
        if line.startswith("##"):
            break
        cells = line.split("|") if line.startswith("|") else []
        if len(cells) < 3:
            continue
        for token in _BACKTICK_TOKEN.findall(cells[2]):
            if token.startswith(AUTHORITY_TOKEN_PREFIX):
                paths.add(AUTHORITY_ROOT + token)
    return frozenset(paths)


def authority_section(readme_text: str) -> list[str]:
    lines = readme_text.splitlines()
    start = lines.index(AUTHORITY_HEADING)
    section = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("##"):
            break
        section.append(line)
    return section


def environment_file_violation(line: str, allowed: frozenset[str]) -> str | None:
    value = directive_value(line, "EnvironmentFile")
    if value is None or value == "":
        return None
    path = value.removeprefix("-")
    return None if path in allowed else path


def example_assignments(text: str) -> list[tuple[str, str]]:
    return [(match.group(1), match.group(2)) for line in text.splitlines() if (match := _EXAMPLE_KEY_LINE.match(line))]


# --- handler harness ----------------------------------------------------------


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_fake_journalctl(tmp_path: Path, *, silent: bool = False) -> None:
    body = "exit 0\n" if silent else f'printf "%s argv=%s\\n" "{JOURNAL_SENTINEL}" "$*"\n'
    _write_executable(tmp_path / "journalctl", body)


def _write_fake_sendmail(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """Appends one argv line and one separated stdin record per invocation."""
    return _write_executable(
        tmp_path / "fake-sendmail",
        f'printf "%s\\n" "$*" >> "{tmp_path / "sendmail.argv"}"\n'
        f'{{ printf "%s\\n" "{CALL_SEPARATOR}"; cat; }} >> "{tmp_path / "sendmail.stdin"}"\n'
        f"exit {exit_code}\n",
    )


def _sendmail_calls(tmp_path: Path) -> list[str]:
    argv_log = tmp_path / "sendmail.argv"
    return argv_log.read_text(encoding="utf-8").splitlines() if argv_log.exists() else []


def _sendmail_messages(tmp_path: Path) -> list[str]:
    text = (tmp_path / "sendmail.stdin").read_text(encoding="utf-8")
    return [chunk for chunk in text.split(CALL_SEPARATOR + "\n") if chunk]


def _env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """Built from scratch: only the system ``PATH`` tail comes from the host."""
    env = {
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "NHMS_ALERT_EMAIL_TO": RECIPIENT,
        "NHMS_ALERT_EMAIL_FROM": SENDER,
        "NHMS_FRONTIER_SENDMAIL": str(tmp_path / "fake-sendmail"),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _run_handler(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # By path, not `bash <path>`: a missing exec bit must fail here.
    return subprocess.run([str(HANDLER_PATH), *args], env=env, capture_output=True, text=True, timeout=60, check=False)


@pytest.fixture
def lane(tmp_path: Path) -> Path:
    _write_fake_journalctl(tmp_path)
    _write_fake_sendmail(tmp_path)
    return tmp_path


# --- scenario: failed unit produces one mail ----------------------------------


def test_failed_unit_hands_exactly_one_message_to_the_transport(lane: Path) -> None:
    result = _run_handler(_env(lane), UNIT)

    assert result.returncode == 0, result.stderr
    assert _sendmail_calls(lane) == ["-t -i"]
    (message,) = _sendmail_messages(lane)
    headers, _, body = message.partition("\n\n")
    header_lines = headers.splitlines()
    assert header_lines[:3] == [
        f"To: {RECIPIENT}",
        f"From: {SENDER}",
        f"Subject: [NHMS node-22] unit failed: {UNIT}",
    ]
    assert "MIME-Version: 1.0" in header_lines
    assert 'Content-Type: text/plain; charset="utf-8"' in header_lines
    (date_line,) = [line for line in header_lines if line.startswith("Date: ")]
    assert parsedate_to_datetime(date_line.removeprefix("Date: ")).tzinfo is not None
    assert f"systemd unit {UNIT} entered the failed state on " in body
    assert "Last 30 journal lines:" in body
    assert f"{JOURNAL_SENTINEL} argv=--user -u {UNIT} -n 30 --no-pager" in body.splitlines()
    assert result.stderr.splitlines() == [f"node22-unit-failure-alert: SENT unit={UNIT} to={RECIPIENT}"]


def test_journal_read_ignores_the_node27_knobs(lane: Path) -> None:
    """node-22 drops the line-count knob and the system-scope branch: 30 lines, user journal, always."""
    env = _env(lane, NHMS_UNIT_FAILURE_JOURNAL_LINES="5", NHMS_UNIT_FAILURE_JOURNAL_SCOPE="system")

    result = _run_handler(env, UNIT)

    assert result.returncode == 0, result.stderr
    (message,) = _sendmail_messages(lane)
    journal = [line for line in message.splitlines() if line.startswith(JOURNAL_SENTINEL)]
    assert journal == [f"{JOURNAL_SENTINEL} argv=--user -u {UNIT} -n 30 --no-pager"]
    assert "Last 30 journal lines:" in message


def test_empty_journal_falls_back_to_a_named_placeholder(tmp_path: Path) -> None:
    _write_fake_journalctl(tmp_path, silent=True)
    _write_fake_sendmail(tmp_path)

    result = _run_handler(_env(tmp_path), UNIT)

    assert result.returncode == 0, result.stderr
    (message,) = _sendmail_messages(tmp_path)
    assert f"(no journal context available for {UNIT})" in message.splitlines()


def test_hostname_failure_falls_back_to_node22(lane: Path) -> None:
    _write_executable(lane / "hostname", "exit 1\n")

    result = _run_handler(_env(lane), UNIT)

    assert result.returncode == 0, result.stderr
    (message,) = _sendmail_messages(lane)
    assert f"systemd unit {UNIT} entered the failed state on node-22 at " in message


# --- scenario: unconfigured recipient does not add a second failure -----------


@pytest.mark.parametrize(
    ("args", "overrides", "reason"),
    [
        pytest.param((), {}, "NO_UNIT_ARGUMENT", id="no-unit-argument"),
        pytest.param((UNIT,), {"NHMS_ALERT_EMAIL_TO": None}, "NHMS_ALERT_EMAIL_TO_UNSET", id="recipient-unset"),
        pytest.param((UNIT,), {"NHMS_ALERT_EMAIL_TO": ""}, "NHMS_ALERT_EMAIL_TO_UNSET", id="recipient-empty"),
        pytest.param((UNIT,), {"NHMS_ALERT_EMAIL_FROM": None}, "NHMS_ALERT_EMAIL_FROM_UNSET", id="sender-unset"),
        pytest.param(
            (UNIT,),
            {"NHMS_ALERT_EMAIL_TO": f"{RECIPIENT}\nBcc: attacker@example.invalid"},
            "NHMS_ALERT_EMAIL_TO_UNSAFE",
            id="recipient-lf",
        ),
        pytest.param(
            (UNIT,),
            {"NHMS_ALERT_EMAIL_TO": f"{RECIPIENT}\rBcc: attacker@example.invalid"},
            "NHMS_ALERT_EMAIL_TO_UNSAFE",
            id="recipient-cr",
        ),
        pytest.param(
            (UNIT,),
            {"NHMS_ALERT_EMAIL_FROM": f"{SENDER}\rX-Injected: 1"},
            "NHMS_ALERT_EMAIL_FROM_UNSAFE",
            id="sender-cr",
        ),
        pytest.param(
            (UNIT,),
            {"NHMS_ALERT_EMAIL_FROM": f"{SENDER}\nX-Injected: 1"},
            "NHMS_ALERT_EMAIL_FROM_UNSAFE",
            id="sender-lf",
        ),
        pytest.param((UNIT,), {"NHMS_FRONTIER_SENDMAIL": None}, "SENDMAIL_UNCONFIGURED", id="transport-unset"),
        pytest.param((UNIT,), {"NHMS_FRONTIER_SENDMAIL": ""}, "SENDMAIL_UNCONFIGURED", id="transport-empty"),
    ],
)
def test_unusable_configuration_skips_with_exit_zero(
    lane: Path, args: tuple[str, ...], overrides: dict[str, str | None], reason: str
) -> None:
    result = _run_handler(_env(lane, **overrides), *args)

    assert result.returncode == 0
    assert "SKIPPED" in result.stderr
    assert f"reason={reason}" in result.stderr
    assert result.stderr.startswith("node22-unit-failure-alert: SKIPPED ")
    assert not (lane / "sendmail.argv").exists()
    assert not (lane / "sendmail.stdin").exists()


@pytest.mark.parametrize("make_target", ["missing", "not-executable"])
def test_unusable_transport_path_skips_with_exit_zero(lane: Path, make_target: str) -> None:
    target = lane / "transport"
    if make_target == "not-executable":
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        target.chmod(0o644)

    result = _run_handler(_env(lane, NHMS_FRONTIER_SENDMAIL=str(target)), UNIT)

    assert result.returncode == 0
    assert "SKIPPED" in result.stderr
    assert "reason=SENDMAIL_UNAVAILABLE" in result.stderr
    assert not (lane / "sendmail.argv").exists()


# --- scenario: failing transport is visible -----------------------------------


@pytest.mark.parametrize("transport_rc", [1, 75])
def test_rejecting_transport_propagates_its_exit_code(tmp_path: Path, transport_rc: int) -> None:
    _write_fake_journalctl(tmp_path)
    _write_fake_sendmail(tmp_path, exit_code=transport_rc)

    result = _run_handler(_env(tmp_path), UNIT)

    assert result.returncode == transport_rc
    assert _sendmail_calls(tmp_path) == ["-t -i"]
    assert f"node22-unit-failure-alert: SEND_FAILED unit={UNIT} rc={transport_rc}" in result.stderr.splitlines()
    assert "SENT unit=" not in result.stderr


# --- handler static contract --------------------------------------------------


def test_handler_shape() -> None:
    text = read_text(HANDLER_PATH)
    lines = text.splitlines()

    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
    assert "systemctl" not in text
    assert os.access(HANDLER_PATH, os.X_OK)
    # Every exec bit, not an exact mode: a fresh checkout's mode is 0777 & ~umask.
    assert HANDLER_PATH.stat().st_mode & 0o111 == 0o111


# --- template unit ------------------------------------------------------------


def test_template_carries_exactly_the_pinned_directives() -> None:
    text = read_text(TEMPLATE_PATH)
    directives = unit_directives(text)

    assert unit_sections(text) == ["Unit", "Service"]
    (description,) = [line for section, line in directives if line.startswith("Description=")]
    assert description.endswith(" %i"), description
    normalized = tuple(
        (section, "Description" if line.startswith("Description=") else line) for section, line in directives
    )
    assert normalized == TEMPLATE_DIRECTIVES


def test_template_assigns_no_secret_and_loads_only_an_authority_env_file() -> None:
    allowed = authority_environment_files(read_text(ENV_README_PATH)) | {SECRET_ENV_FILE}
    lines = [line for _, line in unit_directives(read_text(TEMPLATE_PATH))]

    assert [key for line in lines for key in secret_assignment_violations(line)] == []
    environment_files = [line for line in lines if directive_value(line, "EnvironmentFile") is not None]
    assert environment_files, "the template must load its env file"
    assert [bad for line in environment_files if (bad := environment_file_violation(line, allowed))] == []


def test_authority_rule_functions_bite() -> None:
    allowed = authority_environment_files(read_text(ENV_README_PATH))

    assert "/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env" in allowed
    assert secret_assignment_violations('Environment="NHMS_SMTP_PASS=x"') == ["NHMS_SMTP_PASS"]
    assert environment_file_violation("EnvironmentFile=-/home/nwm/NWM/infra/env/node22-unit-failure-alert.env", allowed)
    assert environment_file_violation("EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.env", allowed)


def test_readme_authority_table_carries_the_alert_row() -> None:
    section = authority_section(read_text(ENV_README_PATH))

    assert section.count(README_ROW) == 1
    assert "DATABASE_URL" not in README_ROW


# --- example ------------------------------------------------------------------


def test_example_key_set_is_exact() -> None:
    text = read_text(EXAMPLE_PATH)
    assignments = example_assignments(text)

    assert sorted(key for key, _ in assignments) == sorted(EXAMPLE_KEYS)
    assert len(assignments) == len(EXAMPLE_KEYS)
    expected = {key: "" for key in EXAMPLE_KEYS} | {"NHMS_FRONTIER_SENDMAIL": "/usr/sbin/sendmail"}
    assert dict(assignments) == expected
    assert "DATABASE_URL" not in text
    assert "REPLACE_ME" not in text


def test_new_files_name_no_database_selector() -> None:
    for path in (TEMPLATE_PATH, HANDLER_PATH, EXAMPLE_PATH):
        assert "DATABASE_URL" not in read_text(path), path


# --- manifest -----------------------------------------------------------------


def test_manifest_lists_the_template_with_its_sha256() -> None:
    entries = load_manifest()["entries"]
    matching = [entry for entry in entries if entry["deployed_relative_path"] == TEMPLATE_DEPLOYED_NAME]

    assert matching == [
        {
            "deployed_relative_path": TEMPLATE_DEPLOYED_NAME,
            "tracked_path": TEMPLATE_TRACKED_PATH,
            "sha256": sha256_hex(TEMPLATE_PATH),
        }
    ]


def test_no_manifest_tracked_file_names_the_home_relative_checkout() -> None:
    """``%h/NWM`` is the node-27 checkout shape; bare ``%h`` (``%h/.local/bin``) stays legal."""
    tracked = [entry["tracked_path"] for entry in load_manifest()["entries"]]

    assert TEMPLATE_TRACKED_PATH in tracked
    offenders = [path for path in tracked if "%h/NWM" in read_text(REPO_ROOT / path)]
    assert offenders == []


# --- scenario: seven oneshots wired, long-running units untouched (#2914, #3121)

SYSTEMD_ROOT = REPO_ROOT / "infra/systemd"
ON_FAILURE_LINE = "OnFailure=nhms-node22-unit-failure-alert@%n.service"
WIRED_UNITS = (
    "nhms-node22-refresh-timer-health.service",
    "nhms-node22-scheduler-stall-health.service",
    "nhms-scheduler-evidence-retention.service",
    "nhms-scheduler-file-provider-refresh.service",
    "nhms-scheduler-journal-retention.service",
    "nhms-scheduler-state-index-capacity.service",
    # #3121: the spec delta moves the exact set from six to seven.
    "nhms-scheduler-state-index-prune.service",
)
# Paths the exact-seven scan must actually reach, so "none of these is wired" is
# a measured fact rather than a vacuous one (spec: gateway and `node22/**`).
MUST_SCAN = (
    "nhms-slurm-gateway.service",
    "nhms-node22-unit-failure-alert@.service",
    "nhms-scheduler-file-provider-refresh.timer",
    "node22/MANIFEST.json",
    "node22/nhms-compute-scheduler.service",
    "node22/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf",
)


def unit_section(text: str, name: str) -> list[str]:
    """Stripped non-comment, non-blank lines of section ``[name]``, header excluded."""
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", ";"))]
    start = lines.index(f"[{name}]")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    return lines[start + 1 : end]


def carries_on_failure_line(text: str) -> bool:
    """Whole-line equality on non-comment lines: prose naming the directive never matches."""
    return any(
        line.strip() == ON_FAILURE_LINE for line in text.splitlines() if not line.lstrip().startswith(("#", ";"))
    )


@pytest.mark.parametrize("unit", WIRED_UNITS)
def test_wired_unit_carries_the_alert_line_once_in_unit_section(unit: str) -> None:
    text = read_text(SYSTEMD_ROOT / unit)

    assert unit_section(text, "Unit").count(ON_FAILURE_LINE) == 1, unit
    assert unit_section(text, "Service").count(ON_FAILURE_LINE) == 0, unit


def test_exactly_the_seven_oneshots_carry_the_alert_line() -> None:
    scanned = {path.relative_to(SYSTEMD_ROOT).as_posix(): path for path in SYSTEMD_ROOT.rglob("*") if path.is_file()}

    assert set(MUST_SCAN) <= set(scanned), sorted(set(MUST_SCAN) - set(scanned))
    wired = {name for name, path in scanned.items() if carries_on_failure_line(read_text(path))}
    assert wired == set(WIRED_UNITS)


def test_wiring_rule_functions_bite() -> None:
    commented = f"[Unit]\n# A consumer wires it as `{ON_FAILURE_LINE}`.\n\n[Service]\nType=oneshot\n"
    in_service = f"[Unit]\nDescription=x\n\n[Service]\n{ON_FAILURE_LINE}\n"

    assert not carries_on_failure_line(commented)
    assert unit_section(commented, "Unit") == []
    assert carries_on_failure_line(in_service)
    assert unit_section(in_service, "Unit").count(ON_FAILURE_LINE) == 0
    assert unit_section(in_service, "Service").count(ON_FAILURE_LINE) == 1
    assert not carries_on_failure_line("OnFailure=nhms-node22-unit-failure-alert@%N.service\n")


@pytest.mark.parametrize("unit", WIRED_UNITS)
def test_manifest_sha256_of_wired_unit_matches_its_content(unit: str) -> None:
    entries = [entry for entry in load_manifest()["entries"] if entry["tracked_path"] == f"infra/systemd/{unit}"]

    assert [entry["sha256"] for entry in entries] == [sha256_hex(SYSTEMD_ROOT / unit)]


# --- scenario: node-22 SMTPS transport wrapper (#3127) --------------------------
#
# `scripts/node22_smtp_sendmail.sh` execs the node-27 SMTPS shim under the exact
# node-22 interpreter. Behaviour is exercised on a tmp copy whose `PY=` / `SHIM=`
# constant lines are textually substituted with fakes (the wrapper has no
# override knob). Every fake in PATH (`python3`, `python`, `uv`) touches a marker,
# so a fallback interpreter would be caught.

WRAPPER_PATH = REPO_ROOT / "scripts/node22_smtp_sendmail.sh"
NODE22_CHECKOUT = "/scratch/frd_muziyao/NWM/"
WRAPPER_PY_LINE = "PY=/scratch/frd_muziyao/NWM/.venv/bin/python"
WRAPPER_SHIM_LINE = "SHIM=/scratch/frd_muziyao/NWM/scripts/node27_frontier_smtp_sendmail.py"
WRAPPER_USAGE_RC = 64
WRAPPER_FORBIDDEN_TOKENS = re.compile(r"python3|(?<![A-Za-z0-9_])uv(?![A-Za-z0-9_])|NHMS_SMTP_")
# CRLF, a non-UTF-8 byte and no trailing newline: any re-encoding or line
# discipline in the pass-through shows up as a byte mismatch.
WRAPPER_STDIN = b"From: a@example.invalid\r\nTo: b@example.invalid\r\n\r\nbody \xff\xfe tail"
DECOYS = ("python3", "python", "uv")


def forbidden_wrapper_tokens(text: str) -> list[str]:
    return WRAPPER_FORBIDDEN_TOKENS.findall(text)


def _wrapper_copy(tmp_path: Path, *, py: Path, shim: Path) -> Path:
    text = read_text(WRAPPER_PATH)
    assert text.splitlines().count(WRAPPER_PY_LINE) == 1
    assert text.splitlines().count(WRAPPER_SHIM_LINE) == 1
    lines = [
        f"PY={py}" if line == WRAPPER_PY_LINE else f"SHIM={shim}" if line == WRAPPER_SHIM_LINE else line
        for line in text.splitlines()
    ]
    copy = tmp_path / "node22_smtp_sendmail.sh"
    copy.write_text("\n".join(lines) + "\n", encoding="utf-8")
    copy.chmod(0o755)
    return copy


@pytest.fixture
def transport(tmp_path: Path) -> Path:
    """Fake interpreter (records argv/stdin/env, exits 69), a shim file and PATH decoys."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in DECOYS:
        _write_executable(bin_dir / name, f'printf "%s\\n" "{name}" >> "{tmp_path / "decoy.calls"}"\nexit 0\n')
    _write_executable(
        tmp_path / "fake-python",
        f'printf "%s\\n" "$@" > "{tmp_path / "py.argv"}"\n'
        f'cat > "{tmp_path / "py.stdin"}"\n'
        f'printf "%s" "${{NHMS_SMTP_PASS-unset}}" > "{tmp_path / "py.env"}"\n'
        "exit 69\n",
    )
    (tmp_path / "fake-shim.py").write_text("# never executed by the fake interpreter\n", encoding="utf-8")
    return tmp_path


def _run_wrapper(tmp_path: Path, wrapper: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "NHMS_SMTP_PASS": "pass-through-sentinel",
    }
    # By path, not `bash <path>`: the copy's exec bit is part of the contract.
    return subprocess.run(
        [str(wrapper), *args], input=WRAPPER_STDIN, env=env, capture_output=True, timeout=60, check=False
    )


def _nothing_invoked(tmp_path: Path) -> bool:
    return not (tmp_path / "py.argv").exists() and not (tmp_path / "decoy.calls").exists()


def test_wrapper_is_tracked_executable_with_exact_constants() -> None:
    text = read_text(WRAPPER_PATH)
    lines = text.splitlines()

    assert os.access(WRAPPER_PATH, os.X_OK)
    assert lines[0] == "#!/usr/bin/env bash"
    assert lines.count(WRAPPER_PY_LINE) == 1
    assert lines.count(WRAPPER_SHIM_LINE) == 1
    assert sum(line.startswith(("PY=", "SHIM=")) for line in lines) == 2
    assert lines.count('exec "$PY" "$SHIM" "$@"') == 1
    assert forbidden_wrapper_tokens(text) == []
    staged = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts/node22_smtp_sendmail.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout.split()[:1] == ["100755"], staged.stdout


def test_wrapper_shim_constant_names_a_tracked_file() -> None:
    relative = WRAPPER_SHIM_LINE.removeprefix("SHIM=").removeprefix(NODE22_CHECKOUT)

    assert relative == "scripts/node27_frontier_smtp_sendmail.py"
    assert (REPO_ROOT / relative).is_file()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr


def test_wrapper_token_rule_bites() -> None:
    assert forbidden_wrapper_tokens("exec /usr/bin/env python3 x") == ["python3"]
    assert forbidden_wrapper_tokens("exec uv run python x") == ["uv"]
    assert forbidden_wrapper_tokens('echo "$NHMS_SMTP_PASS"') == ["NHMS_SMTP_"]
    assert forbidden_wrapper_tokens("PY=/scratch/frd_muziyao/NWM/.venv/bin/python # uvloop") == []


def test_wrapper_forwards_argv_stdin_env_and_exit_status(transport: Path) -> None:
    shim = transport / "fake-shim.py"
    wrapper = _wrapper_copy(transport, py=transport / "fake-python", shim=shim)

    result = _run_wrapper(transport, wrapper, "-t", "-i")

    assert result.returncode == 69
    assert result.stdout == b""
    assert result.stderr == b""
    assert (transport / "py.argv").read_text(encoding="utf-8").splitlines() == [str(shim), "-t", "-i"]
    assert (transport / "py.stdin").read_bytes() == WRAPPER_STDIN
    assert (transport / "py.env").read_text(encoding="utf-8") == "pass-through-sentinel"
    assert not (transport / "decoy.calls").exists()


@pytest.mark.parametrize("interpreter", ("absent", "mode-0644"))
def test_wrapper_without_usable_interpreter_fails_closed(transport: Path, interpreter: str) -> None:
    py = transport / "fake-python"
    if interpreter == "absent":
        py = transport / "no-such-python"
    else:
        py.chmod(0o644)
    wrapper = _wrapper_copy(transport, py=py, shim=transport / "fake-shim.py")

    result = _run_wrapper(transport, wrapper, "-t", "-i")

    assert result.returncode == WRAPPER_USAGE_RC
    assert result.stdout == b""
    assert result.stderr.decode() == f"node22-smtp-sendmail: INTERPRETER_MISSING path={py}\n"
    assert _nothing_invoked(transport)


def test_wrapper_without_shim_fails_closed(transport: Path) -> None:
    shim = transport / "no-such-shim.py"
    wrapper = _wrapper_copy(transport, py=transport / "fake-python", shim=shim)

    result = _run_wrapper(transport, wrapper, "-t", "-i")

    assert result.returncode == WRAPPER_USAGE_RC
    assert result.stdout == b""
    assert result.stderr.decode() == f"node22-smtp-sendmail: SHIM_MISSING path={shim}\n"
    assert _nothing_invoked(transport)


def test_example_points_the_smtps_path_at_the_wrapper() -> None:
    text = read_text(EXAMPLE_PATH)
    comments = " ".join(line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#"))

    assert "/scratch/frd_muziyao/NWM/scripts/node22_smtp_sendmail.sh" in comments
    assert "does not deliver" not in comments
    assert "NHMS_ALERT_EMAIL_FROM must equal NHMS_SMTP_USER" in comments
