"""A ``replace_uncertain`` receipt describes the bytes on disk for every
provider whose rollback was verified (#2297, design D6 option (a)).

The tracked four-lane transaction publishes the registry, its worker mirror
and the readiness index, then the state lane writes without a commit token, so
ownership is unknowable and the run must report ``replace_uncertain``. The
rollback of the three published providers IS verified. The acceptance is stated
against DISK, not against ``before_*``: every restored provider's
``after_sha256`` is the digest of the bytes now at its path, and its
``after_generated_at`` is the on-disk manifest's where the payload carries one.
An unverified rollback keeps the committed (post-publish) evidence exactly, so
readers still choose the registry time by ``outcome`` (R9e is kept).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import scheduler_file_provider_refresh as refresh
from scripts.scheduler_refresh.receipt import _read_provider_header as _real_read_provider_header
from tests.scheduler_refresh_helpers import _tracked_transaction_fixture

RESTORED = ("registry", "registry_worker_mirror", "readiness")
PUBLISHED_BYTES = {
    "registry": b"new-registry-generation",
    "registry_worker_mirror": b"new-registry-generation",
    "readiness": b"new-readiness-generation",
}


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The tracked fixture, with the provider header read from the real bytes
    wherever they are a real manifest (the registry and its mirror). The
    fixture's constant header stub would make ``before_*`` describe a
    generation that was never on disk; the opaque readiness bytes keep it."""
    fixture = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="", unowned_lane="state")
    stub = refresh._read_provider_header

    def header(*args, **kwargs):
        try:
            return _real_read_provider_header(*args, **kwargs)
        except refresh.RefreshError:
            return stub(*args, **kwargs)

    monkeypatch.setattr(refresh, "_read_provider_header", header)
    return fixture


def _disk_generated_at(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_bytes())
    except (ValueError, UnicodeDecodeError):
        return None
    return payload.get("generated_at") if isinstance(payload, dict) else None


def _assert_describes_disk(receipt: dict[str, object], paths: dict[str, Path]) -> None:
    providers = {provider["name"]: provider for provider in receipt["providers"]}
    assert set(providers) == set(RESTORED), sorted(providers)
    assert "state" not in providers
    for name in RESTORED:
        path = paths[name]
        on_disk = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        assert providers[name]["after_sha256"] == on_disk, name
        assert providers[name]["after_sha256"] != hashlib.sha256(PUBLISHED_BYTES[name]).hexdigest(), name
        generated_at = _disk_generated_at(path)
        if generated_at is not None:
            assert providers[name]["after_generated_at"] == generated_at, name


def test_a_verified_rollback_under_a_later_lanes_uncertainty_reports_the_bytes_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _fixture(tmp_path, monkeypatch)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain"
    assert receipt["reason"] == "provider_replace_uncertain"
    for name in RESTORED:
        assert paths[name].read_bytes() == old[name]
    assert _disk_generated_at(paths["registry"]) is not None, "the registry claim must be exercised"
    _assert_describes_disk(receipt, paths)
    assert json.loads((config.receipt_root / "latest.json").read_text()) == receipt
    assert refresh._validate_receipt(receipt) == receipt


def test_the_emergency_record_carries_the_same_disk_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _old, paths = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    (emergency,) = list(config.emergency_root.iterdir())
    record = json.loads(emergency.read_text())
    assert record["outcome"] == "replace_uncertain"
    assert record["providers"] == receipt["providers"]
    _assert_describes_disk(record, paths)
    assert refresh._validate_receipt(record) == record


def test_an_unverified_rollback_keeps_the_committed_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`restored is False`: the branch is unchanged, so the receipt still
    carries the post-publish generation the stub publisher returned -- which is
    why readers keep choosing the registry time by `outcome`."""
    config, _old, _paths = _fixture(tmp_path, monkeypatch)
    real_rollback = refresh._rollback_provider_transaction

    def unverified(records):
        real_rollback(records)
        return False

    monkeypatch.setattr(refresh, "_rollback_provider_transaction", unverified)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain"
    providers = {provider["name"]: provider for provider in receipt["providers"]}
    assert set(providers) == set(RESTORED)
    for name in RESTORED:
        assert providers[name]["after_sha256"] == hashlib.sha256(PUBLISHED_BYTES[name]).hexdigest(), name
        assert providers[name]["after_generated_at"] == "2026-07-14T02:00:00Z", name
        assert providers[name]["after_payload_checksum"] == "sha256:" + "1" * 64, name
    assert refresh._validate_receipt(receipt) == receipt
