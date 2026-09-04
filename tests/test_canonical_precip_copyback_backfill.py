"""Requirement-driven tests for ``scripts/canonical_precip_copyback_backfill.py`` (#2008).

Contract (canonical-precip-copyback spec, Requirement 2):

* mirrors every ``canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/``
  plus each source's ``canonical/<storage_source>/grid/*/`` under ``--source-root``
  into ``--copyback-root`` on the identical keyspace, copying the on-disk
  directory names verbatim (no source-id normalization -- the script must not
  import the publisher's ``normalize_source_id``);
* prints a JSON summary to stdout listing every cycle with copied/skipped/failed;
* writes by default, ``--dry-run`` creates no file and no directory;
* exit 0 with no failure, 1 when something failed, 2 for unusable roots;
* imports the standard library only, so node-22's frozen checkout can run it as
  ``<pinned python> -m scripts.canonical_precip_copyback_backfill`` without
  triggering an environment build.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import canonical_precip_copyback_backfill as backfill

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "canonical_precip_copyback_backfill.py"
MODULE_NAME = "scripts.canonical_precip_copyback_backfill"


def _seed_cycle(
    source_root: Path,
    storage_source: str,
    cycle_token: str,
    *,
    leads: tuple[int, ...] = (3, 6, 9),
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    prcp_dir = source_root / "canonical" / storage_source / cycle_token / "prcp_rate_or_amount"
    prcp_dir.mkdir(parents=True, exist_ok=True)
    for lead in leads:
        name = f"{storage_source}_{cycle_token}_prcp_rate_or_amount_f{lead:03d}.nc"
        payload = f"prcp:{storage_source}:{cycle_token}:{lead:03d}".encode("utf-8")
        (prcp_dir / name).write_bytes(payload)
        payloads[f"canonical/{storage_source}/{cycle_token}/prcp_rate_or_amount/{name}"] = payload
    return payloads


def _seed_grid(source_root: Path, storage_source: str, grid_id: str) -> dict[str, bytes]:
    grid_dir = source_root / "canonical" / storage_source / "grid" / grid_id
    grid_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"grid_id": grid_id}).encode("utf-8")
    (grid_dir / "grid.json").write_bytes(payload)
    return {f"canonical/{storage_source}/grid/{grid_id}/grid.json": payload}


def _seed_two_source_store(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()
    payloads: dict[str, bytes] = {}
    for storage_source, grid_id in (("gfs", "gfs_0p25"), ("IFS", "ifs_0p25")):
        for cycle_token in ("2026090200", "2026090212"):
            payloads.update(_seed_cycle(source_root, storage_source, cycle_token))
        payloads.update(_seed_grid(source_root, storage_source, grid_id))
    return source_root, copyback_root, payloads


def test_backfill_mirrors_every_cycle_and_grid_and_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    for key, payload in payloads.items():
        assert (copyback_root / key).read_bytes() == payload
    assert [(cycle["source"], cycle["cycle_token"], cycle["copied"], cycle["skipped"], cycle["failed"])
            for cycle in summary["cycles"]] == [
        ("IFS", "2026090200", 3, 0, 0),
        ("IFS", "2026090212", 3, 0, 0),
        ("gfs", "2026090200", 3, 0, 0),
        ("gfs", "2026090212", 3, 0, 0),
    ]
    assert [(grid["source"], grid["grid_id"], grid["copied"], grid["skipped"], grid["failed"])
            for grid in summary["grids"]] == [
        ("IFS", "ifs_0p25", 1, 0, 0),
        ("gfs", "gfs_0p25", 1, 0, 0),
    ]
    assert summary["totals"] == {"copied": 14, "skipped": 0, "failed": 0}
    # The script copies directory names verbatim; it never normalizes a source id.
    assert sorted(entry.name for entry in (copyback_root / "canonical").iterdir()) == ["IFS", "gfs"]


def test_backfill_reruns_skip_identically_sized_destinations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)]) == 0
    capsys.readouterr()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["totals"] == {"copied": 0, "skipped": 14, "failed": 0}


def test_backfill_replaces_a_destination_of_different_size(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)
    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)]) == 0
    capsys.readouterr()
    stale_key = "canonical/gfs/2026090212/prcp_rate_or_amount/gfs_2026090212_prcp_rate_or_amount_f003.nc"
    (copyback_root / stale_key).write_bytes(b"short")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["totals"] == {"copied": 1, "skipped": 13, "failed": 0}
    assert (copyback_root / stale_key).read_bytes() == payloads[stale_key]


def test_backfill_dry_run_writes_nothing_and_reports_planned_copies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)

    exit_code = backfill.main(
        ["--source-root", str(source_root), "--copyback-root", str(copyback_root), "--dry-run"]
    )
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["dry_run"] is True
    assert summary["totals"] == {"copied": 14, "skipped": 0, "failed": 0}
    # No file AND no directory was created under the copyback root.
    assert list(copyback_root.rglob("*")) == []


def test_backfill_unreadable_cycle_is_reported_and_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    broken = source_root / "canonical" / "gfs" / "2026090300"
    broken.mkdir()
    # A regular file where the product directory belongs: os.scandir raises
    # NotADirectoryError deterministically, unlike a chmod that a root-owned CI
    # runner would sail straight through.
    (broken / "prcp_rate_or_amount").write_bytes(b"not a directory")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    failed = [cycle for cycle in summary["cycles"] if cycle["failed"]]
    assert len(failed) == 1
    assert failed[0]["source"] == "gfs"
    assert failed[0]["cycle_token"] == "2026090300"
    assert failed[0]["status"] == "failed"
    assert failed[0]["errors"]
    # The healthy cycles were still mirrored and still reported.
    assert summary["totals"]["copied"] == 14
    assert len(summary["cycles"]) == 5


def test_backfill_cycle_without_precipitation_products_is_not_a_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    (source_root / "canonical" / "gfs" / "2026090300" / "t2m").mkdir(parents=True)

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    entry = next(cycle for cycle in summary["cycles"] if cycle["cycle_token"] == "2026090300")
    assert entry["status"] == "no_precip_products"
    assert (entry["copied"], entry["skipped"], entry["failed"]) == (0, 0, 0)


def test_backfill_refuses_to_mirror_a_symlinked_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    prcp_dir = source_root / "canonical" / "gfs" / "2026090212" / "prcp_rate_or_amount"
    (prcp_dir / "linked.nc").symlink_to(prcp_dir / "gfs_2026090212_prcp_rate_or_amount_f003.nc")

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    entry = next(
        cycle for cycle in summary["cycles"] if cycle["source"] == "gfs" and cycle["cycle_token"] == "2026090212"
    )
    assert entry["failed"] == 1
    assert any("symlink" in message for message in entry["errors"])
    assert not (copyback_root / "canonical/gfs/2026090212/prcp_rate_or_amount/linked.nc").exists()


@pytest.mark.parametrize("missing", ["source", "copyback"])
def test_backfill_missing_root_exits_two(tmp_path: Path, missing: str) -> None:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    (copyback_root if missing == "source" else source_root).mkdir()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])

    assert exit_code == 2


def test_backfill_overlapping_roots_exit_two(tmp_path: Path) -> None:
    source_root = tmp_path / "object-store"
    (source_root / "nested").mkdir(parents=True)

    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(source_root)]) == 2
    assert backfill.main(["--source-root", str(source_root), "--copyback-root", str(source_root / "nested")]) == 2


def test_backfill_source_root_without_canonical_tree_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    source_root.mkdir()
    copyback_root.mkdir()

    exit_code = backfill.main(["--source-root", str(source_root), "--copyback-root", str(copyback_root)])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert summary["cycles"] == []
    assert summary["grids"] == []
    assert summary["totals"] == {"copied": 0, "skipped": 0, "failed": 0}


def test_backfill_module_imports_only_the_standard_library() -> None:
    """node-22 runs this with a pinned interpreter and a frozen environment."""

    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(SCRIPT_PATH))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would tie the script to a package"
            assert node.module is not None
            imported.add(node.module.split(".")[0])

    assert imported
    assert imported.isdisjoint({"services", "packages", "workers", "apps", "tests"})
    non_stdlib = sorted(name for name in imported if name not in sys.stdlib_module_names)
    assert non_stdlib == []


def test_backfill_runs_as_a_module_in_a_subprocess(tmp_path: Path) -> None:
    source_root, copyback_root, payloads = _seed_two_source_store(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE_NAME,
            "--source-root",
            str(source_root),
            "--copyback-root",
            str(copyback_root),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["totals"] == {"copied": 14, "skipped": 0, "failed": 0}
    for key, payload in payloads.items():
        assert (copyback_root / key).read_bytes() == payload


@pytest.mark.parametrize("empty", ["source", "copyback"])
def test_backfill_empty_root_argument_exits_two(tmp_path: Path, empty: str) -> None:
    """An unset env var expanded to "" must not silently mirror into the cwd."""

    source_root, copyback_root, _payloads = _seed_two_source_store(tmp_path)
    argv = [
        "--source-root",
        "" if empty == "source" else str(source_root),
        "--copyback-root",
        "" if empty == "copyback" else str(copyback_root),
    ]

    assert backfill.main(argv) == 2
