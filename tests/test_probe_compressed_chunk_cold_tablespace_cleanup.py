"""Disposable-probe cleanup ownership tests (#1892).

These tests never start Docker. Terminal cleanup may remove a container only
when this run proved it created that exact container.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_probe import runner as isolated_runner
from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID, PINNED_IMAGE_REF
from scripts import probe_compressed_chunk_cold_tablespace as probe

_OWNED_NAME = "nhms-1892-probe-abcdef123456"
_CONFLICT = 'Conflict. The container name "/nhms-1892-probe-abcdef123456" is already in use'


def _completed(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker"], returncode=code, stdout=stdout, stderr=stderr)


def _owned(
    *,
    work_root: Path | None,
    created_work_root: bool = False,
    created_container: bool = False,
) -> probe.OwnedResources:
    return probe.OwnedResources(
        container_name=_OWNED_NAME,
        work_root=work_root,
        created_work_root=created_work_root,
        created_container=created_container,
    )


def _pinned_config(work: Path) -> probe.ProbeConfig:
    return probe.config_from_args(
        probe.parse_args(
            [
                "--container-name",
                _OWNED_NAME,
                "--host-port",
                "55492",
                "--work-root",
                str(work),
                "--image-id",
                PINNED_IMAGE_ID,
                "--image-ref",
                PINNED_IMAGE_REF,
            ]
        )
    )


def _stub_live_image(_docker_bin: str, *_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "image_id": PINNED_IMAGE_ID,
        "image_ref": PINNED_IMAGE_REF,
        "repo_tags": [PINNED_IMAGE_REF],
        "repo_digests": [],
    }


def test_owned_resources_default_container_marker_is_false() -> None:
    owned = probe.OwnedResources(container_name=_OWNED_NAME)
    assert owned.created_container is False
    assert owned.created_work_root is False


def test_cleanup_removes_only_identity_bound_resources(tmp_path: Path) -> None:
    work = tmp_path / _OWNED_NAME
    work.mkdir()
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[1] == "inspect":
            return _completed(1, stderr="No such object")
        return _completed(0)

    proof = probe.cleanup_owned(
        _owned(work_root=work, created_work_root=True, created_container=True),
        docker_bin="docker",
        runner=runner,
    )
    assert proof["created_container"] is True
    assert proof["container_removed"] is True
    assert proof["container_absent"] is True
    assert proof["work_root_removed"] is True
    assert not work.exists()
    assert calls[0][:3] == ["docker", "rm", "-f"]
    assert calls[0][3] == _OWNED_NAME
    assert "nhms-db" not in calls[0]


def test_cleanup_name_conflict_does_not_remove_preexisting_container(tmp_path: Path) -> None:
    work = tmp_path / _OWNED_NAME
    work.mkdir()
    sibling = tmp_path / "preexisting-sibling"
    sibling.mkdir()
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return _completed(0, stdout="should-not-run")

    owned = _owned(work_root=work, created_work_root=True, created_container=False)
    proof = probe.cleanup_owned(owned, docker_bin="docker", runner=runner)
    assert owned.created_container is False
    assert proof["created_container"] is False
    assert proof["container_removed"] is False
    assert proof["container_absent"] is False
    assert proof["container_cleanup"] == "not created by this run"
    assert proof["work_root_removed"] is True
    assert not work.exists()
    assert sibling.exists()
    assert calls == []
    assert not any(call[1:3] == ["rm", "-f"] for call in calls)


def test_cleanup_keep_performs_no_removal_even_when_marker_true(tmp_path: Path) -> None:
    work = tmp_path / _OWNED_NAME
    work.mkdir()
    calls: list[list[str]] = []
    proof = probe.cleanup_owned(
        _owned(work_root=work, created_work_root=True, created_container=True),
        docker_bin="docker",
        keep=True,
        runner=lambda argv, **_kwargs: calls.append(list(argv)) or _completed(0),
    )
    assert proof["refused"] == "keep requested"
    assert proof["kept"] is True
    assert proof["container_removed"] is False
    assert proof["work_root_removed"] is False
    assert work.exists()
    assert calls == []


def test_cleanup_refuses_regex_invalid_name_regardless_of_marker(tmp_path: Path) -> None:
    foreign = tmp_path / "not-owned"
    foreign.mkdir()
    calls: list[list[str]] = []
    proof = probe.cleanup_owned(
        probe.OwnedResources(
            container_name="nhms-db",
            work_root=foreign,
            created_work_root=True,
            created_container=True,
        ),
        docker_bin="docker",
        runner=lambda argv, **_kwargs: calls.append(list(argv)) or _completed(0),
    )
    assert proof["refused"] == "unowned identity"
    assert proof["container_removed"] is False
    assert foreign.exists()
    assert calls == []


def test_prepare_work_root_does_not_mark_container_created(tmp_path: Path) -> None:
    work = tmp_path / _OWNED_NAME
    owned = probe.prepare_work_root(_pinned_config(work))
    assert owned.created_work_root is True
    assert owned.created_container is False
    assert (work / "pgdata").is_dir()
    assert (work / "cold").is_dir()
    assert (work / "full").is_dir()


def test_successful_docker_run_sets_created_container_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / _OWNED_NAME
    work.mkdir()
    config = _pinned_config(work)
    owned = probe.OwnedResources(container_name=config.container_name, work_root=config.work_root)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        assert owned.created_container is False
        if argv[1] == "run":
            return _completed(0, stdout="cid-success")
        return _completed(0)

    monkeypatch.setattr(isolated_runner, "inspect_live_image", _stub_live_image)
    monkeypatch.setattr(isolated_runner, "_run", fake_run)
    monkeypatch.setattr(isolated_runner, "_container_logs", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        isolated_runner,
        "wait_for_port",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(probe.ProbeError("stop after docker run")),
    )
    with pytest.raises(probe.ProbeError, match="stop after docker run"):
        isolated_runner.run_isolated_cluster(config, owned)
    assert owned.created_container is True
    assert any(call[1] == "run" and _OWNED_NAME in call for call in calls)
    assert not any(call[1:3] == ["rm", "-f"] for call in calls)


def test_docker_run_name_conflict_leaves_marker_false_and_cleanup_skips_rm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / _OWNED_NAME
    work.mkdir()
    config = _pinned_config(work)
    owned = probe.OwnedResources(container_name=config.container_name, work_root=config.work_root)
    docker_calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        docker_calls.append(list(argv))
        if argv[1] == "run":
            assert owned.created_container is False
            return _completed(125, stderr=_CONFLICT)
        return _completed(0)

    monkeypatch.setattr(isolated_runner, "inspect_live_image", _stub_live_image)
    monkeypatch.setattr(isolated_runner, "_run", fake_run)
    with pytest.raises(probe.ProbeError, match="docker run failed"):
        isolated_runner.run_isolated_cluster(config, owned)
    assert owned.created_container is False
    proof = probe.cleanup_owned(owned, docker_bin="docker", runner=fake_run)
    assert proof["created_container"] is False
    assert proof["container_removed"] is False
    assert proof["container_absent"] is False
    assert proof["container_cleanup"] == "not created by this run"
    assert any(call[1] == "run" and _OWNED_NAME in call for call in docker_calls)
    assert not any(call[1:3] == ["rm", "-f"] for call in docker_calls)


def test_inspect_failure_does_not_mark_container_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _pinned_config(tmp_path / _OWNED_NAME)
    owned = probe.OwnedResources(container_name=config.container_name, work_root=config.work_root)

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise probe.ProbeError("inspect failed before docker run")

    monkeypatch.setattr(isolated_runner, "inspect_live_image", boom)
    with pytest.raises(probe.ProbeError, match="inspect failed before docker run"):
        isolated_runner.run_isolated_cluster(config, owned)
    assert owned.created_container is False


def test_main_reports_pre_container_probe_error_before_truthful_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed live-image inspect reports its primary error without Docker mutation."""
    output = tmp_path / "probe-report.json"
    docker_calls: list[list[str]] = []

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise probe.ProbeError("inspect failed before docker run")

    def no_docker_remove(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        docker_calls.append(list(argv))
        raise AssertionError(f"unexpected Docker cleanup call: {argv}")

    cleanup_owned = probe.cleanup_owned

    def cleanup_without_docker_mutation(owned: probe.OwnedResources, **_kwargs: object) -> dict[str, object]:
        return cleanup_owned(owned, docker_bin="docker", runner=no_docker_remove)

    monkeypatch.setattr(probe, "run_isolated_cluster", boom)
    monkeypatch.setattr(probe, "cleanup_owned", cleanup_without_docker_mutation)

    code = probe.main(
        [
            "--mode",
            "isolated-cluster",
            "--container-name",
            _OWNED_NAME,
            "--host-port",
            "55492",
            "--work-root",
            str(tmp_path / _OWNED_NAME),
            "--output",
            str(output),
        ]
    )

    document = probe.parse_probe_report(output.read_text(encoding="utf-8"))
    assert code == 1
    assert document["status"] == "failed"
    assert document["error_type"] == "ProbeError"
    assert document["error"] == "inspect failed before docker run"
    assert document["cleanup"]["created_container"] is False
    assert document["cleanup"]["container_absent"] is False
    assert document["cleanup"]["container_removed"] is False
    assert docker_calls == []
