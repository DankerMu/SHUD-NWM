"""What the node-27 basin retirement tool refuses before any step, and a step before its predecessor (#2757).

Partition of the ``scripts/node27_retire_basin.py`` suite; the fakes and the
``space`` fixture are in ``tests/node27_retire_basin_helpers.py``.  "Refused"
means here: exit 1 and the whole ``tmp_path`` tree and the committed database
are what they were.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import psycopg2
import pytest

from scripts.basin_retirement import run
from scripts.basin_retirement.model import Basin, StepFailure
from scripts.node27_retire_basin import _parse_args, settings_from_arguments
from tests.node27_retire_basin_helpers import (
    DATABASE_URL,
    HUAI,
    SUCCESSION_ID,
    WEI,
    Space,
    held_flock,
    manifest_row,
    space,  # noqa: F401 - the fixture
)


def _refused(space: Space, *, basin_version_id: str = HUAI, apply: bool = True) -> str:  # noqa: F811
    """Run, assert the refusal left everything as it was, and return what was said."""

    before = space.everything()
    status, report = space.run(basin_version_id, apply=apply)
    assert status == 1 and report is None, (status, report)
    assert space.everything() == before
    assert space.systemctl_calls() == []
    assert space.store.preflights == [] and space.store.operations == []
    return space.last_stderr


def _no_finish_receipt(space: Space) -> None:  # noqa: F811
    (space.succession / "step-finish.json").unlink()


def _finish_not_completed(space: Space) -> None:  # noqa: F811
    path = space.succession / "step-finish.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "outcome": "failed"}))


def _another_kind(space: Space) -> None:  # noqa: F811
    space.write_node22(kind="add_basin")


def _plan_removed_other_models(space: Space) -> None:  # noqa: F811
    space.write_node22(removes=["dg_tao_gfs", "dg_never_registered"])


def _manifest_has_the_basin_version(space: Space) -> None:  # noqa: F811
    space.write_manifest(
        [manifest_row("dg_wei_gfs", "basins_wei", WEI), manifest_row("dg_huai_gfs", "basins_huai", HUAI)]
    )


def _manifest_has_another_version(space: Space) -> None:  # noqa: F811
    space.write_manifest(
        [manifest_row("dg_wei_gfs", "basins_wei", WEI), manifest_row("dg_huai_v2", "basins_huai", "basins_huai_v2")]
    )


def _manifest_has_another_spelling_of_the_basin(space: Space) -> None:  # noqa: F811
    # Not the same string, but the same key for the autopipeline: the exclusion would stop this row's ingest too.
    space.write_manifest([manifest_row("dg_huai_v2", "Basins-HUAI", "basins_huai_v2")])


def _manifest_missing(space: Space) -> None:  # noqa: F811
    space.manifest.unlink()


PRECONDITIONS: list[tuple[Callable[[Space], None], str, str]] = [
    (_no_finish_receipt, HUAI, "has not finished on node-22"),
    (_finish_not_completed, HUAI, "has outcome 'failed', not 'completed'"),
    (_another_kind, HUAI, "has kind 'add_basin', not 'remove_basin'"),
    (lambda space: None, "basins_huai_v9", "is not a row of core.basin_version"),  # noqa: F811
    (_plan_removed_other_models, HUAI, "this succession did not remove this basin"),
    (_manifest_has_the_basin_version, HUAI, f"still holds rows of basin version {HUAI}: ['dg_huai_gfs']"),
    (_manifest_has_another_version, HUAI, "holds rows of another version of basin 'basins_huai': ['dg_huai_v2']"),
    (_manifest_has_another_spelling_of_the_basin, HUAI, "holds rows of another version of basin 'basins_huai'"),
    (_manifest_missing, HUAI, "manifest-last.json cannot be read"),
]


@pytest.mark.parametrize(("arrange", "basin_version_id", "said"), PRECONDITIONS)
def test_a_failed_precondition_is_refused_before_any_write(
    space: Space,  # noqa: F811
    arrange: Callable[[Space], None],
    basin_version_id: str,
    said: str,
) -> None:
    arrange(space)

    message = _refused(space, basin_version_id=basin_version_id)

    assert "Refused before any step" in message and said in message
    assert "Nothing was written." in message
    # Not even the lock file aside: no directory of the basin version, no backup beside the env file.
    assert not space.directory(basin_version_id).exists()
    assert sorted(path.name for path in space.env_file.parent.iterdir()) == [
        "node27-ingest.env",
        "node27-ingest.env.retire-lock",
    ]
    # Read-only sessions only, and nothing committed.
    assert space.database.connections and all(connection.readonly for connection in space.database.connections)
    assert space.database.commits == 0


@pytest.mark.parametrize("name", ["NHMS_AUTH_MODE", "AUTH_BACKEND"])
@pytest.mark.parametrize("apply", [True, False])
def test_an_auth_variable_in_the_environment_is_refused_before_anything(
    space: Space,  # noqa: F811
    name: str,
    apply: bool,
) -> None:
    # Set at all is enough: even a value that selects nothing.
    space.monkeypatch.setenv(name, "")

    message = _refused(space, apply=apply)

    assert f"{name} is set" in message
    assert "env -u NHMS_AUTH_MODE -u AUTH_BACKEND" in message
    assert space.database.connections == []
    assert not space.retire_lock.exists()


def _other_database(space: Space) -> None:  # noqa: F811
    space.monkeypatch.setenv("DATABASE_URL", "postgresql://scratch:pw@127.0.0.1:5432/scratch")


def _quoted_line(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text().replace(f"DATABASE_URL={DATABASE_URL}", f'DATABASE_URL="{DATABASE_URL}"'))


def _two_lines(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text() + f"DATABASE_URL={DATABASE_URL}\n")


def _an_export_line_as_well(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text() + "export DATABASE_URL=postgresql://other:pw@127.0.0.1:5432/other\n")


def _no_line(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text().replace(f"DATABASE_URL={DATABASE_URL}\n", ""))


def _trailing_comment(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text().replace(f"DATABASE_URL={DATABASE_URL}", f"DATABASE_URL={DATABASE_URL} # primary"))


@pytest.mark.parametrize(
    ("arrange", "said"),
    [
        (_other_database, "DATABASE_URL of this process is not the one in"),
        (_quoted_line, "non-empty and unquoted"),
        (_two_lines, "it has 2 such lines"),
        (_an_export_line_as_well, "2 lines assigning DATABASE_URL in any form"),
        (_no_line, "it has 0 such lines"),
        (_trailing_comment, "DATABASE_URL of this process is not the one in"),
    ],
)
@pytest.mark.parametrize("apply", [True, False])
def test_a_database_that_is_not_the_env_files_is_refused_before_anything(
    space: Space,  # noqa: F811
    arrange: Callable[[Space], None],
    said: str,
    apply: bool,
) -> None:
    arrange(space)

    message = _refused(space, apply=apply)

    assert said in message and "Nothing was written." in message
    # Neither URL is ever printed.
    assert "s3cret-pw" not in message and "scratch:pw" not in message and "other:pw" not in message
    assert space.database.connections == []
    assert not space.retire_lock.exists()


@pytest.mark.parametrize("apply", [True, False])
def test_a_second_instance_is_refused_while_the_lock_is_held(space: Space, apply: bool) -> None:  # noqa: F811
    with held_flock(space.retire_lock):
        before = space.everything(without_lock=False)
        status, report = space.run(apply=apply)
        assert space.everything(without_lock=False) == before

    assert status == 1 and report is None
    assert "another basin retirement is running" in space.last_stderr
    assert space.database.connections == [] and space.systemctl_calls() == []

    # Released with its holder: the same command then runs.
    status, _report = space.run(apply=apply)
    assert status == 0, space.last_stderr


def test_the_lock_is_released_when_the_run_ends_and_is_created_private(space: Space) -> None:  # noqa: F811
    assert not space.retire_lock.exists()

    assert space.run(apply=False)[0] == 0
    assert space.retire_lock.stat().st_mode & 0o777 == 0o600
    assert space.run()[0] == 0, space.last_stderr


@pytest.mark.parametrize(
    ("option", "value", "said"),
    [
        ("--basin-version-id", "basins/huai", "Invalid --basin-version-id"),
        ("--basin-version-id", "x" * 121, "Invalid --basin-version-id"),
        ("--succession-id", "..", "Invalid --succession-id"),
        ("--reason", "   ", "--reason must not be empty"),
        ("--operator-id", "", "--operator-id must not be empty"),
        ("--autopipe-wait-seconds", "0", "--autopipe-wait-seconds must be greater than 0"),
    ],
)
def test_arguments_that_cannot_name_a_retirement_are_refused(
    space: Space,  # noqa: F811
    option: str,
    value: str,
    said: str,
) -> None:
    arguments = space.arguments()
    arguments[arguments.index(option) + 1] = value
    before = space.everything(without_lock=False)

    from scripts.node27_retire_basin import main

    assert main(arguments) == 1
    assert said in space.capsys.readouterr().err
    assert space.everything(without_lock=False) == before
    assert space.database.connections == []


@pytest.mark.parametrize("name", ["DATABASE_URL", "OBJECT_STORE_ROOT"])
def test_a_missing_environment_variable_is_refused(space: Space, name: str) -> None:  # noqa: F811
    space.monkeypatch.delenv(name)

    message = _refused(space)

    assert f"{name} must be set" in message
    assert space.database.connections == []


def test_the_defaults_are_the_checkouts_env_file_and_the_stores_succession_root(space: Space) -> None:  # noqa: F811
    arguments = [
        "--succession-id", SUCCESSION_ID, "--basin-version-id", HUAI, "--operator-id", "danker", "--reason", "why",
    ]  # fmt: skip

    settings = settings_from_arguments(_parse_args(arguments))

    assert settings.env_file.parts[-3:] == ("infra", "env", "node27-ingest.env")
    assert settings.env_file.parents[2].joinpath("scripts", "node27_retire_basin.py").is_file()
    assert settings.receipt_root == space.object_store / "scheduler" / "succession"
    assert settings.manifest == space.object_store / "scheduler" / "registry" / "manifest-last.json"
    assert settings.autopipe_wait_seconds == 1800
    assert settings.lock_file.name == "node27-ingest.env.retire-lock"
    assert "s3cret-pw" not in repr(settings)


STEP_ORDER = [
    ("supersede", (), ["retire-exclude.json"]),
    ("deactivate", (), ["retire-exclude.json", "retire-supersede.json"]),
    ("deactivate", ("exclude",), ["retire-supersede.json"]),
    ("deactivate", ("supersede",), ["retire-exclude.json"]),
    ("verify", ("exclude", "supersede"), ["retire-deactivate.json"]),
]


@pytest.mark.parametrize(("step", "present", "missing"), STEP_ORDER)
def test_a_step_without_the_receipt_it_requires_is_refused_and_changes_nothing(
    space: Space,  # noqa: F811
    step: str,
    present: tuple[str, ...],
    missing: list[str],
) -> None:
    settings = settings_from_arguments(_parse_args(space.arguments()))
    settings.directory.mkdir(parents=True)
    for name in present:
        settings.step_receipt(name).write_text("{}", encoding="utf-8")
    before = space.everything()

    with pytest.raises(StepFailure) as refusal:
        run.run_step(settings, Basin(basin_id="basins_huai", key="huai"), step)

    assert f"the {step} step requires the receipt of" in str(refusal.value)
    for name in missing:
        assert str(settings.directory / name) in str(refusal.value)
    assert f"The {step} step changed nothing." in str(refusal.value)
    assert space.everything() == before
    assert space.database.connections == [] and space.systemctl_calls() == []
    assert space.store.preflights == [] and space.store.operations == []


def test_a_database_that_cannot_be_read_is_refused_before_any_write(space: Space) -> None:  # noqa: F811
    space.database.read_error = psycopg2.OperationalError("could not connect to server: Connection refused")

    message = _refused(space)

    assert "Refused before any step" in message
    assert "the database could not be read: The database refused: OperationalError: could not connect" in message
    assert not space.directory().exists() and not space.env_backup().exists()
    assert "s3cret-pw" not in message
