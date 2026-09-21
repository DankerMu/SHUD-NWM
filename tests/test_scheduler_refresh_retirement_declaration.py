"""#1433: the declared-retirement channel -- decision grid and declaration schema.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). The eleven-cell
`_classify_registry` retirement grid (matching entry, wrong old checksum, no
entry, replace-on-removed, still-published, never-canonical, generation
binding, the declared/undeclared split, dry-run, cross-rule poison and its
order independence), the skip-cause evidence family, and the declaration
schema/loader pins: transition modes against the schema enum, the receipt
generation corpus, the mode/checksum pairing, the existing replace-only file
and the committed example.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _registry_row,
    _write_declaration,
)
from tests.scheduler_refresh_receipt_helpers import (
    _classify,
    _receipt_schema_validator,
)


def _full_failed_receipt(classification: dict[str, Any]) -> dict[str, Any]:
    """A full receipt shape carrying ``classification`` for runtime + schema validation."""
    receipt = refresh._receipt(
        run_id="refresh_unreadable_skip_cause",
        started=refresh.datetime(2026, 8, 16, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_removal_refused",
        phase="precommit",
        providers=[],
        registry_classification=classification,
    )
    return {
        **receipt,
        "cutover_gate": {
            "mode": "enforced",
            "declaration_env": None,
            "declaration_present": False,
        },
    }


def _retire_entry(model_id: str, old_checksum: str) -> dict[str, object]:
    return {
        "model_id": model_id,
        "old_checksum": old_checksum,
        "new_checksum": None,
        "effective_cycle_utc": "2026-07-15T00:00:00Z",
        "transition_mode": "retire",
    }


def _replace_entry(model_id: str, old_checksum: str, new_checksum: str) -> dict[str, object]:
    return {
        "model_id": model_id,
        "old_checksum": old_checksum,
        "new_checksum": new_checksum,
        "effective_cycle_utc": "2026-07-15T00:00:00Z",
        "transition_mode": "replace",
    }


def _refusal_reasons(result: Any) -> list[tuple[str, str]]:
    return sorted((item["model_id"], item["reason"]) for item in result.refused)


def test_classify_grid_cell1_matching_retire_entry_admits_the_removal() -> None:
    """Grid 1: the removal enters `declared_retirements` and nothing refuses."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-102", "b" * 64)],
    )

    assert reason is None
    assert result.removed == ["basin-102"]
    assert result.refused == []
    assert result.declared_retirements == [
        {
            "model_id": "basin-102",
            "old_checksum": "b" * 64,
            "new_checksum": None,
            "effective_cycle_utc": "2026-07-15T00:00:00Z",
            "transition_mode": "retire",
        }
    ]


def test_classify_grid_cell2_retire_entry_with_wrong_old_checksum_is_invalid() -> None:
    """Grid 2: the checksum mismatch poisons the declaration, and the removal
    produces exactly ONE refusal row (no extra `removal_refused` for it)."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-102", "9" * 64)],
    )

    assert reason == "registry_cutover_declaration_invalid"
    assert result.declared_retirements == []
    assert _refusal_reasons(result) == [
        ("basin-102", "registry_cutover_declaration_invalid")
    ]


def test_classify_grid_cell3_removal_without_any_entry_keeps_the_1080_refusal() -> None:
    """Grid 3: #1080's fail-closed removal refusal is untouched."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(previous=previous, prospective=prospective)

    assert reason == "registry_cutover_removal_refused"
    assert result.declared_retirements == []
    assert result.refused == [
        {
            "model_id": "basin-102",
            "old_checksum": "b" * 64,
            "new_checksum": None,
            "reason": "registry_cutover_removal_refused",
        }
    ]


def test_classify_grid_cell4_replace_entry_on_a_removed_model_refuses_twice() -> None:
    """Grid 4: a `replace` entry naming a removed model is BOTH an unknown-id
    declaration error (rule 1) and an undeclared removal (rule 4) — two rows."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_replace_entry("basin-102", "b" * 64, "c" * 64)],
    )

    assert reason == "registry_cutover_declaration_invalid"
    assert result.declared_retirements == []
    assert _refusal_reasons(result) == [
        ("basin-102", "registry_cutover_declaration_invalid"),
        ("basin-102", "registry_cutover_removal_refused"),
    ]


def test_classify_grid_cell5_retiring_a_still_published_model_is_invalid() -> None:
    """Grid 5: a retirement can only name a model that is actually leaving."""
    previous = [_registry_row("basin-101", "a" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-101", "a" * 64)],
    )

    assert reason == "registry_cutover_declaration_invalid"
    assert result.removed == []
    assert result.declared_retirements == []
    assert _refusal_reasons(result) == [
        ("basin-101", "registry_cutover_declaration_invalid")
    ]


def test_classify_grid_cell6_retiring_a_never_canonical_model_is_invalid() -> None:
    """Grid 6: retiring a row that was never in the previous canonical set."""
    previous = [_registry_row("basin-101", "a" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-999", "b" * 64)],
    )

    assert reason == "registry_cutover_declaration_invalid"
    assert result.declared_retirements == []
    assert _refusal_reasons(result) == [
        ("basin-999", "registry_cutover_declaration_invalid")
    ]


def test_classify_grid_cell7_retire_entry_inherits_the_generation_binding() -> None:
    """Grid 7: the #1080 generation binding applies to retirements verbatim."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-102", "b" * 64)],
        generation="manifest-deadbeefcafe",
    )

    assert reason == "registry_cutover_declaration_invalid"
    assert result.declared_retirements == []
    assert ("__declaration__", "registry_cutover_declaration_invalid") in _refusal_reasons(
        result
    )


def test_classify_grid_cell8_declared_and_undeclared_removals_split() -> None:
    """Grid 8: one removal is admitted, the other keeps failing closed, and the
    run's reason is the removal refusal (not declaration-invalid)."""
    previous = [
        _registry_row("basin-101", "a" * 64),
        _registry_row("basin-102", "b" * 64),
        _registry_row("basin-103", "c" * 64),
    ]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-102", "b" * 64)],
    )

    assert reason == "registry_cutover_removal_refused"
    assert [item["model_id"] for item in result.declared_retirements] == ["basin-102"]
    assert _refusal_reasons(result) == [
        ("basin-103", "registry_cutover_removal_refused")
    ]


def test_classify_grid_cell9_dry_run_never_evaluates_a_retirement() -> None:
    """Grid 9: the id-only early return is untouched — no removal, no bucket."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [{"model_id": "basin-101", "basin_id": "basin-basin-101"}]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[_retire_entry("basin-102", "b" * 64)],
        dry_run=True,
    )

    assert reason is None
    assert result.mode == "id_only"
    assert result.removed == []
    assert result.declared_retirements == []
    assert result.refused == []


def test_classify_grid_cell10_poison_from_another_rule_blocks_the_retirement() -> None:
    """Grid 10: an unrelated invalid entry (rule 1) means the declaration is not
    valid as a whole, so the otherwise-legal retirement is refused, not booked."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        entries=[
            _replace_entry("basin-777", "a" * 64, "c" * 64),
            _retire_entry("basin-102", "b" * 64),
        ],
    )

    assert reason == "registry_cutover_declaration_invalid"
    assert result.declared_retirements == []
    assert _refusal_reasons(result) == [
        ("basin-102", "registry_cutover_declaration_invalid"),
        ("basin-777", "registry_cutover_declaration_invalid"),
    ]


def test_classify_grid_cell11_retire_poison_is_order_independent() -> None:
    """Grid 11: one bad retirement poisons the declaration for the good one, and
    the outcome does not depend on the order the removals are iterated in.

    The single-pass shape rule 2 uses would book the good retirement whenever it
    happened to be visited first — for a destructive row deletion that is not
    acceptable, so rule 4 settles the poison bit before anything enters the
    bucket."""
    rows = {
        "basin-102": _registry_row("basin-102", "b" * 64),
        "basin-103": _registry_row("basin-103", "c" * 64),
    }
    prospective = [_registry_row("basin-101", "a" * 64)]
    entries = [
        _retire_entry("basin-102", "b" * 64),  # honest
        _retire_entry("basin-103", "9" * 64),  # wrong old_checksum
    ]

    payloads = []
    for order in (("basin-102", "basin-103"), ("basin-103", "basin-102")):
        previous = [_registry_row("basin-101", "a" * 64)] + [rows[name] for name in order]
        result, reason = _classify(
            previous=previous, prospective=prospective, entries=entries
        )
        assert reason == "registry_cutover_declaration_invalid"
        assert result.declared_retirements == []
        payloads.append(result.to_receipt())

    assert payloads[0] == payloads[1]
    assert _refusal_reasons_from_receipt(payloads[0]) == [
        ("basin-102", "registry_cutover_declaration_invalid"),
        ("basin-103", "registry_cutover_declaration_invalid"),
    ]


def _refusal_reasons_from_receipt(payload: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (item["model_id"], item["reason"]) for item in payload["refused"]["items"]
    )


def test_removal_refusal_carries_skip_cause_evidence_when_publish_skipped_it() -> None:
    """#1433/#1553 evidence layer: the refusal says WHY the row disappeared."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={
            "basin-102": {
                "status": "partial",
                "missing_required_files": ["*.tsd.rl"],
                "invalid_required_files": ["basin-102.cfg.ic: 2 numeric token(s)"],
                "unreadable_required_files": ["basin-102.tsd.lai: required file could not be read for checksum"],
            }
        },
    )

    assert reason == "registry_cutover_removal_refused"
    assert result.refused == [
        {
            "model_id": "basin-102",
            "old_checksum": "b" * 64,
            "new_checksum": None,
            "reason": "registry_cutover_removal_refused",
            "status": "partial",
            "missing_required_files": ["*.tsd.rl"],
            "invalid_required_files": ["basin-102.cfg.ic: 2 numeric token(s)"],
            "unreadable_required_files": ["basin-102.tsd.lai: required file could not be read for checksum"],
        }
    ]
    refresh._validate_object_group(
        result.to_receipt()["refused"],
        required_keys={"model_id", "reason"},
        optional_keys={"old_checksum", "new_checksum"} | set(refresh._SKIP_CAUSE_KEYS),
        reason_enum=refresh.REGISTRY_CUTOVER_REFUSAL_REASONS,
    )


def test_removal_refusal_of_a_deleted_directory_omits_the_skip_cause_keys() -> None:
    """#1433 discriminator: no inventory row means the model directory is gone,
    and the refusal must NOT invent skip-cause keys for it."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    skipped, _ = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={"basin-102": {"status": "partial"}},
    )
    deleted, _ = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={"basin-999": {"status": "partial"}},
    )

    assert set(skipped.refused[0]) >= set(refresh._SKIP_CAUSE_KEYS)
    assert set(deleted.refused[0]) & set(refresh._SKIP_CAUSE_KEYS) == set()
    assert deleted.refused[0]["reason"] == "registry_cutover_removal_refused"


def test_skip_cause_evidence_is_bounded_on_the_write_side() -> None:
    """The lists are operator-data; both caps are applied where the row is
    written, so an oversized inventory row cannot blow the receipt bounds."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]

    result, _ = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={
            "basin-102": {
                "status": "x" * (refresh.MAX_STRING_LENGTH + 10),
                "missing_required_files": [
                    f"file-{index}" for index in range(refresh.MAX_COLLECTION_ITEMS + 20)
                ],
                "invalid_required_files": ["y" * (refresh.MAX_STRING_LENGTH + 10)],
                # Long entry first: the list truncates at MAX_COLLECTION_ITEMS,
                # so the over-long string must sit inside the kept prefix to be
                # observed (the item-level cap and the string-level cap are
                # asserted independently).
                "unreadable_required_files": ["w" * (refresh.MAX_STRING_LENGTH + 10)]
                + [f"z-{index}" for index in range(refresh.MAX_COLLECTION_ITEMS + 20)],
            }
        },
    )

    refusal = result.refused[0]
    assert len(refusal["status"]) == refresh.MAX_STRING_LENGTH
    assert len(refusal["missing_required_files"]) == refresh.MAX_COLLECTION_ITEMS
    assert len(refusal["invalid_required_files"][0]) == refresh.MAX_STRING_LENGTH
    assert len(refusal["unreadable_required_files"]) == refresh.MAX_COLLECTION_ITEMS
    assert len(refusal["unreadable_required_files"][0]) == refresh.MAX_STRING_LENGTH
    refresh._validate_value_bounds(result.to_receipt())


def test_unreadable_skip_cause_runtime_and_jsonschema_agree_on_the_same_receipt() -> None:
    """#1553: runtime validator and JSON Schema accept the same three-key receipt.

    A receipt carrying ``unreadable_required_files`` alongside the two existing
    skip-cause lists validates through ``_validate_receipt`` AND through the
    receipt JSON Schema (additionalProperties false admits the additive key).
    """

    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]
    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={
            "basin-102": {
                "status": "partial",
                "missing_required_files": ["*.tsd.rl"],
                "invalid_required_files": ["basin-102.cfg.ic: 2 numeric token(s)"],
                "unreadable_required_files": ["basin-102.tsd.lai: required file could not be read for checksum"],
            }
        },
    )
    assert reason == "registry_cutover_removal_refused"
    # The receipt must actually carry the new key: validating a keyless receipt
    # proves nothing about the schema/runtime admission of it.
    refusal = result.to_receipt()["refused"]["items"][0]
    assert refusal["unreadable_required_files"] == [
        "basin-102.tsd.lai: required file could not be read for checksum"
    ]
    full = _full_failed_receipt(result.to_receipt())
    refresh._validate_receipt(full)
    _receipt_schema_validator().validate(full)


def test_unreadable_skip_cause_over_bound_truncates_identically_at_runtime_and_schema() -> None:
    """#1553: an over-bound unreadable list truncates and both validators agree."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]
    result, _ = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={
            "basin-102": {
                "status": "partial",
                "missing_required_files": [],
                "invalid_required_files": [],
                # Over-long entry first: the kept prefix must contain it for the
                # string cap to be observable after item-level truncation.
                "unreadable_required_files": ["v" * (refresh.MAX_STRING_LENGTH + 10)]
                + [f"u-{index}" for index in range(refresh.MAX_COLLECTION_ITEMS + 20)],
            }
        },
    )
    receipt = result.to_receipt()
    refusal = receipt["refused"]["items"][0]
    assert len(refusal["unreadable_required_files"]) == refresh.MAX_COLLECTION_ITEMS
    assert len(refusal["unreadable_required_files"][0]) == refresh.MAX_STRING_LENGTH
    full = _full_failed_receipt(receipt)
    refresh._validate_receipt(full)
    _receipt_schema_validator().validate(full)


def test_historical_receipt_without_unreadable_key_still_validates(
    tmp_path: Path,
) -> None:
    """#1553: old receipts carrying only status/missing/invalid remain valid."""
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [_registry_row("basin-101", "a" * 64)]
    result, reason = _classify(
        previous=previous,
        prospective=prospective,
        skipped_models={
            "basin-102": {
                "status": "partial",
                "missing_required_files": ["*.tsd.rl"],
                "invalid_required_files": ["basin-102.cfg.ic: 2 numeric token(s)"],
            }
        },
    )
    assert reason == "registry_cutover_removal_refused"
    receipt = result.to_receipt()
    # Simulate a HISTORICAL receipt: strip the additive key exactly as a
    # pre-#1553 writer would have produced it.
    for item in receipt["refused"]["items"]:
        item.pop("unreadable_required_files", None)
    assert "unreadable_required_files" not in receipt["refused"]["items"][0]
    full = _full_failed_receipt(receipt)
    refresh._validate_receipt(full)
    _receipt_schema_validator().validate(full)


# ---------------------------------------------------------------------------
# #1433: declaration schema / loader
# ---------------------------------------------------------------------------


def test_declaration_transition_modes_match_the_schema_enum() -> None:
    """The publisher constant and the schema enum are one corpus.

    (The consumer's copy of the constant is pinned to the same enum by
    ``tests/test_scheduler_generation.py``.)
    """
    schema_enum = set(
        refresh._CUTOVER_DECLARATION_SCHEMA["properties"]["entries"]["items"][
            "properties"
        ]["transition_mode"]["enum"]
    )
    assert schema_enum == set(refresh.CUTOVER_TRANSITION_MODES)
    assert set(refresh.CUTOVER_REPLACE_TRANSITION_MODES) | set(
        refresh.CUTOVER_RETIRE_TRANSITION_MODES
    ) == schema_enum
    assert not set(refresh.CUTOVER_REPLACE_TRANSITION_MODES) & set(
        refresh.CUTOVER_RETIRE_TRANSITION_MODES
    )


def test_receipt_generation_corpus_matches_the_declaration_schema() -> None:
    """The receipt's `generation` and the declaration's must be ONE corpus.

    The operator's move is a copy-paste: read
    `registry_classification.generation` off a refusal receipt, write it into a
    declaration.  If the declaration schema ever narrows its `generation` field
    and the receipt side does not, the receipt keeps publishing values the
    declaration rejects — a silent drift that looks like an operator error.
    Same relation pin as the transition-mode enum above, across all three
    sites: runtime constants, declaration schema, receipt schema."""
    declaration_generation = refresh._CUTOVER_DECLARATION_SCHEMA["properties"]["generation"]
    receipt_generation = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )["properties"]["registry_classification"]["properties"]["generation"]
    receipt_string_branch = next(
        branch for branch in receipt_generation["oneOf"] if branch.get("type") == "string"
    )

    assert declaration_generation["pattern"] == refresh.GENERATION_PATTERN.pattern
    assert declaration_generation["maxLength"] == refresh.MAX_GENERATION_LENGTH
    # The runtime's non-empty check is this minLength; keep them in step.
    assert declaration_generation["minLength"] == 1
    assert receipt_string_branch == {
        "type": "string",
        "minLength": declaration_generation["minLength"],
        "maxLength": declaration_generation["maxLength"],
        "pattern": declaration_generation["pattern"],
    }
    # And the receipt side additionally admits the id-only null.
    assert {"type": "null"} in receipt_generation["oneOf"]


@pytest.mark.parametrize(
    ("entry", "accepted"),
    [
        pytest.param(
            {"transition_mode": "retire", "new_checksum": None}, True, id="retire_null"
        ),
        pytest.param(
            {"transition_mode": "retire", "new_checksum": "c" * 64},
            False,
            id="retire_with_checksum",
        ),
        pytest.param(
            {"transition_mode": "replace", "new_checksum": None},
            False,
            id="replace_without_checksum",
        ),
        pytest.param(
            {"transition_mode": "replace", "new_checksum": "c" * 64},
            True,
            id="replace_hex",
        ),
    ],
)
def test_declaration_loader_mirrors_the_schema_mode_checksum_pairing(
    tmp_path: Path, entry: dict[str, object], accepted: bool
) -> None:
    """The mode/checksum conditional binds in BOTH the schema and the loader."""
    declaration = _write_declaration(
        tmp_path,
        generation="manifest-abcdef123456",
        entries=[
            {
                "model_id": "basin-101",
                "old_checksum": "a" * 64,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                **entry,
            }
        ],
    )
    now = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)

    if accepted:
        payload = refresh._load_cutover_declaration(str(declaration), now=now)
        assert payload["entries"][0]["transition_mode"] == entry["transition_mode"]
        refresh._CUTOVER_DECLARATION_VALIDATOR.validate(payload)
    else:
        with pytest.raises(refresh.RefreshError) as info:
            refresh._load_cutover_declaration(str(declaration), now=now)
        assert info.value.reason == "registry_cutover_declaration_invalid"
        with pytest.raises(jsonschema.ValidationError):
            refresh._CUTOVER_DECLARATION_VALIDATOR.validate(
                json.loads(declaration.read_text())
            )


def test_existing_replace_only_declaration_file_still_loads_verbatim(
    tmp_path: Path,
) -> None:
    """Forward-compat anchor: the v1 file operators already write is unchanged."""
    declaration = _write_declaration(
        tmp_path,
        generation="manifest-abcdef123456",
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

    payload = refresh._load_cutover_declaration(
        str(declaration), now=refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    )

    assert payload["schema_version"] == "nhms.scheduler.registry_package_cutover.v1"
    assert payload["entries"][0]["new_checksum"] == "c" * 64


def test_committed_declaration_example_validates_against_its_schema() -> None:
    """CI pairs `<base>.example.json` with `<base>.schema.json`; keep them green
    here too so a schema edit that orphans the example fails locally first."""
    example = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/examples/scheduler_registry_package_cutover.example.json"
        ).read_text(encoding="utf-8")
    )

    refresh._CUTOVER_DECLARATION_VALIDATOR.validate(example)

    modes = {entry["transition_mode"] for entry in example["entries"]}
    assert modes == {"replace", "retire"}
