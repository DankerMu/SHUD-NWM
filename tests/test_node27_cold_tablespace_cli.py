"""Public CLI configuration contract for the cold tablespace installer."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema

from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY
from scripts import node27_cold_tablespace_install as installer_cli
from tests.test_node27_cold_tablespace_install import FakeConnection, _dependencies


def _args(tmp_path: Path, *extra: str):
    return installer_cli.build_parser().parse_args(
        [
            "--receipt-path",
            str(tmp_path / "receipt.json"),
            "--recovery-path",
            str(tmp_path / "recovery.json"),
            "--expected-uid",
            "999",
            "--expected-gid",
            "999",
            "--expected-mode",
            "700",
            "--expected-device-identity",
            "8:11:1",
            "--install-required-bytes",
            "100",
            "--rollback-headroom-bytes",
            "200",
            *extra,
        ]
    )


def test_cli_defaults_to_dry_run_and_parses_explicit_enforce(tmp_path: Path) -> None:
    assert installer_cli.config_from_args(_args(tmp_path)).enforce is False
    assert installer_cli.config_from_args(_args(tmp_path, "--enforce")).enforce is True


def test_cli_constructs_only_the_fixed_production_identity_and_has_no_identity_override_options(tmp_path: Path) -> None:
    config = installer_cli.config_from_args(_args(tmp_path))
    option_names = {action.dest for action in installer_cli.build_parser()._actions}

    assert config.identity is PRODUCTION_IDENTITY
    assert not (
        {
            "container_name",
            "prior_container_name",
            "host_path",
            "container_path",
            "tablespace",
            "docker_bin",
            "host_port",
            "work_root",
            "image_id",
            "image_ref",
        }
        & option_names
    )


def test_cli_builds_declared_dependencies_and_reaches_run_install(tmp_path: Path, monkeypatch) -> None:
    arguments = [
        "--receipt-path",
        str(tmp_path / "receipt.json"),
        "--recovery-path",
        str(tmp_path / "recovery.json"),
        "--expected-uid",
        "999",
        "--expected-gid",
        "999",
        "--expected-mode",
        "700",
        "--expected-device-identity",
        "8:11:1",
        "--install-required-bytes",
        "100",
        "--rollback-headroom-bytes",
        "200",
        "--evidence-hostname",
        "node27-test",
        "--evidence-max-age-seconds",
        "300",
        "--evidence-approved-mode",
        "600",
        "--mdadm-evidence",
        str(tmp_path / "mdadm.json"),
        "--smart-evidence",
        f"/dev/sdb1={tmp_path / 'sdb.json'}",
        "--smart-evidence",
        f"/dev/sdc1={tmp_path / 'sdc.json'}",
        "--backup-evidence",
        str(tmp_path / "backup.json"),
    ]
    captured: dict[str, object] = {}

    class Docker:
        def __init__(self, **_kwargs) -> None:
            pass

        def inspect(self, _name: str) -> dict:
            return {}

        def action(self, _argv: tuple[str, ...]) -> dict:
            return {"returncode": 0}

        def current_and_stopped_cold_binds(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
            return (), ()

    monkeypatch.setattr(installer_cli, "DockerBoundary", Docker)
    monkeypatch.setattr(installer_cli, "SystemdBoundary", lambda: SimpleNamespace(inspect_quiescence=lambda _units: {}))

    def run_install(config, dependencies):
        captured["config"] = config
        captured["dependencies"] = dependencies
        return SimpleNamespace(outcome="dry_run", receipt={"outcome": "dry_run"})

    monkeypatch.setattr(installer_cli, "run_install", run_install)

    assert installer_cli.main(arguments) == 0
    config = captured["config"]
    assert getattr(config, "identity") is PRODUCTION_IDENTITY
    dependencies = captured["dependencies"]
    assert isinstance(dependencies, installer_cli.InstallDependencies)
    assert "recovery_exists" not in installer_cli.InstallDependencies.__dataclass_fields__


def test_cli_target_wiring_passes_config_expected_uid_gid_into_running_target(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_inspect_running_target(docker, *, expected_uid, expected_gid):
        captured["expected_uid"] = expected_uid
        captured["expected_gid"] = expected_gid
        return {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "8:11:1",
            "writable": True,
            "host_mode": 0o700,
            "host_uid": expected_uid,
            "host_gid": expected_gid,
        }

    monkeypatch.setattr(installer_cli, "inspect_running_target", fake_inspect_running_target)
    monkeypatch.setattr(installer_cli, "SystemdBoundary", lambda: SimpleNamespace(inspect_quiescence=lambda _units: {}))
    args = _args(
        tmp_path,
        "--evidence-hostname",
        "node27-test",
        "--evidence-max-age-seconds",
        "300",
        "--evidence-approved-mode",
        "600",
        "--mdadm-evidence",
        str(tmp_path / "mdadm.json"),
        "--smart-evidence",
        f"/dev/sdb1={tmp_path / 'sdb.json'}",
        "--backup-evidence",
        str(tmp_path / "backup.json"),
    )
    config = installer_cli.config_from_args(args)
    dependencies = installer_cli.dependencies_from_args(
        args,
        config,
    )
    dependencies.inspect_target()

    assert captured == {"expected_uid": 999, "expected_gid": 999}


def test_cli_requires_descriptor_evidence_configuration_before_constructing_live_dependencies(tmp_path: Path) -> None:
    args = _args(tmp_path)
    config = installer_cli.config_from_args(args)

    try:
        installer_cli.dependencies_from_args(args, config)
    except ValueError as error:
        assert "hostname" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("adapter must reject unbound evidence configuration")


def test_cli_missing_dsn_publishes_a_schema_valid_no_go_without_secret_leak(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    deps = _dependencies(FakeConnection())
    deps.connect_readonly = lambda: (_ for _ in ()).throw(RuntimeError("postgresql://user:secret@host/db unavailable"))
    monkeypatch.setattr(installer_cli, "dependencies_from_args", lambda _args, _config: deps)
    argv = [
        "--receipt-path",
        str(tmp_path / "receipt.json"),
        "--recovery-path",
        str(tmp_path / "recovery.json"),
        "--expected-uid",
        "999",
        "--expected-gid",
        "999",
        "--expected-mode",
        "700",
        "--expected-device-identity",
        "8:11:1",
        "--install-required-bytes",
        "100",
        "--rollback-headroom-bytes",
        "200",
    ]

    assert installer_cli.main(argv) == 2

    output = capsys.readouterr()
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["outcome"] == "no_go"
    assert "secret" not in json.dumps(receipt)
    assert "secret" not in output.out + output.err
    jsonschema.validate(receipt, installer_cli.InstallConfig.load_schema())


def test_cli_refuses_invalid_paths_and_publishes_no_live_adapter_claim(tmp_path: Path) -> None:
    relative = _args(tmp_path)
    relative.receipt_path = Path("relative")
    relative.recovery_path = Path("relative")
    try:
        installer_cli.config_from_args(relative)
    except ValueError as error:
        assert "absolute" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("relative receipt/recovery paths must be refused")
    assert (
        installer_cli.main(
            [
                "--receipt-path",
                str(tmp_path / "receipt.json"),
                "--recovery-path",
                str(tmp_path / "recovery.json"),
                "--expected-uid",
                "999",
                "--expected-gid",
                "999",
                "--expected-mode",
                "700",
                "--expected-device-identity",
                "8:11:1",
                "--install-required-bytes",
                "100",
                "--rollback-headroom-bytes",
                "200",
            ]
        )
        == 2
    )


def test_cli_source_has_no_environment_or_argument_identity_override_surface() -> None:
    source = Path(installer_cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    arguments = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]

    assert not any("container" in value or "tablespace" in value or "cold-path" in value for value in arguments)
    assert "NODE27_COLD_CONTAINER" not in source
    assert "NODE27_COLD_HOST_PATH" not in source
