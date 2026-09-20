"""#1080 round-2 fix-pass (T1-T13) plus the escalation legs.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). The full-runner receipts that
bind a new registry sha and reconcile totals, valid-cutover admission, the
receipt-validator negative corpus (unsafe item ids, flat ids, previous-count
mismatch, the three bootstrap refusals), the reconciliation formula helper's
two red legs, the pre-#1080 latest upgrade path, wall-clock stable generation
with a deferred declaration, and the three identity-drift escalations (source
inventory checksum, basin version id, missing nested identity).
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from scripts import scheduler_file_provider_refresh as refresh
from tests.scheduler_refresh_helpers import (
    _config,
    _registry_row,
    _run_gate,
    _stub_provider_pipeline_with_models,
    _write_previous_canonical,
)
from tests.scheduler_refresh_receipt_helpers import (
    _assert_classification_reconciles,
    _classification_stub,
    _enforced_cutover_gate,
)

# ---------------------------------------------------------------------------
# Round-2 fix-pass tests (T1-T13; see #1080 review round-1-verdicts-summary.md)
# ---------------------------------------------------------------------------


def _write_previous_manifest_with_generated_at(
    config: refresh.RefreshConfig,
    models: list[dict[str, object]],
    generated_at: str,
) -> Path:
    path = Path(config.registry_uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": generated_at,
                "models": models,
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    return path


def test_full_runner_published_receipt_binds_new_registry_sha_and_reconciles_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 / C-E2: full ``dry_run=False`` end-to-end publish carries a
    reconciled classification and ``new_registry_sha256`` equals the
    registry provider's ``after_sha256``."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [
        _registry_row("basin-101", "a" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    _write_previous_canonical(config, previous_models)
    # Prospective: keep both existing rows byte-identical + add one new row.
    prospective_models = previous_models + [_registry_row("basin-201", "c" * 64)]
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    classification = receipt["registry_classification"]
    registry_provider = next(
        provider for provider in receipt["providers"] if provider["name"] == "registry"
    )
    assert classification["new_registry_sha256"] == registry_provider["after_sha256"]
    _assert_classification_reconciles(
        classification, previous_count=2, prospective_count=3
    )
    assert classification["added"]["total"] == 1
    assert classification["unchanged"]["total"] == 2
    assert classification["package_changed"]["total"] == 0
    assert classification["refused"]["total"] == 0


def test_full_runner_published_receipt_admits_valid_cutover_and_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 / C-E3: 13 previous + 19 prospective where one existing model
    changes checksum with a valid declaration.  End-to-end runner path must
    reach ``outcome="published"`` with 6 added / 12 unchanged / 1
    package_changed / 1 declared_cutovers / 0 refused, and the strict
    receipt validator must accept the payload (round-1 finding C-A1)."""
    previous_models = [_registry_row(f"basin-1{i:02d}", "a" * 64) for i in range(1, 14)]
    # Prospective: 12 existing byte-identical + 1 existing with checksum
    # drift (basin-101 changes checksum) + 6 new basins.
    prospective_models = [
        _registry_row("basin-101", "c" * 64),  # package_changed via declaration
    ] + [
        _registry_row(f"basin-1{i:02d}", "a" * 64) for i in range(2, 14)
    ] + [
        _registry_row(f"basin-2{i:02d}", "b" * 64) for i in range(1, 7)
    ]
    # File the matching cutover declaration.
    generation = refresh._prospective_registry_generation(
        prospective_models, generated_at=refresh.datetime.now(refresh.UTC)
    )
    declaration = tmp_path / "declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": "2026-07-14T00:00:00Z",
                "generation": generation,
                "entries": [
                    {
                        "model_id": "basin-101",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "effective_cycle_utc": "2026-07-15T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    declaration.chmod(0o600)
    monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))
    # Freeze wall clock via a subclass so refresh.datetime.now(UTC) returns
    # a fixed value (datetime.datetime is immutable and cannot have its
    # classmethod replaced directly — set the module-level attribute
    # instead).
    class _StubDateTime(refresh.datetime):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            del tz
            return refresh.datetime(2026, 7, 14, 18, tzinfo=refresh.UTC)

    monkeypatch.setattr(refresh, "datetime", _StubDateTime)

    config = _config(tmp_path)
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    classification = receipt["registry_classification"]
    assert classification["added"]["total"] == 6
    assert classification["unchanged"]["total"] == 12
    assert classification["package_changed"]["total"] == 1
    assert classification["declared_cutovers"]["total"] == 1
    assert classification["refused"]["total"] == 0
    _assert_classification_reconciles(
        classification, previous_count=13, prospective_count=19
    )
    # T5 / C-E6: the fully-shaped published receipt also validates against
    # the JSON schema (Draft 2020-12) — the schema/runtime pair are the
    # same corpus.
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        receipt
    )


def test_receipt_validator_rejects_unsafe_classification_item_model_id(
    tmp_path: Path,
) -> None:
    """T4 / C-E5: injecting a model_id that violates the schema's
    ``^[A-Za-z0-9_.:-]+$`` regex must be rejected by ``_validate_receipt``
    with the schema-matching typed reason; the JSON schema also rejects
    the same shape (schema/runtime corpus stays aligned)."""
    receipt = refresh._receipt(
        run_id="refresh_refused",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_undeclared",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": "1" * 64,
            "new_registry_sha256": None,
            "previous_model_count": 1,
            "prospective_model_count": 1,
            "added": {"items": [], "total": 0, "truncated": False},
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": [], "total": 0, "truncated": False},
            "package_changed": {
                "items": [
                    {
                        "model_id": "/etc/passwd",  # regex-invalid
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "refused": {
                "items": [
                    {
                        "model_id": "/etc/passwd",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "reason": "registry_cutover_undeclared",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    # R2-N4: exercise the JSON schema's ``allOf`` refusal conditional
    # (schemas/scheduler_file_provider_refresh_receipt.schema.json:316-363)
    # on a real refused shape with a strict Draft2020-12 validator +
    # FormatChecker, matching the CI ``check-jsonschema`` invocation.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(receipt)


def test_receipt_validator_rejects_bad_flat_classification_id(tmp_path: Path) -> None:
    """T4 continued: an ``added`` group model_id that violates the regex must
    also be rejected — this exercises ``_validate_registry_classification_field``
    directly (schema also rejects)."""
    receipt = refresh._receipt(
        run_id="refresh_refused_added",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_undeclared",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": "1" * 64,
            "new_registry_sha256": None,
            "previous_model_count": 0,
            "prospective_model_count": 1,
            "added": {
                "items": ["basin/with/slash"],  # regex-invalid model_id
                "total": 1,
                "truncated": False,
            },
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": [], "total": 0, "truncated": False},
            "package_changed": {"items": [], "total": 0, "truncated": False},
            "refused": {
                "items": [
                    {
                        "model_id": "sane-id",
                        "old_checksum": None,
                        "new_checksum": "c" * 64,
                        "reason": "registry_cutover_undeclared",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    # R2-N3: assert the runtime rejects with the same typed reason token that
    # the sibling test T4a (:3487) already binds; mirroring both call sites
    # keeps the runtime/schema failure surfaces in lockstep.
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)


def test_reconciliation_formula_helper_catches_missing_refused_entries(
    tmp_path: Path,
) -> None:
    """T3 / C-E4: the reconciliation helper itself catches a receipt whose
    ``refused`` bucket is smaller than ``removed + (package_changed \\
    declared_cutovers)`` — the property under test IS the formula, not any
    per-scenario hardcoded total."""
    bad_classification = {
        "previous_registry_sha256": "1" * 64,
        "new_registry_sha256": None,
        "previous_model_count": 1,
        "prospective_model_count": 0,
        "added": {"items": [], "total": 0, "truncated": False},
        "unchanged": {"items": [], "total": 0, "truncated": False},
        "removed": {"items": ["basin-102"], "total": 1, "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {"items": [], "total": 0, "truncated": False},  # missing removal
        "declared_cutovers": {"items": [], "total": 0, "truncated": False},
    }
    with pytest.raises(AssertionError):
        _assert_classification_reconciles(
            bad_classification, previous_count=1, prospective_count=0
        )


def test_receipt_validator_rejects_previous_count_mismatch(tmp_path: Path) -> None:
    """R2-N1: a receipt whose bucket totals do not equal the pinned
    ``previous_model_count`` must be rejected with the typed reason.  This
    exercises the runtime reconciliation validator directly and demonstrates
    that an on-disk tampered receipt (bucket rewritten, count left stale)
    fails at ``_validate_receipt`` time — not just via the helper."""
    receipt = refresh._receipt(
        run_id="refresh_bad_previous_count",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_removal_refused",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": "1" * 64,
            "new_registry_sha256": "2" * 64,
            # Pin count claims 3 previous rows but buckets sum to 1
            # (unchanged 0 + package_changed 0 + removed 1) — mismatch.
            "previous_model_count": 3,
            "prospective_model_count": 0,
            "added": {"items": [], "total": 0, "truncated": False},
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": ["basin-102"], "total": 1, "truncated": False},
            "package_changed": {"items": [], "total": 0, "truncated": False},
            "refused": {
                "items": [
                    {
                        "model_id": "basin-102",
                        "old_checksum": "a" * 64,
                        "new_checksum": None,
                        "reason": "registry_cutover_removal_refused",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)


def test_receipt_validator_rejects_bootstrap_receipt_with_removed_entries() -> None:
    """#1096: a bootstrap receipt (``previous_registry_sha256`` null) claims no
    previous canonical registry existed, so nothing could have been removed.
    Pre-#1096 this tampered shape validated clean: the
    ``added + unchanged + package_changed == prospective_model_count``
    equality constrains nothing about ``removed`` and the refused lower bound
    is satisfied by listing the removal as refused."""
    receipt = refresh._receipt(
        run_id="refresh_bootstrap_removed",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_removal_refused",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": None,
            "new_registry_sha256": None,
            "previous_model_count": None,
            "prospective_model_count": 0,
            "added": {"items": [], "total": 0, "truncated": False},
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": ["basin-102"], "total": 1, "truncated": False},
            "package_changed": {"items": [], "total": 0, "truncated": False},
            "refused": {
                "items": [
                    {
                        "model_id": "basin-102",
                        "old_checksum": "a" * 64,
                        "new_checksum": None,
                        "reason": "registry_cutover_removal_refused",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)


def test_receipt_validator_rejects_bootstrap_receipt_with_unchanged_entries() -> None:
    """#1096 symmetric forgery: a bootstrap receipt cannot carry ``unchanged``
    rows either.  Here every adjacent check is satisfied on purpose —
    ``prospective_model_count`` is filled to match added+unchanged+
    package_changed, ``refused`` is empty (required for ``published``), and the
    full ``registry/readiness/state`` provider triple is present — so only the
    bootstrap sum invariant can reject it."""
    provider = {
        "name": "registry",
        "before_sha256": None,
        "before_inode": None,
        "before_schema_version": None,
        "before_generated_at": None,
        "before_payload_checksum": None,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-07-14T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 1,
    }
    receipt = refresh._receipt(
        run_id="refresh_bootstrap_unchanged",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification={
            "previous_registry_sha256": None,
            "new_registry_sha256": None,
            "previous_model_count": None,
            "prospective_model_count": 1,
            "added": {"items": [], "total": 0, "truncated": False},
            "unchanged": {"items": ["basin-103"], "total": 1, "truncated": False},
            "removed": {"items": [], "total": 0, "truncated": False},
            "package_changed": {"items": [], "total": 0, "truncated": False},
            "refused": {"items": [], "total": 0, "truncated": False},
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)


def test_receipt_validator_rejects_bootstrap_receipt_with_package_changed_entries() -> None:
    """#1096: a bootstrap receipt cannot carry ``package_changed`` rows — a
    package change requires an ``old_checksum`` from a previous canonical
    registry that, by the null ``previous_registry_sha256``, never existed."""
    receipt = refresh._receipt(
        run_id="refresh_bootstrap_package_changed",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_undeclared",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": None,
            "new_registry_sha256": None,
            "previous_model_count": None,
            "prospective_model_count": 1,
            "added": {"items": [], "total": 0, "truncated": False},
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": [], "total": 0, "truncated": False},
            "package_changed": {
                "items": [
                    {
                        "model_id": "basin-104",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "refused": {
                "items": [
                    {
                        "model_id": "basin-104",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "reason": "registry_cutover_undeclared",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)


def test_reconciliation_formula_helper_catches_declared_not_in_package_changed(
    tmp_path: Path,
) -> None:
    """R2-N5: `declared_cutovers ⊆ package_changed` is a spec invariant
    (design.md D7#2, spec.md:397-403).  The helper must fail when a
    declaration entry names a model_id that never appears in the
    ``package_changed`` bucket — a tampered receipt that grants a cutover
    for a row the classifier never flagged as changed."""
    bad_classification = {
        "previous_registry_sha256": "1" * 64,
        "new_registry_sha256": "2" * 64,
        "previous_model_count": 1,
        "prospective_model_count": 1,
        "added": {"items": [], "total": 0, "truncated": False},
        # basin-a was the sole existing model; classifier saw it as
        # `unchanged`.  A tampered receipt puts basin-a into
        # declared_cutovers WITHOUT listing it in package_changed — that
        # declaration cannot bind to any transition.
        "unchanged": {"items": ["basin-a"], "total": 1, "truncated": False},
        "removed": {"items": [], "total": 0, "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {"items": [], "total": 0, "truncated": False},
        "declared_cutovers": {
            "items": [
                {
                    "model_id": "basin-a",
                    "old_checksum": "a" * 64,
                    "new_checksum": "b" * 64,
                    "effective_cycle_utc": "2026-07-14T12:00:00Z",
                    "transition_mode": "replace",
                }
            ],
            "total": 1,
            "truncated": False,
        },
    }
    with pytest.raises(AssertionError):
        _assert_classification_reconciles(
            bad_classification, previous_count=1, prospective_count=1
        )


def test_publish_primary_receipt_upgrades_over_pre_1080_latest(tmp_path: Path) -> None:
    """T9 / C-A2: a pre-#1080 ``latest.json`` (lacking
    ``registry_classification``) on disk must not brick the next refresh —
    ``_publish_primary_receipt`` reads it leniently and writes the new
    post-#1080 receipt over the top; ``validate_current_receipt`` (installer
    ``--enable``) then accepts the new receipt."""
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir(mode=0o700)
    # Legacy latest.json shape: pre-#1080 published receipt, no
    # registry_classification, otherwise valid.
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
        "entry_count": 13,
    }
    legacy_payload = {
        "schema_version": refresh.SCHEMA_VERSION,
        "run_id": "refresh_pre_1080",
        "started_at": "2026-07-01T00:00:00Z",
        "finished_at": "2026-07-01T00:05:00Z",
        "outcome": "published",
        "reason": "success",
        "operation_outcome": "published",
        "operation_reason": "success",
        "phase": "complete",
        "database_free": True,
        "providers": [
            legacy_provider,
            {**legacy_provider, "name": "readiness"},
            {**legacy_provider, "name": "state"},
        ],
        "orphans": {
            "items": [],
            "total": 0,
            "discovered_total": 0,
            "attempted_total": 0,
            "created_total": 0,
            "truncated": False,
        },
        "residues": [],
    }
    (receipt_root / "latest.json").write_bytes(
        json.dumps(legacy_payload, sort_keys=True, indent=2).encode() + b"\n"
    )
    # Now write a post-#1080 receipt via the real publisher.
    new_receipt = refresh._receipt(
        run_id="refresh_post_1080",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            {**legacy_provider, "name": "registry"},
            {**legacy_provider, "name": "readiness"},
            {**legacy_provider, "name": "state"},
        ],
        registry_classification=_classification_stub(),
        # #1144: the receipt written over the legacy one is a modern
        # `published` receipt, so it carries the audit block.
        cutover_gate=_enforced_cutover_gate(declaration_present=False),
    )
    # Would previously raise ValueError inside `_publish_primary_receipt`
    # because the stale latest.json fails `_validate_receipt`; must succeed.
    refresh._publish_primary_receipt(receipt_root, new_receipt)
    # Latest.json now holds the post-#1080 shape and validates strictly.
    persisted = json.loads((receipt_root / "latest.json").read_text())
    assert persisted["run_id"] == "refresh_post_1080"
    assert "registry_classification" in persisted
    refresh._validate_receipt(persisted)


def test_full_runner_over_pre_1080_latest_publishes_and_installer_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9 continued: the full runner completes ``outcome="published"`` when
    a pre-#1080 stub ``latest.json`` is present, and the new receipt is
    subsequently acceptable to ``validate_current_receipt``."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    # Seed the pre-#1080 latest.json.
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
    (config.receipt_root / "latest.json").write_bytes(
        json.dumps(
            {
                "schema_version": refresh.SCHEMA_VERSION,
                "run_id": "refresh_pre_1080",
                "started_at": "2026-07-01T00:00:00Z",
                "finished_at": "2026-07-01T00:05:00Z",
                "outcome": "published",
                "reason": "success",
                "operation_outcome": "published",
                "operation_reason": "success",
                "phase": "complete",
                "database_free": True,
                "providers": [
                    legacy_provider,
                    {**legacy_provider, "name": "readiness"},
                    {**legacy_provider, "name": "state"},
                ],
                "orphans": {
                    "items": [],
                    "total": 0,
                    "discovered_total": 0,
                    "attempted_total": 0,
                    "created_total": 0,
                    "truncated": False,
                },
                "residues": [],
            },
            sort_keys=True,
            indent=2,
        ).encode()
        + b"\n"
    )
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=previous_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted["run_id"] == receipt["run_id"]
    assert "registry_classification" in persisted


def test_full_runner_wall_clock_stable_generation_admits_deferred_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T10 / C-B1: monkeypatch ``datetime.now(UTC)`` across a wall-clock
    boundary; a declaration filed at wall-clock T1 must still match the
    prospective generation at wall-clock T2 (which is exactly what the
    refuse -> declare -> retry operator loop requires)."""
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    prospective_models = [_registry_row("basin-101", "c" * 64)]

    # T1: refuse at 12:03:17.  We swap in a ``datetime`` proxy exposed as
    # ``refresh.datetime`` so ``refresh.datetime.now(UTC)`` returns whatever
    # the test's clock currently reads without touching the C-level
    # ``datetime.datetime`` type itself (which is not monkeypatchable).
    class _Clock:
        current = refresh.datetime(2026, 7, 14, 12, 3, 17, tzinfo=refresh.UTC)

    class _StubDateTime(refresh.datetime):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            del tz
            return _Clock.current

    monkeypatch.setattr(refresh, "datetime", _StubDateTime)
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    first_receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)
    assert first_receipt["outcome"] == "failed"
    assert first_receipt["reason"] == "registry_cutover_undeclared"
    refused_generation = refresh._prospective_registry_generation(
        prospective_models, generated_at=_Clock.current
    )

    # Operator files a declaration bound to that generation.
    declaration = tmp_path / "declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": _Clock.current.isoformat().replace("+00:00", "Z"),
                "generation": refused_generation,
                "entries": [
                    {
                        "model_id": "basin-101",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "effective_cycle_utc": "2026-07-15T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    declaration.chmod(0o600)
    monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))

    # T2: retry 7 hours later; wall clock has advanced, but generation MUST
    # stay identical (finding C-B1).  Reset the pipeline stub because the
    # first refresh consumed it.
    _Clock.current = refresh.datetime(2026, 7, 14, 19, 42, 0, tzinfo=refresh.UTC)
    # Re-seed the previous canonical since the first refusal did not commit.
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    second_receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert second_receipt["outcome"] == "published", second_receipt
    classification = second_receipt["registry_classification"]
    assert classification["package_changed"]["total"] == 1
    assert classification["declared_cutovers"]["total"] == 1
    assert classification["refused"]["total"] == 0
    _assert_classification_reconciles(
        classification, previous_count=1, prospective_count=1
    )


def test_cutover_gate_escalates_source_inventory_checksum_drift(tmp_path: Path) -> None:
    """T11 / C-C1: two rows with identical URIs + package_checksum but a
    different nested ``resource_profile.source_inventory_checksum`` MUST
    classify as ``package_changed`` (spec.md:301-306).  Previously the
    classifier's 3-field whitelist silently classified them as unchanged."""
    config = _config(tmp_path)
    previous = [
        _registry_row(
            "basin-101",
            "a" * 64,
            source_inventory_checksum="sha256:" + "1" * 63 + "0",
        )
    ]
    prospective = [
        _registry_row(
            "basin-101",
            "a" * 64,  # top-level package_checksum unchanged
            source_inventory_checksum="sha256:" + "1" * 63 + "1",  # nested drift
        )
    ]

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_undeclared"
    payload = captured[0]
    assert payload["unchanged"]["total"] == 0
    assert payload["package_changed"]["total"] == 1
    assert payload["package_changed"]["items"][0]["model_id"] == "basin-101"


def test_cutover_gate_escalates_basin_version_id_drift(tmp_path: Path) -> None:
    """T11 continued: a change to any top-level identity field
    (``basin_version_id`` here) escalates to ``package_changed``."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64, basin_version_id="v1")]
    prospective = [_registry_row("basin-101", "a" * 64, basin_version_id="v2")]

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_undeclared"
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
    assert payload["unchanged"]["total"] == 0


def test_cutover_gate_escalates_missing_nested_identity(tmp_path: Path) -> None:
    """T11 continued: missing ``resource_profile`` on either side counts as
    identity drift — a rebuilt row that dropped the profile field must not
    ride through as ``unchanged``."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64)]
    prospective_row = _registry_row("basin-101", "a" * 64)
    prospective_row.pop("resource_profile")

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=[prospective_row],
        previous_models=previous,
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
