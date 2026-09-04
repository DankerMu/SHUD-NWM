"""Basins registry import: policy decisions, CLI authorization and misbinding.

Partition 5 of 7 of the former monolith ``tests/test_basins_registry_import.py``
(issue #1913): the 11 auth contracts / 13 nodes, including the five real-database
``integration`` cases that prove authorization is refused before any write.  Shared
test support lives in the non-collectible
``tests/basins_registry_import_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from packages.common.auth_policy import cli_policy_decision_from_evidence
from tests.basins_registry_import_helpers import (
    _PUBLIC_IMPORT_UNKNOWN_TARGET_ID,
    _assert_registry_fixture_rows_absent,
    _invoke_click,
    _write_registry_fixture,
)
from tests.integration_helpers import apply_migrations_from_zero
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    import_basins_registry,
)
from workers.model_registry.cli import _argparse_main


def test_argparse_import_basins_registry_without_cli_auth_rejects_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    report_path = tmp_path / "import-report.json"

    def fail_model_id_from_manifest(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("_model_id_from_manifest must not run before missing auth is denied")

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before missing auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before denied auth policy")

    monkeypatch.setattr("workers.model_registry.cli._model_id_from_manifest", fail_model_id_from_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--output",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["model_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()


def test_argparse_import_basins_registry_saml_blocks_cli_auth_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "saml")
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    report_path = tmp_path / "import-report.json"

    def fail_model_id_from_manifest(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("_model_id_from_manifest must not run before release-blocked auth is denied")

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before release-blocked auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before release-blocked auth policy")

    monkeypatch.setattr("workers.model_registry.cli._model_id_from_manifest", fail_model_id_from_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--output",
            str(report_path),
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "RELEASE_BLOCKED"
    assert error["model_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["auth_mode"] == "cli_dev_test_blocked_by_auth_backend_saml"
    assert error["policy_decision"]["no_mutation_expected"] is True
    assert not report_path.exists()


def test_click_import_basins_registry_without_cli_auth_rejects_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    report_path = tmp_path / "import-report.json"

    def fail_model_id_from_manifest(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("_model_id_from_manifest must not run before missing auth is denied")

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before missing auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before denied auth policy")

    monkeypatch.setattr("workers.model_registry.cli._model_id_from_manifest", fail_model_id_from_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    exit_code = _invoke_click(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--output",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["model_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()


@pytest.mark.integration
def test_import_basins_registry_without_policy_rejects_before_writes(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-no-direct-auth",
    )

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url=integration_database_url,
        )

    assert exc_info.value.error_code == "AUTH_REQUIRED"
    assert exc_info.value.details["no_mutation_expected"] is True
    assert exc_info.value.details["policy_decision"]["action_id"] == "models.switch_version"
    assert exc_info.value.details["policy_decision"]["target_type"] == "model_registry"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


def test_import_basins_registry_without_policy_rejects_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    manifest_path = tmp_path / "attacker-manifest.json"

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before missing auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before missing auth is denied")

    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
        )

    assert exc_info.value.error_code == "AUTH_REQUIRED"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID


def test_import_basins_registry_release_blocked_rejects_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    manifest_path = tmp_path / "attacker-manifest.json"
    policy_decision = cli_policy_decision_from_evidence(
        "models.switch_version",
        target_type="model_registry",
        target_id=_PUBLIC_IMPORT_UNKNOWN_TARGET_ID,
        actor_id="cli-model-admin",
        roles=("model_admin",),
        env={"AUTH_BACKEND": "saml"},
    )

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before release-blocked auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before release-blocked auth is denied")

    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            policy_decision=policy_decision,
        )

    assert exc_info.value.error_code == "RELEASE_BLOCKED"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["decision"] == "release_blocked"


@pytest.mark.parametrize(
    ("action_id", "target_type", "target_id"),
    [
        ("models.activate", "model_registry", _PUBLIC_IMPORT_UNKNOWN_TARGET_ID),
        ("models.switch_version", "model_instance", _PUBLIC_IMPORT_UNKNOWN_TARGET_ID),
        ("models.switch_version", "model_registry", "basins-other-model"),
    ],
)
def test_import_basins_registry_misbound_allow_rejects_before_manifest_read(
    action_id: str,
    target_type: str,
    target_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    manifest_path = tmp_path / "attacker-manifest.json"
    policy_decision = cli_policy_decision_from_evidence(
        action_id,
        target_type=target_type,
        target_id=target_id,
        actor_id="cli-model-admin",
        roles=("model_admin",),
    )

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before misbound auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before misbound auth is denied")

    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            policy_decision=policy_decision,
        )

    assert policy_decision is not None
    assert policy_decision.decision == "allow"
    assert exc_info.value.error_code == "RBAC_FORBIDDEN"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["no_mutation_expected"] is True
    assert exc_info.value.details["policy_decision"]["action_id"] == "models.switch_version"
    assert exc_info.value.details["policy_decision"]["target_type"] == "model_registry"
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["decision"] == "deny"


@pytest.mark.integration
def test_argparse_import_basins_registry_without_cli_auth_rejects_without_report_or_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-argparse-no-auth",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            "--output",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


@pytest.mark.integration
def test_click_import_basins_registry_without_cli_auth_rejects_without_report_or_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-click-no-auth",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _invoke_click(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            "--output",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


@pytest.mark.integration
def test_argparse_import_basins_registry_with_cli_model_admin_policy_imports_and_reports_auth(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-cli-auth",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            "--output",
            str(report_path),
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    written = json.loads(report_path.read_text(encoding="utf-8"))
    decision = report["auth_policy_decision"]
    assert exit_code == 0
    assert report["status"] == "imported"
    assert written["auth_policy_decision"] == decision
    assert decision["actor_id"] == "cli-model-admin"
    assert decision["roles"] == ["model_admin"]
    assert decision["action_id"] == "models.switch_version"
    assert decision["target_type"] == "model_registry"
    assert decision["target_id"] == model_id
    assert decision["decision"] == "allow"
    assert decision["execution_mode"] == "backend_route_executed"


@pytest.mark.integration
def test_argparse_import_basins_registry_production_mode_blocks_cli_auth_without_report_or_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_AUTH_MODE", "production")
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-prod-cli-blocked",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            "--output",
            str(report_path),
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "RELEASE_BLOCKED"
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["decision"] == "release_blocked"
    assert error["policy_decision"]["no_mutation_expected"] is True
    assert not report_path.exists()
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)
