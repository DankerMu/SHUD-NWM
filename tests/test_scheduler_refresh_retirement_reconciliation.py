"""#1433: retirement-aware reconciliation and the bound generation on the receipt.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). The honest retirement receipt
and every way one can lie -- a retirement outside the removed set, a retired
total above the removed total, an emptied or truncated bucket, a truncated
group without its full item list, the refused lower bound under truncation, the
legacy receipt without the bucket, a retirement on an id-only classification --
the two smuggling refusals and the hex-checksum admission, then #1433 round-1
F-A: the refusal receipt publishes the generation an operator must declare.
"""
from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest

from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _registry_row,
)
from tests.scheduler_refresh_receipt_helpers import (
    _CLASSIFY_GENERATED_AT,
    _classification_receipt,
    _classify,
    _mode_classification,
    _receipt_schema_validator,
    _receipt_with_classification,
)

# ---------------------------------------------------------------------------
# #1433: retirement-aware reconciliation
# ---------------------------------------------------------------------------


def _retirement_classification(
    *,
    removed: list[str],
    retired: list[str],
    refused_total: int,
    retired_total: int | None = None,
    mode: str | None = "full",
    include_bucket: bool = True,
) -> dict[str, Any]:
    """Full-mode classification with `previous = unchanged + removed`."""
    unchanged = ["basin-101"]
    classification: dict[str, Any] = {
        "previous_registry_sha256": "1" * 64,
        "new_registry_sha256": "2" * 64,
        "previous_model_count": len(unchanged) + len(removed),
        "prospective_model_count": len(unchanged),
        "added": {"items": [], "total": 0, "truncated": False},
        "unchanged": {
            "items": unchanged,
            "total": len(unchanged),
            "truncated": False,
        },
        "removed": {"items": removed, "total": len(removed), "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {
            "items": [
                {
                    "model_id": model_id,
                    "old_checksum": None,
                    "new_checksum": None,
                    "reason": "registry_cutover_removal_refused",
                }
                for model_id in removed
                if model_id not in retired
            ],
            "total": refused_total,
            "truncated": False,
        },
        "declared_cutovers": {"items": [], "total": 0, "truncated": False},
    }
    classification["refused"]["truncated"] = refused_total > len(
        classification["refused"]["items"]
    )
    if include_bucket:
        items = [
            {
                "model_id": model_id,
                "old_checksum": "b" * 64,
                "new_checksum": None,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "retire",
            }
            for model_id in retired
        ]
        total = len(items) if retired_total is None else retired_total
        classification["declared_retirements"] = {
            "items": items,
            "total": total,
            "truncated": total > len(items),
        }
    if mode is not None:
        classification["mode"] = mode
    return classification


def test_reconciliation_accepts_an_honest_retirement_receipt() -> None:
    """A fully declared removal is NOT refused, and the lower bound knows it."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )

    refresh._validate_registry_classification_field(
        _classification_receipt(classification, outcome="published", reason="success")
    )


def test_reconciliation_rejects_a_retirement_outside_the_removed_set() -> None:
    """Forged bucket: retiring a row this run never removed."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )
    classification["declared_retirements"]["items"][0]["model_id"] = "basin-999"

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="published", reason="success"
            )
        )


def test_reconciliation_rejects_retired_total_above_removed_total() -> None:
    """F4: a retirement total the removal set cannot support is forged.

    Built so ONLY the total-level inequality can reject it: the bucket is a
    legally-shaped truncated group (a full 256-item list, every id inside
    `removed`), so neither `⊆` nor the honest-truncation shape rule fires — the
    total simply claims 300 retirements out of 260 removals."""
    removed = [f"basin-{index:03d}" for index in range(260)]
    classification = _retirement_classification(
        removed=removed, retired=[], refused_total=0, include_bucket=False
    )
    classification["removed"] = {
        "items": removed[: refresh.MAX_COLLECTION_ITEMS],
        "total": 260,
        "truncated": True,
    }
    # Well-formed on every other axis, so the inequality is the only rule left
    # that can reject this receipt.
    classification["refused"] = {"items": [], "total": 0, "truncated": False}
    classification["declared_retirements"] = {
        "items": [
            {
                "model_id": model_id,
                "old_checksum": "b" * 64,
                "new_checksum": None,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "retire",
            }
            for model_id in removed[: refresh.MAX_COLLECTION_ITEMS]
        ],
        "total": 300,
        "truncated": True,
    }

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="published", reason="success"
            )
        )


def test_reconciliation_rejects_an_emptied_truncated_retirement_bucket() -> None:
    """Round-1 F-B boundary: `retired_total == removed_total` passes BOTH the
    subset check (vacuously — no items to check) and the total inequality, and
    it deducts the whole removal set from the refused lower bound, so a
    `published` receipt with zero refusals looked honest.

    `to_receipt` truncates at exactly MAX_COLLECTION_ITEMS, so a truncated
    bucket holding no items has no legal writer; the shape rule is what rejects
    this."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=[], retired_total=1, refused_total=0
    )
    # The forged receipt claims the removal was declared, so it refuses
    # nothing; every group except the retirement bucket is well-formed.
    classification["refused"] = {"items": [], "total": 0, "truncated": False}
    bucket = classification["declared_retirements"]
    assert (bucket["items"], bucket["total"], bucket["truncated"]) == ([], 1, True)
    assert bucket["total"] == classification["removed"]["total"]

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="published", reason="success"
            )
        )


def test_truncated_classification_groups_must_carry_a_full_item_list() -> None:
    """The same shape rule at its other call sites: the id groups and every
    object group (including the pre-existing `declared_cutovers` hole) admit a
    truncated bucket only with a full item list."""
    honest = _retirement_classification(
        removed=[f"basin-{index:03d}" for index in range(300)],
        retired=[],
        refused_total=300,
        include_bucket=False,
    )
    honest["removed"] = {
        "items": [f"basin-{index:03d}" for index in range(refresh.MAX_COLLECTION_ITEMS)],
        "total": 300,
        "truncated": True,
    }
    honest["refused"] = {
        "items": [
            {
                "model_id": f"basin-{index:03d}",
                "old_checksum": None,
                "new_checksum": None,
                "reason": "registry_cutover_removal_refused",
            }
            for index in range(refresh.MAX_COLLECTION_ITEMS)
        ],
        "total": 300,
        "truncated": True,
    }
    refresh._validate_registry_classification_field(
        _classification_receipt(
            honest, outcome="failed", reason="registry_cutover_removal_refused"
        )
    )

    for group_name in ("removed", "refused"):
        forged = json.loads(json.dumps(honest))
        forged[group_name]["items"] = forged[group_name]["items"][:10]
        with pytest.raises(ValueError, match="receipt_classification_invalid"):
            refresh._validate_registry_classification_field(
                _classification_receipt(
                    forged, outcome="failed", reason="registry_cutover_removal_refused"
                )
            )


def test_refused_lower_bound_survives_a_truncated_retirement_bucket() -> None:
    """Round-1 F-B counterpart: an HONEST run with more than 256 retirements can
    only name 256 of them, so the deduction falls back to the total there.

    Deducting the named intersection in this shape would demand refusals the
    writer never produced."""
    removed = [f"basin-{index:03d}" for index in range(300)]
    classification = _retirement_classification(
        removed=removed, retired=[], refused_total=0, include_bucket=False
    )
    classification["removed"] = {
        "items": removed[: refresh.MAX_COLLECTION_ITEMS],
        "total": 300,
        "truncated": True,
    }
    # Every removal was declared, so the honest run refuses nothing.
    classification["refused"] = {"items": [], "total": 0, "truncated": False}
    classification["declared_retirements"] = {
        "items": [
            {
                "model_id": model_id,
                "old_checksum": "b" * 64,
                "new_checksum": None,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "retire",
            }
            for model_id in removed[: refresh.MAX_COLLECTION_ITEMS]
        ],
        "total": 300,
        "truncated": True,
    }

    refresh._validate_registry_classification_field(
        _classification_receipt(classification, outcome="published", reason="success")
    )


def test_reconciliation_keeps_the_refused_lower_bound_for_undeclared_removals() -> None:
    """Two removals, one retired: the other still has to be refused."""
    honest = _retirement_classification(
        removed=["basin-102", "basin-103"], retired=["basin-102"], refused_total=1
    )
    refresh._validate_registry_classification_field(
        _classification_receipt(
            honest, outcome="failed", reason="registry_cutover_removal_refused"
        )
    )

    forged = _retirement_classification(
        removed=["basin-102", "basin-103"], retired=["basin-102"], refused_total=0
    )
    forged["refused"]["items"] = []
    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(forged, outcome="published", reason="success")
        )


def test_reconciliation_reads_a_legacy_receipt_without_the_bucket() -> None:
    """I7: pre-#1433 receipts on disk carry no bucket and must still validate."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=[], refused_total=1, include_bucket=False
    )
    assert "declared_retirements" not in classification

    refresh._validate_registry_classification_field(
        _classification_receipt(
            classification, outcome="failed", reason="registry_cutover_removal_refused"
        )
    )


def test_reconciliation_rejects_a_retirement_on_an_id_only_classification() -> None:
    """id-only never evaluates removals, so it can never declare a retirement."""
    classification = _mode_classification(
        mode="id_only",
        previous_sha="1" * 64,
        previous_count=2,
        added=[],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=1,
    )
    classification["declared_retirements"] = {
        "items": [
            {
                "model_id": "basin-102",
                "old_checksum": "b" * 64,
                "new_checksum": None,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "retire",
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


def test_receipt_schema_and_runtime_admit_the_same_retirement_bucket() -> None:
    """The on-disk schema and `_validate_receipt` accept the same receipt."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)


def test_a_retire_row_smuggled_into_declared_cutovers_is_rejected() -> None:
    """Per-bucket transition modes: `declared_cutovers` admits `replace` only."""
    classification = _retirement_classification(
        removed=[], retired=[], refused_total=0, include_bucket=False
    )
    classification["package_changed"] = {
        "items": [
            {"model_id": "basin-101", "old_checksum": "a" * 64, "new_checksum": "c" * 64}
        ],
        "total": 1,
        "truncated": False,
    }
    classification["declared_cutovers"] = {
        "items": [
            {
                "model_id": "basin-101",
                "old_checksum": "a" * 64,
                "new_checksum": "c" * 64,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "retire",
            }
        ],
        "total": 1,
        "truncated": False,
    }
    classification["unchanged"] = {"items": [], "total": 0, "truncated": False}
    classification["previous_model_count"] = 1
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_receipt(receipt)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(receipt)


def test_a_replace_row_smuggled_into_declared_retirements_is_rejected() -> None:
    """The mirror image: `declared_retirements` admits `retire` only."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )
    classification["declared_retirements"]["items"][0]["transition_mode"] = "replace"
    classification["declared_retirements"]["items"][0]["new_checksum"] = "c" * 64
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_receipt(receipt)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(receipt)


def test_a_retire_row_carrying_a_new_checksum_is_rejected() -> None:
    """Round-1 F-C: the `retire` mode stays honest but the row claims a new
    package.  The schema pins `new_checksum` to null for this bucket; the
    runtime validator has to reject the same corpus rather than falling through
    to the generic hex check."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )
    retired_row = classification["declared_retirements"]["items"][0]
    retired_row["new_checksum"] = "c" * 64
    assert retired_row["transition_mode"] == "retire"
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_receipt(receipt)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(receipt)


def test_replace_rows_keep_accepting_a_hex_new_checksum() -> None:
    """Control for the pin above: `null_keys` is scoped to the retirement
    bucket, so `declared_cutovers` and `package_changed` still take hex."""
    classification = _retirement_classification(
        removed=[], retired=[], refused_total=0, include_bucket=False
    )
    classification["unchanged"] = {"items": [], "total": 0, "truncated": False}
    classification["previous_model_count"] = 1
    classification["package_changed"] = {
        "items": [
            {"model_id": "basin-101", "old_checksum": "a" * 64, "new_checksum": "c" * 64}
        ],
        "total": 1,
        "truncated": False,
    }
    classification["declared_cutovers"] = {
        "items": [
            {
                "model_id": "basin-101",
                "old_checksum": "a" * 64,
                "new_checksum": "c" * 64,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "replace",
            }
        ],
        "total": 1,
        "truncated": False,
    }
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)


# ---------------------------------------------------------------------------
# #1433 round-1 F-A: the refusal receipt publishes the generation to declare
# ---------------------------------------------------------------------------


def test_full_classification_publishes_the_bound_generation() -> None:
    """The value an operator must copy into a declaration is on the receipt of
    the run that refused them — previously it was nowhere, and the runbook sent
    them to a dry_run that classifies a different model set."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]
    expected = refresh._prospective_registry_generation(
        prospective, generated_at=_CLASSIFY_GENERATED_AT
    )

    result, reason = _classify(previous=previous, prospective=prospective)

    assert reason == "registry_cutover_removal_refused"
    assert result.generation == expected
    assert result.to_receipt()["generation"] == expected


def test_id_only_classification_publishes_no_generation() -> None:
    """dry_run derives its generation from checksum-less rows, so the value is
    not the one a real publish binds; the writer pins it to None (same rule as
    `new_registry_sha256`)."""
    previous = [_registry_row("basin-101", "a" * 64)]
    prospective = [{"model_id": "basin-101", "basin_id": "basin-basin-101"}]

    result, _ = _classify(previous=previous, prospective=prospective, dry_run=True)

    assert result.mode == "id_only"
    assert result.generation is None
    assert result.to_receipt()["generation"] is None


def test_receipt_validators_admit_the_generation_key() -> None:
    """Schema and runtime accept the same generation corpus, and a legacy
    receipt without the key keeps validating."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )
    classification["generation"] = "manifest-b44ab3b785f4"
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )
    refresh._validate_receipt(receipt)
    _receipt_schema_validator().validate(receipt)

    legacy = json.loads(json.dumps(receipt))
    del legacy["registry_classification"]["generation"]
    refresh._validate_receipt(legacy)
    _receipt_schema_validator().validate(legacy)


@pytest.mark.parametrize(
    "generation",
    [
        pytest.param("", id="empty"),
        pytest.param("manifest b44ab3b785f4", id="space"),
        pytest.param("manifest/../etc", id="path_traversal_charset"),
        pytest.param("m" * 129, id="over_length"),
        pytest.param(5, id="non_string"),
    ],
)
def test_receipt_validators_reject_the_same_bad_generation(generation: Any) -> None:
    """An out-of-corpus generation is refused by BOTH validators — the operator
    copies this value into a declaration, whose schema uses the same pattern."""
    classification = _retirement_classification(
        removed=["basin-102"], retired=["basin-102"], refused_total=0
    )
    classification["generation"] = generation
    receipt = _receipt_with_classification(
        classification, outcome="published", reason="success"
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_receipt(receipt)
    with pytest.raises(jsonschema.ValidationError):
        _receipt_schema_validator().validate(receipt)


def test_id_only_receipt_carrying_a_generation_is_rejected() -> None:
    """Forgery pin: the id-only writer never emits one, so a non-null
    generation on an id-only classification has no legal writer."""
    classification = _mode_classification(
        mode="id_only",
        previous_sha="1" * 64,
        previous_count=2,
        added=[],
        unchanged=["basin-101"],
        removed=[],
        prospective_count=1,
    )
    classification["generation"] = "manifest-b44ab3b785f4"

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._validate_registry_classification_field(
            _classification_receipt(
                classification, outcome="failed", reason="provider_invalid"
            )
        )

    classification["generation"] = None
    refresh._validate_registry_classification_field(
        _classification_receipt(
            classification, outcome="failed", reason="provider_invalid"
        )
    )
