"""The merged scheduler registry publish command, its DB-free boundary and its file modes (#2738).

Drives ``scripts.node22_publish_merged_scheduler_registry`` through the shared
workspace of ``tests/merged_registry_publish_helpers.py``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import scripts.node22_publish_merged_scheduler_registry as tool
from packages.common import provision_succession_receipt, succession_receipt
from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from tests.merged_registry_publish_helpers import (
    REPO_ROOT,
    Workspace,
    _cli_environment,
    _refused,
    _remove_case,
    no_database,  # noqa: F401  (registers the autouse fixture on this module)
    workspace_fixture,  # noqa: F401  (registers the `workspace` fixture on this module)
)

# --- the command line ------------------------------------------------------------


def test_the_command_reads_its_environment_says_it_is_a_dry_run_and_then_applies(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(monkeypatch, workspace)
    new = workspace.basin("d")
    workspace.provision("s-1", new)
    arguments = ["--add", "dg_d_gfs_v1", "--add", "dg_d_ifs_v1", "--operator-id", "op", "--succession-id", "s-1"]

    assert tool.main(arguments) == 0
    output = capsys.readouterr()
    assert output.out.startswith("DRY-RUN (no --apply): neither manifest is written")
    assert json.loads(output.out[output.out.index("{") :])["outcome"] == "planned"
    assert str(workspace.receipt_root / "s-1" / "publish-dry-run.json") in output.err
    assert workspace.models(workspace.canonical) == workspace.rows

    assert tool.main([*arguments, "--apply"]) == 0
    output = capsys.readouterr()
    assert "DRY-RUN" not in output.out and json.loads(output.out)["outcome"] == "published"
    assert workspace.models(workspace.canonical) == workspace.models(workspace.mirror) == [*workspace.rows, *new]


def test_the_command_exits_non_zero_with_the_refusal_on_stderr(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(monkeypatch, workspace)

    assert tool.main(["--remove", "dg_x_gfs_v1", "--operator-id", "op"]) == 1
    assert "not in the canonical manifest: ['dg_x_gfs_v1']" in capsys.readouterr().err

    monkeypatch.delenv("NHMS_SCHEDULER_PROVIDER_STORE_ROOT")
    assert tool.main(["--remove", "dg_a_gfs_v1", "--operator-id", "op"]) == 1
    assert "NHMS_SCHEDULER_PROVIDER_STORE_ROOT must be set" in capsys.readouterr().err


def test_the_manifest_options_override_the_environment(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(monkeypatch, workspace)
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", "/nowhere/canonical.json")
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST", "/nowhere/mirror.json")
    arguments = ["--remove", "dg_a_gfs_v1", "--remove", "dg_a_ifs_v1", "--operator-id", "op"]

    assert tool.main(arguments) == 1
    assert "/nowhere/canonical.json" in capsys.readouterr().err
    assert tool.main(
        [*arguments, "--canonical-manifest", str(workspace.canonical), "--mirror-manifest", str(workspace.mirror)]
    ) == 0


@pytest.mark.parametrize("value", ["old", "old:", ":new", "a:b:c"])
def test_a_replace_value_that_is_not_a_pair_is_a_usage_error(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        tool.main(["--replace", value, "--operator-id", "op"])
    assert raised.value.code == 2


# --- DB-free ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["DATABASE_URL", "PIPELINE_DATABASE_URL", "PGHOST", "PGSERVICE"])
def test_a_database_variable_in_the_environment_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "postgresql://somewhere/nhms")

    message = _refused(workspace, _remove_case(workspace)[0])

    assert "DB-free" in message and name in message and "somewhere" not in message


_DATABASE_DRIVERS = {"psycopg", "psycopg2", "psycopg_pool", "asyncpg", "sqlalchemy", "pg8000"}


def test_the_tool_and_the_shared_receipt_helpers_import_no_database_driver_themselves() -> None:
    package = sorted(
        str(path.relative_to(REPO_ROOT)) for path in (REPO_ROOT / "scripts/merged_registry_publish").glob("*.py")
    )
    assert len(package) >= 4, package  # the tool's logic lives in these modules
    for relative in (
        "scripts/node22_publish_merged_scheduler_registry.py",
        *package,
        "packages/common/succession_receipt.py",
    ):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            name.split(".")[0]
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        }
        assert imported & _DATABASE_DRIVERS == set(), relative


def test_importing_the_tool_with_no_database_variable_loads_no_driver_beyond_the_publisher_stack() -> None:
    # The publisher this tool must call lives in ``services.orchestrator``, whose
    # package import already loads psycopg2 (for DSN redaction, not a
    # connection); the tool must add no driver on top of that.
    program = (
        "import sys\n"
        "import services.orchestrator.scheduler_file_providers\n"
        "before = set(sys.modules)\n"
        "import scripts.node22_publish_merged_scheduler_registry\n"
        "print(sorted({name.split('.')[0] for name in set(sys.modules) - before}))\n"
    )
    environment = {name: value for name, value in os.environ.items() if name not in LIBPQ_CONNECTION_ENV_KEYS}
    completed = subprocess.run(
        [sys.executable, "-c", program], cwd=REPO_ROOT, env=environment, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert set(ast.literal_eval(completed.stdout)) & _DATABASE_DRIVERS == set()


# --- the receipt helpers are shared, not copied ---------------------------------------


def test_the_provision_receipt_module_still_exports_the_shared_helpers() -> None:
    for name in (
        "SuccessionReceiptError",
        "SUCCESSION_ID_PATTERN",
        "DEFAULT_RECEIPT_ROOT_KEY",
        "validate_succession_id",
        "default_receipt_root",
        "file_sha256",
        "object_store_key",
        "path_record",
        "git_commit",
        "prepare_receipt_target",
        "write_receipt",
    ):
        assert getattr(provision_succession_receipt, name) is getattr(succession_receipt, name), name


@pytest.fixture
def restrictive_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def test_receipts_and_backups_stay_readable_by_other_users_under_a_restrictive_umask(
    workspace: Workspace, restrictive_umask: None
) -> None:
    receipt = workspace.plan_then_apply(_remove_case(workspace)[0], "s-1")

    written = [
        workspace.receipt_root / "s-1" / "publish-dry-run.json",
        workspace.receipt_root / "s-1" / "publish-apply.json",
        Path(receipt["canonical"]["backup_path"]),
        Path(receipt["mirror"]["backup_path"]),
        workspace.canonical,
        workspace.mirror,
    ]
    assert [path.stat().st_mode & 0o777 for path in written] == [0o644] * 6
