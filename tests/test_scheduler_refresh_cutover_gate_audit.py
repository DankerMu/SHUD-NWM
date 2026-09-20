"""#1132: the refresh receipt persists the `cutover_gate` audit block.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Every outcome that must carry
the block on disk (published, refusal, unexpected error, preimage conflict
after the gate, both rollback flavours), the two that must OMIT it (lock
contention, early preimage conflict), the shared-normalizer pin, and the
schema/runtime agreement on valid and malformed blocks plus the forged-gate
emergency refusal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    provider_destination_lock,
)
from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _config,
    _registry_row,
    _stub_provider_pipeline_with_models,
    _tracked_transaction_fixture,
    _write_declaration,
    _write_previous_canonical,
)
from tests.scheduler_refresh_receipt_helpers import (
    _classification_stub,
    _enforced_cutover_gate,
    _receipt_schema_validator,
)

# ---------------------------------------------------------------------------
# #1132: the runner refresh receipt persists the cutover_gate audit block
# ---------------------------------------------------------------------------


# Legal three-field block (schema- and `_validate_receipt`-clean, otherwise
# `_publish_primary_receipt` would refuse to write it) whose values no
# production path can produce on the registry-publish route.
_NORMALIZER_SENTINEL = {
    "mode": "not_wired",
    "declaration_env": "SENTINEL_ENV",
    "declaration_present": False,
}


def _freeze_refresh_clock(monkeypatch: pytest.MonkeyPatch, moment: refresh.datetime) -> None:
    """Pin ``refresh.datetime.now`` so the prospective generation is precomputable."""

    class _FrozenDateTime(refresh.datetime):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            del tz
            return moment

    monkeypatch.setattr(refresh, "datetime", _FrozenDateTime)


@pytest.mark.parametrize("declaration_present", [False, True])
def test_full_runner_receipt_persists_cutover_gate_audit_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration_present: bool
) -> None:
    """#1132: a routine registry-publish refresh leaves the gate mode and the
    declaration-present bit on the persisted receipt, so gated and bypassed
    runs are distinguishable from the on-disk runner artifact alone.  Both the
    declaration-staged and the declaration-absent run are pinned — otherwise
    the two remain byte-identical in later audit."""
    moment = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    prospective_models = previous_models
    if declaration_present:
        # A package_changed row covered by a matching declaration: the gate
        # runs enforced, opens the staged file, and still publishes.
        prospective_models = [_registry_row("basin-101", "c" * 64)]
        declaration = _write_declaration(
            tmp_path,
            generation=refresh._prospective_registry_generation(
                prospective_models, generated_at=moment
            ),
            entries=[
                {
                    "model_id": "basin-101",
                    "old_checksum": "a" * 64,
                    "new_checksum": "c" * 64,
                    "effective_cycle_utc": "2026-07-15T00:00:00Z",
                    "transition_mode": "replace",
                }
            ],
        )
        declaration.chmod(0o600)
        monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))
    else:
        monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    _freeze_refresh_clock(monkeypatch, moment)
    _stub_provider_pipeline_with_models(monkeypatch, prospective_models=prospective_models)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["cutover_gate"] == {
        "mode": "enforced",
        "declaration_env": refresh.CUTOVER_DECLARATION_ENV,
        "declaration_present": declaration_present,
    }
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        persisted
    )


def test_runner_receipt_cutover_gate_is_produced_by_shared_normalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: pins the normalizer CALL on the runner side.  The block the
    runner builds is a fixed point of normalization, so an equality assertion
    alone stays green against a receipt builder that copies the literal
    through without normalizing."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, models)
    _stub_provider_pipeline_with_models(monkeypatch, prospective_models=models)
    monkeypatch.setattr(
        refresh,
        "normalize_cutover_gate_audit",
        lambda cutover_gate: dict(_NORMALIZER_SENTINEL),
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted["cutover_gate"] == _NORMALIZER_SENTINEL


def test_lock_contention_receipt_omits_cutover_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: a run that never reaches the gate construction must omit the key
    entirely — a ``null`` placeholder would forge "the gate did not run" into
    an auditable-looking field."""
    config = _config(tmp_path)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=[_registry_row("basin-101", "a" * 64)]
    )

    with provider_destination_lock(config.refresh_lock, blocking=False):
        receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "already_running"
    assert "cutover_gate" not in receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert "cutover_gate" not in persisted


def test_early_preimage_conflict_receipt_omits_cutover_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: same omission contract on the provider-preimage failure path.

    Also guards the wiring trap: the audit block is constructed deep inside the
    lock, so a receipt builder fed an unbound name here would raise
    ``UnboundLocalError`` out of the ``except ProviderAtomicError`` handler
    instead of returning a receipt."""
    config = _config(tmp_path)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=[_registry_row("basin-101", "a" * 64)]
    )

    def conflicting_capture(*args: object, **kwargs: object) -> ProviderPreimage:
        del args, kwargs
        raise ProviderAtomicError("provider_preimage_changed", phase="precommit")

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", conflicting_capture)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["reason"] == "provider_preimage_changed"
    assert "cutover_gate" not in receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert "cutover_gate" not in persisted


@pytest.mark.parametrize("declaration_present", [False, True])
def test_refusal_receipt_persists_cutover_gate_audit_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration_present: bool
) -> None:
    """#1132: a gate refusal is the receipt operators read first, so it must
    carry the same audit block as a published run.

    Both variants refuse on the same undeclared row; only the staged-file bit
    differs, which is exactly the forensic question "was a declaration staged
    at all when this refusal happened?".
    """
    moment = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    config = _config(tmp_path)
    _write_previous_canonical(
        config,
        [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)],
    )
    # Two package_changed rows; the declaration (when staged) covers only the
    # first, so `basin-102` stays undeclared drift and the gate refuses.
    prospective_models = [
        _registry_row("basin-101", "c" * 64),
        _registry_row("basin-102", "d" * 64),
    ]
    if declaration_present:
        declaration = _write_declaration(
            tmp_path,
            generation=refresh._prospective_registry_generation(
                prospective_models, generated_at=moment
            ),
            entries=[
                {
                    "model_id": "basin-101",
                    "old_checksum": "a" * 64,
                    "new_checksum": "c" * 64,
                    "effective_cycle_utc": "2026-07-15T00:00:00Z",
                    "transition_mode": "replace",
                }
            ],
        )
        declaration.chmod(0o600)
        monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))
    else:
        monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    _freeze_refresh_clock(monkeypatch, moment)
    _stub_provider_pipeline_with_models(monkeypatch, prospective_models=prospective_models)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "registry_cutover_undeclared", receipt
    # Staged declaration covers basin-101, so only basin-102 is refused; with
    # no declaration at all both rows are.
    assert receipt["registry_classification"]["refused"]["total"] == (
        1 if declaration_present else 2
    )
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["cutover_gate"] == _enforced_cutover_gate(
        declaration_present=declaration_present
    )


def test_unexpected_error_receipt_persists_cutover_gate_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: the catch-all failure receipt keeps the audit block too.

    An untyped crash after the gate was installed must not silently downgrade
    to a gate-less receipt — otherwise the crash looks like a pre-gate run.
    """
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, models)
    _stub_provider_pipeline_with_models(monkeypatch, prospective_models=models)

    def crash(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("injected post-gate crash")

    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", crash)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "provider_invalid"
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["cutover_gate"] == _enforced_cutover_gate(declaration_present=False)


def test_preimage_conflict_after_gate_persists_cutover_gate_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: a CAS conflict raised after the gate ran (typed
    ``ProviderAtomicError`` receipt, earlier lanes rolled back) still reports
    the gate mode — unlike the pre-gate conflict, which omits the key."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config, _old, _paths = _tracked_transaction_fixture(
        tmp_path, monkeypatch, fail_lane="", conflict_lane="registry"
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "provider_preimage_changed"
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["cutover_gate"] == _enforced_cutover_gate(declaration_present=False)


def test_restored_previous_rollback_receipt_persists_cutover_gate_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: the rollback closure builds its own receipts; the
    ``restored_previous`` one must carry the audit block as well."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config, _old, _paths = _tracked_transaction_fixture(
        tmp_path, monkeypatch, fail_lane="readiness"
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["cutover_gate"] == _enforced_cutover_gate(declaration_present=False)


def test_replace_uncertain_rollback_receipt_persists_cutover_gate_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1132: the other rollback-closure receipt.  ``replace_uncertain`` is the
    outcome an operator triages by hand, so the gate facts must survive on it."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config, _old, _paths = _tracked_transaction_fixture(
        tmp_path, monkeypatch, fail_lane="", unowned_lane="state"
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain", receipt
    assert receipt["reason"] == "provider_replace_uncertain"
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["cutover_gate"] == _enforced_cutover_gate(declaration_present=False)


_INVALID_RECEIPT_CUTOVER_GATES: list[Any] = [
    pytest.param(
        {
            "mode": "enforced",
            "declaration_env": "E",
            "declaration_present": True,
            "smuggled": "x",
        },
        id="fourth_field",
    ),
    pytest.param(
        {"mode": "gate_off", "declaration_env": "E", "declaration_present": True},
        id="mode_outside_audited_set",
    ),
    pytest.param(
        {"mode": "enforced", "declaration_env": 42, "declaration_present": True},
        id="non_string_declaration_env",
    ),
    pytest.param(
        {"mode": "enforced", "declaration_env": "E", "declaration_present": "no"},
        id="non_bool_declaration_present",
    ),
    pytest.param({"mode": "enforced", "declaration_env": "E"}, id="missing_declaration_present"),
    pytest.param(
        {"mode": "enforced", "declaration_env": "E" * 513, "declaration_present": True},
        id="declaration_env_over_max_string_length",
    ),
    pytest.param(None, id="null_block"),
]


def _published_receipt_with_cutover_gate(cutover_gate: Any) -> dict[str, Any]:
    provider = {
        "name": "registry",
        "before_sha256": "a" * 64,
        "before_inode": 1,
        "before_schema_version": "v1",
        "before_generated_at": "2026-07-14T00:00:00Z",
        "before_payload_checksum": "sha256:" + "b" * 64,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-07-14T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 1,
    }
    receipt = refresh._receipt(
        run_id="refresh_cutover_gate_corpus",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "registry_worker_mirror"},
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification=_classification_stub(),
    )
    return {**receipt, "cutover_gate": cutover_gate}


@pytest.mark.parametrize(
    "cutover_gate",
    [
        pytest.param(
            {
                "mode": "enforced",
                "declaration_env": refresh.CUTOVER_DECLARATION_ENV,
                "declaration_present": True,
            },
            id="enforced_with_declaration",
        ),
        pytest.param(
            {"mode": "bypassed_allow_uncovered_cutover", "declaration_env": None, "declaration_present": False},
            id="bypass_without_env",
        ),
        pytest.param(
            {"mode": "not_wired", "declaration_env": None, "declaration_present": False},
            id="not_wired",
        ),
    ],
)
def test_receipt_schema_and_runtime_admit_the_same_valid_cutover_gate_blocks(
    cutover_gate: dict[str, Any],
) -> None:
    """#1132: every normalizer-producible block is accepted by BOTH the JSON
    schema and the runtime validator."""
    receipt = _published_receipt_with_cutover_gate(cutover_gate)

    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)


@pytest.mark.parametrize("cutover_gate", _INVALID_RECEIPT_CUTOVER_GATES)
def test_receipt_schema_and_runtime_reject_the_same_malformed_cutover_gate(
    cutover_gate: Any,
) -> None:
    """#1132: schema and runtime validator reject the same malformed corpus —
    a receipt the schema refuses must never be readable back through
    ``_validate_receipt``."""
    receipt = _published_receipt_with_cutover_gate(cutover_gate)

    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_cutover_gate_invalid" in str(info.value)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(receipt)


def test_emergency_reconstruction_refuses_forged_cutover_gate(tmp_path: Path) -> None:
    """#1132: the untrusted emergency-record read path rejects a forged block.

    ``reconstruct_primary_receipt`` swallows the typed ``ValueError`` into
    ``RefreshError("emergency_record_invalid")``, so the typed reason is
    asserted on the direct validator call and the reconstruct path is pinned on
    its own outer contract."""
    config = _config(tmp_path)
    receipt = refresh._receipt(
        run_id="refresh_forged_cutover_gate",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=[],
    )
    forged = {
        **receipt,
        "cutover_gate": {
            "mode": "enforced",
            "declaration_env": "E",
            "declaration_present": True,
            "smuggled": "x",
        },
    }

    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(forged)
    assert "receipt_cutover_gate_invalid" in str(info.value)

    emergency = config.emergency_root / "refresh_forged_cutover_gate.reserved.json"
    emergency.write_text(json.dumps(forged))
    emergency.chmod(0o600)
    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.reconstruct_primary_receipt(config, emergency)
    assert error_info.value.reason == "emergency_record_invalid"
