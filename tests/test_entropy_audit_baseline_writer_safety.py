"""The entropy baseline writer's refusals and durability (#1823 partition).

Snapshot-identity refusals (untracked / ignored / dirty report-visible files,
before and during the report), non-ASCII path identity, the bounded status-path
and inventory reads, oversized-input refusals before the temp write, and the
archive/replace failure paths that must preserve the existing ``latest.json``
bytes. The trend/summary half is in
``tests/test_entropy_audit_baseline_writer_summary.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy, write_entropy_baseline
from tests.entropy_audit_helpers import (
    _baseline_archive_files,
    _commit_all,
    _init_git,
    _run_entropy_baseline_writer_cli,
    _write,
)


def test_entropy_baseline_writer_fails_before_writing_when_snapshot_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\n")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report

    def mutating_build_report(repo_root: Path, *, mode: audit_repo_entropy.AuditMode = "report") -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\nmutated\n")
        return report

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", mutating_build_report)

    with pytest.raises(
        write_entropy_baseline.BaselineWriteError,
        match="repository snapshot changed during baseline generation",
    ):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_untracked_report_visible_files_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    _write(tmp_path / "docs" / "local.md", "Untracked local note still mentions /hydro-met.\n")

    assert any(
        finding["evidence_path"] == "docs/local.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )

    def unexpected_build_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("writer dirty preflight must run before report generation")

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", unexpected_build_report)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_ignored_report_visible_files_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".gitignore", "docs/local.md\n")
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    _write(tmp_path / "docs" / "local.md", "Ignored local note still mentions /hydro-met.\n")

    assert any(
        finding["evidence_path"] == "docs/local.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )

    def unexpected_build_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("writer dirty preflight must run before report generation")

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", unexpected_build_report)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_dirty_tracked_report_visible_files_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "docs" / "active.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    _write(tmp_path / "docs" / "active.md", "Dirty tracked docs still mention /hydro-met.\n")

    assert any(
        finding["evidence_path"] == "docs/active.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )

    def unexpected_build_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("writer dirty preflight must run before report generation")

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", unexpected_build_report)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_untracked_report_visible_files_created_during_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report

    def creating_untracked_build_report(
        repo_root: Path,
        *,
        mode: audit_repo_entropy.AuditMode = "report",
    ) -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / "docs" / "local.md", "Untracked local note still mentions /hydro-met.\n")
        return report

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "build_report",
        creating_untracked_build_report,
    )

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_ignored_report_visible_files_created_during_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".gitignore", "docs/local.md\n")
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report

    def creating_ignored_build_report(
        repo_root: Path,
        *,
        mode: audit_repo_entropy.AuditMode = "report",
    ) -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / "docs" / "local.md", "Ignored local note still mentions /hydro-met.\n")
        return report

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "build_report",
        creating_ignored_build_report,
    )

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert any(
        finding["evidence_path"] == "docs/local.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_bounds_status_path_collection() -> None:
    limit = write_entropy_baseline.MAX_WORKTREE_STATUS_PATHS_IN_ERROR + 1
    yielded: list[int] = []

    def status_records() -> Iterable[bytes]:
        for index in range(100):
            yielded.append(index)
            yield f"?? docs/local-{index}.md".encode()

    paths = write_entropy_baseline._bounded_git_status_porcelain_z_paths(
        status_records(),
        max_paths=limit,
    )

    assert paths == [f"docs/local-{index}.md" for index in range(limit)]
    assert yielded == list(range(limit))


def test_git_tracked_paths_preserves_non_ascii_path_identity(tmp_path: Path) -> None:
    _init_git(tmp_path)
    unicode_path = "docs/说明.md"
    quoted_literal = '"docs/\\350\\257\\264\\346\\230\\216.md"'
    _write(tmp_path / unicode_path, "Unicode path identity.\n")
    _write(tmp_path / "README.md", "Repository readme.\n")
    subprocess.run(["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "docs/说明.md", "README.md"], cwd=tmp_path, check=True)

    tracked_paths = audit_repo_entropy._git_tracked_paths(tmp_path)
    scoped_paths = audit_repo_entropy._git_tracked_paths(tmp_path, ["docs"])

    assert unicode_path in tracked_paths
    assert quoted_literal not in tracked_paths
    assert scoped_paths == [unicode_path]


def test_entropy_baseline_writer_snapshot_uses_unicode_paths_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    unicode_path = "docs/说明.md"
    quoted_literal = '"docs/\\350\\257\\264\\346\\230\\216.md"'
    _write(tmp_path / unicode_path, "Current docs still mention /hydro-met.\n")
    subprocess.run(["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", unicode_path], cwd=tmp_path, check=True)
    _commit_all(tmp_path, "initial unicode docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report
    observed_inventory: write_entropy_baseline.BaselineFileInventory | None = None
    observed_snapshot: write_entropy_baseline.SnapshotIdentity | None = None

    def observing_build_baseline_snapshot(
        repo_root: Path,
        report: dict[str, object],
        *,
        timestamp: object | None = None,
        file_inventory: write_entropy_baseline.BaselineFileInventory | None = None,
        snapshot: write_entropy_baseline.SnapshotIdentity | None = None,
    ) -> dict[str, object]:
        nonlocal observed_inventory, observed_snapshot
        observed_inventory = file_inventory
        observed_snapshot = snapshot
        return original_build_baseline_snapshot(
            repo_root,
            report,
            timestamp=timestamp,
            file_inventory=file_inventory,
            snapshot=snapshot,
        )

    def mutating_build_report(repo_root: Path, *, mode: audit_repo_entropy.AuditMode = "report") -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / unicode_path, "Current docs still mention /hydro-met.\nmutated\n")
        return report

    original_build_baseline_snapshot = write_entropy_baseline.build_baseline_snapshot
    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", mutating_build_report)
    monkeypatch.setattr(
        write_entropy_baseline,
        "build_baseline_snapshot",
        observing_build_baseline_snapshot,
    )

    with pytest.raises(
        write_entropy_baseline.BaselineWriteError,
        match="repository snapshot changed during baseline generation",
    ):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert observed_inventory is not None
    assert observed_snapshot is not None
    assert unicode_path in observed_inventory.relative_paths
    assert quoted_literal not in observed_inventory.relative_paths
    assert any(fingerprint[0] == unicode_path for fingerprint in observed_inventory.file_fingerprints)
    assert unicode_path in observed_snapshot.inventory_paths
    assert quoted_literal not in observed_snapshot.inventory_paths
    assert any(fingerprint[0] == unicode_path for fingerprint in observed_snapshot.inventory_file_fingerprints)
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_oversized_latest_before_temp_write(tmp_path: Path) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b"x" * (write_entropy_baseline.MAX_ARCHIVED_LATEST_BYTES + 1)
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)

    result = _run_entropy_baseline_writer_cli(tmp_path, check=False)

    assert result.returncode == 1
    assert "existing latest baseline exceeds archive size limit" in result.stderr
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_oversized_inventory_before_temp_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    tracked_paths = [
        f"apps/api/generated_{index}.py"
        for index in range(write_entropy_baseline.MAX_BASELINE_INVENTORY_FILES + 1)
    ]

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): tracked_paths,
    )

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="baseline inventory file count"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_snapshot_identity_does_not_walk_huge_fallback_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "services" / "api" / "main.py", "VALUE = 1\n")

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): ["services/api/main.py"],
    )

    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    assert inventory.relative_paths == ("services/api/main.py",)

    fallback_calls = 0

    def huge_fallback_paths(_root: Path) -> list[str]:
        nonlocal fallback_calls
        fallback_calls += 1
        return [
            f"generated/fallback_{index}.py"
            for index in range(write_entropy_baseline.MAX_BASELINE_INVENTORY_FILES + 1)
        ]

    monkeypatch.setattr(write_entropy_baseline, "_fallback_inventory_relative_paths", huge_fallback_paths)

    result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    baseline = json.loads(result.baseline_path.read_text(encoding="utf-8"))

    assert fallback_calls == 0
    assert inventory.total_source_files == 1
    assert baseline["summary"]["total_source_files"] == 1
    assert _baseline_archive_files(tmp_path / ".entropy-baseline") == []


def test_entropy_baseline_writer_archive_failure_cleans_temp_and_preserves_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)

    def failing_copy(_source_path: Path, destination_path: Path) -> None:
        destination_path.write_bytes(b"partial archive\n")
        raise OSError("archive fsync failed")

    monkeypatch.setattr(write_entropy_baseline, "_bounded_copy_file", failing_copy)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="unable to archive existing latest baseline"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_replace_failure_rolls_back_archive_and_preserves_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_replace = os.replace

    def failing_replace(source: object, destination: object) -> None:
        if Path(source).name == ".latest.json.tmp" and Path(destination).name == "latest.json":
            raise OSError("replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(write_entropy_baseline.os, "replace", failing_replace)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="unable to replace latest baseline"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_avoids_archive_timestamp_collisions(tmp_path: Path) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    existing_archive_bytes = b'{"already": "archived"}\n'
    timestamp = write_entropy_baseline.datetime(2026, 6, 12, 16, 2, 45, tzinfo=write_entropy_baseline.UTC)
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    existing_archive = baseline_dir / "2026-06-12T160245Z.json"
    existing_archive.write_bytes(existing_archive_bytes)

    result = write_entropy_baseline.write_entropy_baseline(tmp_path, now=timestamp)

    assert result.archive_path == baseline_dir / "2026-06-12T160245Z-01.json"
    assert existing_archive.read_bytes() == existing_archive_bytes
    assert result.archive_path.read_bytes() == previous_bytes
    assert latest.read_bytes() == result.baseline_bytes


def test_entropy_baseline_writer_fallback_inventory_skips_entropy_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\n")
    _write(tmp_path / ".entropy-baseline" / "latest.json", '{"legacy": true}\n')
    _write(tmp_path / ".entropy-baseline" / "2026-06-12T160245Z.json", '{"archive": true}\n')

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): [],
    )

    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    assert all(not path.startswith(".entropy-baseline/") for path in inventory.relative_paths)
    assert ".entropy-baseline/latest.json" not in inventory.relative_paths

    result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    baseline = json.loads(result.baseline_path.read_text(encoding="utf-8"))

    assert ".entropy-baseline/latest.json" not in result.baseline_bytes.decode("utf-8")
    assert inventory.total_source_files == 1
    assert baseline["summary"]["total_source_files"] == 0
