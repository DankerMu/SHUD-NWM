"""#2918: the node-22 env backup sweep (`scripts/ops/node22_env_backup_sweep.sh`).

Every case builds a fake home under ``tmp_path`` (``NWM/infra/env`` as the env
dir, ``.config/systemd/user`` as the unit dir, a separate backup root), runs
the script as ``["bash", script, ...]`` with a minimal environment whose
``PATH`` holds only the system directories (so on macOS the oracle is
``/bin/bash`` 3.2 with the BSD tools) and passes every path flag explicitly,
except where a case exists to prove a default. Expected values come from the
files the test writes, never from the script's own output.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/ops/node22_env_backup_sweep.sh"
README = REPO_ROOT / "infra/env/README.md"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

RECEIPT_KEYS = {
    "schema_version",
    "generated_at",
    "mode",
    "status",
    "exit_code",
    "env_dir",
    "systemd_user_dir",
    "home",
    "backup_dir",
    "candidates",
    "moved",
    "refusals",
    "conflicts",
    "errors",
}
SCHEMA_VERSION = "nhms.node22.env_backup_sweep.v1"

LIVE_ENV = "compute.scheduler-dbfree.env"
BACKUPS = (
    "compute.env.bak-20261001",
    "compute.scheduler-dbfree.env.backup-2911",
    "slurm-gateway.env.pre-2913",
)
SCHEDULER_UNIT = "nhms-compute-scheduler.service"
REFERENCING_DROPIN = "nhms-compute-scheduler.service.d/20-old-env.conf"


class Tree:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.env_dir = self.home / "NWM/infra/env"
        self.systemd = self.home / ".config/systemd/user"
        self.backup_root = root / "backups/NWM-presync-backup-2026-10-17"
        self.env_dir.mkdir(parents=True)
        self.systemd.mkdir(parents=True)
        (self.env_dir / LIVE_ENV).write_text("NHMS_SCHEDULER_TIMING_LEVEL=stage\n", encoding="utf-8")
        (self.env_dir / "compute.scheduler-dbfree.env.example").write_text("# template\n", encoding="utf-8")
        (self.systemd / SCHEDULER_UNIT).write_text(
            "[Service]\nEnvironmentFile=%h/NWM/infra/env/compute.scheduler-dbfree.env\n", encoding="utf-8"
        )

    @property
    def backup_dir(self) -> Path:
        return self.backup_root / "infra-env"

    def add_backups(self, names: tuple[str, ...] = BACKUPS) -> None:
        for name in names:
            (self.env_dir / name).write_text(f"BACKUP_OF={name}\n", encoding="utf-8")

    def write_unit(self, relative: str, text: str) -> None:
        path = self.systemd / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def argv(
        self,
        *extra: str,
        env_dir: Path | None = None,
        systemd: Path | None = None,
        backup_root: Path | None = None,
        home: Path | None = None,
    ) -> list[str]:
        return [
            "--env-dir",
            str(env_dir or self.env_dir),
            "--systemd-user-dir",
            str(systemd or self.systemd),
            "--backup-root",
            str(backup_root or self.backup_root),
            "--home",
            str(home or self.home),
            *extra,
        ]

    def env(self, **overrides: str) -> dict[str, str]:
        return {"PATH": SYSTEM_PATH, "HOME": str(self.home), "LC_ALL": "C", **overrides}

    def receipts(self, backup_dir: Path | None = None) -> list[Path]:
        return sorted((backup_dir or self.backup_dir).glob("sweep-receipt-*.json"))

    def receipt(self, backup_dir: Path | None = None) -> dict:
        found = self.receipts(backup_dir)
        assert len(found) == 1, found
        return json.loads(found[0].read_text(encoding="utf-8"))


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def snapshot(root: Path) -> dict[str, tuple]:
    """(relative path -> mode, mtime_ns, bytes-or-link-target) for every node under ``root``."""
    state: dict[str, tuple] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                payload: object = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                payload = path.read_bytes()
            else:
                payload = None
            state[path.relative_to(root).as_posix()] = (info.st_mode, info.st_mtime_ns, payload)
    return state


def plan_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("plan: ")]


def assert_receipt_shape(receipt: dict, *, mode: str, status: str, exit_code: int) -> None:
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["schema_version"] == SCHEMA_VERSION
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", receipt["generated_at"])
    assert receipt["mode"] == mode
    assert receipt["status"] == status
    assert receipt["exit_code"] == exit_code


def mode_bits(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --- spec scenarios ----------------------------------------------------------


def test_dry_run_moves_nothing(tree: Tree) -> None:
    tree.add_backups()
    # Not candidates: a dot file, a directory and a symlink matching the
    # patterns, plus the live file and the template.
    (tree.env_dir / ".compute.env.bak-hidden").write_text("x\n", encoding="utf-8")
    (tree.env_dir / "old.bak.d").mkdir()
    (tree.env_dir / "old.bak.d/inner.env").write_text("x\n", encoding="utf-8")
    (tree.env_dir / "link.env.bak-1").symlink_to(tree.env_dir / LIVE_ENV)
    before_env = snapshot(tree.env_dir)
    before_units = snapshot(tree.systemd)

    result = run(tree.argv(), tree.env())

    assert result.returncode == 0, result.stderr
    expected = sorted(BACKUPS)
    assert plan_lines(result.stdout) == [
        f"plan: {tree.env_dir}/{name} -> {tree.backup_dir}/{name}" for name in expected
    ]
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="dry_run", status="ok", exit_code=0)
    assert receipt["candidates"] == expected
    assert receipt["moved"] == []
    assert receipt["refusals"] == []
    assert receipt["conflicts"] == []
    assert receipt["errors"] == []
    assert receipt["env_dir"] == str(tree.env_dir)
    assert receipt["systemd_user_dir"] == str(tree.systemd)
    assert receipt["home"] == str(tree.home)
    assert receipt["backup_dir"] == str(tree.backup_dir)
    assert snapshot(tree.env_dir) == before_env
    assert snapshot(tree.systemd) == before_units
    assert [path.name for path in tree.backup_dir.iterdir()] == [tree.receipts()[0].name]
    assert mode_bits(tree.backup_dir) == 0o700
    assert mode_bits(tree.receipts()[0]) == 0o600
    # The receipt names files, never their content.
    assert "BACKUP_OF" not in tree.receipts()[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("apply", (False, True), ids=("dry-run", "apply"))
def test_referenced_backup_blocks_the_sweep(tree: Tree, apply: bool) -> None:
    tree.add_backups(("compute.env.bak-x", "compute.env.pre-2913"))
    tree.write_unit(REFERENCING_DROPIN, "[Service]\nEnvironmentFile=-%h/NWM/infra/env/compute.env.bak-x\n")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv(*(["--apply"] if apply else [])), tree.env())

    assert result.returncode == 2
    assert REFERENCING_DROPIN in result.stderr
    assert plan_lines(result.stdout) == []
    assert snapshot(tree.env_dir) == before_env
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply" if apply else "dry_run", status="refused", exit_code=2)
    assert receipt["refusals"] == [{"candidate": "compute.env.bak-x", "unit": REFERENCING_DROPIN}]
    assert receipt["moved"] == []
    assert receipt["candidates"] == ["compute.env.bak-x", "compute.env.pre-2913"]


@pytest.mark.parametrize(
    ("relative", "line"),
    (
        # A top-level leftover with any suffix still counts (rather refuse too much).
        ("nhms-slurm-gateway.service.pre-2913", "EnvironmentFile=%h/NWM/infra/env/compute.env.bak-x"),
        # Whitespace around `=` and a trailing blank are trimmed.
        (REFERENCING_DROPIN, "EnvironmentFile = -%h/NWM/infra/env/compute.env.bak-x  "),
        # An absolute value without `%h`.
        (REFERENCING_DROPIN, "EnvironmentFile=-{env_dir}/compute.env.bak-x"),
        # A non-canonical spelling of the same file.
        (REFERENCING_DROPIN, "EnvironmentFile=-%h/NWM/infra/../infra/env/./compute.env.bak-x"),
    ),
    ids=("top-level-pre-leftover", "spaces-around-equals", "absolute", "dot-segments"),
)
def test_refusal_set_normalization(tree: Tree, relative: str, line: str) -> None:
    tree.add_backups(("compute.env.bak-x",))
    tree.write_unit(relative, "[Service]\n" + line.replace("{env_dir}", str(tree.env_dir)) + "\n")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 2
    assert relative in result.stderr
    assert snapshot(tree.env_dir) == before_env
    assert tree.receipt()["refusals"] == [{"candidate": "compute.env.bak-x", "unit": relative}]


def test_refusal_resolves_symlinked_home(tree: Tree, tmp_path: Path) -> None:
    """`%h` expands to a symlink of the real home; `readlink -f` makes both sides meet."""
    tree.add_backups(("compute.env.bak-x",))
    tree.write_unit(REFERENCING_DROPIN, "[Service]\nEnvironmentFile=-%h/NWM/infra/env/compute.env.bak-x\n")
    linked_home = tmp_path / "home-link"
    linked_home.symlink_to(tree.home)

    result = run(tree.argv("--apply", home=linked_home), tree.env())

    assert result.returncode == 2
    assert REFERENCING_DROPIN in result.stderr
    assert (tree.env_dir / "compute.env.bak-x").is_file()


def test_non_matching_references_do_not_block(tree: Tree) -> None:
    """A reset line, a comment and a reference to the live file leave the backups movable."""
    tree.add_backups(("compute.env.bak-x",))
    tree.write_unit(
        REFERENCING_DROPIN,
        "[Service]\nEnvironmentFile=\n# EnvironmentFile=%h/NWM/infra/env/compute.env.bak-x\n"
        "EnvironmentFile=-%h/NWM/infra/env/compute.scheduler-dbfree.env\n",
    )
    # Outside the scanned set: deeper than one drop-in level and a non-nhms unit.
    tree.write_unit("other.service", "EnvironmentFile=%h/NWM/infra/env/compute.env.bak-x\n")
    tree.write_unit(
        "nhms-compute-scheduler.service.d/nested/10.conf", "EnvironmentFile=%h/NWM/infra/env/compute.env.bak-x\n"
    )
    tree.write_unit(
        "nhms-compute-scheduler.service.d/10.conf.disabled", "EnvironmentFile=%h/NWM/infra/env/compute.env.bak-x\n"
    )

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 0, result.stderr
    assert (tree.backup_dir / "compute.env.bak-x").is_file()


def test_apply_moves_into_the_dated_directory(tree: Tree) -> None:
    """The default backup root: `$HOME/NWM-presync-backup-<UTC date>`."""
    tree.add_backups()
    live_before = {name: snapshot(tree.env_dir)[name] for name in (LIVE_ENV, "compute.scheduler-dbfree.env.example")}
    before_units = snapshot(tree.systemd)
    argv = [
        "--env-dir",
        str(tree.env_dir),
        "--systemd-user-dir",
        str(tree.systemd),
        "--home",
        str(tree.home),
        "--apply",
    ]
    day_before = datetime.now(UTC).strftime("%Y-%m-%d")

    result = run(argv, tree.env())

    day_after = datetime.now(UTC).strftime("%Y-%m-%d")
    assert result.returncode == 0, result.stderr
    roots = sorted(path for path in tree.home.iterdir() if path.name.startswith("NWM-presync-backup-"))
    assert len(roots) == 1
    assert re.fullmatch(r"NWM-presync-backup-\d{4}-\d{2}-\d{2}", roots[0].name)
    assert roots[0].name.removeprefix("NWM-presync-backup-") in {day_before, day_after}
    backup_dir = roots[0] / "infra-env"
    assert mode_bits(backup_dir) == 0o700
    for name in BACKUPS:
        assert not (tree.env_dir / name).exists()
        assert (backup_dir / name).read_text(encoding="utf-8") == f"BACKUP_OF={name}\n"
    for name, state in live_before.items():
        assert snapshot(tree.env_dir)[name] == state
    assert snapshot(tree.systemd) == before_units
    assert len(plan_lines(result.stdout)) == len(BACKUPS)
    receipt = tree.receipt(backup_dir)
    assert_receipt_shape(receipt, mode="apply", status="ok", exit_code=0)
    assert receipt["candidates"] == sorted(BACKUPS)
    assert receipt["moved"] == sorted(BACKUPS)
    assert receipt["backup_dir"] == str(backup_dir)
    # One stderr line per move: the record when the receipt cannot be written.
    moved_lines = [line for line in result.stderr.splitlines() if ": moved: " in line]
    assert moved_lines == [f"node22-env-backup-sweep: moved: {name}" for name in sorted(BACKUPS)]


@pytest.mark.parametrize("apply", (True, False), ids=("apply", "dry-run"))
def test_existing_target_refuses(tree: Tree, apply: bool) -> None:
    tree.add_backups()
    tree.backup_dir.mkdir(parents=True)
    taken = BACKUPS[1]
    (tree.backup_dir / taken).write_text("EARLIER_SWEEP=1\n", encoding="utf-8")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv(*(["--apply"] if apply else [])), tree.env())

    assert result.returncode == 2
    assert taken in result.stderr
    # Checked before the first move: the other two candidates stay too.
    assert snapshot(tree.env_dir) == before_env
    assert (tree.backup_dir / taken).read_text(encoding="utf-8") == "EARLIER_SWEEP=1\n"
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply" if apply else "dry_run", status="conflict", exit_code=2)
    assert receipt["conflicts"] == [taken]
    assert receipt["moved"] == []


def test_existing_dangling_symlink_target_refuses(tree: Tree) -> None:
    tree.add_backups()
    tree.backup_dir.mkdir(parents=True)
    (tree.backup_dir / BACKUPS[0]).symlink_to(tree.backup_dir / "nowhere")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 2
    assert snapshot(tree.env_dir) == before_env
    assert tree.receipt()["conflicts"] == [BACKUPS[0]]


def test_zero_candidates_is_ok(tree: Tree) -> None:
    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 0, result.stderr
    assert plan_lines(result.stdout) == []
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply", status="ok", exit_code=0)
    assert receipt["candidates"] == []


# --- fail-closed cases -------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    (
        # A glob would load `compute.env.bak-x`; a literal compare would miss it.
        "EnvironmentFile={env_dir}/compute.env*",
        "EnvironmentFile=-%h/NWM/infra/env/compute.env.bak-?",
        "EnvironmentFile=-%h/NWM/infra/env/compute.env.bak-[x]",
        # Specifiers other than `%h`, `%%` and a trailing `%`.
        "EnvironmentFile=-%h/NWM/infra/env/%i.env",
        "EnvironmentFile=%E/nhms/compute.env.bak-x",
        "EnvironmentFile=%h/NWM/infra/env/100%%.env",
        "EnvironmentFile=%h/NWM/infra/env/compute.env.bak-x%",
        # A continuation: the next line is part of the value.
        "EnvironmentFile=-%h/NWM/infra/env/\\\ncompute.env.bak-x",
        "EnvironmentFile=-%h/NWM/infra/env/compute.env.bak-x \\  \n",
    ),
    ids=(
        "glob-star",
        "glob-question",
        "glob-bracket",
        "specifier-i",
        "specifier-E",
        "escaped-percent",
        "trailing-percent",
        "continuation",
        "continuation-trailing-blank",
    ),
)
def test_unparseable_environment_file_value_fails_closed(tree: Tree, line: str) -> None:
    tree.add_backups(("compute.env.bak-x",))
    text = line.replace("{env_dir}", str(tree.env_dir))
    tree.write_unit(REFERENCING_DROPIN, "[Service]\n" + text + "\n")
    value = text.split("\n")[0].split("=", 1)[1].strip().removeprefix("-")
    before_env = snapshot(tree.env_dir)
    before_units = snapshot(tree.systemd)

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 2
    assert REFERENCING_DROPIN in result.stderr
    assert value in result.stderr
    assert plan_lines(result.stdout) == []
    assert snapshot(tree.env_dir) == before_env
    assert snapshot(tree.systemd) == before_units
    assert [path.name for path in tree.backup_dir.iterdir()] == [tree.receipts()[0].name]
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply", status="error", exit_code=2)
    assert len(receipt["errors"]) == 1
    assert REFERENCING_DROPIN in receipt["errors"][0]
    assert value in receipt["errors"][0]
    # Refused at scan time, before candidates are listed.
    assert receipt["candidates"] == []
    assert receipt["moved"] == []
    assert receipt["refusals"] == []


def test_unparseable_value_in_a_top_level_unit_fails_closed_in_dry_run(tree: Tree) -> None:
    tree.add_backups(("compute.env.bak-x",))
    tree.write_unit(SCHEDULER_UNIT, "[Service]\nEnvironmentFile=%h/NWM/infra/env/compute.env*\n")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv(), tree.env())

    assert result.returncode == 2
    assert SCHEDULER_UNIT in result.stderr
    assert snapshot(tree.env_dir) == before_env
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="dry_run", status="error", exit_code=2)
    assert SCHEDULER_UNIT in receipt["errors"][0]


def test_symlinked_backup_dir_refuses(tree: Tree, tmp_path: Path) -> None:
    """`chmod 700` would follow a pre-existing `infra-env` link into a directory the script did not create."""
    tree.add_backups()
    other = tmp_path / "other"
    other.mkdir()
    other.chmod(0o755)
    tree.backup_root.mkdir(parents=True)
    tree.backup_dir.symlink_to(other)
    before_env = snapshot(tree.env_dir)
    before_root = snapshot(tree.backup_root)

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 2
    assert "symlink" in result.stderr
    assert mode_bits(other) == 0o755
    assert list(other.iterdir()) == []
    assert snapshot(tree.backup_root) == before_root
    assert snapshot(tree.env_dir) == before_env



def test_missing_env_dir_fails_closed(tree: Tree, tmp_path: Path) -> None:
    missing = tmp_path / "no-such/infra/env"

    result = run(tree.argv("--apply", env_dir=missing), tree.env())

    assert result.returncode == 2
    assert not missing.exists()
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply", status="error", exit_code=2)
    assert receipt["errors"] and str(missing) in receipt["errors"][0]


def test_missing_systemd_dir_fails_closed(tree: Tree, tmp_path: Path) -> None:
    tree.add_backups()
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv("--apply", systemd=tmp_path / "no-such/systemd/user"), tree.env())

    assert result.returncode == 2
    assert snapshot(tree.env_dir) == before_env
    assert tree.receipt()["status"] == "error"


def test_zero_nhms_units_fails_closed(tree: Tree) -> None:
    tree.add_backups()
    (tree.systemd / SCHEDULER_UNIT).rename(tree.systemd / "other.service")
    (tree.systemd / "nhms-compute-scheduler.service.d").mkdir()  # a directory is not a unit file
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 2
    assert "no nhms-* unit files" in result.stderr
    assert snapshot(tree.env_dir) == before_env
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply", status="error", exit_code=2)
    assert receipt["moved"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_dropin_fails_closed(tree: Tree) -> None:
    tree.add_backups()
    tree.write_unit(REFERENCING_DROPIN, "EnvironmentFile=%h/NWM/infra/env/compute.env.bak-20261001\n")
    dropin = tree.systemd / REFERENCING_DROPIN
    dropin.chmod(0)
    before_env = snapshot(tree.env_dir)
    try:
        result = run(tree.argv("--apply"), tree.env())
    finally:
        dropin.chmod(0o600)

    assert result.returncode == 2
    assert REFERENCING_DROPIN in result.stderr
    assert snapshot(tree.env_dir) == before_env


@pytest.mark.parametrize("target", ("env", "systemd", "env-via-symlink"))
def test_backup_root_under_guarded_dir_refuses(tree: Tree, tmp_path: Path, target: str) -> None:
    tree.add_backups()
    if target == "env":
        backup_root = tree.env_dir / "backups"
    elif target == "systemd":
        backup_root = tree.systemd / "backups"
    else:
        link = tmp_path / "env-link"
        link.symlink_to(tree.env_dir)
        backup_root = link / "not-yet/created"
    before_env = snapshot(tree.env_dir)
    before_units = snapshot(tree.systemd)

    result = run(tree.argv("--apply", backup_root=backup_root), tree.env())

    assert result.returncode == 2
    assert "resolves under" in result.stderr
    assert snapshot(tree.env_dir) == before_env
    assert snapshot(tree.systemd) == before_units


def test_backup_dir_equal_to_env_dir_refuses(tree: Tree, tmp_path: Path) -> None:
    # An env dir that happens to be named `infra-env`: <backup-root>/infra-env is the env dir itself.
    env_dir = tmp_path / "elsewhere/infra-env"
    env_dir.mkdir(parents=True)
    (env_dir / BACKUPS[0]).write_text("x\n", encoding="utf-8")
    before_env = snapshot(env_dir)

    result = run(tree.argv("--apply", env_dir=env_dir, backup_root=env_dir.parent), tree.env())

    assert result.returncode == 2
    assert "resolves under" in result.stderr
    assert snapshot(env_dir) == before_env


def test_receipt_collision_refuses(tree: Tree, tmp_path: Path) -> None:
    """Two runs in the same second: a `date` shim pins the stamp (system-boundary mock)."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "date").write_text("#!/bin/sh\necho 20261017T120000Z\n", encoding="utf-8")
    (shim / "date").chmod(0o755)
    tree.add_backups()
    tree.backup_dir.mkdir(parents=True)
    existing = tree.backup_dir / "sweep-receipt-20261017T120000Z.json"
    existing.write_text('{"earlier": true}\n', encoding="utf-8")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv("--apply"), tree.env(PATH=f"{shim}:{SYSTEM_PATH}"))

    assert result.returncode == 2
    assert "receipt already exists" in result.stderr
    assert existing.read_text(encoding="utf-8") == '{"earlier": true}\n'
    assert snapshot(tree.env_dir) == before_env
    assert tree.receipts() == [existing]


def test_control_character_in_candidate_basename_fails_closed(tree: Tree) -> None:
    tree.add_backups()
    (tree.env_dir / "compute.env.bak-\x01x").write_text("x\n", encoding="utf-8")
    before_env = snapshot(tree.env_dir)

    result = run(tree.argv("--apply"), tree.env())

    assert result.returncode == 2
    assert "control character" in result.stderr
    assert snapshot(tree.env_dir) == before_env
    receipt = tree.receipt()
    assert_receipt_shape(receipt, mode="apply", status="error", exit_code=2)
    assert receipt["moved"] == []


def test_json_escapes_quote_and_backslash(tree: Tree) -> None:
    names = ('compute.env.bak-"q"', "compute.env.bak-\\b")
    tree.add_backups(names)

    result = run(tree.argv(), tree.env())

    assert result.returncode == 0, result.stderr
    assert tree.receipt()["candidates"] == sorted(names)


@pytest.mark.parametrize(
    "argv",
    (
        [],
        ["--env-dir"],
        ["--env-dir", ""],
        ["--bogus"],
    ),
    ids=("no-env-dir", "missing-value", "empty-value", "unknown-flag"),
)
def test_argument_errors_exit_two_and_create_nothing(tree: Tree, argv: list[str]) -> None:
    before_home = snapshot(tree.home)

    result = run(argv, tree.env())

    assert result.returncode == 2
    assert snapshot(tree.home) == before_home


def test_systemd_user_dir_default_follows_xdg_config_home(tree: Tree, tmp_path: Path) -> None:
    """Default unit dir is `$XDG_CONFIG_HOME/systemd/user` when absolute (same as the #2911 probe)."""
    xdg = tmp_path / "xdg"
    dropin = xdg / "systemd/user" / REFERENCING_DROPIN
    dropin.parent.mkdir(parents=True)
    dropin.write_text("EnvironmentFile=%h/NWM/infra/env/x.bak\n", encoding="utf-8")
    tree.add_backups(("x.bak",))
    argv = ["--env-dir", str(tree.env_dir), "--backup-root", str(tree.backup_root), "--home", str(tree.home)]

    result = run(argv, tree.env(XDG_CONFIG_HOME=str(xdg)))

    assert result.returncode == 2
    assert REFERENCING_DROPIN in result.stderr
    assert tree.receipt()["systemd_user_dir"] == str(xdg / "systemd/user")


# --- static assertions -------------------------------------------------------


def test_script_static_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert "umask 077" in text
    for forbidden in ("rm ", "systemctl", "jq", "python", "realpath -m", "declare -A", "mapfile"):
        assert forbidden not in text, forbidden
    assert stat.S_IMODE(SCRIPT.stat().st_mode) == 0o755
    syntax = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr


def backup_copies_section() -> str:
    text = README.read_text(encoding="utf-8")
    match = re.search(r"^## Backup copies\n(.*?)(?=^#{1,6} |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "infra/env/README.md has no '## Backup copies' section"
    return match.group(1)


def test_readme_backup_copies_section_matches_the_script_defaults(tree: Tree) -> None:
    section = backup_copies_section()

    assert "scripts/ops/node22_env_backup_sweep.sh" in section
    assert "sweep-receipt-" in section
    assert "--apply" in section
    assert "`infra/env/`" in section
    templates = re.findall(r"~/NWM-presync-backup-<([^>]+)>/infra-env/", section)
    assert templates == ["UTC date YYYY-MM-DD"], templates

    # Run the script on its defaults and match the documented template against
    # the directory it actually creates.
    tree.add_backups(("compute.env.bak-1",))
    argv = ["--env-dir", str(tree.env_dir), "--systemd-user-dir", str(tree.systemd), "--home", str(tree.home)]
    result = run(argv, tree.env())
    assert result.returncode == 0, result.stderr
    created = [path for path in tree.home.iterdir() if path.name.startswith("NWM-presync-backup-")]
    assert len(created) == 1
    documented = re.compile(r"NWM-presync-backup-\d{4}-\d{2}-\d{2}")
    assert documented.fullmatch(created[0].name)
    assert (created[0] / "infra-env").is_dir()
