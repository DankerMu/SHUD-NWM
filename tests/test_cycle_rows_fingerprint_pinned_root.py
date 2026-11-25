"""The cycle-rows fingerprint under one pinned containment root (#3236 B1).

`_cycle_rows_source_fingerprint` runs its body inside
`pinned_containment_root(self.root)`, so its 21 stats open the journal root's
no-follow chain once instead of 21 times.  These tests pin that this changes
neither the fingerprint's value nor its fault marker on any tree (compared
with a stand-in block that pins nothing), nor the per-path seams other tests
replace, and that a symlink swap after a stored read still fails loud as on a
cold read.

The stand-in replaces `pinned_containment_root` in the journal module, where
the fingerprint looks it up at call time.  The fingerprint is called with
`source_segments` passed explicitly: `_cycle_read_source_segments` folds the
two spellings into one on a case-insensitive filesystem, and the tree that
needs two distinct spelling directories is skipped there.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from packages.common import safe_fs
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.file_journal_paths import _journal_segment_names
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.test_file_orchestration_journal_read_cache import (
    CYCLE_SEGMENT,
    CYCLE_TIME,
    _basin_manifest,
    _job_record,
)

_FAULT = journal_module._FINGERPRINT_CONTAINMENT_FAULT


@contextlib.contextmanager
def _pins_nothing(_root: Path) -> Iterator[None]:
    yield


def _populate_spelling(root: Path, spelling: str) -> None:
    """Every surface the fingerprint stats, for one source spelling."""

    latest = root / "latest" / spelling / CYCLE_SEGMENT
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "model_a.json").write_text("{}", encoding="utf-8")
    (latest / "model_b.json").write_text("{\"b\": 1}", encoding="utf-8")
    (latest / "notes.txt").write_text("not a view", encoding="utf-8")
    for surface in ("journal", "pipeline-events"):
        directory = root / surface / spelling
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{CYCLE_SEGMENT}.jsonl").write_text("{}\n", encoding="utf-8")
    direct = root / "pipeline-jobs" / "by-cycle" / spelling / CYCLE_SEGMENT
    direct.mkdir(parents=True, exist_ok=True)
    (direct / "job_a.json").write_text("{}", encoding="utf-8")
    (root / "pipeline-jobs" / "job_flat.json").write_text("{}", encoding="utf-8")


def _case_folds(tmp_path: Path) -> bool:
    probe = tmp_path / "case_probe"
    probe.mkdir()
    folds = (tmp_path / "CASE_PROBE").exists()
    probe.rmdir()
    return folds


def _tree_empty(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root = tmp_path / "journal"
    root.mkdir()
    return root, ("gfs",)


def _tree_one_spelling(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root = tmp_path / "journal"
    _populate_spelling(root, "gfs")
    return root, ("gfs",)


def _tree_both_spellings(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root = tmp_path / "journal"
    _populate_spelling(root, "gfs")
    _populate_spelling(root, "GFS")
    return root, ("gfs", "GFS")


def _tree_root_missing(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    return tmp_path / "journal", ("gfs",)


def _tree_rollover(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root, segments = _tree_one_spelling(tmp_path)
    for index in (1, 2):
        (root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.{index}.jsonl").write_text("{}\n", encoding="utf-8")
    return root, segments


def _tree_surface_absent(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root, segments = _tree_one_spelling(tmp_path)
    shutil.rmtree(root / "pipeline-events" / "gfs")
    return root, segments


def _replace_with_symlink_to_copy(path: Path, copy: Path) -> None:
    shutil.copytree(path, copy, symlinks=True)
    path.rename(copy.with_name(copy.name + "_original"))
    path.symlink_to(copy, target_is_directory=True)


def _tree_surface_symlink(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root, segments = _tree_one_spelling(tmp_path)
    _replace_with_symlink_to_copy(root / "journal" / "gfs", tmp_path / "journal_gfs_copy")
    return root, segments


def _tree_segment_slot_symlink(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root, segments = _tree_one_spelling(tmp_path)
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text("{}\n", encoding="utf-8")
    (root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.1.jsonl").symlink_to(decoy)
    return root, segments


def _tree_latest_cycle_symlink(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root, segments = _tree_one_spelling(tmp_path)
    _replace_with_symlink_to_copy(root / "latest" / "gfs" / CYCLE_SEGMENT, tmp_path / "latest_copy")
    return root, segments


def _tree_pipeline_jobs_symlink(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root, segments = _tree_one_spelling(tmp_path)
    _replace_with_symlink_to_copy(root / "pipeline-jobs", tmp_path / "pipeline_jobs_copy")
    return root, segments


def _tree_root_through_symlink(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    real, segments = _tree_one_spelling(tmp_path)
    root = tmp_path / "journal_link"
    root.symlink_to(real, target_is_directory=True)
    return root, segments


#: Tree -> (builder, fingerprint is the fault marker, needs two distinct spelling directories).
_TREES: dict[str, tuple[Callable[[Path], tuple[Path, tuple[str, ...]]], bool, bool]] = {
    "empty_root": (_tree_empty, False, False),
    "one_spelling": (_tree_one_spelling, False, False),
    "both_spellings": (_tree_both_spellings, False, True),
    "root_missing": (_tree_root_missing, False, False),
    "rollover_segments": (_tree_rollover, False, False),
    "surface_absent": (_tree_surface_absent, False, False),
    "surface_symlink_to_copy": (_tree_surface_symlink, True, False),
    "segment_slot_symlink": (_tree_segment_slot_symlink, True, False),
    "latest_cycle_symlink": (_tree_latest_cycle_symlink, True, False),
    "pipeline_jobs_symlink": (_tree_pipeline_jobs_symlink, True, False),
    "root_through_symlink": (_tree_root_through_symlink, True, False),
}


def _fingerprint(repository: FileOrchestrationJournalRepository, segments: tuple[str, ...]) -> Any:
    return repository._cycle_rows_source_fingerprint(source_segments=segments, cycle_segment=CYCLE_SEGMENT)


@pytest.mark.parametrize("tree", sorted(_TREES))
def test_fingerprint_value_is_unchanged_by_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: str
) -> None:
    build, faulted, needs_two_directories = _TREES[tree]
    if needs_two_directories and _case_folds(tmp_path):
        pytest.skip("filesystem folds case: the two spellings are one directory here (runs on node-27)")
    root, segments = build(tmp_path)
    repository = FileOrchestrationJournalRepository(root)

    pinned = _fingerprint(repository, segments)
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "pinned_containment_root", _pins_nothing)
        unpinned = _fingerprint(repository, segments)

    if faulted:
        assert pinned is _FAULT
        assert unpinned is _FAULT
    else:
        assert pinned is not _FAULT
        assert isinstance(pinned, tuple)
        assert pinned == unpinned


def test_root_missing_tree_is_not_created_by_the_repository(tmp_path: Path) -> None:
    root, segments = _tree_root_missing(tmp_path)
    repository = FileOrchestrationJournalRepository(root)
    assert not root.exists()
    assert _fingerprint(repository, segments) is not _FAULT
    assert not root.exists()


# --- 3.8 call shape -------------------------------------------------------------


def _expected_paths_per_fingerprint(segments: tuple[str, ...]) -> int:
    # Per spelling: the latest cycle directory, every journal slot, every
    # event slot, the by-cycle direct partition; plus the pipeline-jobs root.
    return len(segments) * (2 * len(_journal_segment_names(CYCLE_SEGMENT)) + 2) + 1


def test_fingerprint_keeps_its_per_path_seams_and_opens_the_root_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    _populate_spelling(root, "gfs")
    _populate_spelling(root, "GFS")
    segments = ("gfs", "GFS")
    expected = _expected_paths_per_fingerprint(segments)
    repository = FileOrchestrationJournalRepository(root)

    signature_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_signature = repository._containment_stat_signature

    def spy_signature(*args: Any, **kwargs: Any) -> Any:
        signature_calls.append((args, kwargs))
        return real_signature(*args, **kwargs)

    stat_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_stat = journal_module.stat_no_follow

    def spy_stat(*args: Any, **kwargs: Any) -> Any:
        stat_calls.append((args, kwargs))
        return real_stat(*args, **kwargs)

    opens: list[Path] = []
    real_open = safe_fs._open_directory_no_follow

    def counting_open(path: Path) -> int:
        opens.append(Path(path))
        return real_open(path)

    monkeypatch.setattr(repository, "_containment_stat_signature", spy_signature)
    monkeypatch.setattr(journal_module, "stat_no_follow", spy_stat)
    monkeypatch.setattr(safe_fs, "_open_directory_no_follow", counting_open)

    fingerprint = _fingerprint(repository, segments)
    assert fingerprint is not _FAULT

    assert len(signature_calls) == expected
    assert all(len(args) == 1 and kwargs == {} for args, kwargs in signature_calls)
    assert len(stat_calls) == expected
    assert all(
        len(args) == 1 and kwargs == {"containment_root": repository.root} for args, kwargs in stat_calls
    )
    assert len(opens) == 1, f"root chain opened {len(opens)} times in one fingerprint"

    opens.clear()
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "pinned_containment_root", _pins_nothing)
        assert _fingerprint(repository, segments) == fingerprint
    assert len(opens) == expected


# --- 3.9 through the cache ------------------------------------------------------


def _cycle_rows(repository: FileOrchestrationJournalRepository) -> Any:
    return repository._cycle_rows(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")


def _cache_entries(repository: FileOrchestrationJournalRepository) -> dict[Any, int]:
    return {key: id(entry) for key, entry in repository._cycle_rows_cache.items()}


def test_symlink_swap_after_a_stored_read_fails_loud_as_a_cold_read(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(root)
    writer.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    writer.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())
    writer.upsert_pipeline_job(_job_record())
    assert (root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.jsonl").is_file()

    repository = FileOrchestrationJournalRepository(root)
    first = _cycle_rows(repository)
    stored = _cache_entries(repository)
    assert stored, "the first read must store"
    assert first.hydro_run is not None and first.pipeline_jobs, "the cycle must be populated"

    _cycle_rows(repository)
    assert _cache_entries(repository) == stored, "the second read must be a hit"

    journal_source = root / "journal" / "gfs"
    copy = tmp_path / "journal_gfs_copy"
    shutil.copytree(journal_source, copy)
    journal_source.rename(tmp_path / "journal_gfs_original")
    journal_source.symlink_to(copy, target_is_directory=True)

    with pytest.raises(Exception) as warm:
        _cycle_rows(repository)
    with pytest.raises(Exception) as cold:
        _cycle_rows(FileOrchestrationJournalRepository(root))
    assert type(warm.value) is type(cold.value)
    assert isinstance(warm.value, journal_module.FileOrchestrationJournalError)
    assert warm.value.reason == cold.value.reason
    assert _cache_entries(repository) == stored, "the faulted read must store nothing"

    # Restore the content (the copy has new inodes, so the fingerprint moves
    # and the next read recomputes and stores instead of hitting).
    journal_source.unlink()
    copy.rename(journal_source)
    restored = _cycle_rows(repository)
    assert _cache_entries(repository) != stored, "the read after the restore must store again"
    assert restored.hydro_run == first.hydro_run
    assert restored.pipeline_jobs == first.pipeline_jobs
