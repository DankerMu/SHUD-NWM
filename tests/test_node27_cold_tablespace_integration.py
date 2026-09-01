"""Disposable Docker/PG15.2/TimescaleDB2.10.2 oracle for the installer core.

Only the three-marked tests touch Docker.  Local identity and cleanup contract
checks remain ordinary unit tests so CI can verify the oracle cannot accidentally
point at node-27 production identities.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
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
    RootEvidenceCapability,
    assert_new_chunk_pg_default,
    bootstrap_business_tables,
    cleanup,
    connect,
    default_config,
    dependencies,
    execute,
    initial_container_argv,
    inspect_container,
    pinned_image_root_argv,
    prepare_resources,
    require_root_evidence_capability,
    root_evidence_ready,
    root_evidence_setup_argv,
    start_prior,
    validate_isolated_config,
)
from packages.common.node27_cold_tablespace_types import InstallDependencies


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


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _capability_runner(
    *,
    sudo_returncode: int = 1,
    image_id: str = PINNED_IMAGE_ID,
    default_user: str = "postgres",
    postgres_identity: str = "1000:1000",
    root_identity: str = "0:0",
    helper_exists: bool = False,
    post_helper_exists: bool = False,
    helper_returncode: int = 0,
    runtime_identity: str | None = None,
    runtime_returncode: int | None = None,
    stale_runtime_helper: bool = False,
) -> tuple[Callable[..., SimpleNamespace], list[tuple[str, ...]]]:
    seen: list[tuple[str, ...]] = []
    inspections: dict[str, int] = {}

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        if argv[:3] == ("/usr/bin/sudo", "-n", "true"):
            return _result(sudo_returncode)
        if argv[1:4] == ("image", "inspect", PINNED_IMAGE_ID):
            if "{{.Config.User}}" in argv:
                return _result(0, default_user + "\n")
            return _result(0, image_id + "\n")
        if argv[1:4] == ("image", "inspect", PINNED_IMAGE_REF):
            return _result(0, image_id + "\n")
        if argv[1] == "inspect":
            name = argv[-1]
            inspections[name] = inspections.get(name, 0) + 1
            exists = helper_exists or (post_helper_exists and inspections[name] > 1)
            if stale_runtime_helper:
                exists = exists or "runtime-identity" in name
            return _result(0 if exists else 1)
        if argv[1] == "exec":
            user = argv[argv.index("--user") + 1] if "--user" in argv else None
            uid, gid = (runtime_identity or user or "").split(":", maxsplit=1)
            return _result(0 if (uid, gid) == ("1005", "1005") else 1)
        if argv[1] == "run":
            user = argv[argv.index("--user") + 1] if "--user" in argv else None
            script = argv[-1]
            if user not in {None, "0:0"}:
                uid, gid = (runtime_identity or user).split(":", maxsplit=1)
                return _result(
                    helper_returncode if runtime_returncode is None else runtime_returncode,
                    uid + "\n" + gid + "\n",
                )
            if "id -un" in script:
                uid, gid = postgres_identity.split(":", maxsplit=1)
                return _result(helper_returncode, uid + "\n" + gid + "\n")
            if "id -u; id -g" in script:
                uid, gid = root_identity.split(":", maxsplit=1)
                return _result(helper_returncode, uid + "\n" + gid + "\n")
            return _result(helper_returncode)
        raise AssertionError(f"unexpected argv: {argv}")

    return runner, seen


def test_root_capability_falls_back_to_exact_pinned_image_before_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0006", host_port=55494)
    runner, seen = _capability_runner()
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.geteuid", lambda: 1005)
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.getegid", lambda: 1005)

    capability = require_root_evidence_capability(config, runner=runner)

    assert capability == RootEvidenceCapability(
        strategy="pinned_image",
        image_postgres_uid=1000,
        image_postgres_gid=1000,
        runtime_uid=1005,
        runtime_gid=1005,
        image_id=PINNED_IMAGE_ID,
        image_ref=PINNED_IMAGE_REF,
        image_default_user="postgres",
        root_proof="pinned-image-user-0:0",
    )
    assert not config.work_root.exists()
    assert all(str(config.work_root) not in command for command in seen)
    assert not any(config.container_name in command or config.prior_container_name in command for command in seen)
    assert any("--user" in command and command[command.index("--user") + 1] == "1005:1005" for command in seen)
    assert not any(
        command[1] == "run" and "--user" in command and command[command.index("--user") + 1] == "1000:1000"
        for command in seen
    )


def test_root_capability_refuses_stale_root_action_helper_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0015", host_port=55494)
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.geteuid", lambda: 1005)
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.getegid", lambda: 1005)

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        if argv[:3] == ("/usr/bin/sudo", "-n", "true"):
            return _result(1)
        if argv[1:4] == ("image", "inspect", PINNED_IMAGE_ID):
            if "{{.Config.User}}" in argv:
                return _result(0, "postgres\n")
            return _result(0, PINNED_IMAGE_ID + "\n")
        if argv[1:4] == ("image", "inspect", PINNED_IMAGE_REF):
            return _result(0, PINNED_IMAGE_ID + "\n")
        if argv[1] == "inspect":
            return _result(0 if "root-action" in argv[-1] else 1)
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(ColdTablespaceIntegrationError, match="helper name"):
        require_root_evidence_capability(config, runner=runner)

    assert not config.work_root.exists()
    assert not any(command[1] == "run" for command in seen)


def test_fallback_cleanup_refuses_unknown_child_and_requires_root_helper_post_absence(
    tmp_path: Path,
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0011", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="pinned_image",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="pinned-image-user-0:0",
        ),
        created_work_root=True,
    )
    config.work_root.mkdir()
    (config.work_root / "foreign").mkdir()
    seen: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        if argv[1] == "inspect":
            return _result(1)
        return _result()

    with pytest.raises(ColdTablespaceIntegrationError, match="unknown children"):
        cleanup(resources, runner=runner)

    assert not any(command[1] == "run" for command in seen)
    assert config.work_root.exists()
    (config.work_root / "foreign").rmdir()
    config.work_root.rmdir()


def test_fallback_cleanup_removes_only_known_children_and_the_owned_work_root(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0014", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="pinned_image",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="pinned-image-user-0:0",
        ),
        created_work_root=True,
    )
    config.work_root.mkdir(mode=0o700)
    config.work_root.chmod(0o700)
    assert config.work_root.stat().st_mode & 0o777 == 0o700
    for name in ("pgdata", "cold", "evidence", "receipts"):
        (config.work_root / name).mkdir()
    (config.work_root / "postgres.env").write_text("safe-test-only\n", encoding="utf-8")
    actions: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        actions.append(argv)
        if argv[1] == "inspect":
            return _result(1)
        if argv[1] == "run":
            for name in ("pgdata", "cold", "evidence", "receipts"):
                (config.work_root / name).rmdir()
            (config.work_root / "postgres.env").unlink()
        return _result(1 if argv[1] == "rm" else 0)

    assert cleanup(resources, runner=runner) == {"container_absent": True, "work_root_absent": True}
    assert not config.work_root.exists()
    fallback = next(action for action in actions if action[1] == "run")
    assert fallback[fallback.index("--entrypoint") + 1] == "/bin/sh"


def test_fallback_cleanup_rejects_a_stranded_root_helper_after_work_root_removal(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0012", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="pinned_image",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="pinned-image-user-0:0",
        ),
        created_work_root=True,
    )
    config.work_root.mkdir()
    helper_inspections = 0

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        nonlocal helper_inspections
        if argv[1] == "inspect":
            if "root-action" in argv[-1]:
                helper_inspections += 1
                return _result(0)
            return _result(1)
        return _result()

    with pytest.raises(ColdTablespaceIntegrationError, match="root helper remains"):
        cleanup(resources, runner=runner)

    assert config.work_root.exists()
    assert helper_inspections >= 1
    config.work_root.rmdir()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"image_id": "sha256:" + "0" * 64}, "image authority"),
        ({"default_user": "root"}, "default postgres"),
        ({"postgres_identity": "0:0"}, "postgres identity"),        ({"root_identity": "1000:1000"}, "root identity"),
        ({"helper_exists": True}, "helper name"),
        ({"post_helper_exists": True}, "helper name"),
        ({"helper_returncode": 1}, "identity helper"),
    ),
)
def test_root_capability_refuses_ambiguous_pinned_image_probe_before_resources(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0007", host_port=55494)
    runner, seen = _capability_runner(**kwargs)

    with pytest.raises(ColdTablespaceIntegrationError, match=message):
        require_root_evidence_capability(config, runner=runner)

    assert not config.work_root.exists()
    assert not any(config.container_name in command or config.prior_container_name in command for command in seen)


def test_root_capability_retains_noninteractive_sudo_and_measures_the_pinned_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0008", host_port=55494)
    runner, seen = _capability_runner(sudo_returncode=0)
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.geteuid", lambda: 1005)
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.getegid", lambda: 1005)

    capability = require_root_evidence_capability(config, runner=runner)

    assert capability.strategy == "sudo"
    assert (capability.image_postgres_uid, capability.image_postgres_gid) == (1000, 1000)
    assert (capability.runtime_uid, capability.runtime_gid) == (1005, 1005)
    assert any("--user" in command and command[command.index("--user") + 1] == "1005:1005" for command in seen)


@pytest.mark.parametrize(
    ("kwargs", "host", "message"),
    (
        ({"runtime_identity": "1000:1000"}, (1005, 1005), "runtime identity"),
        ({"runtime_returncode": 1}, (1005, 1005), "runtime identity"),
        ({"stale_runtime_helper": True}, (1005, 1005), "helper name"),
        ({}, (0, 1005), "runtime identity"),
    ),
)
def test_root_capability_refuses_unproven_or_privileged_runtime_identity_before_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    host: tuple[int, int],
    message: str,
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0016", host_port=55494)
    runner, seen = _capability_runner(**kwargs)
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.geteuid", lambda: host[0])
    monkeypatch.setattr("packages.common.node27_cold_tablespace_root_capability.os.getegid", lambda: host[1])

    with pytest.raises(ColdTablespaceIntegrationError, match=message):
        require_root_evidence_capability(config, runner=runner)

    assert not config.work_root.exists()
    if host[0] == 0 or kwargs.get("stale_runtime_helper"):
        assert not any(
            command[1] == "run" and "--user" in command and command[command.index("--user") + 1] != "0:0"
            for command in seen
        )


def test_pinned_image_root_action_uses_one_owned_bind_no_port_and_fixed_script(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0009", host_port=55494)
    capability = RootEvidenceCapability(
        strategy="pinned_image",
        image_postgres_uid=1000,
        image_postgres_gid=1000,
        runtime_uid=1005,
        runtime_gid=1005,
        image_id=PINNED_IMAGE_ID,
        image_ref=PINNED_IMAGE_REF,
        image_default_user="postgres",
        root_proof="pinned-image-user-0:0",
    )
    config.work_root.mkdir(mode=0o700)
    config.work_root.chmod(0o700)

    argv = pinned_image_root_argv(
        config.identity,
        capability=capability,
        work_root=config.work_root,
        reader_gid=os.getgid(),
        action="prepare",
    )

    assert argv[0:3] == ("/usr/bin/docker", "run", "--rm")
    assert argv.count("-v") == 1
    assert argv[argv.index("-v") + 1] == f"{config.work_root}:/nhms-owned:rw"
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--user" in argv and argv[argv.index("--user") + 1] == "0:0"
    assert "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "/bin/sh"
    assert config.image_id in argv
    assert str(config.identity.host_path) not in argv
    assert f"127.0.0.1:{config.host_port}:5432" not in argv
    assert "/var/run/docker.sock" not in argv
    assert "python" not in " ".join(argv).lower()
    config.work_root.rmdir()


def test_proven_host_runtime_identity_controls_disposable_container_and_cold_path_contract(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0010", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
    )

    argv = initial_container_argv(resources)
    assert argv[argv.index("--user") + 1] == "1005:1005"
    helper_argv = root_evidence_setup_argv(resources, action="create-cold-path")
    assert helper_argv[helper_argv.index("--runtime-uid") + 1] == "1005"
    assert helper_argv[helper_argv.index("--runtime-gid") + 1] == "1005"
    assert "--postgres-uid" not in helper_argv
    config.work_root.mkdir(mode=0o700)
    config.work_root.chmod(0o700)
    root_argv = pinned_image_root_argv(
        config.identity,
        capability=RootEvidenceCapability(
            strategy="pinned_image",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="pinned-image-user-0:0",
        ),
        work_root=config.work_root,
        reader_gid=os.getgid(),
        action="create-cold-path",
    )
    assert root_argv[-3:-1] == ("1005", "1005")
    assert "1000" not in root_argv[-3:]
    config.work_root.rmdir()


_RUNTIME_HOST_OWNER = {
    "exists": True,
    "is_symlink": False,
    "is_directory": True,
    "entry_count": 0,
    "uid": 1005,
    "gid": 1005,
    "mode": 0o700,
    "mount_device": "synthetic",
    "device_identity": "synthetic-device",
    "free_bytes": 1_000_000,
}


def test_integration_target_writability_is_proven_as_runtime_numeric_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0017", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
    )
    monkeypatch.setattr(
        "packages.common.node27_cold_tablespace_integration._host_path", lambda config: dict(_RUNTIME_HOST_OWNER)
    )
    seen: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        if argv[1] == "exec":
            user = argv[argv.index("--user") + 1]
            return _result(0 if user == "1005:1005" else 1)
        return _result(0, "[]\n")

    deps = dependencies(resources, runner=runner)
    observed = deps.inspect_target()

    assert observed["writable"] is True
    assert observed["host_uid"] == 1005
    assert observed["host_gid"] == 1005
    exec_argv = next(command for command in seen if command[1] == "exec")
    assert exec_argv[exec_argv.index("--user") + 1] == "1005:1005"
    assert "postgres" not in exec_argv


def test_integration_target_refuses_runtime_owner_mismatch_before_docker_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0018", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
    )
    monkeypatch.setattr(
        "packages.common.node27_cold_tablespace_integration._host_path",
        lambda config: {**_RUNTIME_HOST_OWNER, "uid": 999, "gid": 999},
    )
    seen: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        if argv[1] == "exec":
            return _result(0)
        return _result(0, "[]\n")

    deps = dependencies(resources, runner=runner)
    with pytest.raises(ColdTablespaceIntegrationError, match="owner"):
        deps.inspect_target()

    assert not any(command[1] == "exec" for command in seen)


def test_prepare_resources_forces_an_owned_work_root_to_mode_0700(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0005", host_port=55494)

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        if argv[1:3] == ("sudo", "-n"):
            raise AssertionError("cleanup must not route through a relative sudo binary")
        if argv[:2] == ("/usr/bin/sudo", "-n"):
            for child in (config.work_root / "pgdata", config.work_root / "receipts"):
                child.rmdir()
            (config.work_root / "postgres.env").unlink()
            return _result()
        assert argv[1] in {"rm", "inspect"}
        return _result(1 if argv[1] == "inspect" else 0)

    resources = prepare_resources(
        config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
        runner=runner,
    )
    try:
        assert stat_mode(config.work_root) == 0o700
    finally:
        cleanup(resources, runner=runner)


def test_cleanup_requires_checked_container_absence_and_port_release(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0001")
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
        created_work_root=True,
    )
    config.work_root.mkdir()
    seen: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        seen.append(argv)
        return SimpleNamespace(returncode=1 if argv[1] == "inspect" else 0, stdout="", stderr="")

    assert cleanup(resources, runner=runner) == {"container_absent": True, "work_root_absent": True}
    assert sum(command[1] == "inspect" for command in seen) == 6


def test_cleanup_fails_when_post_remove_inspect_still_finds_an_owned_container(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0001")
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
        created_work_root=True,
    )
    config.work_root.mkdir()

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ColdTablespaceIntegrationError, match="container remains"):
        cleanup(resources, runner=runner)


def test_cleanup_does_not_remove_an_owned_root_before_container_absence(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0013", host_port=55494)
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=PINNED_IMAGE_ID,
            image_ref=PINNED_IMAGE_REF,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
        created_work_root=True,
    )
    config.work_root.mkdir()

    def runner(argv: tuple[str, ...], *, timeout: int = 90):
        if argv[1] == "inspect" and argv[-1] == config.container_name:
            return _result(0)
        if argv[:2] == ("/usr/bin/sudo", "-n"):
            raise AssertionError("cleanup must not reach the root helper before container absence")
        return _result(1 if argv[1] == "inspect" else 0)

    with pytest.raises(ColdTablespaceIntegrationError, match="container remains"):
        cleanup(resources, runner=runner)

    assert config.work_root.exists()
    config.work_root.rmdir()


def test_cleanup_fails_when_the_disposable_port_cannot_be_rebound(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 55494))
        config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0001", host_port=55494)
        resources = IntegrationResources(
            config=config,
            capability=RootEvidenceCapability(
                strategy="sudo",
                image_postgres_uid=1000,
                image_postgres_gid=1000,
                runtime_uid=1005,
                runtime_gid=1005,
                image_id=PINNED_IMAGE_ID,
                image_ref=PINNED_IMAGE_REF,
                image_default_user="postgres",
                root_proof="sudo-noninteractive",
            ),
            created_work_root=True,
        )
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
        capability = require_root_evidence_capability(config)
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    evidence: dict[str, bool] | None = None
    try:
        resources = prepare_resources(config, capability=capability)
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
            expected_uid=resources.require_capability().runtime_uid,
            expected_gid=resources.require_capability().runtime_gid,
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
        assert result.receipt["container_snapshot"]["config_digest"] == after_snapshot.config_digest
        assert sorted(
            item.split("=", 1)[0] for item in after_snapshot.environment
        ) == result.receipt["container_snapshot"]["environment_names"]
        assert config.password not in receipt_path.read_text(encoding="utf-8")
        assert any(action[1] == "run" for action in resources.actions)
        assert_new_chunk_pg_default(config)

        assert stat_mode(receipt_path) == 0o600
        after_digest = after_snapshot.config_digest
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
        capability = require_root_evidence_capability(config)
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    try:
        resources = prepare_resources(config, capability=capability)
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
                expected_uid=resources.require_capability().runtime_uid,
                expected_gid=resources.require_capability().runtime_gid,
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
@pytest.mark.parametrize("action", ("stop", "rename", "run"))
def test_real_interrupted_replacement_recovers_without_install_replay(tmp_path: Path, action: str) -> None:
    pytest.importorskip("psycopg2")
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0003")
    try:
        capability = require_root_evidence_capability(config)
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    try:
        resources = prepare_resources(config, capability=capability)
        health = root_evidence_ready(resources)
        before = normalize_raw_inspect(start_prior(resources))
        bootstrap_business_tables(config)
        receipt_path = config.work_root / "receipts" / "interrupted.json"
        recovery_path = config.work_root / "receipts" / "interrupted.recovery.json"

        settings = dict(
            enforce=True,
            receipt_path=receipt_path,
            recovery_path=recovery_path,
            head_sha="a" * 40,
            expected_uid=resources.require_capability().runtime_uid,
            expected_gid=resources.require_capability().runtime_gid,
            expected_mode=0o700,
            expected_device_identity="synthetic-device",
            install_required_bytes=1,
            rollback_headroom_bytes=1,
            identity=config.identity,
        )
        base_deps = dependencies(resources, health=health)
        original_docker = base_deps.docker

        def docker(argv: tuple[str, ...]):
            result = original_docker(argv)
            if argv[1] == action:
                raise InstallInterrupted(f"after {action} before confirm")
            return result

        interrupted_deps = InstallDependencies(**{**base_deps.__dict__, "docker": docker})
        with pytest.raises(InstallInterrupted):
            run_install(InstallConfig(**settings), interrupted_deps)
        assert recovery_path.exists()
        authority = json.loads(recovery_path.read_text(encoding="utf-8"))
        assert authority.get("pending_action")
        actions_before = list(resources.actions)
        recovered = run_install(InstallConfig(**settings), dependencies(resources, health=health))
        assert recovered.outcome == "rollback", recovered.receipt
        recovery_actions = resources.actions[len(actions_before) :]
        assert not any(item[1] == "run" for item in recovery_actions)
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


@pytest.mark.integration
@pytest.mark.timescaledb_210
@pytest.mark.node27_docker
def test_real_terminal_unlink_retry_closes_installed_without_docker_replay(tmp_path: Path) -> None:
    pytest.importorskip("psycopg2")
    config = default_config(work_root=tmp_path / f"{INTEGRATION_PREFIX}c1ea0005")
    try:
        capability = require_root_evidence_capability(config)
    except ColdTablespaceIntegrationError as error:
        pytest.skip(str(error))
    resources: IntegrationResources | None = None
    try:
        resources = prepare_resources(config, capability=capability)
        health = root_evidence_ready(resources)
        start_prior(resources)
        bootstrap_business_tables(config)
        receipt_path = config.work_root / "receipts" / "unlink.json"
        recovery_path = config.work_root / "receipts" / "unlink.recovery.json"
        settings = dict(
            enforce=True,
            receipt_path=receipt_path,
            recovery_path=recovery_path,
            head_sha="a" * 40,
            expected_uid=resources.require_capability().runtime_uid,
            expected_gid=resources.require_capability().runtime_gid,
            expected_mode=0o700,
            expected_device_identity="synthetic-device",
            install_required_bytes=1,
            rollback_headroom_bytes=1,
            identity=config.identity,
        )
        first_deps = dependencies(resources, health=health)
        first_deps.remove_recovery = lambda _path: (_ for _ in ()).throw(RuntimeError("unlink failed"))
        first = run_install(InstallConfig(**settings), first_deps)
        assert first.outcome == "pending_cleanup", first.receipt
        assert recovery_path.exists()
        actions_before = list(resources.actions)
        second = run_install(InstallConfig(**settings), dependencies(resources, health=health))
        assert second.outcome == "installed", second.receipt
        assert not recovery_path.exists()
        assert not any(item[1] in {"stop", "rename", "run", "rm"} for item in resources.actions[len(actions_before) :])
    finally:
        if resources is not None:
            cleanup(resources)
