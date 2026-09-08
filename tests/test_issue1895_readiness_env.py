"""G4 env rewrite executes on the checked-in example, not a grep token."""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.common.node27_issue1895_env import (
    DATABASE_URL_KEY,
    G4_GOVERNED_KEYS,
    parse_assignment_line,
    rewrite_cold_env_file,
    rewrite_cold_env_text,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts import node27_issue1895_env_rewrite as env_cli
from tests.test_issue1895_runbook_contract import _gate

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "infra" / "env" / "node27-cold-residency.example"

UPDATES = {
    "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "4096",
    "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "4096",
    "NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID": "1005",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID": "1005",
}


def test_rewrite_preserves_comments_blanks_and_unassigned_optional_keys() -> None:
    original = EXAMPLE.read_text(encoding="utf-8")
    rewritten = rewrite_cold_env_text(original, updates=UPDATES)
    assert original.splitlines()[0].startswith("#")
    assert rewritten.splitlines()[0] == original.splitlines()[0]
    assert "\n\n" in original
    assert any(line.startswith("#NODE27_COLD_RESIDENCY_LAG_SECONDS") for line in rewritten.splitlines())
    assert any(line.startswith("#NODE27_COLD_RESIDENCY_DEVICE_IDENTITY") for line in rewritten.splitlines())
    assert "NODE27_COLD_RESIDENCY_REPO_ROOT=/home/nwm/NWM" in rewritten
    original_url = next(line for line in original.splitlines() if line.startswith("DATABASE_URL="))
    rewritten_url = next(line for line in rewritten.splitlines() if line.startswith("DATABASE_URL="))
    assert rewritten_url == original_url
    assert original_url.encode("utf-8") == rewritten_url.encode("utf-8")
    for key, value in UPDATES.items():
        assert f"{key}={value}" in rewritten
    assert rewritten.count("NODE27_COLD_RESIDENCY_PER_TICK_BOUND=") == 1


def test_rewrite_refuses_duplicates_and_assigned_device_identity() -> None:
    original = EXAMPLE.read_text(encoding="utf-8")
    duplicated = original + "DATABASE_URL=postgresql://other@127.0.0.1/nhms\n"
    with pytest.raises(Issue1895ReadinessError) as duplicate:
        rewrite_cold_env_text(duplicated, updates=UPDATES)
    assert duplicate.value.code == "ENV_DUPLICATE_KEY"
    assigned = original.replace(
        "#NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=",
        "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=1:2",
    )
    with pytest.raises(Issue1895ReadinessError) as device:
        rewrite_cold_env_text(assigned, updates=UPDATES)
    assert device.value.code == "ENV_DEVICE_IDENTITY_ASSIGNED"


def test_g5_replaces_an_assigned_device_identity_when_explicitly_allowed() -> None:
    original = EXAMPLE.read_text(encoding="utf-8")
    assigned = original.replace(
        "#NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=",
        "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=1:2",
    )
    original_url = next(line for line in assigned.splitlines(keepends=True) if line.startswith("DATABASE_URL="))
    rewritten = rewrite_cold_env_text(
        assigned,
        updates={
            "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY": "3:4",
            "NODE27_COLD_RESIDENCY_LAG_SECONDS": "172800",
        },
        governed_keys=("NODE27_COLD_RESIDENCY_DEVICE_IDENTITY", "NODE27_COLD_RESIDENCY_LAG_SECONDS"),
        require_device_identity_unassigned=False,
    )
    assert rewritten.count("NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=") == 1
    assert "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=3:4" in rewritten
    assert "NODE27_COLD_RESIDENCY_LAG_SECONDS=172800" in rewritten
    rewritten_url = next(line for line in rewritten.splitlines(keepends=True) if line.startswith("DATABASE_URL="))
    assert rewritten_url == original_url


def test_rewrite_file_publishes_privately(tmp_path: Path) -> None:
    path = tmp_path / "node27-cold-residency.env"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    rewrite_cold_env_file(path, updates=UPDATES)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text(encoding="utf-8")
    original_url = next(
        line
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.startswith("DATABASE_URL=")
    )
    assert next(line for line in text.splitlines() if line.startswith("DATABASE_URL=")) == original_url
    assert "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES=4096" in text


@pytest.mark.parametrize(
    ("flag", "value"),
    (
        ("--cold-reserve-bytes", "0"),
        ("--wal-reserve-bytes", "0"),
        ("--per-tick-bound", "0"),
        ("--container-exec-uid", "0"),
        ("--container-exec-gid", "0"),
        ("--container-exec-uid", "4294967295"),
        ("--container-exec-gid", "4294967295"),
    ),
)
def test_g4_owner_refuses_zero_or_out_of_range_values_before_rewrite(
    tmp_path: Path, flag: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "node27-cold-residency.env"
    original = EXAMPLE.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    argv = {
        "--cold-reserve-bytes": "4096",
        "--wal-reserve-bytes": "4096",
        "--per-tick-bound": "1",
        "--container-exec-uid": "1005",
        "--container-exec-gid": "1005",
    }
    argv[flag] = value
    rc = env_cli.main(["--path", str(path), *[item for pair in argv.items() for item in pair]])
    assert rc == 1
    assert "ENV_VALUE_INVALID" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("uid", "gid"),
    (("1", "1"), ("4294967294", "4294967294"), ("1005", "1005")),
)
def test_g4_owner_accepts_positive_and_bound_uid_gid(tmp_path: Path, uid: str, gid: str) -> None:
    path = tmp_path / "node27-cold-residency.env"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert env_cli.main(
        [
            "--path",
            str(path),
            "--cold-reserve-bytes",
            "4096",
            "--wal-reserve-bytes",
            "4096",
            "--per-tick-bound",
            "1",
            "--container-exec-uid",
            uid,
            "--container-exec-gid",
            gid,
        ]
    ) == 0
    text = path.read_text(encoding="utf-8")
    assert f"NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID={uid}" in text
    assert f"NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID={gid}" in text


def test_g4_owner_still_rejects_leading_zero_and_empty(tmp_path: Path) -> None:
    path = tmp_path / "node27-cold-residency.env"
    original = EXAMPLE.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    assert env_cli.main(
        [
            "--path",
            str(path),
            "--cold-reserve-bytes",
            "01",
            "--wal-reserve-bytes",
            "4096",
            "--per-tick-bound",
            "1",
            "--container-exec-uid",
            "1005",
            "--container-exec-gid",
            "1005",
        ]
    ) == 1
    assert path.read_text(encoding="utf-8") == original
    with pytest.raises(SystemExit):
        env_cli.main(
            [
                "--path",
                str(path),
                "--cold-reserve-bytes",
                "4096",
                "--wal-reserve-bytes",
                "4096",
                "--per-tick-bound",
                "1",
                "--container-exec-uid",
                "1005",
            ]
        )
    assert path.read_text(encoding="utf-8") == original


def test_g4_fence_invokes_the_checked_in_rewrite_owner() -> None:
    g4 = _gate("G4")
    assert "scripts/node27_issue1895_env_rewrite.py" in g4
    assert "rewrite_cold_env_text" in g4 or "node27_issue1895_env_rewrite.py" in g4
    assert "open(path, encoding=\"utf-8\")" not in g4 or "scripts/node27_issue1895_env_rewrite.py" in g4


def test_commented_optional_key_is_not_an_assignment() -> None:
    assert parse_assignment_line("#NODE27_COLD_RESIDENCY_LAG_SECONDS=604800\n") is None
    assert parse_assignment_line("NODE27_COLD_RESIDENCY_PER_TICK_BOUND=1\n") == (
        "NODE27_COLD_RESIDENCY_PER_TICK_BOUND",
        "1",
    )
    assert DATABASE_URL_KEY in G4_GOVERNED_KEYS or DATABASE_URL_KEY == "DATABASE_URL"
