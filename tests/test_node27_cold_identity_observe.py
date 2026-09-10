"""Requirement-driven tests for the read-only host identity observer (#1895 task 4.0).

The observer has two deliberately distinct lanes: the installer lane mounts
identity ``major:minor:mount-id:source`` from
``node27_cold_tablespace_host.inspect_host_path`` for the *absent* production
child, and the runner lane descriptor identity ``st_dev:st_ino`` from
``compressed_chunk_cold_target.inspect_host_path`` for the *created* directory.
Only the narrow production owners may be called; the movement runtime module,
target preflight owner, probe-private modules and Docker must never be loaded or
executed.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from scripts import node27_cold_identity_observe as observer

_ROOT = Path(__file__).resolve().parents[1]
_HEAD = "a" * 40


def _installer_module():
    return importlib.import_module("packages.common.node27_cold_tablespace_host")


def _target_module():
    return importlib.import_module("packages.common.compressed_chunk_cold_target")


def _installer_ok(**overrides):
    return dict(
        {
            "exists": False,
            "is_symlink": False,
            "is_directory": False,
            "entry_count": None,
            "uid": None,
            "gid": None,
            "mode": None,
            "mount_device": "/dev/md0",
            "device_identity": "8:1:42:/dev/md0",
            "free_bytes": 12345,
        },
        **overrides,
    )


def _runner_ok():
    return type("HostIdentity", (), {"device_identity": "16777233:12345"})()


# --- installer lane -----------------------------------------------------------


def test_installer_lane_absent_child_success(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _installer_module()
    monkeypatch.setattr(host, "inspect_host_path", lambda path, identity=None: _installer_ok())
    result = observer.observe_installer_identity(head_sha=_HEAD)
    assert result == "8:1:42:/dev/md0"
    assert _does_not_match_runner_format(result)


def test_installer_lane_existing_child_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _installer_module()
    monkeypatch.setattr(host, "inspect_host_path", lambda path, identity=None: _installer_ok(exists=True))
    with pytest.raises(observer.IdentityObserveError) as raised:
        observer.observe_installer_identity(head_sha=_HEAD)
    assert raised.value.error_class == "device_identity"


def test_installer_lane_symlink_child_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _installer_module()
    monkeypatch.setattr(host, "inspect_host_path", lambda path, identity=None: _installer_ok(is_symlink=True))
    with pytest.raises(observer.IdentityObserveError) as raised:
        observer.observe_installer_identity(head_sha=_HEAD)
    assert raised.value.error_class == "path_alias"


def test_installer_lane_wrong_host_path_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _installer_module()
    calls: list[object] = []

    def fake(path, identity=None):
        calls.append(path)
        return _installer_ok()

    monkeypatch.setattr(host, "inspect_host_path", fake)
    with pytest.raises(observer.IdentityObserveError) as raised:
        observer.observe_installer_identity(head_sha=_HEAD, host_path="/wrong/path")
    assert raised.value.error_class == "path_alias"
    assert calls == []


def test_installer_lane_cross_format_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _installer_module()
    # The runner format nibble (st_dev:st_ino) returned by the installer owner
    # must not satisfy the installer mount-identity pattern.
    monkeypatch.setattr(host, "inspect_host_path", lambda path, identity=None: _installer_ok(
        device_identity="16777233:12345",
    ))
    with pytest.raises(observer.IdentityObserveError):
        observer.observe_installer_identity(head_sha=_HEAD)


# --- runner lane --------------------------------------------------------------


def test_runner_lane_created_directory_success(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target_module()
    monkeypatch.setattr(target, "inspect_host_path", lambda path: _runner_ok())
    result = observer.observe_runner_identity(head_sha=_HEAD)
    assert result == "16777233:12345"
    assert _does_not_match_installer_format(result)


def test_runner_lane_missing_path_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError

    target = _target_module()

    def missing(path):
        raise ColdRuntimeError(
            f"target host path is unavailable: {path}",
            error_class="target_identity",
            stage="target_identity",
        )

    monkeypatch.setattr(target, "inspect_host_path", missing)
    with pytest.raises(observer.IdentityObserveError) as raised:
        observer.observe_runner_identity(head_sha=_HEAD)
    assert raised.value.error_class == "host_path"
    assert raised.value.stage == "observe"


def test_runner_lane_wrong_host_path_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target_module()
    calls: list[object] = []

    def fake(path):
        calls.append(path)
        return _runner_ok()

    monkeypatch.setattr(target, "inspect_host_path", fake)
    with pytest.raises(observer.IdentityObserveError) as raised:
        observer.observe_runner_identity(head_sha=_HEAD, host_path="/wrong/path")
    assert raised.value.error_class == "path_alias"
    assert calls == []


def test_runner_lane_cross_format_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target_module()
    monkeypatch.setattr(
        target,
        "inspect_host_path",
        lambda path: type("HostIdentity", (), {"device_identity": "8:1:42:/dev/md0"})(),
    )
    with pytest.raises(observer.IdentityObserveError):
        observer.observe_runner_identity(head_sha=_HEAD)


# --- head gates ---------------------------------------------------------------


def test_dirty_head_refuses_both_lanes(tmp_path: Path) -> None:
    for lane in ("installer", "runner"):
        code = observer.main([lane], head_observer=lambda: (_HEAD, True, True))
        assert code == 2, lane


def test_unobservable_head_refuses_both_lanes(tmp_path: Path) -> None:
    for lane in ("installer", "runner"):
        code = observer.main([lane], head_observer=lambda: (None, False, False))
        assert code == 2, lane


@pytest.mark.parametrize("lane", ["installer", "runner"])
def test_dirty_or_unobservable_head_never_calls_the_owner(
    lane: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head gate refusal must happen before any host observer is invoked."""

    if lane == "installer":
        host = _installer_module()

        def fail(path, identity=None):
            raise AssertionError("installer observer must not run under an unclean head")

        monkeypatch.setattr(host, "inspect_host_path", fail)
    else:
        target = _target_module()

        def fail(path):
            raise AssertionError("runner observer must not run under an unclean head")

        monkeypatch.setattr(target, "inspect_host_path", fail)
    for head in ((_HEAD, True, True), (None, False, False)):
        code = observer.main([lane], head_observer=lambda head=head: head)
        assert code == 2, head


def test_head_oserror_is_classified_head_not_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object, **kwargs: object):
        raise OSError("git vanished")

    monkeypatch.setattr(observer.subprocess, "run", boom)
    code = observer.main(["installer"])
    assert code == 2
    err = capsys.readouterr().err
    assert "head" in err
    assert "identity" not in err
    assert "connection" not in err


# --- owner error classification -----------------------------------------------


def test_installer_owner_oserror_is_stable_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host = _installer_module()

    def boom(path, identity=None):
        del path, identity
        raise OSError("boom")

    monkeypatch.setattr(host, "inspect_host_path", boom)
    code = observer.main(["installer"], head_observer=lambda: (_HEAD, True, False))
    assert code == 2
    err = capsys.readouterr().err
    assert "host_path (observe)" in err


def test_runner_owner_oserror_is_stable_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _target_module()

    def boom(path):
        del path
        raise OSError("boom")

    monkeypatch.setattr(target, "inspect_host_path", boom)
    code = observer.main(["runner"], head_observer=lambda: (_HEAD, True, False))
    assert code == 2
    err = capsys.readouterr().err
    assert "host_path (observe)" in err


def test_installer_owner_runtime_error_is_stable_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host = _installer_module()

    def boom(path, identity=None):
        del path, identity
        raise RuntimeError("boom")

    monkeypatch.setattr(host, "inspect_host_path", boom)
    code = observer.main(["installer"], head_observer=lambda: (_HEAD, True, False))
    assert code == 2
    err = capsys.readouterr().err
    assert "host_path (observe)" in err


def test_runner_owner_runtime_error_is_stable_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _target_module()

    def boom(path):
        del path
        raise RuntimeError("boom")

    monkeypatch.setattr(target, "inspect_host_path", boom)
    code = observer.main(["runner"], head_observer=lambda: (_HEAD, True, False))
    assert code == 2
    err = capsys.readouterr().err
    assert "host_path (observe)" in err


# --- import surface -----------------------------------------------------------


def test_identity_observe_imports_only_narrow_production_owners() -> None:
    source = (_ROOT / "scripts/node27_cold_identity_observe.py").read_text(encoding="utf-8")
    # Installer lane owner.
    assert "from packages.common.node27_cold_tablespace_host import" in source
    # Runner lane owner — the narrow target inspector module, never the movement
    # runtime module or the target-preflight owner.
    assert "from packages.common.compressed_chunk_cold_target import" in source
    assert "from packages.common.compressed_chunk_cold_runtime import" not in source
    assert "import packages.common.compressed_chunk_cold_runtime" not in source
    assert "compressed_chunk_cold_runtime_target" not in source
    assert "compressed_chunk_cold_runtime_catalog" not in source
    assert "compressed_chunk_cold_probe" not in source
    # No Docker import or invocation in code (the docstring may name Docker as
    # the thing the reader never touches).
    imports = " ".join(
        f"{node.module} {alias.name}" if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(ast.parse(source))
        for alias in getattr(node, "names", ())
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ).lower()
    assert "docker" not in imports
    # subprocess is allowed only for the git HEAD observation.
    assert source.count("subprocess.run") == 4
    assert "Popen" not in source


def test_identity_observe_import_chain_never_imports_docker_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observer's import graph must never reach the Docker SDK package.

    The installer owner legitimately imports the pure-data container/evidence
    modules, but neither they nor the target owner import the ``docker`` Python
    package; blocking it in ``sys.modules`` makes any accidental reach a hard
    failure instead of an assumption.
    """

    class Blocked:
        def __getattr__(self, name):
            raise AssertionError(f"forbidden Docker SDK import attempted: {name}")

    for name in [name for name in list(sys.modules) if name == "docker" or name.startswith("docker.")]:
        del sys.modules[name]
    monkeypatch.setitem(sys.modules, "docker", Blocked())  # type: ignore[arg-type]
    for name in list(sys.modules):
        if name == "scripts.node27_cold_identity_observe":
            del sys.modules[name]
    module = importlib.import_module("scripts.node27_cold_identity_observe")
    assert module is not None


def _does_not_match_runner_format(value: str) -> bool:
    from scripts.node27_cold_identity_observe import _INSTALLER_PATTERN, _RUNNER_PATTERN

    assert _INSTALLER_PATTERN.fullmatch(value)
    return _RUNNER_PATTERN.fullmatch(value) is None


def _does_not_match_installer_format(value: str) -> bool:
    from scripts.node27_cold_identity_observe import _INSTALLER_PATTERN, _RUNNER_PATTERN

    assert _RUNNER_PATTERN.fullmatch(value)
    return _INSTALLER_PATTERN.fullmatch(value) is None
