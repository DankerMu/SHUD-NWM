"""Identity predicate, bounded loaders, the lenient reader and dry-run
reconciliation.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Three direct-branch families
that share nothing but their shape -- each pins one reader or predicate rather
than a whole refresh run: #1093's None-versus-missing identity equality, the
oversize refusals and sha binding of the previous-canonical and declaration
loaders, the refresh lock held across the precommit gate, refusal byte/inode
preservation, the CAS refusal of a concurrent authoritative swap, #1094's
lenient receipt-order fail-safe with the corrupt-latest replacement, and the
#1080 dry-run reconciliation corpus.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.provider_atomic import (
    ProviderAtomicError,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
)
from scripts import scheduler_file_provider_refresh as refresh
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination
from tests.scheduler_refresh_helpers import (
    _config,
    _registry_row,
    _stub_provider_pipeline_with_models,
    _write_previous_canonical,
)
from tests.scheduler_refresh_receipt_helpers import (
    _classification_stub,
    _dry_run_classification,
    _enforced_cutover_gate,
)

# ---------------------------------------------------------------------------
# #1093 identity-equality None/missing semantics — direct predicate coverage.
# Minimal dicts (not ``_registry_row``) so exactly one identity field differs
# between the two sides; every other identity field is equal or equally absent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "previous_row"),
    [
        pytest.param(
            {"lifecycle_state": None},
            {"lifecycle_state": None},
            id="flat-field-none-on-both-sides",
        ),
        pytest.param(
            {"resource_profile": {"source_inventory_checksum": None}},
            {"resource_profile": {"source_inventory_checksum": None}},
            id="nested-checksum-none-on-both-sides",
        ),
        pytest.param(
            {"package_checksum": f"sha256:{'a' * 64}"},
            {"package_checksum": f"sha256:{'a' * 64}"},
            id="resource-profile-missing-on-both-sides",
        ),
        pytest.param(
            {"basin_version_id": "v1"},
            {"basin_version_id": "v1", "lifecycle_state": None},
            id="flat-field-missing-versus-explicit-none",
        ),
    ],
)
def test_identity_predicate_treats_symmetric_absence_as_identical(
    row: dict[str, object], previous_row: dict[str, object]
) -> None:
    """#1093 (a)/(b)/(c)/(f): symmetric absence is identity, not drift.

    Both-None flat fields, both-None nested
    ``resource_profile.source_inventory_checksum``, and a top-level
    ``resource_profile`` missing on both sides (sentinel == sentinel) all
    classify as ``unchanged``.  A flat field missing on one side and
    explicitly ``None`` on the other is also identical because ``dict.get()``
    collapses both to ``None`` — pinned here against a sentinel-based rewrite
    of the flat path that would silently flip it to drift.
    """
    assert refresh._rows_have_identical_identity(row, previous_row) is True
    assert refresh._rows_have_identical_identity(previous_row, row) is True


@pytest.mark.parametrize(
    ("row", "previous_row"),
    [
        pytest.param(
            {"segment_count": 0},
            {"segment_count": None},
            id="segment-count-zero-versus-none",
        ),
        pytest.param(
            {"lifecycle_state": ""},
            {"lifecycle_state": None},
            id="lifecycle-state-empty-string-versus-none",
        ),
    ],
)
def test_identity_predicate_rejects_asymmetric_falsy_flat_values(
    row: dict[str, object], previous_row: dict[str, object]
) -> None:
    """#1093 (d): a falsy non-None flat value versus ``None`` is drift.

    ``0`` and ``""`` are deliberately chosen: a truthiness comparison
    (``bool(row.get(f)) != bool(previous_row.get(f))``) would conflate them
    with ``None`` and misclassify real drift as ``unchanged``.
    """
    assert refresh._rows_have_identical_identity(row, previous_row) is False
    assert refresh._rows_have_identical_identity(previous_row, row) is False


def test_identity_predicate_rejects_missing_nested_key_versus_explicit_null() -> None:
    """#1093 (e): a missing top-level ``resource_profile`` differs from an
    explicit ``source_inventory_checksum: null``.

    ``_extract_nested_identity`` returns its ``_MISSING_IDENTITY`` sentinel on
    any path gap precisely so a rebuilt profile that dropped the checksum key
    cannot ride through as ``unchanged``.
    """
    row = {"basin_version_id": "v1"}
    previous_row = {
        "basin_version_id": "v1",
        "resource_profile": {"source_inventory_checksum": None},
    }

    assert refresh._rows_have_identical_identity(row, previous_row) is False
    assert refresh._rows_have_identical_identity(previous_row, row) is False


def test_load_previous_canonical_rejects_oversize_file(tmp_path: Path) -> None:
    """T (C-F1): explicit ``len > MAX`` sentinel after
    ``read_bytes_limited_no_follow`` in ``_load_previous_canonical_registry``."""
    manifest_dir = tmp_path / "registry"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "manifest-last.json"
    # Write MAX+1 bytes so read_bytes_limited_no_follow returns exactly
    # MAX+1 bytes (its sentinel-plus-one contract) and the caller's
    # explicit len > MAX check fires.
    manifest_path.write_bytes(b"x" * (refresh.MAX_REGISTRY_MANIFEST_BYTES + 1))
    assert manifest_path.stat().st_size > refresh.MAX_REGISTRY_MANIFEST_BYTES

    with pytest.raises(refresh.RefreshError) as info:
        refresh._load_previous_canonical_registry(
            str(manifest_path), containment_root=manifest_dir
        )
    assert info.value.reason == "provider_invalid"


def test_load_cutover_declaration_rejects_oversize_file(tmp_path: Path) -> None:
    """T (C-F4): explicit ``len > MAX`` sentinel in
    ``_load_cutover_declaration``."""
    declaration = tmp_path / "declaration.json"
    filler = b" " * (refresh.MAX_CUTOVER_DECLARATION_BYTES + 1)
    declaration.write_bytes(b"{}" + filler)
    with pytest.raises(refresh.RefreshError) as info:
        refresh._load_cutover_declaration(
            str(declaration), now=refresh.datetime.now(refresh.UTC)
        )
    assert info.value.reason == "registry_cutover_declaration_invalid"


def test_load_previous_canonical_returns_bytes_bound_to_sha(tmp_path: Path) -> None:
    """T (C-F2): the loader returns the exact bytes it hashed so callers
    can hand the snapshot forward without a second read (bytes+SHA must
    come from the same read)."""
    manifest_dir = tmp_path / "registry"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "manifest-last.json"
    manifest_path.write_bytes(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": "2026-07-14T00:00:00Z",
                "models": [_registry_row("basin-101", "a" * 64)],
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    loaded = refresh._load_previous_canonical_registry(
        str(manifest_path), containment_root=manifest_dir
    )
    assert loaded is not None
    sha, models, raw_bytes = loaded
    assert sha == refresh.hashlib.sha256(raw_bytes).hexdigest()
    assert models[0]["model_id"] == "basin-101"


def test_full_runner_refresh_lock_is_held_during_precommit_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13 (part a) / C-E1: the precommit gate is invoked while
    ``config.refresh_lock`` is held by the runner.  A second attempt to
    acquire the same lock non-blocking must fail while the gate runs."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    prospective_models = [_registry_row("basin-101", "a" * 64)]

    lock_holder_state: dict[str, bool] = {"seen_locked": False}

    # Monkeypatch the precommit gate implementation so we can observe that,
    # at the moment the gate runs, a competing non-blocking acquisition of
    # the same refresh_lock fails.  We do NOT replace the gate's semantics.
    original_gate = refresh._registry_precommit_gate

    def instrumented_gate(*args: object, **kwargs: object) -> None:
        # A second non-blocking acquire of the same refresh_lock must fail
        # with a typed ProviderAtomicError — proving the runner holds it.
        try:
            with provider_atomic_module.provider_destination_lock(
                config.refresh_lock, blocking=False
            ):
                # Successfully acquired means the runner did NOT hold it.
                lock_holder_state["seen_locked"] = False
        except provider_atomic_module.ProviderAtomicError:
            lock_holder_state["seen_locked"] = True
        return original_gate(*args, **kwargs)

    monkeypatch.setattr(refresh, "_registry_precommit_gate", instrumented_gate)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)
    assert receipt["outcome"] == "published", receipt
    assert lock_holder_state["seen_locked"] is True, (
        "refresh_lock was NOT held while the precommit gate ran — the "
        "concurrency invariant #1080 spec §D3/D7 relies on is broken."
    )


def test_full_runner_refusal_preserves_canonical_bytes_inode_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13 (part c) / C-E1: a refused runner call leaves canonical bytes,
    inode, and mtime byte-identical (the receipt refusal contract)."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    canonical_path = _write_previous_canonical(config, previous_models)
    before_bytes = canonical_path.read_bytes()
    before_stat = canonical_path.stat()
    prospective_models = [_registry_row("basin-101", "c" * 64)]

    def publish_registry_with_drift(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](workspace, [], prospective_models)
        return {"selected_model_count": 1, "registry": None, "packages": []}

    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )
    monkeypatch.setattr(
        refresh, "publish_all_basin_scheduler_registry", publish_registry_with_drift
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "registry_cutover_undeclared"
    assert canonical_path.read_bytes() == before_bytes
    after_stat = canonical_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )


def test_provider_atomic_cas_refuses_concurrent_authoritative_swap(
    tmp_path: Path,
) -> None:
    """T13 (part b) / C-E1: ``expected_preimage`` CAS prevents a concurrent
    canonical writer from committing after a snapshot.  This mirrors the
    invariant D3 relies on for the registry lane.
    """
    canonical = tmp_path / "registry" / "manifest-last.json"
    # Explicit modes, not the ambient umask (#1513).
    make_directory_with_explicit_mode(canonical.parent)
    write_provider_destination(canonical, b"old\n")
    snapshot = capture_provider_preimage(canonical, max_bytes=1024)
    # Concurrent authoritative writer swaps the bytes.
    canonical.write_bytes(b"authoritative-new\n")

    with pytest.raises(ProviderAtomicError) as info:
        atomic_replace_provider_bytes(
            canonical,
            b"refresh-would-be\n",
            max_bytes=1024,
            expected_preimage=snapshot,
        )

    assert info.value.reason == "provider_preimage_changed"
    # Concurrent bytes preserved unchanged.
    assert canonical.read_bytes() == b"authoritative-new\n"


# ---------------------------------------------------------------------------
# #1094 lenient receipt-order fail-safe — direct branch coverage.
# ``_lenient_receipt_order`` is the reader guarding C-A2: a legacy or
# corrupted on-disk ``latest.json`` must never brick the next refresh's
# primary-receipt publish.  Every malformed shape returns ``None`` (never
# raises) so ``_publish_primary_receipt`` defaults to ``replace_latest=True``.
# The T9 tests above cover only well-formed pre-#1080 payloads; these pin the
# fail-safe branches themselves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="non-mapping-none"),
        pytest.param("not a mapping", id="non-mapping-str"),
        pytest.param([1, 2, 3], id="non-mapping-list"),
        pytest.param({"started_at": "2026-07-01T00:00:00Z"}, id="run-id-missing"),
        pytest.param(
            {"run_id": "", "started_at": "2026-07-01T00:00:00Z"}, id="run-id-empty"
        ),
        pytest.param(
            {"run_id": 42, "started_at": "2026-07-01T00:00:00Z"}, id="run-id-non-str"
        ),
        pytest.param(
            {"run_id": "refresh_x", "started_at": "not-a-datetime"},
            id="started-at-unparsable",
        ),
        pytest.param({"run_id": "refresh_x"}, id="started-at-missing"),
    ],
)
def test_lenient_receipt_order_returns_none_for_malformed_payload(
    payload: object,
) -> None:
    """#1094 (a)/(b)/(c): every malformed payload shape fails safe to ``None``.

    Non-Mapping payloads (including the ``None`` that
    ``_publish_primary_receipt`` substitutes for undecodable/non-JSON
    ``latest.json`` bytes), a missing/empty/non-str ``run_id``, and a
    missing/unparsable ``started_at`` must all return ``None`` rather than
    raise — a raise here bricks the next daily refresh's publish.
    """
    assert refresh._lenient_receipt_order(payload) is None


@pytest.mark.parametrize(
    "started_at",
    [
        pytest.param("2026-07-01T00:00:00Z", id="utc-zulu"),
        pytest.param("2026-07-01T08:00:00+08:00", id="non-utc-offset"),
    ],
)
def test_lenient_receipt_order_returns_tz_aware_order_for_valid_payload(
    started_at: str,
) -> None:
    """#1094: a valid payload yields ``(started_at, run_id)`` with the
    datetime timezone-aware and normalized to UTC — pins against a future
    naive-datetime regression that would make order comparison in
    ``_publish_primary_receipt`` raise on tz-aware/naive mixing.
    """
    order = refresh._lenient_receipt_order(
        {"run_id": "refresh_x", "started_at": started_at}
    )

    assert order is not None
    started, run_id = order
    assert run_id == "refresh_x"
    assert started.tzinfo is not None
    assert started.utcoffset() == timedelta(0)
    assert started == refresh.datetime(2026, 7, 1, tzinfo=refresh.UTC)


@pytest.mark.parametrize(
    "corrupt_bytes",
    [
        pytest.param(b"{ not json", id="json-decode-error"),
        pytest.param(b"\x80\x81", id="unicode-decode-error"),
    ],
)
def test_publish_primary_receipt_replaces_corrupt_latest(
    tmp_path: Path, corrupt_bytes: bytes
) -> None:
    """#1094 end-to-end: a corrupted ``latest.json`` does not brick the next
    publish.  Both members of the catch tuple in ``_publish_primary_receipt``
    are exercised — ``b"{ not json"`` raises ``json.JSONDecodeError`` and
    ``b"\\x80\\x81"`` raises ``UnicodeDecodeError``; either way the existing
    payload becomes ``None``, ``_lenient_receipt_order`` fails safe, and the
    new receipt is published over the top.
    """
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    provider = {
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
    receipt = refresh._receipt(
        run_id="refresh_after_corruption",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification=_classification_stub(),
        # #1144: `published` receipts carry the audit block.
        cutover_gate=_enforced_cutover_gate(declaration_present=False),
    )
    (root / "latest.json").write_bytes(corrupt_bytes)

    # Must not raise: the corrupt bytes are unreadable, so the publisher
    # defaults to replace.
    refresh._publish_primary_receipt(root, receipt)

    assert (root / "latest.json").read_bytes() == refresh._receipt_bytes(
        refresh._validate_receipt(receipt)
    )
    # Implementation-independent check (mirrors the monotonic test's style):
    # the persisted JSON is the new receipt itself.
    assert json.loads((root / "latest.json").read_text()) == receipt
    assert {path.stem for path in (root / "history").iterdir()} == {
        "refresh_after_corruption"
    }


def test_dry_run_reconciliation_rejects_bootstrap_removed_entries() -> None:
    """#1135(i): a bootstrap dry_run cannot carry removals — dry_run classify
    returns before the removal loop, so the removal is forged."""
    classification = _dry_run_classification(
        previous_sha=None,
        previous_count=None,
        added=["basin-101"],
        unchanged=[],
        removed=["basin-102"],
        prospective_count=1,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_rejects_non_bootstrap_removed_entries() -> None:
    """#1135(ii): the same forgery with a previous registry recorded.  All
    previous-side numbers stay self-consistent (unchanged 2 <= previous 5), so
    only the ``removed.total != 0`` rule can reject it."""
    classification = _dry_run_classification(
        previous_sha="1" * 64,
        previous_count=5,
        added=["basin-103"],
        unchanged=["basin-101", "basin-102"],
        removed=["basin-104"],
        prospective_count=3,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_rejects_count_without_previous_sha() -> None:
    """#1135(iii): contradictory shape — a null ``previous_registry_sha256``
    claims no previous canonical registry, so the pinned count must be null
    too."""
    classification = _dry_run_classification(
        previous_sha=None,
        previous_count=7,
        added=["basin-101"],
        unchanged=[],
        removed=[],
        prospective_count=1,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_rejects_unchanged_above_previous_count() -> None:
    """#1135(iv): ``unchanged`` rows are ids present in BOTH sets, so their
    total cannot exceed the pinned ``previous_model_count``."""
    classification = _dry_run_classification(
        previous_sha="1" * 64,
        previous_count=2,
        added=[],
        unchanged=["basin-101", "basin-102", "basin-103"],
        removed=[],
        prospective_count=3,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_rejects_bootstrap_unchanged_entries() -> None:
    """#1135(iv-dual): the bootstrap counterpart of the ``unchanged <=
    previous_model_count`` bound.  A null ``previous_registry_sha256`` means
    ``_classify_registry`` built an empty ``previous_by_id``, so every
    prospective row lands in ``added`` and no row can be ``unchanged``.  Every
    adjacent dry_run check is satisfied on purpose (added+unchanged ==
    prospective, removed 0, null count with null sha) so only the bootstrap
    ``unchanged`` rule can reject it."""
    classification = _dry_run_classification(
        previous_sha=None,
        previous_count=None,
        added=[],
        unchanged=["basin-101", "basin-102", "basin-103"],
        removed=[],
        prospective_count=3,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_rejects_forged_new_registry_sha() -> None:
    """#1135(v): a dry_run never publishes a canonical registry, so the writer
    pins ``new_registry_sha256`` to None; a well-formed 64-hex value here is a
    forged publish claim (format validation alone accepts it)."""
    classification = _dry_run_classification(
        previous_sha="1" * 64,
        previous_count=3,
        added=["basin-103", "basin-104"],
        unchanged=["basin-101", "basin-102"],
        removed=[],
        prospective_count=4,
        new_registry_sha256="2" * 64,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_rejects_boolean_previous_count() -> None:
    """#1135: pins the branch-local isinstance/bool guard.  Only meaningful via
    this direct call — on the ``_validate_receipt`` path
    ``_validate_registry_classification_field`` already rejects boolean counts
    for every outcome, so a receipt-level version would be vacuously green."""
    classification = _dry_run_classification(
        previous_sha="1" * 64,
        previous_count=True,
        added=["basin-101"],
        unchanged=[],
        removed=[],
        prospective_count=1,
    )

    with pytest.raises(ValueError, match="receipt_classification_invalid"):
        refresh._enforce_registry_classification_reconciliation(
            classification, outcome="dry_run", reason="dry_run_complete"
        )


def test_dry_run_reconciliation_accepts_previous_models_absent_from_prospective() -> None:
    """#1135 acceptance criterion 4: the previous-side sum equality
    (``unchanged + package_changed + removed == previous_model_count``) must
    NEVER be applied to dry_run.  This is the writer's honest output when the
    previous registry holds 3 models, 2 of them survive into a 4-model
    prospective set and 1 is absent: 2 + 0 + 0 != 3 by design, because dry_run
    never computes removals.  Must not raise."""
    classification = _dry_run_classification(
        previous_sha="1" * 64,
        previous_count=3,
        added=["basin-103", "basin-104"],
        unchanged=["basin-101", "basin-102"],
        removed=[],
        prospective_count=4,
    )

    refresh._enforce_registry_classification_reconciliation(
        classification, outcome="dry_run", reason="dry_run_complete"
    )


def test_receipt_validator_rejects_dry_run_receipt_with_forged_new_registry_sha() -> None:
    """#1135 wiring: the dry_run constraints must be reachable through
    ``_validate_receipt``, not just by direct call.  Built like the #1096
    receipt-level tests (full ``registry/readiness/state`` provider triple, so
    the provider gate cannot fire ``receipt_provider_invalid`` first) and
    tampered ONLY with a forged ``new_registry_sha256`` — a dry_run-exclusive
    rule with no counterpart on the publish path, so a receipt whose outcome is
    routed away from the dry_run branch would validate clean."""
    provider = {
        "name": "registry",
        "before_sha256": "1" * 64,
        "before_inode": None,
        "before_schema_version": "v1",
        "before_generated_at": "2026-07-14T00:00:00Z",
        "before_payload_checksum": "sha256:" + "b" * 64,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-07-14T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 4,
    }
    receipt = refresh._receipt(
        run_id="refresh_dry_run_forged_new_sha",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="dry_run",
        reason="dry_run_complete",
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification=_dry_run_classification(
            previous_sha="1" * 64,
            previous_count=2,
            added=["basin-103", "basin-104"],
            unchanged=["basin-101", "basin-102"],
            removed=[],
            prospective_count=4,
            new_registry_sha256="2" * 64,
        ),
    )

    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)
