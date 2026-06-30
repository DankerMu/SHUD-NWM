from __future__ import annotations

from pathlib import Path

import pytest

from services.orchestrator.run_tree_copyback import RunTreeCopybackError, copyback_run_trees


def _write_run(root: Path, run_id: str, *, output_text: str = "q\n") -> None:
    run = root / "runs" / run_id
    (run / "input").mkdir(parents=True)
    (run / "output").mkdir()
    (run / "logs").mkdir()
    (run / "input" / "manifest.json").write_text('{"run_id":"' + run_id + '"}\n', encoding="utf-8")
    (run / "output" / "q.rivqdown.csv").write_text(output_text, encoding="utf-8")
    (run / "logs" / "shud_stdout.log").write_text("ok\n", encoding="utf-8")


def test_copyback_run_trees_replaces_stale_target_tree(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud", output_text="new\n")
    stale = copyback_root / "runs" / "fcst_gfs_2026062700_basins_heihe_shud" / "output"
    stale.mkdir(parents=True)
    (stale / "old.csv").write_text("old\n", encoding="utf-8")

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
    )

    assert summary is not None
    assert summary["status"] == "copied"
    assert summary["run_ids"] == ["fcst_gfs_2026062700_basins_heihe_shud"]
    target = copyback_root / "runs" / "fcst_gfs_2026062700_basins_heihe_shud"
    assert (target / "input" / "manifest.json").is_file()
    assert (target / "output" / "q.rivqdown.csv").read_text(encoding="utf-8") == "new\n"
    assert not (target / "output" / "old.csv").exists()


def test_copyback_run_trees_rejects_unsafe_run_id(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()

    with pytest.raises(RunTreeCopybackError) as exc_info:
        copyback_run_trees(
            object_store_root=object_root,
            copyback_root=tmp_path / "shared-object-store",
            run_ids=["../escape"],
        )

    assert exc_info.value.code == "OBJECT_STORE_COPYBACK_UNSAFE_RUN_ID"
