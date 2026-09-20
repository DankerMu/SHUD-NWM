"""#1140: reconciliation keys on the classification MODE, not the receipt outcome.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). The dry-run failure that must
persist its true reason, the id-only and full mode receipts, the
writer-producible failure corpus and the two refusal shapes the writer cannot
attach, the unloadable-declaration refusal receipt, both legacy mode-less
branches, then the mode/outcome cross-pin: honest pairs, forged combinations,
the forged emergency record, the previous-side and prospective-count
equalities, the key-set refusals and the schema/runtime agreement on
classification modes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _config,
    _registry_row,
    _stub_provider_pipeline_with_models,
    _write_previous_canonical,
)
from tests.scheduler_refresh_receipt_helpers import (
    _CROSS_PIN_SHAPE,
    _DECLARATION_REFUSAL_ITEM,
    _classification_receipt,
    _classification_stub,
    _mode_classification,
    _receipt_schema_validator,
    _receipt_with_classification,
)


def test_dry_run_failure_after_the_gate_persists_the_true_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1140 reproduction: a dry_run whose previous canonical registry holds a
    model absent from the prospective set (the removal preview dry_run exists
    for) and which fails AFTER the precommit gate.

    Keying reconciliation on the terminal outcome routed the honest id-only
    classification into the full-equality branch (`unchanged 1 + 0 + 0 !=
    previous_model_count 2`), `_publish_primary_receipt` refused to write, and
    the whole run died as `primary_receipt_failed` with NO receipt on disk —
    masking the true reason.  The receipt must land on both channels with the
    injected reason and the id-only classification retained.
    """
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    _write_previous_canonical(
        config,
        [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)],
    )
    # basin-102 is pending removal: present in the previous canonical registry,
    # absent from the prospective set.
    prospective_models = [_registry_row("basin-101", "a" * 64)]
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    def failing_readiness_validation(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise refresh.RefreshError("provider_invalid")

    # MUST be installed AFTER `_stub_provider_pipeline_with_models`, which
    # no-ops this very symbol — the reverse order silently false-greens.
    monkeypatch.setattr(
        refresh, "validate_catalog_bound_readiness_entries", failing_readiness_validation
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "failed", receipt
    # `provider_invalid` is also the catch-all fold value, so pin the phase too.
    assert receipt["reason"] == "provider_invalid", receipt
    assert receipt["phase"] == "precommit", receipt
    classification = receipt["registry_classification"]
    assert classification["mode"] == "id_only"
    assert classification["previous_model_count"] == 2
    assert classification["unchanged"]["total"] == 1
    assert classification["removed"]["total"] == 0
    # The receipt actually reaches disk on BOTH channels (a degraded
    # implementation that swallows the publish error would still return it).
    latest = json.loads((config.receipt_root / "latest.json").read_text())
    assert latest == receipt
    history = config.receipt_root / "history" / f"{receipt['run_id']}.json"
    assert json.loads(history.read_text()) == receipt


def test_dry_run_success_receipt_carries_id_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1140: the happy dry_run path persists `mode == "id_only"`."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [
        _registry_row("basin-101", "a" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline_with_models(
        monkeypatch,
        prospective_models=previous_models + [_registry_row("basin-201", "c" * 64)],
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["registry_classification"]["mode"] == "id_only"


def test_published_receipt_carries_full_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1140: a real publish classified in full mode and says so."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline_with_models(
        monkeypatch,
        prospective_models=previous_models + [_registry_row("basin-201", "c" * 64)],
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt
    assert persisted["registry_classification"]["mode"] == "full"


_ID_ONLY_MODE_ACCEPTED: list[Any] = [
    pytest.param(
        {
            "previous_sha": None,
            "previous_count": None,
            "added": ["basin-101"],
            "unchanged": [],
            "removed": [],
            "prospective_count": 1,
        },
        None,
        "provider_invalid",
        id="bootstrap",
    ),
    pytest.param(
        {
            "previous_sha": "1" * 64,
            "previous_count": 2,
            "added": ["basin-103"],
            "unchanged": ["basin-101", "basin-102"],
            "removed": [],
            "prospective_count": 3,
        },
        None,
        "provider_invalid",
        id="previous_without_removal",
    ),
    pytest.param(
        {
            "previous_sha": None,
            "previous_count": None,
            "added": ["basin-101"],
            "unchanged": [],
            "removed": [],
            "prospective_count": 1,
        },
        [_DECLARATION_REFUSAL_ITEM],
        "registry_cutover_declaration_invalid",
        id="bootstrap_with_declaration_refusal",
    ),
    pytest.param(
        {
            "previous_sha": "1" * 64,
            "previous_count": 2,
            "added": ["basin-103"],
            "unchanged": ["basin-101", "basin-102"],
            "removed": [],
            "prospective_count": 3,
        },
        [_DECLARATION_REFUSAL_ITEM],
        "registry_cutover_declaration_invalid",
        id="previous_with_declaration_refusal",
    ),
    pytest.param(
        {
            "previous_sha": "1" * 64,
            "previous_count": 3,
            "added": ["basin-103"],
            "unchanged": ["basin-101", "basin-102"],
            "removed": [],
            "prospective_count": 3,
        },
        None,
        "provider_invalid",
        id="previous_with_pending_removal",
    ),
]


@pytest.mark.parametrize(("shape", "refused_items", "reason"), _ID_ONLY_MODE_ACCEPTED)
def test_id_only_mode_accepts_writer_producible_failure_classifications(
    shape: dict[str, Any], refused_items: list[dict[str, Any]] | None, reason: str
) -> None:
    """#1140: on an `outcome="failed"` receipt the lenient branch is selected by
    `mode`, so every classification the id-only writer path can produce — with
    or without the synthetic `__declaration__` refusal, with or without previous
    models pending removal — validates."""
    classification = _mode_classification(refused_items=refused_items, **shape)

    refresh._validate_registry_classification_field(
        _classification_receipt(classification, outcome="failed", reason=reason)
    )


def test_id_only_mode_rejects_refusal_reason_the_writer_cannot_attach() -> None:
    """#1140 P1-1 boundary: the id-only branch loosens by EXACTLY one reason.

    `registry_cutover_undeclared` is produced only by the checksum comparison
    the id-only path never runs, so an id-only classification carrying it is
    forged."""
    classification = _mode_classification(
        refused_items=[
            {
                "model_id": "basin-101",
                "old_checksum": "a" * 64,
                "new_checksum": "c" * 64,
                "reason": "registry_cutover_undeclared",
            }
        ],
        previous_sha="1" * 64,
        previous_count=2,
        added=["basin-103"],
        unchanged=["basin-101", "basin-102"],
        removed=[],
        prospective_count=3,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification,
                outcome="failed",
                reason="registry_cutover_undeclared",
            )
        )


def test_id_only_mode_rejects_cutover_reason_with_flattened_refused_bucket() -> None:
    """#1140 P2-1 terminal pin: the writer sets the receipt-level refusal reason
    and appends the refused row in the SAME action, so wiping the bucket while
    keeping the reason is tampering — the pin must survive the id-only branch's
    early return."""
    classification = _mode_classification(
        previous_sha="1" * 64,
        previous_count=2,
        added=["basin-103"],
        unchanged=["basin-101", "basin-102"],
        removed=[],
        prospective_count=3,
    )
    assert classification["refused"]["total"] == 0

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification,
                outcome="failed",
                reason="registry_cutover_declaration_invalid",
            )
        )


def test_dry_run_with_unloadable_declaration_persists_the_refusal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1140 end-to-end declaration variant: the writer appends the synthetic
    `__declaration__` refusal after `_classify_registry` without consulting
    `dry_run`, so a dry_run receipt legitimately carries one refused row.

    With a model pending removal this receipt fails the full-equality branch,
    which is exactly the pre-#1140 masking bug; an id-only branch that still
    rejects every refused row (no P1-1 relaxation) reproduces it too."""
    config = _config(tmp_path)
    _write_previous_canonical(
        config,
        [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)],
    )
    declaration = tmp_path / "declaration.json"
    declaration.write_text("{not json", encoding="utf-8")
    declaration.chmod(0o600)
    monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=[_registry_row("basin-101", "a" * 64)]
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "registry_cutover_declaration_invalid", receipt
    classification = receipt["registry_classification"]
    assert classification["mode"] == "id_only"
    assert classification["refused"]["items"] == [_DECLARATION_REFUSAL_ITEM]
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted == receipt


def test_legacy_mode_less_dry_run_receipt_keeps_the_lenient_branch() -> None:
    """#1140 legacy direction 1: a pre-#1140 receipt has no `mode`, so the
    branch still keys on `outcome == "dry_run"` — the id-only shape with a
    previous model pending removal keeps validating."""
    classification = _mode_classification(
        mode=None,
        previous_sha="1" * 64,
        previous_count=3,
        added=["basin-103"],
        unchanged=["basin-101", "basin-102"],
        removed=[],
        prospective_count=3,
    )
    assert "mode" not in classification

    refresh._validate_registry_classification_field(
        _classification_receipt(
            classification, outcome="dry_run", reason="dry_run_complete"
        )
    )


def test_legacy_mode_less_failed_receipt_still_takes_the_strict_branch() -> None:
    """#1140 legacy direction 2: the fallback must not silently widen.  The same
    id-only shape on an `outcome="failed"` receipt WITHOUT `mode` is still
    rejected — only a receipt that actually declares `mode="id_only"` earns the
    lenient branch.

    `reason` deliberately stays outside `REGISTRY_CUTOVER_REFUSAL_REASONS` so
    the rejection can only come from the previous-side equality arm.
    """
    classification = _mode_classification(
        mode=None,
        previous_sha="1" * 64,
        previous_count=3,
        added=["basin-103"],
        unchanged=["basin-101", "basin-102"],
        removed=[],
        prospective_count=3,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )


@pytest.mark.parametrize(
    ("outcome", "reason", "mode"),
    [
        pytest.param("dry_run", "dry_run_complete", "id_only", id="dry_run_id_only"),
        pytest.param("published", "success", "full", id="published_full"),
        pytest.param("failed", "provider_invalid", "id_only", id="failed_id_only"),
        pytest.param("failed", "provider_invalid", "full", id="failed_full"),
        pytest.param(
            "restored_previous", "provider_postread_failed", "full", id="restored_full"
        ),
        pytest.param(
            "replace_uncertain",
            "provider_replace_uncertain",
            "full",
            id="replace_uncertain_full",
        ),
        pytest.param(
            "published_receipt_failed",
            "primary_receipt_failed",
            "full",
            id="published_receipt_failed_full",
        ),
    ],
)
def test_honest_mode_outcome_pairs_validate(outcome: str, reason: str, mode: str) -> None:
    """#1140 positive control for the cross-pins below: every writer-producible
    mode/outcome pair passes on the same payload the forged pairs use."""
    classification = _mode_classification(mode=mode, **_CROSS_PIN_SHAPE)

    refresh._validate_registry_classification_field(
        _classification_receipt(classification, outcome=outcome, reason=reason)
    )


@pytest.mark.parametrize(
    ("outcome", "reason", "mode"),
    [
        pytest.param("dry_run", "dry_run_complete", "full", id="dry_run_claiming_full"),
        pytest.param("published", "success", "id_only", id="published_claiming_id_only"),
        pytest.param("dry_run", "dry_run_complete", "bogus", id="mode_outside_enum"),
        pytest.param("published", "success", "", id="empty_mode"),
        pytest.param(
            "published_receipt_failed",
            "primary_receipt_failed",
            "id_only",
            id="published_receipt_failed_claiming_id_only",
        ),
    ],
)
def test_forged_mode_outcome_combinations_are_rejected(
    outcome: str, reason: str, mode: str
) -> None:
    """#1140: `dry_run` can only classify id-only and `published` can only
    classify full; a mode outside the enum is rejected outright."""
    classification = _mode_classification(mode=mode, **_CROSS_PIN_SHAPE)

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(classification, outcome=outcome, reason=reason)
        )


def test_forged_emergency_record_cannot_buy_leniency_with_id_only_mode() -> None:
    """#1140 C1: the `published_receipt_failed` cross-pin has teeth.

    That outcome is stamped only onto an already committed `published` receipt,
    so it always classified in full mode — and it is the ONLY outcome
    `reconstruct_primary_receipt` accepts off the untrusted emergency slot.
    This payload breaks the full previous-side equality (`unchanged 1 +
    package_changed 0 + removed 0 != previous_model_count 3`) yet satisfies
    every id-only rule, so without the cross-pin a forged emergency record
    would buy the lenient branch just by declaring `mode="id_only"`.
    """
    classification = _mode_classification(
        mode="id_only",
        previous_sha="1" * 64,
        previous_count=3,
        added=[],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=1,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification,
                outcome="published_receipt_failed",
                reason="primary_receipt_failed",
            )
        )


def test_honest_published_receipt_failed_classification_still_validates() -> None:
    """#1140 C1 honest direction: the cross-pin must not break the real
    emergency-reconstruct corpus.  A committed publish that lost only its
    primary receipt carries a balanced full-mode classification and validates."""
    classification = _mode_classification(
        mode="full",
        previous_sha="1" * 64,
        previous_count=1,
        added=["basin-201"],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=2,
    )

    refresh._validate_registry_classification_field(
        _classification_receipt(
            classification,
            outcome="published_receipt_failed",
            reason="primary_receipt_failed",
        )
    )


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(
            {
                "previous_sha": "1" * 64,
                "previous_count": 3,
                "added": ["basin-104"],
                "unchanged": ["basin-101"],
                "removed": [],
                "prospective_count": 2,
            },
            id="removed_bucket_wiped",
        ),
        pytest.param(
            {
                "previous_sha": "1" * 64,
                "previous_count": 3,
                "added": [],
                "unchanged": ["basin-101", "basin-102", "basin-103", "basin-104"],
                "removed": [],
                "prospective_count": 4,
            },
            id="unchanged_inflated_past_previous_count",
        ),
    ],
)
def test_full_mode_keeps_the_previous_side_equality(shape: dict[str, Any]) -> None:
    """#1140 anti-regression: mode routing must NOT weaken tamper resistance on
    real (full-mode) classifications.  Both payloads break
    `unchanged + package_changed + removed == previous_model_count` and are
    rejected exactly as the outcome-keyed branch rejects them today."""
    classification = _mode_classification(mode="full", **shape)

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(
            {
                "previous_sha": None,
                "previous_count": None,
                "added": ["basin-101"],
                "unchanged": [],
                "removed": ["basin-102"],
                "prospective_count": 1,
            },
            id="bootstrap_removed_entries",
        ),
        pytest.param(
            {
                "previous_sha": "1" * 64,
                "previous_count": 5,
                "added": ["basin-103"],
                "unchanged": ["basin-101", "basin-102"],
                "removed": ["basin-104"],
                "prospective_count": 3,
            },
            id="non_bootstrap_removed_entries",
        ),
        pytest.param(
            {
                "previous_sha": None,
                "previous_count": 7,
                "added": ["basin-101"],
                "unchanged": [],
                "removed": [],
                "prospective_count": 1,
            },
            id="count_without_previous_sha",
        ),
        pytest.param(
            {
                "previous_sha": "1" * 64,
                "previous_count": 2,
                "added": [],
                "unchanged": ["basin-101", "basin-102", "basin-103"],
                "removed": [],
                "prospective_count": 3,
            },
            id="unchanged_above_previous_count",
        ),
        pytest.param(
            {
                "previous_sha": None,
                "previous_count": None,
                "added": [],
                "unchanged": ["basin-101", "basin-102", "basin-103"],
                "removed": [],
                "prospective_count": 3,
            },
            id="bootstrap_unchanged_entries",
        ),
        pytest.param(
            {
                "previous_sha": "1" * 64,
                "previous_count": 3,
                "added": ["basin-103", "basin-104"],
                "unchanged": ["basin-101", "basin-102"],
                "removed": [],
                "prospective_count": 4,
                "new_registry_sha256": "2" * 64,
            },
            id="forged_new_registry_sha",
        ),
    ],
)
def test_id_only_mode_keeps_every_dry_run_constraint(shape: dict[str, Any]) -> None:
    """#1140: the five #1135 dry_run constraints follow the MODE onto failure
    outcomes — they are guarantees of the id-only classify path, not of the
    `dry_run` terminal outcome, so none of them may be lost in the move."""
    classification = _mode_classification(mode="id_only", **shape)

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )


def test_classification_key_set_rejects_an_unknown_extra_key() -> None:
    """#1140 T1: the key-set guard's `keys - required - optional` component.

    `mode` widened the accepted key set from "exactly required" to "required plus
    a named optional"; the payload below is the otherwise-clean stub with ONE
    unknown key added, so only that component can reject it.  Without it any
    junk key rides along on an on-disk receipt."""
    classification = {**_classification_stub(), "bogus": 1}

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="published", reason="success"
            )
        )


def test_classification_key_set_rejects_a_missing_required_key() -> None:
    """#1140 T1: the key-set guard's `required <= keys` component.

    `previous_registry_sha256` is dropped entirely.  Its absence is invisible to
    every downstream rule — `.get()` yields None, which is exactly the legal
    bootstrap value the stub already carries — so only the required-subset check
    can reject this receipt."""
    classification = dict(_classification_stub())
    del classification["previous_registry_sha256"]

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="published", reason="success"
            )
        )


def test_id_only_mode_rejects_non_empty_package_changed() -> None:
    """#1140 T2: the id-only branch's `package_changed_total != 0` pin.

    The id-only classify path never reads a prospective checksum, so it cannot
    emit a `package_changed` row.  Every adjacent id-only rule is satisfied on
    purpose — notably `added 0 + unchanged 1 + package_changed 1 ==
    prospective_count 2` — so only the zero-pin can reject it."""
    classification = _mode_classification(
        mode="id_only",
        previous_sha="1" * 64,
        previous_count=2,
        added=[],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=2,
    )
    classification["package_changed"] = {
        "items": [
            {
                "model_id": "basin-102",
                "old_checksum": "a" * 64,
                "new_checksum": "b" * 64,
            }
        ],
        "total": 1,
        "truncated": False,
    }

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )


def test_id_only_mode_rejects_non_empty_declared_cutovers() -> None:
    """#1140 T2: the id-only branch's `declared_total != 0` pin.

    Declarations are matched against `package_changed`, which the id-only path
    never populates, so a declared row is forged.  The group is expressed as a
    truncated bucket (`total 1`, no items) so `declared_cutovers ⊆
    package_changed` still holds and `package_changed` can stay empty —
    otherwise the subset rule, or the package_changed zero-pin, would mask this
    one."""
    classification = _mode_classification(
        mode="id_only",
        previous_sha="1" * 64,
        previous_count=2,
        added=[],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=1,
    )
    classification["declared_cutovers"] = {
        "items": [],
        "total": 1,
        "truncated": True,
    }

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )


def test_id_only_mode_keeps_the_prospective_count_equality() -> None:
    """#1140 T2: the id-only branch's `added + unchanged + package_changed ==
    prospective_model_count` equality.

    id-only leniency drops the PREVIOUS-side equality only; the prospective side
    is still fully partitioned by the classify path.  Here `added 1 + unchanged
    1 + package_changed 0 != prospective_count 3` while every other id-only rule
    holds (`unchanged 1 <= previous_model_count 3`, no removals, no refusals)."""
    classification = _mode_classification(
        mode="id_only",
        previous_sha="1" * 64,
        previous_count=3,
        added=["basin-103"],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=3,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )


@pytest.mark.parametrize(
    ("outcome", "reason", "mode"),
    [
        pytest.param("dry_run", "dry_run_complete", "id_only", id="dry_run_id_only"),
        pytest.param("published", "success", "full", id="published_full"),
        pytest.param("published", "success", None, id="legacy_without_mode"),
    ],
)
def test_receipt_schema_and_runtime_admit_the_same_classification_modes(
    outcome: str, reason: str, mode: str | None
) -> None:
    """#1140 schema contract: schema and runtime accept the same corpus — both
    mode values AND the mode-less legacy shape."""
    classification = dict(_classification_stub())
    if mode is not None:
        classification["mode"] = mode
    receipt = _receipt_with_classification(
        classification, outcome=outcome, reason=reason
    )

    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("bogus", id="outside_enum"),
        pytest.param("Full", id="wrong_case"),
        pytest.param(None, id="null_mode"),
        pytest.param(5, id="non_string_mode"),
    ],
)
def test_receipt_schema_and_runtime_reject_the_same_bad_classification_mode(
    mode: Any,
) -> None:
    """#1140: an out-of-enum `mode` is refused by BOTH validators, so a receipt
    the schema refuses can never be read back through `_validate_receipt`."""
    classification = {**_classification_stub(), "mode": mode}
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(receipt)
