"""#1144 and #1832: which receipt blocks a given outcome must CARRY.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). The presence half of the
receipt contract -- gated outcomes and cutover refusals are rejected by BOTH
validators when `cutover_gate` is stripped, early failures stay valid without
it, the committed example and the pre-#1132 latest legs, the forged id-only
refused-bucket corpus -- plus #1832's `calibration_overrides` block, whose
runtime validator and JSON Schema must reject the same corpus.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _config,
    _registry_row,
    _stub_provider_pipeline_with_models,
    _write_current_published_receipt,
    _write_previous_canonical,
)
from tests.scheduler_refresh_receipt_helpers import (
    _CROSS_PIN_SHAPE,
    _DECLARATION_REFUSAL_ITEM,
    _classification_receipt,
    _classification_stub,
    _enforced_cutover_gate,
    _mode_classification,
    _receipt_schema_validator,
)

# ---------------------------------------------------------------------------
# #1144: gated outcomes must CARRY the cutover_gate audit block
# ---------------------------------------------------------------------------


def _presence_receipt(
    *,
    outcome: str,
    reason: str,
    classification: dict[str, Any],
    with_providers: bool,
) -> dict[str, Any]:
    """A receipt that is legal under BOTH validators, block included.

    The negative corpus is this very payload minus the `cutover_gate` key, so
    the missing key is the ONLY difference between the accepted and the
    rejected receipt.
    """
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
    providers = (
        [
            provider,
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ]
        if with_providers
        else []
    )
    return refresh._receipt(
        run_id="refresh_cutover_gate_presence",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome=outcome,
        reason=reason,
        phase="complete",
        providers=providers,
        registry_classification=classification,
        cutover_gate=_enforced_cutover_gate(declaration_present=False),
    )


def _without_cutover_gate(receipt: dict[str, Any]) -> dict[str, Any]:
    stripped = {key: value for key, value in receipt.items() if key != "cutover_gate"}
    assert "cutover_gate" not in stripped
    return stripped


@pytest.mark.parametrize(
    ("outcome", "reason", "mode"),
    [
        pytest.param("published", "success", "full", id="published"),
        pytest.param("dry_run", "dry_run_complete", "id_only", id="dry_run"),
    ],
)
def test_gated_outcome_receipt_without_cutover_gate_is_rejected_by_both_validators(
    outcome: str, reason: str, mode: str
) -> None:
    """#1144: the runner builds the audit block unconditionally before
    `publish_registry`, so a `published`/`dry_run` receipt that reached disk
    without it is forged (or a regression that dropped the pass-through).

    Both validators must reject the SAME corpus; the accepted control below is
    byte-identical apart from the deleted key."""
    classification = {**_classification_stub(), "mode": mode}
    receipt = _presence_receipt(
        outcome=outcome, reason=reason, classification=classification, with_providers=True
    )
    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)

    stripped = _without_cutover_gate(receipt)

    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(stripped)
    assert "receipt_cutover_gate_required" in str(info.value)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(stripped)


def _refusal_classification() -> dict[str, Any]:
    return {
        **_classification_stub(),
        "refused": {
            "items": [_DECLARATION_REFUSAL_ITEM],
            "total": 1,
            "truncated": False,
        },
    }


@pytest.mark.parametrize(
    "reason", sorted(refresh.REGISTRY_CUTOVER_REFUSAL_REASONS)
)
def test_cutover_refusal_receipt_without_cutover_gate_is_rejected_by_both_validators(
    reason: str,
) -> None:
    """#1144: a gate refusal proves the gate ran, so its receipt must carry the
    audit block — the refusal receipt is the one operators read first."""
    receipt = _presence_receipt(
        outcome="failed",
        reason=reason,
        classification=_refusal_classification(),
        with_providers=False,
    )
    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)

    stripped = _without_cutover_gate(receipt)

    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(stripped)
    assert "receipt_cutover_gate_required" in str(info.value)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(stripped)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        pytest.param("already_running", "refresh_already_running", id="lock_contention"),
        pytest.param("failed", "provider_preimage_changed", id="early_preimage_conflict"),
        pytest.param("failed", "provider_invalid", id="early_provider_failure"),
    ],
)
def test_early_failure_receipt_without_cutover_gate_stays_valid(
    outcome: str, reason: str
) -> None:
    """#1144 legal-omission guard: runs that die before the block is built must
    keep validating without the key — the presence rule may not swallow them."""
    receipt = refresh._receipt(
        run_id="refresh_early_failure",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome=outcome,
        reason=reason,
        phase="precommit",
        providers=[],
    )
    assert "cutover_gate" not in receipt

    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)


def test_receipt_example_stays_valid_under_both_validators() -> None:
    """#1144: `schemas/examples/...` is the published contract sample and the
    CI `json-schema-validate` gate's input; the presence rule must not
    invalidate it."""
    example = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/examples/scheduler_file_provider_refresh_receipt.example.json"
        ).read_text()
    )
    assert example["outcome"] == "published"
    assert "cutover_gate" in example

    refresh._validate_receipt(example)
    _receipt_schema_validator().validate(example)


def test_pre_1132_published_latest_json_fails_validate_current_receipt(
    tmp_path: Path,
) -> None:
    """#1144 upgrade-path oracle (runbook `current-production-ops.md`): a
    pre-#1132 `latest.json` — a real `published` receipt whose only defect is
    the missing audit block — is HARD-rejected by the installer's `--enable`
    validation step, not merely flagged for operator judgement."""
    config = _config(tmp_path)
    receipt_path, receipt = _write_current_published_receipt(config)
    # Control: the same receipt WITH the block is accepted, so the rejection
    # below can only come from the missing key.
    assert refresh.validate_current_receipt(config, receipt_path) == receipt

    legacy = _without_cutover_gate(receipt)
    receipt_path.write_bytes(refresh._receipt_bytes(legacy))

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.validate_current_receipt(config, receipt_path)
    assert error_info.value.reason == "emergency_record_invalid"
    assert error_info.value.phase == "receipt"


def test_refresh_over_pre_1132_latest_json_publishes_a_receipt_that_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1144 remedy oracle: the documented fix is one SUCCESSFUL manual
    refresh.  The stale legacy `latest.json` does not block the write path
    (`_publish_primary_receipt` orders receipts through the lenient reader),
    and the receipt written over the top carries the audit block and clears
    the strict validator the `--enable` step runs."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, models)
    # Seed a pre-#1132 latest.json: published, classification present (post
    # #1080), audit block absent.
    legacy_provider = {
        "name": "registry",
        "before_sha256": "1" * 64,
        "before_inode": 100,
        "before_schema_version": "nhms.scheduler.file_model_registry.v1",
        "before_generated_at": "2026-07-01T00:00:00Z",
        "before_payload_checksum": "sha256:" + "a" * 64,
        "after_sha256": "2" * 64,
        "after_schema_version": "nhms.scheduler.file_model_registry.v1",
        "after_generated_at": "2026-07-01T01:00:00Z",
        "after_payload_checksum": "sha256:" + "b" * 64,
        "entry_count": 1,
    }
    legacy_payload = _without_cutover_gate(
        refresh._receipt(
            run_id="refresh_pre_1132",
            started=refresh.datetime(2026, 7, 1, tzinfo=refresh.UTC),
            outcome="published",
            reason="success",
            phase="complete",
            providers=[
                legacy_provider,
                {**legacy_provider, "name": "readiness"},
                {**legacy_provider, "name": "state"},
            ],
            registry_classification=_classification_stub(),
            cutover_gate=_enforced_cutover_gate(declaration_present=False),
        )
    )
    (config.receipt_root / "latest.json").write_bytes(
        json.dumps(legacy_payload, sort_keys=True, indent=2).encode() + b"\n"
    )
    _stub_provider_pipeline_with_models(monkeypatch, prospective_models=models)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted["run_id"] == receipt["run_id"]
    assert persisted["cutover_gate"] == _enforced_cutover_gate(declaration_present=False)
    refresh._validate_receipt(persisted)


_FORGED_ID_ONLY_REFUSED_BUCKETS: list[Any] = [
    pytest.param(
        {"items": [], "total": 500, "truncated": True},
        id="items_wiped_total_inflated",
    ),
    pytest.param(
        {"items": [], "total": 1, "truncated": True},
        id="wiped_items_inflated_total_truncated",
    ),
    pytest.param(
        {"items": [_DECLARATION_REFUSAL_ITEM], "total": 2, "truncated": True},
        id="truncated_bucket",
    ),
    pytest.param(
        {
            "items": [_DECLARATION_REFUSAL_ITEM, _DECLARATION_REFUSAL_ITEM],
            "total": 2,
            "truncated": False,
        },
        id="two_declaration_entries",
    ),
    pytest.param(
        {
            "items": [{**_DECLARATION_REFUSAL_ITEM, "model_id": "mdl-x"}],
            "total": 1,
            "truncated": False,
        },
        id="model_id_not_the_synthetic_marker",
    ),
]


@pytest.mark.parametrize("refused", _FORGED_ID_ONLY_REFUSED_BUCKETS)
def test_id_only_mode_rejects_forged_refused_buckets(refused: dict[str, Any]) -> None:
    """#1144 (#1145 residue): the id-only writer path can attach AT MOST one
    refused row — the synthetic `__declaration__` marker appended by
    `_registry_precommit_gate` when the declaration file fails to load — and
    `_RegistryClassification.to_receipt` never truncates a one-item bucket.
    Every shape outside that is forged.

    `reason` stays a cutover refusal so the `refused.total >= 1` pin cannot be
    what rejects these payloads."""
    classification = _mode_classification(mode="id_only", **_CROSS_PIN_SHAPE)
    classification["refused"] = refused

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification,
                outcome="failed",
                reason="registry_cutover_declaration_invalid",
            )
        )


def test_dry_run_receipt_carrying_a_well_formed_refusal_is_rejected() -> None:
    """#1144 (#1145 residue): a declaration failure always terminates the run
    as `outcome="failed"` (`_registry_precommit_gate` raises), so no legal
    writer emits a `dry_run` receipt with a refusal — not even the otherwise
    well-formed synthetic `__declaration__` row."""
    classification = _mode_classification(
        mode="id_only", refused_items=[_DECLARATION_REFUSAL_ITEM], **_CROSS_PIN_SHAPE
    )
    assert classification["refused"]["total"] == 1

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="dry_run", reason="dry_run_complete"
            )
        )


def test_failed_id_only_receipt_keeps_the_single_declaration_refusal() -> None:
    """#1144 positive control for the two pins above: the ONE bucket shape the
    id-only writer path can produce (single `__declaration__` row, untruncated,
    `total == 1`) on the outcome it actually terminates with still validates."""
    classification = _mode_classification(
        mode="id_only", refused_items=[_DECLARATION_REFUSAL_ITEM], **_CROSS_PIN_SHAPE
    )

    refresh._validate_registry_classification_field(
        _classification_receipt(
            classification,
            outcome="failed",
            reason="registry_cutover_declaration_invalid",
        )
    )


# ---------------------------------------------------------------------------
# #1832 round-2 C1: the `calibration_overrides` receipt block.  The end-to-end
# lane pins live in tests/test_publish_registry_refresh_lane.py (they need a
# real Basins tree); these pin the receipt contract itself, on both readers --
# the runtime validator and the JSON Schema must reject the same corpus, or a
# receipt that one accepts and the other refuses breaks the emergency channel.
# ---------------------------------------------------------------------------


def _calibration_receipt(**overrides: Any) -> dict[str, Any]:
    receipt = refresh._receipt(
        run_id="run",
        started=refresh.datetime.now(refresh.UTC),
        outcome="failed",
        reason=refresh.CALIBRATION_OVERRIDE_REFUSAL_REASON,
        phase="precommit",
        providers=[],
        calibration_overrides={
            "declaration_path": "/etc/nhms/calibration_overrides.yaml",
            "not_applied": [],
            "error": {
                "error_code": "CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY",
                "message": "Declared calibration override(s) name a basin ...: charlie:GEOL_DMAC.",
                "entries": [{"basin_slug": "charlie", "parameter": "GEOL_DMAC"}],
            },
        },
    )
    receipt.update(overrides)
    return receipt


def test_calibration_override_refusal_receipt_validates_on_both_readers() -> None:
    receipt = _calibration_receipt()

    assert refresh._validate_receipt(receipt) == receipt
    _receipt_schema_validator().validate(receipt)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda receipt: {k: v for k, v in receipt.items() if k != "calibration_overrides"},
            id="block_absent",
        ),
        pytest.param(
            lambda receipt: {
                **receipt,
                "calibration_overrides": {
                    k: v for k, v in receipt["calibration_overrides"].items() if k != "error"
                },
            },
            id="error_absent",
        ),
        pytest.param(
            lambda receipt: {
                **receipt,
                "calibration_overrides": {
                    **receipt["calibration_overrides"],
                    "error": {**receipt["calibration_overrides"]["error"], "error_code": ""},
                },
            },
            id="error_code_empty",
        ),
        pytest.param(
            lambda receipt: {
                **receipt,
                "calibration_overrides": {
                    **receipt["calibration_overrides"],
                    "not_applied": [
                        {
                            "basin_slug": "bravo",
                            "parameter": "GEOL_DMAC",
                            "reason_not_applied": "basin_not_in_publish_set",
                        }
                    ],
                },
            },
            id="pre_c2_reason_token",
        ),
        pytest.param(
            lambda receipt: {
                **receipt,
                "calibration_overrides": {**receipt["calibration_overrides"], "surprise": 1},
            },
            id="unknown_field",
        ),
    ],
)
def test_calibration_override_block_is_rejected_by_both_readers(
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Absence on the refusal reason is itself forged: the block IS the refusal.

    The stale `basin_not_in_publish_set` case is deliberate -- that token used to
    cover both "narrowed out of this run" and "exists nowhere", so admitting it
    now would let an old-vocabulary writer keep the two indistinguishable.
    """
    forged = mutate(_calibration_receipt())

    with pytest.raises(ValueError, match="receipt_calibration_overrides_"):
        refresh._validate_receipt(forged)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(forged)


def test_receipt_without_the_calibration_block_stays_valid_on_other_reasons() -> None:
    """A run that failed before the declaration was reached knows nothing about
    it and must not be forced to invent a block."""
    receipt = refresh._receipt(
        run_id="run",
        started=refresh.datetime.now(refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="precommit",
        providers=[],
    )

    assert "calibration_overrides" not in receipt
    assert refresh._validate_receipt(receipt) == receipt
    _receipt_schema_validator().validate(receipt)
