"""Shared fakes and harnesses of the node-22 refresh-timer health probe suites.

Non-collectible support module (#2532 partition of the 3456-line / 202-case
tests/test_node22_refresh_timer_health.py).
It holds the definitions more than one partition consumes: the pinned clock and unit constants, the fake
``systemctl`` shim, the refresh-receipt builders and the ``_run`` / ``_verdict``
probe drivers; the installer harness (``INSTALLER``, ``PROTECTED_UNITS``,
``PROBE_UNITS``, the installer fake ``systemctl``, ``_run_installer``,
``_probe_timer_state``); and the probe runbook section reader (``RUNBOOK``,
``PROBE_TIMER_UNIT``, ``_probe_runbook_section``).

Repo-root resolution stays ``Path(__file__).resolve().parents[1]``: this module
sits directly under ``tests/`` exactly as the monolith did. Nothing here is a
monkeypatch target -- every ``monkeypatch.setattr`` in the corpus names the
production probe module (``scripts.node22_refresh_timer_health``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import node22_refresh_timer_health as probe

UNIT = "nhms-scheduler-file-provider-refresh.timer"
NOW = datetime(2026, 9, 12, 15, 34, tzinfo=UTC)
PROBE_SOURCE = Path(probe.__file__)

# The verbs the probe must never contain, matched on word boundaries so that
# the verdict names (`timer_stopped`, `timer_not_enabled`) and the threshold
# env name (`..._STOPPED_DWELL_HOURS`) are not false positives.
MUTATION_VERBS = re.compile(
    r"\b(start|stop|enable|disable|restart|daemon-reload)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _systemd_timestamp(moment: datetime) -> str:
    """Render an instant the way ``systemctl show`` does.

    Built in the *local* zone so the string round-trips through the probe's
    parser on any host, exactly as it does on node-22.
    """
    return moment.astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")


def _write_fake_systemctl(
    tmp_path: Path,
    *,
    properties: dict[str, str],
    exit_code: int = 0,
    failing_subcommand: str = "",
) -> tuple[Path, Path]:
    """Create a fake ``systemctl`` that logs every invocation it receives.

    ``exit_code`` fails every invocation; ``failing_subcommand`` fails exactly
    one of the two read-only subcommands, which is what lets a test distinguish
    "``show`` was never readable" from "``show`` answered and the NON-GRADED
    ``list-timers`` call is what failed".
    """
    log = tmp_path / "systemctl.log"
    show_output = "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n"
    payload = tmp_path / "show.txt"
    payload.write_text(show_output)
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f"if [ {exit_code} -ne 0 ]; then exit {exit_code}; fi\n"
        f'failing="{failing_subcommand}"\n'
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        f'    show) [ "$failing" = show ] && exit 7; cat {payload}; exit 0 ;;\n'
        "    list-timers) [ \"$failing\" = list-timers ] && exit 7; "
        "printf 'NEXT LEFT LAST PASSED UNIT ACTIVATES\\n'; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _properties(
    *,
    unit_file_state: str = "enabled",
    active_state: str = "active",
    sub_state: str = "waiting",
    inactive_enter: str | None = None,
    next_elapse: str | None = None,
    last_trigger: str | None = None,
) -> dict[str, str]:
    return {
        "UnitFileState": unit_file_state,
        "ActiveState": active_state,
        "SubState": sub_state,
        "InactiveEnterTimestamp": (
            _systemd_timestamp(NOW - timedelta(days=15)) if inactive_enter is None else inactive_enter
        ),
        "NextElapseUSecRealtime": (
            _systemd_timestamp(NOW + timedelta(hours=11)) if next_elapse is None else next_elapse
        ),
        "LastTriggerUSec": (
            _systemd_timestamp(NOW - timedelta(hours=13)) if last_trigger is None else last_trigger
        ),
    }


def _refresh_receipt_payload(
    *,
    manifest_age_hours: float | None = 5.0,
    schema_version: str = probe.REFRESH_RECEIPT_SCHEMA_VERSION,
    providers: list[dict[str, object]] | None = None,
    anchor: datetime = NOW,
    outcome: str = "published",
) -> dict[str, object]:
    if providers is None:
        generated_at = (anchor - timedelta(hours=manifest_age_hours or 0.0)).isoformat().replace(
            "+00:00", "Z"
        )
        providers = [
            {"name": "registry", "after_generated_at": generated_at, "entry_count": 18},
            {"name": "registry_worker_mirror", "after_generated_at": generated_at},
            {"name": "readiness", "after_generated_at": generated_at},
            {"name": "state", "after_generated_at": generated_at},
        ]
    return {"schema_version": schema_version, "outcome": outcome, "providers": providers}


def _write_refresh_receipt(
    tmp_path: Path,
    *,
    manifest_age_hours: float | None = 5.0,
    schema_version: str = probe.REFRESH_RECEIPT_SCHEMA_VERSION,
    providers: list[dict[str, object]] | None = None,
    raw: str | None = None,
    anchor: datetime = NOW,
    name: str = "refresh-latest.json",
) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw)
        return path
    path.write_text(
        json.dumps(
            _refresh_receipt_payload(
                manifest_age_hours=manifest_age_hours,
                schema_version=schema_version,
                providers=providers,
                anchor=anchor,
            )
        )
    )
    return path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    properties: dict[str, str] | None = None,
    systemctl_exit_code: int = 0,
    systemctl_path: str | None = None,
    failing_subcommand: str = "",
    receipt: Path | None = None,
    receipt_root: Path | None = None,
    thresholds: dict[str, str] | None = None,
    environment: dict[str, str] | None = None,
    now: datetime | None = NOW,
    extra_argv: list[str] | None = None,
) -> tuple[int, Path, Path]:
    """Run the probe end-to-end; return (exit status, receipt root, call log).

    ``now=None`` omits ``--now`` entirely, so the probe runs on its real clock
    -- the only shape the shipped unit ever uses, since the clock has no
    environment seam (B1 / design D4).
    """
    script, log = _write_fake_systemctl(
        tmp_path,
        properties=properties if properties is not None else _properties(),
        exit_code=systemctl_exit_code,
        failing_subcommand=failing_subcommand,
    )
    monkeypatch.setenv(
        probe.ENV_SYSTEMCTL, systemctl_path if systemctl_path is not None else str(script)
    )
    for name in (
        probe.ENV_MAX_NEXT_DWELL_HOURS,
        probe.ENV_MAX_MANIFEST_AGE_HOURS,
        probe.ENV_STOPPED_DWELL_HOURS,
        probe.ENV_UNIT,
        probe.ENV_REFRESH_RECEIPT,
        probe.ENV_HEALTH_RECEIPT_ROOT,
        probe.ENV_JSON,
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in {**(thresholds or {}), **(environment or {})}.items():
        monkeypatch.setenv(key, value)
    root = receipt_root if receipt_root is not None else tmp_path / "health-receipts"
    refresh_receipt = receipt if receipt is not None else _write_refresh_receipt(tmp_path)
    argv = [
        "--unit",
        UNIT,
        "--refresh-receipt",
        str(refresh_receipt),
        "--health-receipt-root",
        str(root),
        *([] if now is None else ["--now", now.isoformat().replace("+00:00", "Z")]),
        *(extra_argv or []),
    ]
    return probe.main(argv), root, log


def _verdict(root: Path) -> str:
    return json.loads((root / "latest.json").read_text())["verdict"]


INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install_node22_refresh_timer_health.sh"
PROTECTED_UNITS = (
    "nhms-compute-scheduler.timer",
    "nhms-compute-scheduler.service",
    "nhms-scheduler-file-provider-refresh.timer",
    "nhms-scheduler-file-provider-refresh.service",
)
PROBE_UNITS = (
    "nhms-node22-refresh-timer-health.service",
    "nhms-node22-refresh-timer-health.timer",
)


def _installer_fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    """A fake systemctl that answers the read-only queries and logs mutations.

    Two capabilities the R15 regression needs (C2):

    * the probe's OWN units carry real state, so an `enable --now` followed by a
      restoring `disable`/`stop` is observable rather than a no-op;
    * ``NHMS_FAKE_DIVERGE`` / ``NHMS_FAKE_DIVERGE_VALUE`` make ONE
      ``<unit>.<query>`` pair answer differently from its SECOND call onwards,
      which is exactly the "the protected units moved under us" shape
      `assert_protected_unchanged` exists to catch;
    * ``NHMS_FAKE_FAIL_VERB`` makes every call of one mutating verb exit 1
      without changing state, which reaches an action's ERR trap BEFORE its
      main protected-unit assertion -- so the trap's own assertion is the only
      protected re-read the invocation makes;
    * ``NHMS_FAKE_PROBE_IS_ENABLED`` / ``NHMS_FAKE_PROBE_IS_ACTIVE``, when SET
      (the empty string included), are the literal answer to that query for
      both probe units -- the states a real user manager reports that the
      stateful fake never produces (`static`, `not-found`, `failed`, no answer);
    * ``NHMS_FAKE_PROBE_TIMER_IS_*`` / ``NHMS_FAKE_PROBE_SERVICE_IS_*`` do the
      same for ONE probe unit and win over the shared knob, so the timer and
      the service can answer differently.
    """
    log = tmp_path / "installer-systemctl.log"
    state = tmp_path / "fake-state"
    state.mkdir(exist_ok=True)
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f'state={state}\n'
        "shift\n"  # drop --user
        "verb=$1\n"
        "shift\n"
        '[ "$verb" = "${NHMS_FAKE_FAIL_VERB:-}" ] && exit 1\n'
        "now=no\n"
        "unit=\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    --now) now=yes ;;\n"
        "    -*) ;;\n"
        "    *) unit=$arg ;;\n"
        "  esac\n"
        "done\n"
        'enabled=$(cat "$state/probe.enabled" 2>/dev/null || printf disabled)\n'
        'active=$(cat "$state/probe.active" 2>/dev/null || printf inactive)\n'
        "case \"$verb\" in\n"
        '  enable) enabled=enabled; [ "$now" = yes ] && active=active ;;\n'
        '  disable) enabled=disabled; [ "$now" = yes ] && active=inactive ;;\n'
        "  start) active=active ;;\n"
        "  stop) active=inactive ;;\n"
        "esac\n"
        'printf "%s" "$enabled" > "$state/probe.enabled"\n'
        'printf "%s" "$active" > "$state/probe.active"\n'
        'case "$verb" in\n'
        "  is-enabled|is-active) ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
        'case "$unit" in\n'
        "  nhms-node22-refresh-timer-health.*)\n"
        r"""    kind=SERVICE; case "$unit" in *.timer) kind=TIMER ;; esac
    query=IS_ENABLED; [ "$verb" = is-active ] && query=IS_ACTIVE
    for name in "NHMS_FAKE_PROBE_${kind}_${query}" "NHMS_FAKE_PROBE_${query}"; do
      eval "isset=\${$name+set}"
      if [ -n "$isset" ]; then eval "printf '%s\n' \"\$$name\""; exit 0; fi
    done
"""
        '    if [ "$verb" = is-enabled ]; then printf "%s\\n" "$enabled"; '
        'else printf "%s\\n" "$active"; fi\n'
        "    exit 0 ;;\n"
        "esac\n"
        'value=enabled\n'
        '[ "$verb" = is-active ] && value=active\n'
        'key=$(printf "%s" "$unit.$verb" | tr -c "a-zA-Z0-9._-" "_")\n'
        'count=$(cat "$state/$key" 2>/dev/null || printf 0)\n'
        'count=$((count + 1))\n'
        'printf "%s" "$count" > "$state/$key"\n'
        'if [ "$unit.$verb" = "${NHMS_FAKE_DIVERGE:-}" ] && [ "$count" -ge 2 ]; then\n'
        '  value=${NHMS_FAKE_DIVERGE_VALUE:-}\n'
        "fi\n"
        'printf "%s\\n" "$value"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _run_installer(
    tmp_path: Path,
    action: str,
    *,
    diverge: str = "",
    diverge_value: str = "",
    fail_verb: str = "",
    probe_answers: dict[str, str] | None = None,
    timer_answers: dict[str, str] | None = None,
    service_answers: dict[str, str] | None = None,
    installer: Path = INSTALLER,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    script, log = _installer_fake_systemctl(tmp_path)
    # Divergence is scoped to ONE installer invocation: the per-(unit, query)
    # counters reset so the "before" snapshot is call 1 and the assertion's
    # re-read is call 2, which is the real mid-run flip being modelled.
    for counter in (tmp_path / "fake-state").iterdir():
        if counter.name not in {"probe.enabled", "probe.active"}:
            counter.unlink()
    environment = dict(os.environ)
    environment.update(
        {
            "NHMS_REFRESH_HEALTH_REPO": str(Path(__file__).resolve().parents[1]),
            "NHMS_REFRESH_HEALTH_UNIT_DIR": str(tmp_path / "units"),
            "NHMS_REFRESH_HEALTH_INSTALL_STATE_ROOT": str(tmp_path / "install-state"),
            "NHMS_REFRESH_HEALTH_RECEIPT_ROOT": str(tmp_path / "receipts"),
            "NHMS_REFRESH_HEALTH_SYSTEMCTL": str(script),
            "NHMS_FAKE_DIVERGE": diverge,
            "NHMS_FAKE_DIVERGE_VALUE": diverge_value,
            "NHMS_FAKE_FAIL_VERB": fail_verb,
        }
    )
    for scope in ("", "TIMER_", "SERVICE_"):
        for query in ("IS_ENABLED", "IS_ACTIVE"):
            environment.pop(f"NHMS_FAKE_PROBE_{scope}{query}", None)
    for scope, answers in (
        ("", probe_answers),
        ("TIMER_", timer_answers),
        ("SERVICE_", service_answers),
    ):
        for query, answer in (answers or {}).items():
            environment[f"NHMS_FAKE_PROBE_{scope}{query.upper().replace('-', '_')}"] = answer
    completed = subprocess.run(
        ["bash", str(installer), action],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    return completed, log


def _probe_timer_state(tmp_path: Path) -> tuple[str, str]:
    state = tmp_path / "fake-state"
    enabled = (state / "probe.enabled").read_text() if (state / "probe.enabled").exists() else "disabled"
    active = (state / "probe.active").read_text() if (state / "probe.active").exists() else "inactive"
    return enabled, active


# #1103 moved §3.1.2 (the refresh steady state, and with it the probe section)
# into its own sub-runbook; the index page carries only the section stub.
RUNBOOK = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "runbooks"
    / "production-ops"
    / "file-provider-refresh.md"
)
PROBE_TIMER_UNIT = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "systemd"
    / "nhms-node22-refresh-timer-health.timer"
)


def _probe_runbook_section() -> str:
    """The probe's own section of the runbook, sliced by its heading.

    #1103: the old terminator `#### 3.1.4` moved to a different sub-runbook, so
    the probe section now runs to the end of `file-provider-refresh.md`. The
    start heading is still resolved by `index`, so a missing section raises
    instead of yielding an empty (vacuously passing) slice.
    """
    text = RUNBOOK.read_text()
    start = text.index("##### refresh timer 健康探针")
    return text[start:]
