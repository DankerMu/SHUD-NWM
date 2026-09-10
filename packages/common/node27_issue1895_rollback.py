"""Rollback table/state machine: never read UNITS_ENABLE_STATE before G4 creates it."""

from __future__ import annotations

from typing import Any

from packages.common.node27_issue1895_types import Issue1895ReadinessError

POINT_BEFORE_MUTATION = "before_any_mutation"
POINT_AFTER_DRAIN = "after_writer_drain"
POINT_AFTER_ASSEMBLY = "after_env_unit_assembly"
POINT_INSTALL_IN_PROGRESS = "install_in_progress"
POINT_TERMINAL_INSTALLED = "terminal_installed"
POINT_AFTER_MOVEMENT = "after_any_group_moved"

LEGAL_POINTS: tuple[str, ...] = (
    POINT_BEFORE_MUTATION,
    POINT_AFTER_DRAIN,
    POINT_AFTER_ASSEMBLY,
    POINT_INSTALL_IN_PROGRESS,
    POINT_TERMINAL_INSTALLED,
    POINT_AFTER_MOVEMENT,
)


def parse_enablement_rows(text: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        unit, separator, state = line.partition(" ")
        if not separator or not unit or not state:
            raise Issue1895ReadinessError(
                "enablement row is malformed",
                code="ROLLBACK_ROW_INVALID",
                stage="rollback",
            )
        if unit in seen:
            raise Issue1895ReadinessError(
                "enablement row is duplicated",
                code="ROLLBACK_ROW_DUPLICATE",
                stage="rollback",
            )
        seen.add(unit)
        rows.append((unit, state))
    return tuple(rows)


def rollback_action(
    point: str,
    *,
    units_enable_state_exists: bool,
    enablement_text: str | None = None,
) -> dict[str, Any]:
    """Map a rollback-table row onto an explicit, testable action.

    Before any mutation (and before G4 creates ``$UNITS_ENABLE_STATE``) the
    rollback is a no-op: the file must not be referenced. After drain and
    before movement, restore exactly the captured enable/active state.
    After movement use the verified per-group three-state reverse path
    (stop and preserve; no move-back).
    """

    if point not in LEGAL_POINTS:
        raise Issue1895ReadinessError(
            "rollback point is unknown",
            code="ROLLBACK_POINT_INVALID",
            stage="rollback",
        )
    if point == POINT_BEFORE_MUTATION:
        if units_enable_state_exists:
            raise Issue1895ReadinessError(
                "UNITS_ENABLE_STATE must not exist before G4 creates it",
                code="ROLLBACK_STATE_PREMATURE",
                stage="rollback",
            )
        return {
            "point": point,
            "action": "no-op",
            "restore_units": (),
            "reference_units_enable_state": False,
        }
    if not units_enable_state_exists or enablement_text is None:
        raise Issue1895ReadinessError(
            "UNITS_ENABLE_STATE is required after the writer drain",
            code="ROLLBACK_STATE_MISSING",
            stage="rollback",
        )
    rows = parse_enablement_rows(enablement_text)
    if point in {POINT_AFTER_DRAIN, POINT_AFTER_ASSEMBLY}:
        return {
            "point": point,
            "action": "restore_captured_enablement",
            "restore_units": rows,
            "reference_units_enable_state": True,
        }
    if point == POINT_INSTALL_IN_PROGRESS:
        return {
            "point": point,
            "action": "reconcile_only",
            "restore_units": (),
            "reference_units_enable_state": True,
        }
    if point == POINT_TERMINAL_INSTALLED:
        return {
            "point": point,
            "action": "stop_and_preserve",
            "restore_units": (),
            "reference_units_enable_state": True,
        }
    return {
        "point": point,
        "action": "three_state_reverse_preserve",
        "restore_units": (),
        "reference_units_enable_state": True,
    }


def assert_row_state_consistency(
    *,
    point: str,
    units_enable_state_exists: bool,
    enablement_text: str | None,
    expected_action: str,
) -> None:
    observed = rollback_action(
        point,
        units_enable_state_exists=units_enable_state_exists,
        enablement_text=enablement_text,
    )
    if observed["action"] != expected_action:
        raise Issue1895ReadinessError(
            "rollback row does not match the captured state",
            code="ROLLBACK_INCONSISTENT",
            stage="rollback",
        )
