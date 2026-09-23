"""#2453: the scheduler root check attributes a symlink-loop root identically on every CPython.

``Path.resolve()`` is not a loop predicate on the supported interpreter range
(ADR 0009 "为什么 ``Path.resolve()`` 被逐出本家族";
``openspec/specs/runtime-evidence-and-operations/spec.md``, requirement "DB-free
scheduler config path adjudication survives symlink loops"): up to 3.12 BOTH of
its forms raise an errno-less ``RuntimeError`` on a loop, while 3.13+ folds the
non-strict form and returns.  ``_scheduler_root_check`` used to canonicalise with
``path.resolve(strict=False)`` and answer the pin's ``RuntimeError`` with an early
return that assembled its own 8-key ``check`` -- so one loop root was
``..._UNSAFE_PATH`` with 8 keys on 3.11 and ``..._SYMLINK`` with 10 keys on 3.13.
The verdict (``blocked``) was the same; the evidence was not comparable.

Every assertion here is interpreter-independent on purpose: the same expected
reason and the same key set must hold when this module runs on 3.11 and on
3.14.  Expected reasons are worked by hand from the kernel's ``lstat`` of the
configured path (the design's table); expected key sets are never hard-coded
lists -- each is derived from a NON-loop call on the same field that goes through
the full assembly (an ordinary symlink, a ``<regular file>/child`` ENOTDIR path,
or a pre-check path), so the test pins "a loop is assembled like its non-loop
sibling" rather than a member list.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.scheduler import ProductionSchedulerConfig
from services.orchestrator.scheduler_config.path_modes import _config_path_preserve_final_component_for_mode

_FIELDS = ("workspace_root", "lock_root", "evidence_root")
_SHAPES = ("self_loop", "under_loop", "missing_dotdot_loop", "loop_dotdot_loop")
_SPELLINGS = ("raw", "normalised")

# Which non-loop reference a cell's key set must equal.
_SYMLINK_KEYS = "ordinary-symlink"
_LSTAT_ERRNO_KEYS = "lstat-errno"
_PRECHECK_KEYS = "component-precheck"

# (evidence_safe_paths, shape, spelling) -> (reason, key-set reference).  Worked
# by hand from the kernel, not measured from the code under test:
# * self-loop `a`: lstat succeeds on the link itself -> SYMLINK;
# * `a/child`: the kernel follows `a` as a middle component -> lstat ELOOP -> UNSAFE_PATH;
# * raw `<missing>/../a`: the kernel stops at the missing component -> ENOENT -> NOT_FOUND;
# * raw `a/../a`: the kernel follows `a` as a middle component -> ELOOP -> UNSAFE_PATH;
# * the upstream normaliser rewrites `<missing>/../a` and `a/../a` to `<ws>/a`, so their
#   normalised spelling is the self-loop again -> SYMLINK;
# * with evidence_safe_paths=True the component pre-check refuses any raw string that
#   carries `..` before any canonicalisation (the unchanged 7-key pre-check set).
_EXPECTED: dict[tuple[bool, str, str], tuple[str, str]] = {
    (False, "self_loop", "raw"): ("SYMLINK", _SYMLINK_KEYS),
    (False, "self_loop", "normalised"): ("SYMLINK", _SYMLINK_KEYS),
    (False, "under_loop", "raw"): ("UNSAFE_PATH", _LSTAT_ERRNO_KEYS),
    (False, "under_loop", "normalised"): ("UNSAFE_PATH", _LSTAT_ERRNO_KEYS),
    (False, "missing_dotdot_loop", "raw"): ("NOT_FOUND", _SYMLINK_KEYS),
    (False, "missing_dotdot_loop", "normalised"): ("SYMLINK", _SYMLINK_KEYS),
    (False, "loop_dotdot_loop", "raw"): ("UNSAFE_PATH", _LSTAT_ERRNO_KEYS),
    (False, "loop_dotdot_loop", "normalised"): ("SYMLINK", _SYMLINK_KEYS),
    (True, "self_loop", "raw"): ("SYMLINK", _SYMLINK_KEYS),
    (True, "self_loop", "normalised"): ("SYMLINK", _SYMLINK_KEYS),
    (True, "under_loop", "raw"): ("UNSAFE_PATH", _LSTAT_ERRNO_KEYS),
    (True, "under_loop", "normalised"): ("UNSAFE_PATH", _LSTAT_ERRNO_KEYS),
    (True, "missing_dotdot_loop", "raw"): ("UNSAFE_PATH", _PRECHECK_KEYS),
    (True, "missing_dotdot_loop", "normalised"): ("SYMLINK", _SYMLINK_KEYS),
    (True, "loop_dotdot_loop", "raw"): ("UNSAFE_PATH", _PRECHECK_KEYS),
    (True, "loop_dotdot_loop", "normalised"): ("SYMLINK", _SYMLINK_KEYS),
}

# The normaliser's product for each shape, relative to the workspace, worked by
# hand: it canonicalises the parent segment and keeps the final name, so the
# `..` shapes collapse onto the loop link itself.  Pinning the literal is what
# makes "the two interpreters fed byte-identical normalised strings" a checked
# fact instead of a remark in the receipt.
_NORMALISED_RELATIVE = {
    "self_loop": "a",
    "under_loop": "a/child",
    "missing_dotdot_loop": "a",
    "loop_dotdot_loop": "a",
}

# The measured 3.13 key set of a SYMLINK lock root (issue #2453, end-to-end on a
# real ProductionSchedulerConfig): the independent source for the config-level case.
_ISSUE_MEASURED_LOCK_ROOT_KEYS = frozenset(
    {
        "allow_create",
        "approved_root_required",
        "configured",
        "contained",
        "exists",
        "is_dir",
        "path",
        "symlink",
        "under_workspace",
        "writable",
    }
)


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """A canonical approved root holding a workspace with a self-loop `a` in it."""

    root = Path(os.path.realpath(tmp_path)) / "approved"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "a").symlink_to(workspace / "a")
    (workspace / "real-dir").mkdir()
    (workspace / "ref-link").symlink_to(workspace / "real-dir")
    (workspace / "regular-file").write_text("x", encoding="utf-8")
    return root, workspace


def _raw_value(workspace: Path, shape: str) -> str:
    return {
        "self_loop": f"{workspace}/a",
        "under_loop": f"{workspace}/a/child",
        "missing_dotdot_loop": f"{workspace}/missing/../a",
        "loop_dotdot_loop": f"{workspace}/a/../a",
    }[shape]


def _check(
    field_name: str,
    value: Path | str,
    *,
    root: Path,
    workspace: Path,
    evidence_safe_paths: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    # Mirrors the lock/evidence preflight's arguments for these three fields.
    return scheduler_module._scheduler_root_check(
        field_name,
        value,
        (root,),
        required=True,
        must_exist=True,
        allow_create=False,
        require_approved_root=field_name == "workspace_root",
        require_under_workspace=field_name in {"lock_root", "evidence_root"},
        workspace_root=workspace,
        evidence_safe_paths=evidence_safe_paths,
    )


def _reference_key_sets(
    field_name: str,
    *,
    root: Path,
    workspace: Path,
    evidence_safe_paths: bool,
) -> dict[str, frozenset[str]]:
    """The key set each kind of NON-loop input gets through the full assembly."""

    references = {
        _SYMLINK_KEYS: (workspace / "ref-link", "SYMLINK"),
        _LSTAT_ERRNO_KEYS: (workspace / "regular-file" / "child", "UNSAFE_PATH"),
    }
    if evidence_safe_paths:
        references[_PRECHECK_KEYS] = (f"{workspace}/real-dir/../real-dir", "UNSAFE_PATH")
    key_sets: dict[str, frozenset[str]] = {}
    for kind, (value, reason) in references.items():
        check, blocker = _check(
            field_name, value, root=root, workspace=workspace, evidence_safe_paths=evidence_safe_paths
        )
        # The references must themselves land where claimed, or the comparison is vacuous.
        assert blocker is not None and blocker["code"] == f"SCHEDULER_ROOT_{field_name.upper()}_{reason}", (
            kind,
            blocker,
        )
        key_sets[kind] = frozenset(check)
    if evidence_safe_paths:
        assert "symlink" not in key_sets[_PRECHECK_KEYS]
    assert "unsafe_reason" in key_sets[_LSTAT_ERRNO_KEYS]
    assert "unsafe_reason" not in key_sets[_SYMLINK_KEYS]
    return key_sets


@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize("field_name", _FIELDS)
@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("evidence_safe_paths", (False, True), ids=("db_backed", "evidence_safe_paths"))
def test_loop_root_check_reason_and_key_set_match_the_non_loop_assembly(
    tmp_path: Path,
    evidence_safe_paths: bool,
    shape: str,
    field_name: str,
    spelling: str,
) -> None:
    root, workspace = _layout(tmp_path)
    raw = _raw_value(workspace, shape)
    if spelling == "raw":
        value: Path | str = raw
    else:
        value = _config_path_preserve_final_component_for_mode(raw, db_free_required=evidence_safe_paths)
        assert str(value) == f"{workspace}/{_NORMALISED_RELATIVE[shape]}"
    expected_reason, key_kind = _EXPECTED[(evidence_safe_paths, shape, spelling)]
    expected_keys = _reference_key_sets(
        field_name, root=root, workspace=workspace, evidence_safe_paths=evidence_safe_paths
    )[key_kind]

    check, blocker = _check(field_name, value, root=root, workspace=workspace, evidence_safe_paths=evidence_safe_paths)

    assert blocker is not None
    assert blocker["code"] == f"SCHEDULER_ROOT_{field_name.upper()}_{expected_reason}"
    assert blocker["reason"] == expected_reason.lower()
    assert frozenset(check) == expected_keys
    if field_name in {"lock_root", "evidence_root"} and key_kind != _PRECHECK_KEYS:
        # The workspace anchor is canonicalised the same way on both interpreters,
        # so the loop, which lives inside the workspace, stays "under" it.
        assert check["under_workspace"] is True


# --- allow_create arm: published_artifact_root through the runtime-roots preflight --


def _set_root_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: Path) -> dict[str, Path]:
    base = Path(os.path.realpath(tmp_path))
    workspace = base / "workspace"
    roots = {
        "workspace_root": workspace,
        "object_store_root": base / "object-store",
        "published_root": base / "published",
        "runtime_root": base / "runtime",
        "temp_root": base / "tmp",
        "lock_root": workspace / "locks",
        "evidence_root": workspace / "evidence",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o755)
    approved = {key: roots[key] for key in ("workspace_root", "object_store_root", "published_root", "runtime_root")}
    approved["temp_root"] = roots["temp_root"]
    roots.update(overrides)
    monkeypatch.setenv("WORKSPACE_ROOT", str(roots["workspace_root"]))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(roots["published_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_RUNTIME_ROOT", str(roots["runtime_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_TEMP_ROOT", str(roots["temp_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(roots["lock_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(roots["evidence_root"]))
    # The approved set is the real directories, so an override root sits inside one of them.
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_ROOTS", os.pathsep.join(str(path) for path in approved.values()))
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_ROOTS", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_SOURCES", "gfs")
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    for key in ("NHMS_SCHEDULER_DB_FREE_REQUIRED", "NHMS_SCHEDULER_REPAIR_MISSING_FORCING"):
        monkeypatch.delenv(key, raising=False)
    return roots


def _published_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, published: Path
) -> tuple[Mapping[str, Any], list[str]]:
    _set_root_env(monkeypatch, tmp_path, published_root=published)
    preflight = scheduler_module._scheduler_runtime_root_preflight(ProductionSchedulerConfig())
    codes = [blocker["code"] for blocker in preflight["blockers"] if blocker.get("field") == "published_artifact_root"]
    return preflight["checks"]["published_artifact_root"], codes


def _published_parent(tmp_path: Path) -> Path:
    parent = Path(os.path.realpath(tmp_path)) / "published"
    parent.mkdir(parents=True, exist_ok=True)
    return parent


@pytest.mark.parametrize(
    ("shape", "reason", "reference"),
    (
        ("self_loop", "SYMLINK", "ordinary-symlink"),
        ("under_loop", "UNSAFE_PATH", "lstat-errno"),
    ),
)
def test_allow_create_published_root_loop_is_blocked_like_its_non_loop_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shape: str,
    reason: str,
    reference: str,
) -> None:
    # published_artifact_root is the one field with allow_create=True / must_exist=False,
    # i.e. the arm where the canonicaliser's non-strict fallback is taken. A loop there
    # must be blocked with the same code and key set on every interpreter.
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    parent = _published_parent(reference_dir)
    (parent / "real").mkdir()
    (parent / "link").symlink_to(parent / "real")
    (parent / "file").write_text("x", encoding="utf-8")
    reference_value = parent / "link" if reference == "ordinary-symlink" else parent / "file" / "child"
    reference_check, reference_codes = _published_check(monkeypatch, reference_dir, reference_value)
    assert reference_codes == [f"SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_{reason}"]
    assert reference_check["allow_create"] is True

    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    parent = _published_parent(loop_dir)
    (parent / "a").symlink_to(parent / "a")
    value = parent / "a" if shape == "self_loop" else parent / "a" / "child"
    check, codes = _published_check(monkeypatch, loop_dir, value)

    assert codes == [f"SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_{reason}"]
    assert frozenset(check) == frozenset(reference_check)
    assert check["allow_create"] is True


# --- config level: the issue's end-to-end reproduction -------------------------------


def test_self_loop_lock_root_reports_symlink_with_the_full_key_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = Path(os.path.realpath(tmp_path))
    lock_root = base / "workspace" / "loop-locks"
    roots = _set_root_env(monkeypatch, tmp_path, lock_root=lock_root)
    lock_root.symlink_to(lock_root)
    assert roots["lock_root"] == lock_root

    preflight = scheduler_module._scheduler_lock_evidence_root_preflight(ProductionSchedulerConfig())

    assert preflight["status"] == "blocked"
    assert [blocker["code"] for blocker in preflight["blockers"]] == ["SCHEDULER_ROOT_LOCK_ROOT_SYMLINK"]
    assert frozenset(preflight["checks"]["lock_root"]) == _ISSUE_MEASURED_LOCK_ROOT_KEYS
    assert preflight["checks"]["lock_root"]["symlink"] is True
    assert preflight["checks"]["lock_root"]["under_workspace"] is True
