"""One shared init-state identity comparison for both strict warm-start readers.

The cycle-completion verdict (``scheduler_discovery``) and the candidate
admission ladder (``scheduler_candidates``) used to compare a terminal
decision's recorded init state against the strict warm-start resolution with
two independently drifting rules.  Both now consume
:func:`terminal_init_state_match`, whose three-state answer separates the two
cases a boolean cannot:

``absent``
    the terminal evidence records NO init-state identity at all — an
    unaccounted row, not a disagreeing one.  Accepted-submit cohort terminal
    rows had this shape for every cycle before #1183.
``match``
    every identity field present on BOTH sides agrees.
``conflict``
    at least one field present on both sides disagrees — the lineage claim is
    wrong, and no caller may relax on it.

Comparison is deliberately per-present-field.  Legacy ``hydro_run`` rows carry
only ``init_state_id`` (``file_orchestration_journal.py:1203/1234``), so
demanding all four fields would classify every historical cycle as a conflict
and gap it forever — a worse failure than the one #1183 fixes.

This helper is a PURE field comparison.  The candidate ladder's special
branches (the ``candidate_state`` terminal-source branch and the
``COLD_START_QUARANTINED`` escape) stay in the candidate-side wrapper ahead of
it: they answer "this terminal is current" from evidence other than the
recorded identity, and hoisting them into the verdict would complete cycles
that have no continuity proof at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TERMINAL_INIT_STATE_MATCH = "match"
TERMINAL_INIT_STATE_ABSENT = "absent"
TERMINAL_INIT_STATE_CONFLICT = "conflict"

INIT_STATE_IDENTITY_FIELDS = ("state_id", "checksum", "uri", "valid_time")

_INIT_STATE_FIELD_ALIASES = {
    "state_id": ("init_state_id", "initial_state_id", "state_id"),
    "checksum": ("init_state_checksum", "initial_state_checksum", "checksum"),
    "uri": ("init_state_uri", "initial_state_uri", "ic_file_uri", "state_uri"),
    "valid_time": ("init_state_valid_time", "initial_state_valid_time", "valid_time"),
}

# Evidence sanitizers replace URIs/paths/checksums with placeholders; a
# placeholder means the value was withheld, not that it differs.
EVIDENCE_REDACTION_PLACEHOLDERS = frozenset(
    {"[object-uri]", "[uri]", "[local-path]", "[redacted]", "sha256:[redacted]"}
)


def init_state_field(record: Mapping[str, Any], field: str) -> Any:
    """Read one canonical identity field through its recorded aliases."""

    for key in _INIT_STATE_FIELD_ALIASES[field]:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def terminal_init_state_match(
    selected: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
) -> str:
    """Classify one terminal record against the selected warm-start state.

    ``selected`` is the strict resolution's ``candidate_state``; callers must
    already have established that it names a state id (a resolution without one
    is not a comparison this helper can make).
    """

    if not isinstance(observed, Mapping):
        return TERMINAL_INIT_STATE_ABSENT
    present = {
        field: value
        for field in INIT_STATE_IDENTITY_FIELDS
        if (value := init_state_field(observed, field)) not in (None, "")
    }
    if not present:
        return TERMINAL_INIT_STATE_ABSENT
    if "state_id" not in present:
        # Identity fragments without the state id name no lineage at all;
        # production never writes this shape, so treat it as corruption.
        return TERMINAL_INIT_STATE_CONFLICT
    for field, actual in present.items():
        expected = init_state_field(selected, field)
        if expected in (None, ""):
            continue
        if str(expected) in EVIDENCE_REDACTION_PLACEHOLDERS:
            continue
        if str(actual) in EVIDENCE_REDACTION_PLACEHOLDERS:
            continue
        if str(actual) != str(expected):
            return TERMINAL_INIT_STATE_CONFLICT
    return TERMINAL_INIT_STATE_MATCH


__all__ = (
    "EVIDENCE_REDACTION_PLACEHOLDERS",
    "INIT_STATE_IDENTITY_FIELDS",
    "TERMINAL_INIT_STATE_ABSENT",
    "TERMINAL_INIT_STATE_CONFLICT",
    "TERMINAL_INIT_STATE_MATCH",
    "init_state_field",
    "terminal_init_state_match",
)
