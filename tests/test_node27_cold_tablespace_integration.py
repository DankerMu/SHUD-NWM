"""Disposable Docker/PG15.2/TimescaleDB2.10.2 oracle for the installer core.

Only the three-marked tests touch Docker.  Local identity and cleanup contract
checks remain ordinary unit tests so CI can verify the oracle cannot accidentally
point at node-27 production identities.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID, PINNED_IMAGE_REF
from packages.common.node27_cold_tablespace_container import diff_container_config, normalize_raw_inspect
from packages.common.node27_cold_tablespace_install import InstallConfig, InstallInterrupted, run_install
from packages.common.node27_cold_tablespace_integration import (
    DEFAULT_HOST_PORT,
    INTEGRATION_PREFIX,
    ColdTablespaceIntegrationError,
    IntegrationConfig,
    IntegrationResources,
    assert_new_chunk_pg_default,
    bootstrap_business_tables,
    cleanup,
    connect,
    default_config,
    dependencies,
    execute,
    inspect_container,
    prepare_resources,
    require_root_evidence_capability,
    root_evidence_ready,
    start_prior,
    validate_isolated_config,
)


def test_disposable_oracle_defaults_to_1892_pin_and_separate_identity(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}deadbeef", host_port=DEFAULT_HOST_PORT)

    assert config.image_id == PINNED_IMAGE_ID
    assert config.image_ref == PINNED_IMAGE_REF
    assert config.container_name.startswith(INTEGRATION_PREFIX)
    assert config.prior_container_name == f"{config.container_name}-before"
    assert config.host_port != 55432
    validate_isolated_config(config)


def test_disposable_oracle_chooses_an_ephemeral_nonproduction_port(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}feedface")

    assert 1024 <= config.host_port <= 65535
    assert config.host_port != 55432


@pytest.mark.parametrize(
    "identity_change",
    (
        {"container_name": "nhms-db"},
        {"prior_container_name": "nhms-db-before"},
        {"host_port": 55432},
        {"work_root": Path("/data/GHDC/nhms-1894-tablespace-owned")},
        {"work_root": Path("/home/nwm/NWM/nhms-1894-tablespace-owned")},
    ),
)
def test_disposable_oracle_refuses_live_or_unowned_identity(tmp_path: Path, identity_change: dict[str, object]) -> None:
    base = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}deadbeef")
    values = base.identity.public_payload()
    values.update(identity_change)
    from packages.common.node27_cold_tablespace_identity import identity_from_public_payload

    with pytest.raises((ColdTablespaceIntegrationError, ValueError)):
        identity = identity_from_public_payload(values)
        validate_isolated_config(IntegrationConfig(identity=identity, password=base.password))


def test_prepare_resources_forces_an_owned_work_root_to_mode_0700(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0005", host_port=55494)

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        assert argv[1] in {"rm", "inspect"}
        return SimpleNamespace(returncode=1 if argv[1] == "inspect" else 0, stdout="", stderr="")

    resources = prepare_resources(config, runner=runner)
    try:
        assert stat_mode(config.work_root) == 0o700
    finally:
        cleanup(resources, runner=runner)


def test_cleanup_requires_checked_container_absence_and_port_release(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0001")
    resources = IntegrationResources(config=config, created_work_root=True)
    config.work_root.mkdir()
    seen: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        return SimpleNamespace(returncode=1 if argv[1] == "inspect" else 0, stdout="", stderr="")

    assert cleanup(resources, runner=runner) == {"container_absent": True, "work_root_absent": True}
    assert sum(command[1] == "inspect" for command in seen) == 2


def test_cleanup_fails_when_post_remove_inspect_still_finds_an_owned_container(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0001")
    resources = IntegrationResources(config=config, created_work_root=True)
    config.work_root.mkdir()

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ColdTablespaceIntegrationError, match="container remains"):
        cleanup(resources, runner=runner)


def test_cleanup_fails_when_the_disposable_port_cannot_be_rebound(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 55494))
        config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0001", host_port=55494)
        resources = IntegrationResources(config=config, created_work_root=True)
        config.work_root.mkdir()

        def runner(argv: tuple[str, ...], *, timeout: int = 90):
            return SimpleNamespace(returncode=1 if argv[1] == "inspect" else 0, stdout="", stderr="")

        with pytest.raises(ColdTablespaceIntegrationError, match="port"):
            cleanup(resources, runner=runner)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


@pytest.mark.integration
@pytest.mark.timescaledb_210
@pytest.mark.node27_docker
def test_real_disposable_cluster_installs_through_run_install(tmp_path: Path) -> None:
    """Exercise stop/rename/recreate/DDL/readback/recovery in the core owner."""

    pytest.importorskip("psycopg2")
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0002")
    try:
        require_root_evidence_capability()
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    evidence: dict[str, bool] | None = None
    try:
        resources = prepare_resources(config)
        try:
            health = root_evidence_ready(resources)
        except ColdTablespaceIntegrationError as error:
            pytest.fail(str(error))
        before = start_prior(resources)
        before_snapshot = normalize_raw_inspect(before)
        bootstrap_business_tables(config)
        receipt_path = config.work_root / "receipts" / "install.json"
        recovery_path = config.work_root / "receipts" / "install.recovery.json"
        settings = dict(
            enforce=True,
            receipt_path=receipt_path,
            recovery_path=recovery_path,
            head_sha="a" * 40,
            expected_uid=999,
            expected_gid=999,
            expected_mode=0o700,
            expected_device_identity="synthetic-device",
            install_required_bytes=1,
            rollback_headroom_bytes=1,
            identity=config.identity,
        )
        result = run_install(InstallConfig(**settings), dependencies(resources, health=health))
        assert result.outcome == "installed", result.receipt
        assert result.receipt["authority"]["state"] == "closed"
        assert not recovery_path.exists()
        after_snapshot = normalize_raw_inspect(inspect_container(config, config.container_name))
        assert diff_container_config(before_snapshot, after_snapshot, identity=config.identity).approved
        assert result.receipt["container_snapshot"]["environment_names"]
        assert config.password not in receipt_path.read_text(encoding="utf-8")
        assert any(action[1] == "run" for action in resources.actions)
        assert_new_chunk_pg_default(config)

        assert stat_mode(receipt_path) == 0o600
        after_digest = result.receipt["container_snapshot"]["config_digest"]
        after_id = after_snapshot.container_id
        actions_before_again = list(resources.actions)
        again = run_install(InstallConfig(**settings), dependencies(resources, health=health))
        assert again.outcome == "already_ready"
        assert again.receipt["container_snapshot"]["config_digest"] == after_digest
        assert normalize_raw_inspect(inspect_container(config, config.container_name)).container_id == after_id
        assert not any(action[1] == "run" for action in resources.actions[len(actions_before_again) :])
        assert not recovery_path.exists()
        assert before["Id"] != ""
    finally:
        if resources is not None:
            evidence = cleanup(resources)
    assert evidence == {"container_absent": True, "work_root_absent": True}


@pytest.mark.integration
@pytest.mark.timescaledb_210
@pytest.mark.node27_docker
def test_real_post_recreate_failure_rolls_back_only_owned_state(tmp_path: Path) -> None:
    """Inject a post-recreate readback failure through the public dependency seam."""

    pytest.importorskip("psycopg2")
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0004")
    try:
        require_root_evidence_capability()
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    try:
        resources = prepare_resources(config)
        health = root_evidence_ready(resources)
        before = normalize_raw_inspect(start_prior(resources))
        bootstrap_business_tables(config)
        receipt_path = config.work_root / "receipts" / "rollback.json"
        recovery_path = config.work_root / "receipts" / "rollback.recovery.json"
        deps = dependencies(resources, health=health)
        observed_target = deps.inspect_target

        def failing_target() -> dict:
            target = dict(observed_target())
            target["writable"] = False
            return target

        deps.inspect_target = failing_target
        result = run_install(
            InstallConfig(
                enforce=True,
                receipt_path=receipt_path,
                recovery_path=recovery_path,
                head_sha="a" * 40,
                expected_uid=999,
                expected_gid=999,
                expected_mode=0o700,
                expected_device_identity="synthetic-device",
                install_required_bytes=1,
                rollback_headroom_bytes=1,
                identity=config.identity,
            ),
            deps,
        )
        assert result.outcome == "rollback", result.receipt
        assert result.receipt["rollback"]["prior_restored"] is True
        assert not recovery_path.exists()
        after = normalize_raw_inspect(inspect_container(config, config.container_name))
        assert after.config_payload() == before.config_payload()
        assert config.identity.cold_bind not in after.binds
        connection = connect(config)
        try:
            assert (
                execute(
                    connection,
                    "SELECT pg_tablespace_location(oid) AS location FROM pg_tablespace WHERE spcname = %s",
                    (config.identity.tablespace,),
                )
                == []
            )
            assert execute(connection, "SELECT 1 AS live") == [{"live": 1}]
        finally:
            connection.close()
    finally:
        if resources is not None:
            cleanup(resources)


@pytest.mark.integration
@pytest.mark.timescaledb_210
@pytest.mark.node27_docker
@pytest.mark.parametrize("phase", ("prior_stopped", "prior_renamed", "replacement_created"))
def test_real_interrupted_replacement_recovers_without_install_replay(tmp_path: Path, phase: str) -> None:
    pytest.importorskip("psycopg2")
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0003")
    try:
        require_root_evidence_capability()
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    try:
        resources = prepare_resources(config)
        health = root_evidence_ready(resources)
        before = normalize_raw_inspect(start_prior(resources))
        bootstrap_business_tables(config)
        receipt_path = config.work_root / "receipts" / "interrupted.json"
        recovery_path = config.work_root / "receipts" / "interrupted.recovery.json"

        def interrupt(observed_phase: str) -> None:
            if observed_phase == phase:
                raise InstallInterrupted("test interruption")

        settings = dict(
            enforce=True,
            receipt_path=receipt_path,
            recovery_path=recovery_path,
            head_sha="a" * 40,
            expected_uid=999,
            expected_gid=999,
            expected_mode=0o700,
            expected_device_identity="synthetic-device",
            install_required_bytes=1,
            rollback_headroom_bytes=1,
            identity=config.identity,
        )
        with pytest.raises(InstallInterrupted):
            run_install(InstallConfig(**settings), dependencies(resources, health=health, after_phase=interrupt))
        assert recovery_path.exists()
        actions_before = list(resources.actions)
        recovered = run_install(InstallConfig(**settings), dependencies(resources, health=health))
        assert recovered.outcome == "rollback", recovered.receipt
        recovery_actions = resources.actions[len(actions_before) :]
        assert not any(action[1] == "run" for action in recovery_actions)
        after = normalize_raw_inspect(inspect_container(config, config.container_name))
        assert after.config_payload() == before.config_payload()
        assert config.identity.cold_bind not in after.binds
        connection = connect(config)
        try:
            assert execute(connection, "SELECT 1 AS live") == [{"live": 1}]
        finally:
            connection.close()
        assert not recovery_path.exists()
    finally:
        if resources is not None:
            cleanup(resources)
