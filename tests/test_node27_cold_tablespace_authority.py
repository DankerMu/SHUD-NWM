"""Durable private recovery-authority contract tests for the cold installer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.node27_cold_tablespace_authority import (
    AuthorityError,
    advance_authority,
    authority_exists,
    private_snapshot_digest,
    read_authority,
    remove_authority,
    write_authority,
)
from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _authority() -> dict:
    return {
        "schema_version": "1.0",
        "phase": "prepared",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "updated_at": NOW.isoformat().replace("+00:00", "Z"),
        "head_sha": "a" * 40,
        "identity": PRODUCTION_IDENTITY.public_payload(),
        "prior_name": "nhms-db-before",
        "prior": {
            "container_id": "sha256:prior",
            "config_digest": "b" * 64,
            "private_snapshot": {"environment": ["POSTGRES_PASSWORD=private-value"]},
            "private_snapshot_digest": private_snapshot_digest({"environment": ["POSTGRES_PASSWORD=private-value"]}),
        },
        "expected": {"cold_bind": "/host:/container:rw", "config_digest": "c" * 64},
        "path": {"device_identity": "8:11:1", "uid": 999, "gid": 999, "mode": 0o700},
        "ownership": {
            "host_path_created": False,
            "prior_stopped": False,
            "prior_renamed": False,
            "installer_container_created": False,
            "catalog_created": False,
        },
    }


def test_authority_is_mode_0600_durable_and_can_be_removed_with_absence_proof(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"

    written = write_authority(path, _authority())

    assert authority_exists(path) is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert "private-value" in path.read_text(encoding="utf-8")
    assert read_authority(path) == written
    advanced = advance_authority(written, phase="prior_renamed", prior_renamed=True)
    write_authority(path, advanced)
    assert read_authority(path)["phase"] == "prior_renamed"

    remove_authority(path)

    assert authority_exists(path) is False
    assert not path.exists()


def test_authority_refuses_phase_without_its_owned_mutation(tmp_path: Path) -> None:
    with pytest.raises(AuthorityError, match="phase"):
        advance_authority(_authority(), phase="prior_renamed")


def test_authority_refuses_tampered_private_snapshot_digest(tmp_path: Path) -> None:
    document = _authority()
    document["prior"]["private_snapshot"] = {"environment": ["POSTGRES_PASSWORD=tampered"]}

    with pytest.raises(AuthorityError, match="digest"):
        write_authority(tmp_path / "authority.json", document)


def test_authority_refuses_unsafe_mode_unknown_phase_and_malformed_document(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    path.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuthorityError, match="phase"):
        read_authority(path)

    malformed = _authority()
    malformed["phase"] = "invented"
    with pytest.raises(AuthorityError, match="phase"):
        write_authority(path, malformed)

    path.write_text(json.dumps(_authority()), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(AuthorityError, match="mode"):
        read_authority(path)
