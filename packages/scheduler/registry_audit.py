"""Single definition point for the scheduler registry ``cutover_gate`` audit.

Every persistence channel of the scheduler registry publish flow — the CLI
summary (`scripts/publish_scheduler_file_registry.py`), the runner receipt
(`scripts/scheduler_file_provider_refresh.py`), and the manifest companion
receipt (`services/orchestrator/scheduler_file_providers.py`) — must persist
the same three-field audit block validated by the same rules.  Keeping the
normalizer (and the error type it raises) here lets both `scripts/` and
`services/` import downward instead of duplicating the check; a second copy is
what let the manifest channel silently degrade a malformed block to
``"not_wired"`` (#1097).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CUTOVER_GATE_MODES = frozenset(
    {
        "enforced",
        "bypassed_allow_uncovered_cutover",
        "not_wired",
    }
)


class SchedulerRegistryPublishError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "message": str(self), **self.details}


def normalize_cutover_gate_audit(
    cutover_gate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a bounded, well-typed `cutover_gate` audit block for the summary.

    R2-A1 requires every summary path (bypass/normal/dry-run) and the manifest
    publication receipt to persist the same three-field audit shape:

    - ``mode``: one of ``CUTOVER_GATE_MODES``; missing input becomes
      ``"not_wired"`` so callers that never opted in are recorded truthfully
      instead of masquerading as ``"enforced"``.
    - ``declaration_env``: the env name consulted (``str``) when the gate ran
      enforced, else ``None`` — bypass and not_wired never consult an env.
    - ``declaration_present``: ``bool``; whether the env resolved to a
      readable declaration file.  Absent (or explicit ``None``) defaults to
      ``False``; any other non-``bool`` value is rejected instead of coerced,
      so a stringy ``"no"`` can never be audited as presence.
    """
    if cutover_gate is None:
        return {
            "mode": "not_wired",
            "declaration_env": None,
            "declaration_present": False,
        }
    if not isinstance(cutover_gate, Mapping):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID",
            "cutover_gate audit must be a mapping.",
            details={"provided_type": type(cutover_gate).__name__},
        )
    mode = cutover_gate.get("mode")
    if mode not in CUTOVER_GATE_MODES:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID",
            "cutover_gate.mode must be one of the audited modes.",
            details={"mode": mode, "allowed": sorted(CUTOVER_GATE_MODES)},
        )
    env_name = cutover_gate.get("declaration_env")
    if env_name is not None and not isinstance(env_name, str):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID",
            "cutover_gate.declaration_env must be a string or null.",
            details={"provided_type": type(env_name).__name__},
        )
    declaration_present = cutover_gate.get("declaration_present")
    if declaration_present is None:
        declaration_present = False
    elif not isinstance(declaration_present, bool):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID",
            "cutover_gate.declaration_present must be a boolean.",
            details={"provided_type": type(declaration_present).__name__},
        )
    return {
        "mode": str(mode),
        "declaration_env": env_name,
        "declaration_present": declaration_present,
    }
