"""Immutable production and disposable identity contracts for the cold installer."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID, PINNED_IMAGE_REF

_PREFIX = "nhms-1894-tablespace-"


def _identity_module():
    return importlib.import_module("packages.common.node27_cold_tablespace_identity")


def _synthetic_arguments(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / f"{_PREFIX}deadbeef"
    return {
        "container_name": f"{_PREFIX}deadbeef",
        "prior_container_name": f"{_PREFIX}deadbeef-before",
        "host_port": 55494,
        "work_root": root,
        "host_path": root / "cold",
        "image_id": PINNED_IMAGE_ID,
        "image_ref": PINNED_IMAGE_REF,
    }


def test_production_identity_is_the_exact_non_overridable_node27_contract() -> None:
    identity = _identity_module().PRODUCTION_IDENTITY

    assert identity.container_name == "nhms-db"
    assert identity.prior_container_name == "nhms-db-before"
    assert identity.host_path == Path("/data/GHDC/nhms-cold-tablespace")
    assert identity.container_path == "/home/postgres/pgdata/tablespaces/nhms_cold"
    assert identity.tablespace == "nhms_cold"
    assert identity.docker_bin == "/usr/bin/docker"
    assert identity.kind == "production"


def test_disposable_identity_factory_accepts_only_an_owned_pinned_contract(tmp_path: Path) -> None:
    module = _identity_module()

    identity = module.make_disposable_identity(**_synthetic_arguments(tmp_path))

    assert identity.kind == "synthetic"
    assert identity.container_name == f"{_PREFIX}deadbeef"
    assert identity.prior_container_name == f"{_PREFIX}deadbeef-before"
    assert identity.host_path == identity.work_root / "cold"
    assert identity.host_port == 55494
    assert identity.image_id == PINNED_IMAGE_ID
    assert identity.image_ref == PINNED_IMAGE_REF
    module.validate_identity_for_action(identity)
    module.assert_disposable_absent(
        identity,
        path_exists=lambda _path: False,
        container_exists=lambda _name: False,
        port_is_available=lambda _port: True,
    )


@pytest.mark.parametrize(
    "change",
    (
        {"container_name": "nhms-db"},
        {"prior_container_name": "nhms-db-before"},
        {"host_port": 55432},
        {"work_root": Path("/data/GHDC/nhms-1894-tablespace-deadbeef")},
        {"work_root": Path("/home/nwm/NWM/nhms-1894-tablespace-deadbeef")},
        {"container_name": "nhms-1892-probe-abcdef12"},
        {"image_id": "sha256:" + "0" * 64},
        {"image_ref": "untrusted:image"},
    ),
)
def test_disposable_identity_rejects_live_or_unowned_authority_before_any_action(
    tmp_path: Path, change: dict[str, object]
) -> None:
    module = _identity_module()
    arguments = _synthetic_arguments(tmp_path)
    arguments.update(change)
    if "work_root" in change:
        root = change["work_root"]
        assert isinstance(root, Path)
        arguments["host_path"] = root / "cold"

    with pytest.raises(module.IdentityContractError):
        module.make_disposable_identity(**arguments)


def test_disposable_identity_rejects_a_host_cold_path_outside_its_owned_root(tmp_path: Path) -> None:
    module = _identity_module()
    arguments = _synthetic_arguments(tmp_path)
    arguments["host_path"] = tmp_path / "outside-cold"

    with pytest.raises(module.IdentityContractError, match="under"):
        module.make_disposable_identity(**arguments)


def test_disposable_absence_proof_rejects_an_already_bound_port_before_action(tmp_path: Path) -> None:
    module = _identity_module()
    identity = module.make_disposable_identity(**_synthetic_arguments(tmp_path))

    with pytest.raises(module.IdentityContractError, match="port"):
        module.assert_disposable_absent(
            identity,
            path_exists=lambda _path: False,
            container_exists=lambda _name: False,
            port_is_available=lambda _port: False,
        )
