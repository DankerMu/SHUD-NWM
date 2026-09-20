"""#1080 registry cutover gate: direct classification and refusal coverage.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). The gate driven directly:
byte-identical superset admission, the undeclared package-checksum drift
refusal, a valid declaration for a specific checksum transition, the invalid
declaration-mode corpus, the removed previously-canonical refusal, first
publication, bounded evidence truncation, the dry-run id-only classification,
receipt binding on success and on refusal, and generation determinism.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _config,
    _registry_row,
    _run_gate,
    _stub_provider_pipeline,
    _write_declaration,
    _write_previous_canonical,
)
from tests.scheduler_refresh_receipt_helpers import (
    _assert_classification_reconciles,
)


def test_cutover_gate_admits_prospective_superset_with_byte_identical_existing_rows(
    tmp_path: Path,
) -> None:
    """(a) 13 previous + 6 new -> 6 added + 13 unchanged, refresh proceeds."""
    config = _config(tmp_path)
    previous = [
        _registry_row(f"basin-1{index:02d}", "a" * 64)
        for index in range(1, 14)
    ]
    prospective = previous + [
        _registry_row(f"basin-2{index:02d}", "b" * 64)
        for index in range(1, 7)
    ]

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
    )

    assert error is None
    payload = captured[0]
    assert payload["added"]["total"] == 6
    assert payload["unchanged"]["total"] == 13
    assert payload["removed"]["total"] == 0
    assert payload["package_changed"]["total"] == 0
    assert payload["refused"]["total"] == 0
    assert payload["declared_cutovers"]["total"] == 0
    assert payload["previous_registry_sha256"] is not None
    assert payload["new_registry_sha256"] is not None


def test_cutover_gate_refuses_undeclared_package_checksum_drift(tmp_path: Path) -> None:
    """(b) 1 existing package_changed without declaration -> refusal + previous intact.

    Also asserts (T8 / C-E9) that inode and mtime of the previous canonical
    file survive the refusal, not just the byte content.
    """
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [
        _registry_row("basin-101", "c" * 64),  # checksum drift, undeclared
        _registry_row("basin-102", "b" * 64),
    ]
    registry_path = _write_previous_canonical(config, previous)
    before = registry_path.read_bytes()
    before_stat = registry_path.stat()

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_undeclared"
    assert error.details["provider_phase"] == "precommit"
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
    assert payload["refused"]["items"][0]["reason"] == "registry_cutover_undeclared"
    assert payload["refused"]["items"][0]["model_id"] == "basin-101"
    assert payload["refused"]["items"][0]["old_checksum"] == "a" * 64
    assert payload["refused"]["items"][0]["new_checksum"] == "c" * 64
    # Previous canonical bytes, inode, and mtime untouched.
    assert registry_path.read_bytes() == before
    after_stat = registry_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    _assert_classification_reconciles(
        payload, previous_count=2, prospective_count=2
    )


def test_cutover_gate_admits_valid_declaration_for_specific_checksum_transition(
    tmp_path: Path,
) -> None:
    """(c) Valid declaration accepts the same transition."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [
        _registry_row("basin-101", "c" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    generated_at = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    generation = refresh._prospective_registry_generation(
        prospective, generated_at=generated_at
    )
    declaration = _write_declaration(
        tmp_path,
        generation=generation,
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

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
        declaration_path=declaration,
        generated_at=generated_at,
        now=generated_at,
    )

    assert error is None, error
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
    assert payload["declared_cutovers"]["total"] == 1
    assert payload["declared_cutovers"]["items"][0]["model_id"] == "basin-101"
    assert payload["declared_cutovers"]["items"][0]["transition_mode"] == "replace"
    assert payload["refused"]["total"] == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "schema_invalid",
        "wrong_generation",
        "wrong_old_checksum",
        "wrong_new_checksum",
        "non_cycle_aligned",
        "out_of_window_past",
        "out_of_window_future",
        "duplicate_model_id",
        "unknown_model_id",
        "symlinked_declaration",
        "over_size_declaration",
    ],
)
def test_cutover_gate_rejects_invalid_declaration_modes(
    tmp_path: Path, corruption: str
) -> None:
    """(d) Every invalid declaration mode fails closed."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [
        _registry_row("basin-101", "c" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    registry_path = _write_previous_canonical(config, previous)
    before = registry_path.read_bytes()
    before_stat = registry_path.stat()
    generated_at = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    generation = refresh._prospective_registry_generation(
        prospective, generated_at=generated_at
    )
    valid_entry = {
        "model_id": "basin-101",
        "old_checksum": "a" * 64,
        "new_checksum": "c" * 64,
        "effective_cycle_utc": "2026-07-15T00:00:00Z",
        "transition_mode": "replace",
    }
    declaration_path: Path
    if corruption == "schema_invalid":
        # Missing the schema_version key entirely.
        declaration_path = tmp_path / "declaration.json"
        declaration_path.write_text('{"entries": []}\n')
    elif corruption == "symlinked_declaration":
        target = tmp_path / "real-declaration.json"
        target.write_text(
            json.dumps(
                {
                    "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                    "generated_at": "2026-07-14T00:00:00Z",
                    "generation": generation,
                    "entries": [valid_entry],
                },
                sort_keys=True,
            )
        )
        declaration_path = tmp_path / "declaration.json"
        declaration_path.symlink_to(target)
    elif corruption == "over_size_declaration":
        declaration_path = tmp_path / "declaration.json"
        oversized = {
            "schema_version": "nhms.scheduler.registry_package_cutover.v1",
            "generated_at": "2026-07-14T00:00:00Z",
            "generation": generation,
            "entries": [valid_entry],
        }
        # Inflate the payload well past MAX_CUTOVER_DECLARATION_BYTES via
        # padding-embedded entries that still schema-validate.  The bounded
        # no-follow read rejects the file before parsing.
        entries = []
        for index in range(300):  # >256-cap forces schema-invalidity too
            entries.append(
                {
                    "model_id": f"basin-{index:03d}",
                    "old_checksum": "a" * 64,
                    "new_checksum": "c" * 64,
                    "effective_cycle_utc": "2026-07-15T00:00:00Z",
                    "transition_mode": "replace",
                }
            )
        oversized["entries"] = entries
        raw = (json.dumps(oversized, sort_keys=True) + "\n").encode()
        # Pad to actually exceed the byte cap.
        raw = raw + b" " * (refresh.MAX_CUTOVER_DECLARATION_BYTES + 1024)
        declaration_path.write_bytes(raw)
    else:
        entry = dict(valid_entry)
        gen_for_file = generation
        if corruption == "wrong_generation":
            gen_for_file = "manifest-99999999-deadbeefcafe"
        elif corruption == "wrong_old_checksum":
            entry["old_checksum"] = "9" * 64
        elif corruption == "wrong_new_checksum":
            entry["new_checksum"] = "9" * 64
        elif corruption == "non_cycle_aligned":
            entry["effective_cycle_utc"] = "2026-07-15T06:00:00Z"
        elif corruption == "out_of_window_past":
            entry["effective_cycle_utc"] = "2026-07-10T00:00:00Z"
        elif corruption == "out_of_window_future":
            entry["effective_cycle_utc"] = "2027-01-01T00:00:00Z"
        elif corruption == "duplicate_model_id":
            declaration_path = _write_declaration(
                tmp_path,
                generation=gen_for_file,
                entries=[valid_entry, dict(valid_entry)],
            )
        elif corruption == "unknown_model_id":
            entry["model_id"] = "basin-does-not-exist"
        if corruption not in {"duplicate_model_id"}:
            declaration_path = _write_declaration(
                tmp_path, generation=gen_for_file, entries=[entry]
            )

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
        declaration_path=declaration_path,
        generated_at=generated_at,
        now=generated_at,
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_declaration_invalid"
    assert error.details["provider_phase"] == "precommit"
    # Previous canonical bytes, inode, and mtime untouched (T8 / C-E9).
    assert registry_path.read_bytes() == before
    after_stat = registry_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )


def test_cutover_gate_refuses_removed_previously_canonical_model(tmp_path: Path) -> None:
    """(e) Previously canonical model removed -> refusal, previous intact.

    Explicitly asserts (T7 / C-E8) that the removed row's refused entry
    carries the previous checksum in ``old_checksum`` and ``null`` in
    ``new_checksum``, and asserts (T8 / C-E9) that inode+mtime survive.
    """
    config = _config(tmp_path)
    previous = [
        _registry_row("basin-101", "a" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    prospective = [_registry_row("basin-101", "a" * 64)]  # basin-102 dropped
    registry_path = _write_previous_canonical(config, previous)
    before = registry_path.read_bytes()
    before_stat = registry_path.stat()

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_removal_refused"
    payload = captured[0]
    assert payload["removed"]["items"] == ["basin-102"]
    refused_removed = next(
        item
        for item in payload["refused"]["items"]
        if item["reason"] == "registry_cutover_removal_refused"
    )
    assert refused_removed["model_id"] == "basin-102"
    # Symmetric to the drift branch: previous checksum is populated, no new
    # checksum exists on the prospective side (T7 / C-E8).
    assert refused_removed["old_checksum"] == "b" * 64
    assert refused_removed["new_checksum"] is None
    # bytes / inode / mtime survive (T8 / C-E9).
    assert registry_path.read_bytes() == before
    after_stat = registry_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    _assert_classification_reconciles(
        payload, previous_count=2, prospective_count=1
    )


def test_cutover_gate_missing_previous_canonical_is_first_publication(tmp_path: Path) -> None:
    """(f) Missing previous canonical registry -> every row is `added`."""
    config = _config(tmp_path)
    prospective = [
        _registry_row("basin-a", "a" * 64),
        _registry_row("basin-b", "b" * 64),
    ]
    # Do not write any previous manifest.
    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=None
    )

    assert error is None
    payload = captured[0]
    assert payload["previous_registry_sha256"] is None
    assert payload["added"]["total"] == 2
    assert set(payload["added"]["items"]) == {"basin-a", "basin-b"}
    assert payload["unchanged"]["total"] == 0
    assert payload["removed"]["total"] == 0


def test_cutover_classification_bounded_evidence_truncates_over_cap(tmp_path: Path) -> None:
    """Classification arrays cap at 256 with total + truncated fields."""
    config = _config(tmp_path)
    # 300 previous canonical models, all removed in the prospective set.
    previous = [_registry_row(f"basin-{index:03d}", "a" * 64) for index in range(300)]
    prospective: list[dict[str, object]] = []
    _write_previous_canonical(config, previous)

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    # Empty prospective is technically valid classification (all removed).
    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_removal_refused"
    payload = captured[0]
    assert payload["removed"]["total"] == 300
    assert payload["removed"]["truncated"] is True
    assert len(payload["removed"]["items"]) == refresh.MAX_COLLECTION_ITEMS
    assert payload["refused"]["total"] == 300
    assert payload["refused"]["truncated"] is True
    assert len(payload["refused"]["items"]) == refresh.MAX_COLLECTION_ITEMS


def test_cutover_gate_dry_run_reports_id_only_classification_without_refusal(
    tmp_path: Path,
) -> None:
    """dry_run: id-only classification, no refusal even if drift-shaped."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64)]
    # In dry-run mode the prospective rows carry only id/basin_id.
    prospective = [
        {"model_id": "basin-101", "basin_id": "basin-basin-101"},
        {"model_id": "basin-201", "basin_id": "basin-201"},
    ]

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
        dry_run=True,
    )

    assert error is None
    payload = captured[0]
    assert payload["added"]["items"] == ["basin-201"]
    assert payload["unchanged"]["items"] == ["basin-101"]
    assert payload["package_changed"]["total"] == 0
    assert payload["new_registry_sha256"] is None  # dry_run does not publish


def test_refresh_receipt_binds_registry_classification_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful published receipt carries a `registry_classification` payload."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    # Seed one previous canonical model so classification has real content.
    previous_models = [_registry_row("model-1", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline(monkeypatch)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    classification = receipt["registry_classification"]
    assert classification["previous_registry_sha256"] is not None
    # Stubbed pipeline emits 13 minimal id-only rows.
    assert classification["added"]["total"] + classification["unchanged"]["total"] == 13


def test_refresh_receipt_binds_registry_classification_on_undeclared_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused refresh emits registry_classification pinpointing the drift."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("model-a", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline(monkeypatch)

    def publish_registry_with_drift(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [_registry_row("model-a", "c" * 64)],
        )
        return {"selected_model_count": 1, "registry": None, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry_with_drift)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "registry_cutover_undeclared"
    classification = receipt["registry_classification"]
    assert classification["refused"]["total"] == 1
    assert classification["refused"]["items"][0]["reason"] == "registry_cutover_undeclared"
    # Previous canonical registry SHA is captured on the receipt.
    assert classification["previous_registry_sha256"] is not None
    _assert_classification_reconciles(
        classification, previous_count=1, prospective_count=1
    )


def test_prospective_registry_generation_is_deterministic(
    tmp_path: Path,
) -> None:
    """Same models produce the same generation string; wall clock is excluded.

    Operators observe this value on a refused refresh receipt and file the
    matching cutover declaration; determinism (across wall-clock intervals,
    finding C-B1) is the operational contract that makes the refuse ->
    declare -> retry loop convergent.
    """
    models = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    generated_at = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    gen1 = refresh._prospective_registry_generation(models, generated_at=generated_at)
    gen2 = refresh._prospective_registry_generation(
        list(models), generated_at=generated_at
    )
    assert gen1 == gen2
    assert gen1.startswith("manifest-")
    # Generation is a pure content hash: shape is manifest-<12 hex chars>.
    _, _, digest = gen1.partition("-")
    assert len(digest) == 12
    assert all(character in "0123456789abcdef" for character in digest)
    # A different model set produces a different generation.
    perturbed = models + [_registry_row("basin-103", "c" * 64)]
    gen3 = refresh._prospective_registry_generation(perturbed, generated_at=generated_at)
    assert gen3 != gen1
    # Wall-clock changes MUST NOT change the generation for the same models
    # (T10, finding C-B1).  A generation that varies with wall clock breaks
    # the refuse-declaration-retry operator loop.
    later = refresh.datetime(2026, 7, 15, 9, 42, 17, 500000, tzinfo=refresh.UTC)
    much_later = refresh.datetime(2026, 7, 16, 3, 0, tzinfo=refresh.UTC)
    gen_later = refresh._prospective_registry_generation(models, generated_at=later)
    gen_much_later = refresh._prospective_registry_generation(
        models, generated_at=much_later
    )
    assert gen1 == gen_later == gen_much_later
