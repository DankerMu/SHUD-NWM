"""#1097: the shared `cutover_gate` audit normalizer.

`packages/scheduler/registry_audit.py` is the single definition point every
persistence channel (CLI summary, runner receipt, manifest companion receipt)
must route through.  These tests pin the four strict branches directly on the
shared module plus the re-export identity the CLI/runner import paths depend
on; channel-level behavior lives in
`tests/test_publish_scheduler_file_registry.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from packages.scheduler import registry_audit
from scripts import scheduler_file_provider_refresh as refresh


def test_normalizer_rejects_non_mapping_audit_block() -> None:
    """(a) A non-Mapping block is not coerced — it is rejected."""
    with pytest.raises(registry_audit.SchedulerRegistryPublishError) as excinfo:
        registry_audit.normalize_cutover_gate_audit(["enforced"])  # type: ignore[arg-type]

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert excinfo.value.to_payload()["provided_type"] == "list"


@pytest.mark.parametrize("mode", ["", "not-a-mode", "ENFORCED", None, 0])
def test_normalizer_rejects_mode_outside_audited_set(mode: Any) -> None:
    """(b) A mode outside `CUTOVER_GATE_MODES` never degrades to not_wired."""
    with pytest.raises(registry_audit.SchedulerRegistryPublishError) as excinfo:
        registry_audit.normalize_cutover_gate_audit({"mode": mode})

    payload = excinfo.value.to_payload()
    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert payload["allowed"] == sorted(registry_audit.CUTOVER_GATE_MODES)
    assert mode not in registry_audit.CUTOVER_GATE_MODES


def test_normalizer_rejects_non_string_declaration_env() -> None:
    """(c) `declaration_env` is str-or-null; anything else is rejected."""
    with pytest.raises(registry_audit.SchedulerRegistryPublishError) as excinfo:
        registry_audit.normalize_cutover_gate_audit(
            {"mode": "enforced", "declaration_env": 42, "declaration_present": True}
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert excinfo.value.to_payload()["provided_type"] == "int"


def test_normalizer_maps_none_to_not_wired_fallback() -> None:
    """(d) `None` means "caller never wired the gate", recorded truthfully."""
    assert registry_audit.normalize_cutover_gate_audit(None) == {
        "mode": "not_wired",
        "declaration_env": None,
        "declaration_present": False,
    }


def test_shared_error_class_is_the_one_object_all_import_paths_see() -> None:
    """The move must keep exactly one class object: the CLI re-export and the
    refresh runner's `except`/`isinstance` sites bind to the same type."""
    assert registry_script.SchedulerRegistryPublishError is registry_audit.SchedulerRegistryPublishError
    assert refresh.SchedulerRegistryPublishError is registry_audit.SchedulerRegistryPublishError
    assert registry_script.CUTOVER_GATE_MODES is registry_audit.CUTOVER_GATE_MODES
