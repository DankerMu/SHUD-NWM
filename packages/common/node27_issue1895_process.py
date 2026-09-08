"""Exact systemd unit/MainPID/cgroup process ownership for G4/G7."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from packages.common.node27_cold_tablespace_topology import WRITER_TIMER_UNITS
from packages.common.node27_issue1895_types import Issue1895ReadinessError

DISPLAY_API_UNIT = "nhms-display-api.service"
QUIESCED_UNITS: tuple[str, ...] = WRITER_TIMER_UNITS + (
    "nhms-node27-resource-governance.service",
    "nhms-node27-resource-governance.timer",
    "nhms-node27-timeseries-compression-replay.service",
)

_REQUIRED_SHOW_KEYS = ("MainPID", "ActiveState", "ControlGroup", "Id")


def parse_main_pid(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        if isinstance(raw, str) and raw.isdigit():
            value = int(raw)
        else:
            raise Issue1895ReadinessError(
                "unit MainPID is not a canonical decimal",
                code="PROCESS_PID_INVALID",
                stage="process",
            )
    else:
        value = raw
    if value < 0:
        raise Issue1895ReadinessError(
            "unit MainPID is negative",
            code="PROCESS_PID_INVALID",
            stage="process",
        )
    return value


def _require_show(show: Mapping[str, Any], unit: str) -> dict[str, Any]:
    if not isinstance(show, Mapping):
        raise Issue1895ReadinessError(
            f"{unit} systemd show is missing",
            code="PROCESS_SHOW_INVALID",
            stage="process",
        )
    missing = [key for key in _REQUIRED_SHOW_KEYS if key not in show]
    if missing:
        raise Issue1895ReadinessError(
            f"{unit} systemd show is missing required fields",
            code="PROCESS_SHOW_INVALID",
            stage="process",
        )
    identity = str(show.get("Id") or "")
    if identity != unit:
        raise Issue1895ReadinessError(
            f"{unit} systemd show Id does not match the requested unit",
            code="PROCESS_UNIT_MISMATCH",
            stage="process",
        )
    return dict(show)


def assert_unit_quiesced(unit: str, show: Mapping[str, Any]) -> None:
    """Refuse any live MainPID. Editors/wrappers matching a script name never count."""

    document = _require_show(show, unit)
    pid = parse_main_pid(document["MainPID"])
    active = str(document.get("ActiveState") or "")
    if pid != 0:
        raise Issue1895ReadinessError(
            f"{unit} still has a MainPID",
            code="PROCESS_NOT_QUIESCED",
            stage="process",
        )
    if active not in {"inactive", "dead"}:
        raise Issue1895ReadinessError(
            f"{unit} is not inactive",
            code="PROCESS_NOT_QUIESCED",
            stage="process",
        )


def assert_exact_unit_pid(
    unit: str,
    show: Mapping[str, Any],
    *,
    cgroup_text: str,
    expected_cgroup_needle: str | None = None,
) -> int:
    """Accept exactly one live MainPID owned by the unit cgroup; reject 0/stale/mismatch."""

    document = _require_show(show, unit)
    pid = parse_main_pid(document["MainPID"])
    if pid == 0:
        raise Issue1895ReadinessError(
            f"{unit} MainPID is zero",
            code="PROCESS_PID_ZERO",
            stage="process",
        )
    needle = expected_cgroup_needle if expected_cgroup_needle is not None else unit
    control_group = str(document.get("ControlGroup") or "")
    if needle not in control_group:
        raise Issue1895ReadinessError(
            f"{unit} ControlGroup does not name the unit",
            code="PROCESS_CGROUP_MISMATCH",
            stage="process",
        )
    if not isinstance(cgroup_text, str) or needle not in cgroup_text:
        raise Issue1895ReadinessError(
            f"{unit} process cgroup does not name the unit",
            code="PROCESS_CGROUP_MISMATCH",
            stage="process",
        )
    return pid


def assert_writers_quiesced(shows: Mapping[str, Mapping[str, Any]]) -> None:
    missing = [unit for unit in QUIESCED_UNITS if unit not in shows]
    if missing:
        raise Issue1895ReadinessError(
            "quiescence show is missing a required unit",
            code="PROCESS_SHOW_INVALID",
            stage="process",
        )
    extra = [unit for unit in shows if unit not in QUIESCED_UNITS]
    if extra:
        raise Issue1895ReadinessError(
            "quiescence show includes an unexpected unit",
            code="PROCESS_SHOW_INVALID",
            stage="process",
        )
    for unit in QUIESCED_UNITS:
        assert_unit_quiesced(unit, shows[unit])


def resolve_display_api_pid(
    show: Mapping[str, Any],
    *,
    read_cgroup: Callable[[int], str],
) -> int:
    pid = parse_main_pid(_require_show(show, DISPLAY_API_UNIT)["MainPID"])
    try:
        cgroup_text = read_cgroup(pid)
    except Issue1895ReadinessError:
        raise
    except Exception:
        raise Issue1895ReadinessError(
            "display API cgroup is unreadable",
            code="PROCESS_CGROUP_UNREADABLE",
            stage="process",
        ) from None
    return assert_exact_unit_pid(DISPLAY_API_UNIT, show, cgroup_text=cgroup_text)
