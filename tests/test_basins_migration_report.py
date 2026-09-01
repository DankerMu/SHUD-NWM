"""Basins migration-report evidence, identity refusals and the opt-in real smoke.

Partition 5 of 6 of the former monolith ``tests/test_basins_package_publication.py``
(issue #1912).  ``test_real_basins_package_smoke_opt_in`` lives here and stays behind
the ``NHMS_RUN_BASINS_SMOKE=1`` opt-in.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

import workers.model_registry.basins_package as basins_package
from tests.basins_package_helpers import _invoke_click, _make_valid_model, _object_store_env, _write_valid_inventory
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import DEFAULT_BASINS_MIGRATION_SOURCE_URI, _argparse_main


def test_basins_migration_report_root_denied_by_an_ancestor_is_basins_root_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #1554: the migration-report entrypoint classifies the root with the same
    # errno-aware probe as discovery.  EACCES on a denied ancestor is
    # BASINS_ROOT_UNREADABLE through BasinsPackageError -- never a bare
    # PermissionError, never a misleading BASINS_ROOT_NOT_FOUND.
    root = tmp_path / "basins"
    root.mkdir()
    _make_valid_model(root / "basin-a", "alias-a")
    real_stat = Path.stat

    def denied_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == root:
            raise PermissionError(errno.EACCES, "simulated denied traversal")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", denied_stat)
        exit_code = _argparse_main(
            [
                "basins-migration-report",
                "--basins-root",
                str(root),
                "--output",
                str(tmp_path / "report.json"),
            ]
        )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_ROOT_UNREADABLE"
    assert error["path"] == str(root)
    assert not (tmp_path / "report.json").exists()


def test_basins_migration_report_root_denied_never_becomes_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The directionality pin: EACCES must not degrade into the missing-root
    # verdict on the migration-report entrypoint either.
    root = tmp_path / "basins"
    root.mkdir()
    _make_valid_model(root / "basin-a", "alias-a")

    def denied_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        raise PermissionError(errno.EACCES, "simulated denied traversal")

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", denied_stat)
        exit_code = _argparse_main(
            [
                "basins-migration-report",
                "--basins-root",
                str(root),
                "--output",
                str(tmp_path / "report.json"),
            ]
        )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] != "BASINS_ROOT_NOT_FOUND"


def test_basins_migration_report_symlink_root_follow_stat_eaccess_is_basins_root_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # cand-r1-03: lstat succeeds (symlink root), follow-stat raises EACCES; the
    # migration entrypoint must report exact BASINS_ROOT_UNREADABLE, never a
    # raw PermissionError and never the symlink-target refusal (which requires
    # the follow-stat to succeed).
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    real_stat = Path.stat

    def denied_follow_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == linked_root and kwargs.get("follow_symlinks", True) is True:
            raise PermissionError(errno.EACCES, "simulated follow-stat denial")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", denied_follow_stat)
        exit_code = _argparse_main(
            [
                "basins-migration-report",
                "--basins-root",
                str(linked_root),
                "--output",
                str(tmp_path / "report.json"),
            ]
        )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_ROOT_UNREADABLE"
    assert error["path"] == str(linked_root)
    assert not (tmp_path / "report.json").exists()


def test_basins_migration_report_rejects_symlink_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(linked_root),
            "--output",
            str(output),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_MIGRATION_SYMLINK_TARGET"
    assert error["path"] == str(linked_root)
    assert not output.exists()


def test_basins_migration_report_refuses_symlink_from_classifier_identity_without_a_second_root_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1554: the migration report consumes the classifier-produced source-symlink
    # identity for its production symlink refusal.  It must NOT re-probe the root
    # with a bare `Path.is_symlink()`; a second root probe would fail this
    # injection, while the refusal still fires from the classifier's verdict.
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    real_is_symlink = Path.is_symlink

    def root_is_symlink_denied(self: Path) -> bool:
        if self == linked_root:
            raise PermissionError(errno.EACCES, "second root is_symlink probe must not happen")
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", root_is_symlink_denied)

    with pytest.raises(basins_package.BasinsPackageError) as exc_info:
        basins_package.write_basins_migration_report(
            basins_root=linked_root,
            source_uri=DEFAULT_BASINS_MIGRATION_SOURCE_URI,
            output_path=tmp_path / "report.json",
        )

    payload = exc_info.value.to_payload()
    assert payload["error_code"] == "BASINS_MIGRATION_SYMLINK_TARGET"
    assert payload["path"] == str(linked_root)
    assert not (tmp_path / "report.json").exists()


def test_click_basins_migration_report_rejects_symlink_target_without_source_uri(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("click")
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    output = tmp_path / "report.json"

    exit_code = _invoke_click(
        [
            "basins-migration-report",
            "--basins-root",
            str(linked_root),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MIGRATION_SYMLINK_TARGET"
    assert error["path"] == str(linked_root)
    assert "Missing option" not in captured.err
    assert not output.exists()


def test_basins_migration_report_rejects_unresolvable_symlink_descendant_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    loop = real_root / "loop"
    try:
        loop.symlink_to(loop)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(loop)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_publish_basins_reports_symlink_loop_inventory_root_as_unresolvable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    loop = tmp_path / "loop-basins-root"
    try:
        loop.symlink_to(loop)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["resolved_root"] = str(loop)
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-loop-inventory-root",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNRESOLVABLE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-loop-inventory-root"
    assert error["path"] == str(loop)
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-loop-inventory-root" / "manifest.json").exists()


def test_publish_basins_reports_loop_behind_missing_inventory_root_as_source_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ENOENT lane: the strict walk aborts on the missing `gone` component while
    # the non-strict fallback collapses `..` onto a real symlink loop. That must
    # stay nonexistence (SOURCE_NOT_FOUND), never an uncaught RuntimeError.
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    loop = tmp_path / "loopdir"
    try:
        loop.symlink_to(loop)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    loop_behind_missing = tmp_path / "gone" / ".." / "loopdir"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["resolved_root"] = str(loop_behind_missing)
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-loop-behind-missing-root",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_SOURCE_NOT_FOUND"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-loop-behind-missing-root"
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-loop-behind-missing-root" / "manifest.json").exists()


def test_basins_migration_report_rejects_symlink_descendant_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    model_dir = real_root / "basin-a"
    _make_valid_model(model_dir, "alias-a", calibration_count=1, forcing_count=1)
    linked_file = model_dir / "CALIB" / "linked.calib"
    try:
        linked_file.symlink_to(model_dir / "CALIB" / "top01.calib")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(linked_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_reports_output_write_failure_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output_parent = tmp_path / "not-a-dir"
    output_parent.write_text("file blocks output parent\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output_parent / "report.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MIGRATION_REPORT_WRITE_FAILED"
    assert error["path"] == str(output_parent / "report.json")
    assert "Traceback" not in captured.err


def test_basins_migration_report_reports_deleted_evidence_file_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output = tmp_path / "report.json"
    stale_file = real_root / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    original_migration_source_file_evidence = basins_package._migration_source_file_evidence
    deleted = False

    def deleting_migration_source_file_evidence(path: Path, source_root: Path) -> tuple[int, str]:
        nonlocal deleted
        if path == stale_file and not deleted:
            deleted = True
            path.unlink()
        return original_migration_source_file_evidence(path, source_root)

    monkeypatch.setattr(basins_package, "_migration_source_file_evidence", deleting_migration_source_file_evidence)

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MIGRATION_EVIDENCE_READ_FAILED"
    assert error["path"] == str(stale_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_rejects_file_replaced_with_symlink_before_evidence_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output = tmp_path / "report.json"
    source_file = real_root / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    outside_file = tmp_path / "outside-secret.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    original_walk_source_files = basins_package._walk_source_files
    mutated = False

    def symlink_swapping_walk_source_files(root: Path, source_root: Path):
        nonlocal mutated
        for path in original_walk_source_files(root, source_root):
            if path == source_file and not mutated:
                mutated = True
                path.unlink()
                try:
                    path.symlink_to(outside_file)
                except (NotImplementedError, OSError) as error:
                    pytest.skip(f"symlink support unavailable: {error}")
            yield path

    monkeypatch.setattr(basins_package, "_walk_source_files", symlink_swapping_walk_source_files)

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(source_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_rejects_ancestor_replaced_with_symlink_before_evidence_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output = tmp_path / "report.json"
    source_file = real_root / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    outside_dir = tmp_path / "outside-input"
    outside_dir.mkdir()
    (outside_dir / source_file.name).write_text("outside\n", encoding="utf-8")
    original_migration_source_file_evidence = basins_package._migration_source_file_evidence
    original_open = basins_package.os.open
    enabled = False
    mutated = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal mutated
        if enabled and not mutated and dir_fd is not None and os.fspath(path) == source_file.name:
            mutated = True
            moved_dir = source_file.parent.with_name("alias-a.original")
            source_file.parent.rename(moved_dir)
            try:
                source_file.parent.symlink_to(outside_dir, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def enabling_migration_source_file_evidence(path: Path, source_root: Path) -> tuple[int, str]:
        nonlocal enabled
        if path == source_file:
            enabled = True
        return original_migration_source_file_evidence(path, source_root)

    monkeypatch.setattr(basins_package.os, "open", swapping_open)
    monkeypatch.setattr(basins_package, "_migration_source_file_evidence", enabling_migration_source_file_evidence)

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert mutated is True
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(source_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_accepts_real_copied_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a", forcing_count=1)
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["production_ready"] is True
    assert report["schema_version"] == "basins.migration.v1"
    assert report["source_uri"] == DEFAULT_BASINS_MIGRATION_SOURCE_URI
    assert report["target_path"] == str(real_root)
    assert report["source_is_symlink"] is False
    assert report["file_count"] > 0
    assert report["byte_count"] > 0
    assert report["inventory_checksum"]
    assert report["content_checksum"]
    assert report["source_to_target"]["symlink_allowed"] is False
    assert report["production_ready"] is True


@pytest.mark.skipif(
    not Path("data/Basins").exists(),
    reason="real Basins package smoke requires data/Basins",
)
@pytest.mark.skipif(
    os.getenv("NHMS_RUN_BASINS_SMOKE") != "1",
    reason="real Basins package smoke is opt-in with NHMS_RUN_BASINS_SMOKE=1",
)
def test_real_basins_package_smoke_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory = discover_basins_inventory(Path("data/Basins"))
    publishable_model = next(model for model in inventory["models"] if model["status"] == "valid")
    inventory_path = tmp_path / "real-inventory.json"
    write_inventory(inventory, inventory_path)
    _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "real-manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            publishable_model["model_id"],
            "--version",
            "vbasins-real-smoke",
            "--output",
            str(manifest_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["status"] == "published"
    assert payload["model_id"] == publishable_model["model_id"]
    assert manifest["forcing"]["payload_copied"] is False
    assert all(entry["role"] != "forcing" for entry in manifest["included_files"])
