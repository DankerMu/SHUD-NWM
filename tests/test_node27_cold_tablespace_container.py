"""Contract tests for inert exact nhms-db snapshot/recreate/rollback planning."""

from __future__ import annotations

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID, PINNED_IMAGE_REF
from packages.common.node27_cold_tablespace_container import (
    COLD_BIND,
    COLD_CONTAINER_PATH,
    build_recreate_argv,
    diff_container_config,
    rollback_plan,
    with_cold_bind,
)
from packages.common.node27_pgdata_container import ContainerContractError, normalize_raw_inspect
from tests.test_node27_pgdata_container import (
    _SYNTHETIC_MEMORY_SWAP,
    _captured_host_defaults,
    _inspect,
)


def test_recreate_argv_preserves_exact_supported_nondefault_configuration_and_adds_one_bind() -> None:
    before = normalize_raw_inspect(_inspect())

    argv = build_recreate_argv(before, replacement_name="nhms-db")

    assert argv[:5] == ("/usr/bin/docker", "run", "-d", "--name", "nhms-db")
    assert "POSTGRES_PASSWORD=ultra-secret" in argv
    assert ("-p", "127.0.0.1:55432:5432") == tuple(argv[argv.index("-p") : argv.index("-p") + 2])
    assert ("--memory", "8589934592") == tuple(argv[argv.index("--memory") : argv.index("--memory") + 2])
    assert ("--cpus", "2") == tuple(argv[argv.index("--cpus") : argv.index("--cpus") + 2])
    assert ("--restart", "unless-stopped") == tuple(argv[argv.index("--restart") : argv.index("--restart") + 2])
    assert COLD_BIND in argv
    assert argv[-2:] == (PINNED_IMAGE_ID, "postgres")
    assert PINNED_IMAGE_REF not in argv
    assert not any("/bin/sh" in item or "$(" in item for item in argv)


def test_exact_diff_accepts_only_cold_bind_and_rejects_image_env_port_resource_or_mount_drift() -> None:
    before = normalize_raw_inspect(_inspect())
    recreated = _inspect()
    recreated["Id"] = "sha256:container-after"
    recreated["HostConfig"]["Binds"].append(COLD_BIND)
    after = normalize_raw_inspect(recreated)

    assert before.container_id != after.container_id
    assert with_cold_bind(before).config_digest == after.config_digest
    assert diff_container_config(before, after).approved is True

    changed = _inspect()
    changed["Config"]["Image"] = "other:image"
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect(env=["POSTGRES_PASSWORD=changed", "POSTGRES_USER=nhms", "PGDATA=/home/postgres/pgdata/data"])
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect()
    changed["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "55433"
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect()
    changed["HostConfig"]["Memory"] = 1
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect()
    changed["HostConfig"]["Binds"].append("/tmp/extra:/extra:rw")
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    same_ref_different_id = _inspect()
    same_ref_different_id["Image"] = "sha256:" + "0" * 64
    same_ref_different_id["HostConfig"]["Binds"].append(COLD_BIND)
    drifted = normalize_raw_inspect(same_ref_different_id)
    assert drifted.image == PINNED_IMAGE_ID
    assert drifted.resolved_image_id != before.resolved_image_id
    assert diff_container_config(before, drifted).approved is False


def test_rollback_plan_never_deletes_host_path_if_any_reference_or_identity_doubt_remains() -> None:
    safe = rollback_plan(
        installer_container="nhms-db",
        prior_container="nhms-db-before",
        installer_created_catalog=True,
        catalog_dependents=0,
        pg_tblspc_references=(),
        current_bind_references=(),
        stopped_bind_references=(),
        host_path_identity_matches=True,
        host_path_empty=True,
    )
    assert safe.restore_prior is True
    assert safe.remove_host_path is True

    for kwargs in (
        {"catalog_dependents": 1},
        {"pg_tblspc_references": (COLD_CONTAINER_PATH,)},
        {"current_bind_references": (COLD_BIND,)},
        {"stopped_bind_references": (COLD_BIND,)},
        {"host_path_identity_matches": False},
        {"host_path_empty": False},
    ):
        arguments = {
            "installer_container": "nhms-db",
            "prior_container": "nhms-db-before",
            "installer_created_catalog": True,
            "catalog_dependents": 0,
            "pg_tblspc_references": (),
            "current_bind_references": (),
            "stopped_bind_references": (),
            "host_path_identity_matches": True,
            "host_path_empty": True,
        }
        arguments.update(kwargs)
        plan = rollback_plan(**arguments)
        assert plan.remove_host_path is False, kwargs
        assert plan.blockers


def test_rollback_plan_does_not_remove_an_uncreated_installer_container() -> None:
    plan = rollback_plan(
        installer_container="nhms-db",
        prior_container="nhms-db-before",
        installer_created_catalog=False,
        catalog_dependents=0,
        pg_tblspc_references=(),
        current_bind_references=(),
        stopped_bind_references=(),
        host_path_identity_matches=True,
        host_path_empty=True,
        installer_container_created=False,
    )

    assert plan.remove_installer_container is False


def test_altered_memory_swap_or_derived_paths_fail_exact_diff() -> None:
    before = normalize_raw_inspect(_captured_host_defaults())
    after_raw = _captured_host_defaults()
    after_raw["Id"] = "sha256:container-after"
    after_raw["HostConfig"]["Binds"].append(COLD_BIND)
    after = normalize_raw_inspect(after_raw)
    assert diff_container_config(before, after).approved is True

    swapped = _captured_host_defaults()
    swapped["HostConfig"]["MemorySwap"] = _SYNTHETIC_MEMORY_SWAP + 1
    assert diff_container_config(before, normalize_raw_inspect(swapped)).approved is False

    missing_paths = _captured_host_defaults()
    missing_paths["HostConfig"].pop("MaskedPaths")
    missing_paths["HostConfig"].pop("ReadonlyPaths")
    assert diff_container_config(before, normalize_raw_inspect(missing_paths)).approved is False

    readonly_changed = _captured_host_defaults()
    readonly_changed["HostConfig"]["ReadonlyPaths"] = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys"]
    with pytest.raises(ContainerContractError, match="ReadonlyPaths"):
        normalize_raw_inspect(readonly_changed)


def test_real_two_segment_default_rw_binds_normalize_recreate_and_diff() -> None:
    raw = _inspect()
    raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm/Basins:/data/GHDC:rw",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
    ]
    snapshot = normalize_raw_inspect(raw)
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")
    volumes = [argv[index + 1] for index, item in enumerate(argv) if item in {"-v", "--volume"}]

    assert "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data:rw" in snapshot.binds
    assert "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence:rw" in snapshot.binds
    assert "/home/ghdc/nwm/Basins:/data/GHDC:rw" in snapshot.binds
    assert all(":" in bind for bind in volumes)
    after_raw = _inspect()
    after_raw["Id"] = "sha256:container-after"
    after_raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm/Basins:/data/GHDC:rw",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
        COLD_BIND,
    ]
    after = normalize_raw_inspect(after_raw)
    assert diff_container_config(snapshot, after).approved is True
    assert after.binds != snapshot.binds
    assert set(after.binds) - set(snapshot.binds) == {COLD_BIND}


def test_live_three_two_segment_binds_only_differ_by_cold_bind() -> None:
    raw = _inspect()
    raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm:/data/GHDC",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
    ]
    before = normalize_raw_inspect(raw)
    after_raw = _inspect()
    after_raw["Id"] = "sha256:container-after"
    after_raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm:/data/GHDC",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
        COLD_BIND,
    ]
    after = normalize_raw_inspect(after_raw)
    diff = diff_container_config(before, after)
    assert diff.approved is True
    assert diff.changed_fields == ("binds",)


def test_tag_only_config_image_normalizes_but_preflight_must_refuse_it() -> None:
    raw = _inspect()
    raw["Config"]["Image"] = PINNED_IMAGE_REF
    snapshot = normalize_raw_inspect(raw)
    assert snapshot.image == PINNED_IMAGE_REF
    assert snapshot.resolved_image_id == PINNED_IMAGE_ID
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")
    assert argv[-2] == PINNED_IMAGE_ID
    assert snapshot.image != argv[-2]
