"""Requirement-driven tests for the run-tree copyback backup lifecycle (#2237).

Contract, from the ADDED requirement "A run-tree replacement never destroys its
only backup on a failure path" in `harden-copyback-mutex-residuals`:

* the backup of a replaced target is deleted only once the target is known to be
  in place -- the promote succeeded, or the backup was restored;
* a failure to clean up the temporary copy never prevents the restore;
* a backup that was neither promoted over nor restored stays on disk and the
  raised `RunTreeCopybackError` names it (`OBJECT_STORE_COPYBACK_BACKUP_RETAINED`);
* a successful restore re-raises the original error and leaves no residue; a
  successful replacement leaves neither the backup nor the temporary copy.

Both helpers (`_replace_tree`, `_replace_file`) are exercised through the same
rows. Failures are injected on `os.replace` by the SOURCE name, which is what
tells the promote (`.tmp` -> target) from the restore (`.backup` -> target).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import acquire_copyback_batch_lock, release_copyback_batch_lock
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator import run_tree_copyback as run_tree_copyback_module
from services.orchestrator.run_tree_copyback import RunTreeCopybackError, copyback_run_trees

OLD = b"old-content\n"
NEW = b"new-content\n"


def _real_dir(tmp_path: Path, name: str) -> Path:
    """No symlinked ancestor: `safe_fs` walks with `O_NOFOLLOW` (macOS `/var`)."""

    path = tmp_path.resolve() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inject_replace_failures(
    monkeypatch: pytest.MonkeyPatch, *, promote: bool = False, restore: bool = False
) -> None:
    real_replace = os.replace

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        name = Path(src).name
        if promote and name.endswith(".tmp"):
            raise OSError(f"injected promote failure for {dst}")
        if restore and name.endswith(".backup"):
            raise OSError(f"injected restore failure for {dst}")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def _copyback_residue(parent: Path) -> dict[str, list[str]]:
    names = sorted(entry.name for entry in parent.iterdir() if ".copyback-" in entry.name)
    return {
        "backup": [name for name in names if name.endswith(".backup")],
        "tmp": [name for name in names if name.endswith(".tmp")],
    }


class _Shape:
    """One helper under test, with its own content reader and temp-cleanup seam."""

    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        self.root = _real_dir(tmp_path, "copyback")
        source_root = _real_dir(tmp_path, "staging")
        if kind == "tree":
            self.source = source_root / "run"
            (self.source / "output").mkdir(parents=True)
            (self.source / "output" / "q.csv").write_bytes(NEW)
            self.target = self.root / "runs" / "run"
            (self.target / "output").mkdir(parents=True)
            (self.target / "output" / "q.csv").write_bytes(OLD)
        else:
            self.source = source_root / "index.json"
            self.source.write_bytes(NEW)
            self.target = self.root / "scheduler" / "index.json"
            self.target.parent.mkdir(parents=True)
            self.target.write_bytes(OLD)

    def call(self) -> dict[str, Any]:
        helper = (
            run_tree_copyback_module._replace_tree if self.kind == "tree" else run_tree_copyback_module._replace_file
        )
        return helper(source=self.source, target=self.target, containment_root=self.root)

    @staticmethod
    def read(path: Path, kind: str) -> bytes:
        return (path / "output" / "q.csv").read_bytes() if kind == "tree" else path.read_bytes()

    def content(self) -> bytes:
        return self.read(self.target, self.kind)

    def fail_temp_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        attempts: list[str] = []
        if self.kind == "tree":
            real_rmtree = run_tree_copyback_module.rmtree_no_follow

            def rmtree(path: Path, **kwargs: Any) -> Any:
                if Path(path).name.endswith(".tmp"):
                    attempts.append(Path(path).name)
                    raise SafeFilesystemError(f"injected temp cleanup failure for {path}", kind="io")
                return real_rmtree(path, **kwargs)

            monkeypatch.setattr(run_tree_copyback_module, "rmtree_no_follow", rmtree)
        else:
            real_unlink = Path.unlink

            def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
                if path.name.endswith(".tmp"):
                    attempts.append(path.name)
                    raise OSError(f"injected temp cleanup failure for {path}")
                real_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", unlink)
        return attempts


SHAPES = ("tree", "file")


# --- EF-5 (a): promote fails and the restore fails -----------------------------


@pytest.mark.parametrize("kind", SHAPES)
def test_ef5_a_a_failed_restore_keeps_the_backup_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    shape = _Shape(kind, tmp_path)
    _inject_replace_failures(monkeypatch, promote=True, restore=True)

    with pytest.raises(RunTreeCopybackError) as error_info:
        shape.call()

    error = error_info.value
    assert error.code == "OBJECT_STORE_COPYBACK_BACKUP_RETAINED"
    backup = Path(error.details["backup_path"])
    assert backup.parent == shape.target.parent
    assert backup.name.endswith(".backup")
    # The only copy of the old content survives, where the error says it is.
    assert _Shape.read(backup, kind) == OLD
    assert error.details["target"] == str(shape.target)
    assert "injected promote failure" in error.details["error"]
    assert "injected restore failure" in error.details["restore_error"]
    assert isinstance(error.__cause__, OSError)
    assert "injected promote failure" in str(error.__cause__)
    assert not shape.target.exists()
    assert _copyback_residue(shape.target.parent)["backup"] == [backup.name]


# --- EF-5 (b): promote fails and the temp cleanup raises -----------------------


@pytest.mark.parametrize("kind", SHAPES)
def test_ef5_b_a_raising_temp_cleanup_does_not_pre_empt_the_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    shape = _Shape(kind, tmp_path)
    _inject_replace_failures(monkeypatch, promote=True)
    cleanup_attempts = shape.fail_temp_cleanup(monkeypatch)

    with pytest.raises(OSError) as error_info:
        shape.call()

    # The promote failure surfaces, not the cleanup failure it was followed by.
    assert "injected promote failure" in str(error_info.value)
    assert cleanup_attempts, "the temp cleanup must still have been attempted"
    # Never neither: the restore ran, so the old content is back at the target.
    assert shape.content() == OLD
    assert _copyback_residue(shape.target.parent)["backup"] == []


# --- EF-5 (c): promote fails and the restore succeeds --------------------------


@pytest.mark.parametrize("kind", SHAPES)
def test_ef5_c_a_successful_restore_re_raises_the_original_and_leaves_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    shape = _Shape(kind, tmp_path)
    _inject_replace_failures(monkeypatch, promote=True)

    with pytest.raises(OSError) as error_info:
        shape.call()

    assert not isinstance(error_info.value, RunTreeCopybackError)
    assert "injected promote failure" in str(error_info.value)
    assert shape.content() == OLD
    assert _copyback_residue(shape.target.parent) == {"backup": [], "tmp": []}


@pytest.mark.parametrize("kind", SHAPES)
def test_ef5_c_a_failed_first_promote_with_no_prior_target_retains_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    shape = _Shape(kind, tmp_path)
    if kind == "tree":
        run_tree_copyback_module.rmtree_no_follow(shape.target, containment_root=shape.root)
    else:
        shape.target.unlink()
    _inject_replace_failures(monkeypatch, promote=True)

    with pytest.raises(OSError) as error_info:
        shape.call()

    assert not isinstance(error_info.value, RunTreeCopybackError)
    assert not shape.target.exists()
    assert _copyback_residue(shape.target.parent) == {"backup": [], "tmp": []}


# --- EF-5 (d): success -----------------------------------------------------------


@pytest.mark.parametrize("kind", SHAPES)
def test_ef5_d_a_successful_replacement_leaves_new_content_and_no_residue(tmp_path: Path, kind: str) -> None:
    shape = _Shape(kind, tmp_path)

    summary = shape.call()

    assert summary["file_count"] == 1
    assert shape.content() == NEW
    assert _copyback_residue(shape.target.parent) == {"backup": [], "tmp": []}


# --- EF-5b: the public entry point on a re-copyback ------------------------------


def _write_run(object_store: Path, run_id: str, output: bytes) -> None:
    run = object_store / "runs" / run_id
    (run / "input").mkdir(parents=True, exist_ok=True)
    (run / "output").mkdir(parents=True, exist_ok=True)
    (run / "input" / "manifest.json").write_text('{"run_id":"' + run_id + '"}\n', encoding="utf-8")
    (run / "output" / "q.csv").write_bytes(output)


def test_ef5b_a_re_copyback_whose_promote_and_restore_fail_raises_backup_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    object_store = _real_dir(tmp_path, "object-store")
    copyback_root = _real_dir(tmp_path, "shared-object-store")
    run_id = "fcst_gfs_2026062700_model_a"
    _write_run(object_store, run_id, OLD)
    first = copyback_run_trees(object_store_root=object_store, copyback_root=copyback_root, run_ids=[run_id])
    assert first is not None and first["status"] == "copied"
    target = copyback_root / "runs" / run_id
    assert (target / "output" / "q.csv").read_bytes() == OLD

    _write_run(object_store, run_id, NEW)
    _inject_replace_failures(monkeypatch, promote=True, restore=True)
    with pytest.raises(RunTreeCopybackError) as error_info:
        copyback_run_trees(object_store_root=object_store, copyback_root=copyback_root, run_ids=[run_id])
    monkeypatch.undo()

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_BACKUP_RETAINED"
    backup = Path(error_info.value.details["backup_path"])
    assert backup.is_dir()
    assert (backup / "output" / "q.csv").read_bytes() == OLD
    assert (backup / "input" / "manifest.json").is_file()
    # The batch mutex was released on the way out of the failed batch.
    fd = acquire_copyback_batch_lock(copyback_root, timeout_seconds=0.5)
    release_copyback_batch_lock(fd)
