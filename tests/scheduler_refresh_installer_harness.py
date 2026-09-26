"""Fake-``systemctl`` harness of the refresh-installer failure-path suites (#2294).

Non-collectible support module shared by
``tests/test_scheduler_refresh_installer_failure_paths.py`` and
``tests/test_scheduler_refresh_installer_mutations.py``. It runs the real
``scripts/install_node22_scheduler_file_provider_refresh.sh`` (or a mutated
copy of it) under a bash >= 4 against:

* a stateful fake ``systemctl`` that answers ``is-enabled`` / ``is-active``
  with **real systemd exit codes** -- ``is-active`` exits 3 for anything not
  active, ``is-enabled`` exits 0 only for the enabled-like states (``enabled``,
  ``enabled-runtime``, ``static``, ``alias``, ``indirect``, ``generated``,
  ``transient``), 4 for ``not-found`` and 1 otherwise. A fake that always exits
  0 hides a failing command substitution, which is exactly the shape the
  exactly-one-restore scenarios exist to exercise;
* models ``static``: ``enable`` / ``disable`` leave a ``static`` unit static, as
  a real user manager does (node-22's refresh service baseline is ``static``);
* a fake receipt validator standing in for the ``--validate-current-receipt``
  process (the real validator is covered by the deployment-contract lifecycle
  case); ``validate_rc`` sets its exit status.

Every call is appended to the trace without ``--user``. The per-call knobs match
a call by its exact trace line, optionally ``@N`` for its N-th occurrence in
THIS installer invocation:

* ``fail`` -- exit 1 without changing state;
* ``noop`` -- exit 0 without changing state (a silent refusal);
* ``after`` -- once the matching call ran: ``set`` unit states, ``chmod`` a
  path, or ``write`` bytes to a path;
* ``flip`` -- each ``is-active`` query of the named unit toggles it first, a
  timer-driven oneshot firing between two reads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_node22_scheduler_file_provider_refresh.sh"
TIMER = "nhms-scheduler-file-provider-refresh.timer"
SERVICE = "nhms-scheduler-file-provider-refresh.service"
REFRESH_UNITS = (SERVICE, TIMER)
SCHEDULER_TIMER = "nhms-compute-scheduler.timer"
SCHEDULER_SERVICE = "nhms-compute-scheduler.service"
MUTATING_VERBS = frozenset({"enable", "disable", "start", "stop", "restart", "daemon-reload"})
STATUS = {
    "--install": '{"status":"installed_stopped","scheduler_unchanged":true}\n',
    "--enable": '{"status":"enabled_active","scheduler_unchanged":true}\n',
    "--rollback": '{"status":"rolled_back","scheduler_unchanged":true}\n',
}
# node-22's live install state, byte-exact (design Context, 2026-09-25).
NODE22_REFRESH_BEFORE = b"disabled\tinactive\nstatic\tinactive\n"
NODE22_SCHEDULER_BEFORE = b"enabled\tinactivestatic\tinactive"
# The restore signature: `disable <refresh timer>` WITHOUT `--now` is issued only
# by a restore of a disarmed target, never by a main path.
RESTORE_SIGNATURE = f"disable {TIMER}"
REFRESH_READ_BACK = (f"is-enabled {TIMER}", f"is-active {TIMER}", f"is-enabled {SERVICE}")
SCHEDULER_READ_BACK = (
    f"is-enabled {SCHEDULER_TIMER}",
    f"is-active {SCHEDULER_TIMER}",
    f"is-enabled {SCHEDULER_SERVICE}",
)

_FAKE_SYSTEMCTL = r"""
import json, os, sys

state_path = os.environ["FAKE_SYSTEMCTL_STATE"]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
config = json.loads(os.environ.get("FAKE_SYSTEMCTL_CONFIG") or "{}")
args = [arg for arg in sys.argv[1:] if arg != "--user"]
line = " ".join(args)
with open(os.environ["FAKE_SYSTEMCTL_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(line + "\n")
counts = state.setdefault("counts", {})
counts[line] = counts.get(line, 0) + 1
ordinal = counts[line]
units = state.setdefault("units", {})
verb = args[0] if args else ""
names = [arg for arg in args[1:] if not arg.startswith("-")]
unit = names[-1] if names else ""


def matches(spec):
    target, _, nth = spec.partition("@")
    return target == line and (not nth or int(nth) == ordinal)


def current(name):
    return units.setdefault(name, {"enabled": "disabled", "active": "inactive"})


ENABLED_OK = {"enabled", "enabled-runtime", "static", "alias", "indirect", "generated", "transient"}
ACTIVE_OK = {"active", "reloading", "refreshing"}
rc = 0
if any(matches(spec) for spec in config.get("fail", [])):
    rc = 1
    print(f"fake systemctl: {line}: injected failure", file=sys.stderr)
elif any(matches(spec) for spec in config.get("noop", [])):
    pass
elif verb == "is-enabled":
    value = current(unit)["enabled"]
    print(value)
    rc = 0 if value in ENABLED_OK else (4 if value == "not-found" else 1)
elif verb == "is-active":
    if unit in config.get("flip", []):
        current(unit)["active"] = "inactive" if current(unit)["active"] == "active" else "active"
    value = current(unit)["active"]
    print(value)
    rc = 0 if value in ACTIVE_OK else 3
elif verb in {"enable", "disable"}:
    entry = current(unit)
    if entry["enabled"] != "static":
        entry["enabled"] = "enabled" if verb == "enable" else "disabled"
    if "--now" in args:
        entry["active"] = "active" if verb == "enable" else "inactive"
elif verb in {"start", "stop"}:
    current(unit)["active"] = "active" if verb == "start" else "inactive"
elif verb != "daemon-reload":
    rc = 2
for action in config.get("after", []):
    if matches(action["on"]):
        for name, values in action.get("set", {}).items():
            current(name).update(values)
        if "chmod" in action:
            os.chmod(action["chmod"][0], int(action["chmod"][1]))
        if "write" in action:
            with open(action["write"][0], "w", encoding="utf-8") as handle:
                handle.write(action["write"][1])
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump(state, handle)
sys.exit(rc)
"""


def _bash_major(executable: str) -> int | None:
    try:
        probe = subprocess.run(
            [executable, "-c", 'echo "${BASH_VERSINFO[0]}"'], check=False, capture_output=True, text=True
        )
    except OSError:
        return None
    try:
        return int(probe.stdout.strip()) if probe.returncode == 0 else None
    except ValueError:
        return None


def require_modern_bash() -> str:
    """A bash >= 4: the installers' main-shell guard reads ``$BASHPID``."""
    candidates = [shutil.which("bash") or "", "/bin/bash", "/usr/bin/bash", "/opt/homebrew/bin/bash"]
    for candidate in dict.fromkeys(path for path in candidates if path):
        version = _bash_major(candidate)
        if version is not None and version >= 4:
            return candidate
    pytest.skip(f"requires bash >= 4 ($BASHPID in the installer's main-shell guard); probed: {candidates}")


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    trace: list[str]

    def mutating(self) -> list[str]:
        return [line for line in self.trace if line.split()[0] in MUTATING_VERBS]

    def after_last_mutation(self) -> list[str]:
        indexes = [index for index, line in enumerate(self.trace) if line.split()[0] in MUTATING_VERBS]
        return self.trace[indexes[-1] + 1 :] if indexes else list(self.trace)

    def restores(self) -> int:
        return self.trace.count(RESTORE_SIGNATURE)


@dataclass
class Rig:
    root: Path
    bash: str

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def unit_dir(self) -> Path:
        return self.root / "units"

    @property
    def state_root(self) -> Path:
        return self.root / "install-state"

    @property
    def env_file(self) -> Path:
        return self.repo / "infra" / "env" / "compute.scheduler-provider-refresh.env"

    @property
    def _state_file(self) -> Path:
        return self.root / "fake-systemctl-state.json"

    def units(self) -> dict[str, dict[str, str]]:
        return json.loads(self._state_file.read_text())["units"]

    def unit(self, name: str) -> tuple[str, str]:
        entry = self.units().get(name, {"enabled": "disabled", "active": "inactive"})
        return entry["enabled"], entry["active"]

    def set_unit(self, name: str, enabled: str, active: str) -> None:
        state = json.loads(self._state_file.read_text())
        state["units"][name] = {"enabled": enabled, "active": active}
        self._state_file.write_text(json.dumps(state))

    def snapshot(self) -> dict[str, bytes]:
        """Every file under the state root and the unit dir, by relative path."""
        files: dict[str, bytes] = {}
        for base in (self.state_root, self.unit_dir):
            if base.exists():
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        files[str(path.relative_to(self.root))] = path.read_bytes()
        return files

    def mutated_installer(self, *edits: tuple[str, str]) -> Path:
        """A copy of the installer with each exact anchor replaced once."""
        source = INSTALLER.read_text()
        for anchor, replacement in edits:
            assert source.count(anchor) == 1, f"mutation anchor not unique/present: {anchor!r}"
            source = source.replace(anchor, replacement)
        copy = self.root / f"installer-mutant-{len(list(self.root.glob('installer-mutant-*')))}.sh"
        copy.write_text(source)
        return copy

    def run(
        self,
        action: str,
        *,
        fail: Iterable[str] = (),
        noop: Iterable[str] = (),
        after: Sequence[dict[str, object]] = (),
        flip: Iterable[str] = (),
        validate_rc: int = 0,
        installer: Path | None = None,
    ) -> Result:
        state = json.loads(self._state_file.read_text())
        state["counts"] = {}
        self._state_file.write_text(json.dumps(state))
        trace = self.root / "fake-systemctl-trace.log"
        trace.write_text("")
        environment = {
            **os.environ,
            "NHMS_SCHEDULER_REFRESH_REPO": str(self.repo),
            "NHMS_SCHEDULER_REFRESH_UNIT_DIR": str(self.unit_dir),
            "NHMS_SCHEDULER_REFRESH_INSTALL_STATE_ROOT": str(self.state_root),
            "NHMS_SCHEDULER_REFRESH_SYSTEMCTL": str(self.root / "fake-systemctl"),
            "NHMS_SCHEDULER_REFRESH_PYTHON": str(self.root / "fake-validator"),
            "NHMS_SCHEDULER_REFRESH_RECEIPT": str(self.root / "latest.json"),
            "FAKE_SYSTEMCTL_STATE": str(self._state_file),
            "FAKE_SYSTEMCTL_TRACE": str(trace),
            "FAKE_SYSTEMCTL_CONFIG": json.dumps(
                {"fail": list(fail), "noop": list(noop), "after": list(after), "flip": list(flip)}
            ),
            "FAKE_VALIDATE_RC": str(validate_rc),
        }
        completed = subprocess.run(
            [self.bash, str(installer or INSTALLER), action],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        return Result(completed.returncode, completed.stdout, completed.stderr, trace.read_text().splitlines())


def make_rig(tmp_path: Path) -> Rig:
    """A repo checkout with the real refresh units, a mode-0600 env file, and a
    user manager where the compute scheduler is armed (timer enabled/active,
    oneshot static/inactive) and the refresh service is ``static``."""
    rig = Rig(tmp_path, require_modern_bash())
    systemd = rig.repo / "infra" / "systemd"
    systemd.mkdir(parents=True)
    for unit in REFRESH_UNITS:
        shutil.copy2(ROOT / "infra" / "systemd" / unit, systemd / unit)
    rig.env_file.parent.mkdir(parents=True)
    write_env(rig, ["NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true"])
    validator = tmp_path / "fake-validator"
    validator.write_text('#!/bin/sh\nexit "${FAKE_VALIDATE_RC:-0}"\n')
    validator.chmod(0o755)
    fake = tmp_path / "fake-systemctl"
    fake.write_text(f"#!{sys.executable}\n{_FAKE_SYSTEMCTL}")
    fake.chmod(0o755)
    rig._state_file.write_text(
        json.dumps(
            {
                "units": {
                    SCHEDULER_TIMER: {"enabled": "enabled", "active": "active"},
                    SCHEDULER_SERVICE: {"enabled": "static", "active": "inactive"},
                    SERVICE: {"enabled": "static", "active": "inactive"},
                },
                "counts": {},
            }
        )
    )
    return rig


def write_env(rig: Rig, lines: Sequence[str], *, mode: int = 0o600) -> None:
    rig.env_file.write_text("\n".join(("NHMS_BASINS_ROOT=/trusted/Basins", *lines)) + "\n")
    rig.env_file.chmod(mode)


def installed_and_armed(rig: Rig) -> None:
    """Install, then enable: the steady state node-22 is in."""
    installed = rig.run("--install")
    assert installed.stdout == STATUS["--install"], installed.stderr
    enabled = rig.run("--enable")
    assert enabled.stdout == STATUS["--enable"], enabled.stderr
    assert rig.unit(TIMER) == ("enabled", "active")


def set_on(on: str, unit: str, enabled: str, active: str) -> dict[str, object]:
    """An ``after`` action: once call ``on`` ran, ``unit`` reads ``enabled/active``."""
    return {"on": on, "set": {unit: {"enabled": enabled, "active": active}}}


def is_subsequence(needles: Sequence[str], haystack: Sequence[str]) -> bool:
    iterator = iter(haystack)
    return all(any(line == needle for line in iterator) for needle in needles)
