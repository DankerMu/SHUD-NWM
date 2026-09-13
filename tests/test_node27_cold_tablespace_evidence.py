"""Contract tests for descriptor-bound cold-tablespace admission evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.common.node27_cold_tablespace_evidence import (
    PathObservation,
    assess_fresh_path,
    assess_install_capacity,
    assess_resident_path,
    parse_backup_inventory,
)
from tests.test_node27_pgdata_evidence import NOW, _envelope, _policy, _write_evidence


def test_backup_inventory_requires_pgdata_and_every_external_tablespace_target(tmp_path: Path) -> None:
    policy = _policy()
    targets = ("/home/postgres/pgdata/tablespaces/ghdc", "/home/postgres/pgdata/tablespaces/nhms_cold")
    base = _envelope(
        command=["/usr/local/sbin/nhms-backup-inventory", "--json"],
        subject={"pgdata": policy.expected_pgdata, "external_pg_tblspc_targets": list(targets)},
        output="backup inventory captured",
    )
    base["covered_paths"] = [policy.expected_pgdata]
    pgdata_only = _write_evidence(tmp_path / "backup-pgdata-only.json", base)

    coverage = parse_backup_inventory(pgdata_only, policy=policy, external_targets=targets, now=NOW)

    assert coverage.complete is False
    assert set(coverage.missing_targets) == set(targets)

    base["covered_paths"] = [policy.expected_pgdata, *targets]
    all_targets = _write_evidence(tmp_path / "backup-all.json", base)
    coverage = parse_backup_inventory(all_targets, policy=policy, external_targets=targets, now=NOW)
    assert coverage.complete is True


@pytest.mark.parametrize(
    ("label", "observation", "approved"),
    [
        ("correct", PathObservation(True, False, True, 0, 999, 999, 0o700, "8:11", "8:11:1", 1_000), True),
        ("wrong-mount", PathObservation(True, False, True, 0, 999, 999, 0o700, "8:12", "8:12:1", 1_000), False),
        ("missing", PathObservation(False, False, False, None, None, None, None, None, None, None), False),
        ("symlink", PathObservation(True, True, False, 0, 999, 999, 0o700, "8:11", "8:11:1", 1_000), False),
        ("nonempty", PathObservation(True, False, True, 1, 999, 999, 0o700, "8:11", "8:11:1", 1_000), False),
        ("wrong-owner", PathObservation(True, False, True, 0, 998, 999, 0o700, "8:11", "8:11:1", 1_000), False),
        ("wrong-mode", PathObservation(True, False, True, 0, 999, 999, 0o755, "8:11", "8:11:1", 1_000), False),
        ("wrong-device", PathObservation(True, False, True, 0, 999, 999, 0o700, "8:11", "8:11:2", 1_000), False),
    ],
)
def test_fresh_path_contract_rejects_every_unsafe_shape(
    label: str, observation: PathObservation, approved: bool
) -> None:
    decision = assess_fresh_path(
        observation,
        expected_uid=999,
        expected_gid=999,
        expected_mode=0o700,
        expected_device_identity="8:11:1",
    )

    assert decision.approved is approved, label
    assert (not decision.blockers) is approved


_RESIDENT_OK = dict(uid=999, gid=999, mode=0o700, mount_device="8:11", device_identity="8:11:1", free_bytes=1_000)


def _resident_path(*, entry_count: int | None = 1, **overrides) -> PathObservation:
    values = dict(_RESIDENT_OK, entry_count=entry_count, **overrides)
    return PathObservation(
        exists=values.pop("exists", True),
        is_symlink=values.pop("is_symlink", False),
        is_directory=values.pop("is_directory", True),
        **values,
    )


@pytest.mark.parametrize(
    ("label", "observation", "approved"),
    [
        ("resident-version-subtree", _resident_path(), True),
        ("resident-many", _resident_path(entry_count=3), True),
        ("resident-none-count", _resident_path(entry_count=None), False),
        ("resident-negative-count", _resident_path(entry_count=-1), False),
        ("resident-symlink", _resident_path(is_symlink=True, is_directory=False), False),
        ("resident-not-directory", _resident_path(is_directory=False), False),
        ("resident-missing", _resident_path(exists=False, entry_count=None), False),
        ("resident-wrong-owner", _resident_path(uid=998), False),
        ("resident-wrong-mode", _resident_path(mode=0o755), False),
        ("resident-wrong-device", _resident_path(device_identity="8:11:2"), False),
        ("resident-no-mount", _resident_path(mount_device=None), False),
    ],
)
def test_resident_path_contract_accepts_postgres_version_subtree_and_rejects_unsafe_shapes(
    label: str, observation: PathObservation, approved: bool
) -> None:
    decision = assess_resident_path(
        observation,
        expected_uid=999,
        expected_gid=999,
        expected_mode=0o700,
        expected_device_identity="8:11:1",
    )

    assert decision.approved is approved, label
    if not approved:
        assert decision.blockers


def test_install_capacity_requires_install_and_rollback_headroom() -> None:
    exact = assess_install_capacity(free_bytes=300, install_required_bytes=100, rollback_headroom_bytes=200)
    short = assess_install_capacity(free_bytes=299, install_required_bytes=100, rollback_headroom_bytes=200)
    zero = assess_install_capacity(free_bytes=0, install_required_bytes=100, rollback_headroom_bytes=200)
    missing = assess_install_capacity(free_bytes=None, install_required_bytes=100, rollback_headroom_bytes=200)

    assert exact.approved is True
    assert exact.free_bytes == 300
    assert exact.install_required_bytes == 100
    assert exact.rollback_headroom_bytes == 200
    assert exact.required_bytes == 300
    assert exact.blockers == ()

    assert short.approved is False
    assert short.free_bytes == 299
    assert short.required_bytes == 300
    assert short.blockers == ("cold filesystem lacks install plus rollback headroom",)

    assert zero.approved is False
    assert zero.free_bytes == 0
    assert zero.required_bytes == 300
    assert zero.blockers == ("cold filesystem lacks install plus rollback headroom",)

    assert missing.approved is False
    assert missing.free_bytes is None
    assert missing.required_bytes == 300
    assert missing.blockers == ("host path capacity observation is unavailable",)


def test_install_capacity_rejects_negative_observed_and_config_bytes() -> None:
    with pytest.raises(ValueError, match="capacity values must be non-negative"):
        assess_install_capacity(free_bytes=-1, install_required_bytes=100, rollback_headroom_bytes=200)
    with pytest.raises(ValueError, match="capacity values must be non-negative"):
        assess_install_capacity(free_bytes=300, install_required_bytes=-1, rollback_headroom_bytes=200)
    with pytest.raises(ValueError, match="capacity values must be non-negative"):
        assess_install_capacity(free_bytes=300, install_required_bytes=100, rollback_headroom_bytes=-1)
    with pytest.raises(ValueError, match="capacity values must be non-negative"):
        assess_install_capacity(free_bytes=None, install_required_bytes=-1, rollback_headroom_bytes=200)
