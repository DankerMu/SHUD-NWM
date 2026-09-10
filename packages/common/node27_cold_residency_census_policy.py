"""Checked canonical-decimal capacity policy for the #1895 pre-target census.

Purely policy: renders the E/S/rollback/installer-required byte values as
short canonical decimal strings inside the signed-bigint ceiling, and refuses
(``CensusPolicyError``, or any caller-supplied error type) instead of emitting
a value a second parser could read differently.  The census CLI passes its own
``CensusError`` as ``error_type`` so the CLI's historical public exception type
survives; this module itself imports no target, preflight, runtime, or probe
owner — exactly the import restriction the pre-target census must preserve.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Signed-bigint byte ceiling: every canonical decimal shipped to the installer
# argv or the runner env must stay inside it, so an overflow refuses rather than
# emitting a value a second parser could read differently.
MAX_BYTE_VALUE = 2**63 - 1


class CensusPolicyError(Exception):
    """A stable, non-secret capacity-policy refusal. Never carries a DSN."""

    def __init__(self, message: str, *, error_class: str = "capacity", stage: str = "policy") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.stage = stage


def _canonical_decimal(value: int, *, label: str, error_type: type[Exception]) -> str:
    """Render one non-negative byte count as a short canonical decimal string.

    A checked canonical decimal is what #1895 ships to the installer argv and the
    mode-0600 runner env: no sign, no whitespace, no base prefix, no grouping, and
    never a value a second parser could read differently.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{label} must be an integer byte count", error_class="capacity", stage="policy")
    if value < 0 or value > MAX_BYTE_VALUE:
        raise error_type(
            f"{label} is outside the canonical decimal byte range",
            error_class="capacity",
            stage="policy",
        )
    return str(value)


def _positive_decimal(value: int, *, label: str, error_type: type[Exception]) -> str:
    rendered = _canonical_decimal(value, label=label, error_type=error_type)
    if rendered == "0":
        raise error_type(f"{label} must be positive", error_class="capacity", stage="policy")
    return rendered


def _checked_add(left: int, right: int, *, label: str, error_type: type[Exception]) -> int:
    total = left + right
    if total > MAX_BYTE_VALUE:
        raise error_type(
            f"{label} overflows the canonical decimal byte range",
            error_class="capacity",
            stage="policy",
        )
    return total


def capacity_policy(
    *,
    expansions: Sequence[int],
    retained: Sequence[int],
    group_count: int,
    error_type: type[Exception] = CensusPolicyError,
) -> dict[str, Any]:
    """Checked canonical-decimal policy: E = max expansion, S = sum retained.

    ``WAL_RESERVE=E`` is a deliberately conservative same-order proxy taken from
    fresh live expansion.  It is not a WAL measurement, not a per-group WAL
    attribution, not an LSN calculation, and never the disposable 165736-byte
    observation.
    """

    if group_count < 1 or len(expansions) != group_count or len(retained) != group_count:
        raise error_type(
            "capacity policy needs one positive expansion and retained-source value per census group",
            error_class="capacity",
            stage="policy",
        )
    for value in expansions:
        _positive_decimal(value, label="before_compression_total_bytes", error_type=error_type)
    for value in retained:
        _positive_decimal(value, label="retained_source_bytes", error_type=error_type)
    expansion_max = max(expansions)
    e_value = _positive_decimal(expansion_max, label="E", error_type=error_type)
    total = 0
    for value in retained:
        total = _checked_add(total, value, label="S", error_type=error_type)
    s_value = _positive_decimal(total, label="S", error_type=error_type)
    rollback = _checked_add(expansion_max, expansion_max, label="ROLLBACK_HEADROOM", error_type=error_type)
    installer_required = _checked_add(
        total, rollback, label="installer cold requirement", error_type=error_type
    )
    return {
        "status": "resolved",
        "group_count": _canonical_decimal(group_count, label="group_count", error_type=error_type),
        "E": e_value,
        "S": s_value,
        "expansion_values": [
            _positive_decimal(value, label="expansion", error_type=error_type) for value in expansions
        ],
        "retained_values": [
            _positive_decimal(value, label="retained", error_type=error_type) for value in retained
        ],
        "cold_reserve_bytes": e_value,
        "wal_reserve_bytes": e_value,
        "install_required_bytes": s_value,
        "rollback_headroom_bytes": _positive_decimal(rollback, label="ROLLBACK_HEADROOM", error_type=error_type),
        "installer_required_cold_free_bytes": _canonical_decimal(
            installer_required, label="installer requirement", error_type=error_type
        ),
        "wal_reserve_basis": (
            "conservative same-order live-expansion proxy; not a WAL measurement, "
            "not a per-group WAL attribution, not an LSN calculation, and never a "
            "disposable probe observation"
        ),
    }
