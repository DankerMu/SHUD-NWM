"""Raw-manifest readiness is computed once per scored cycle (#3236).

Seams:

- ``source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env`` inside and
  outside ``nfs_raw_manifest_readiness_scope()``, with the underlying
  ``nfs_raw_manifest_readiness`` counted through a wrapper (3.5);
- ``ProductionScheduler._cycle_completion_status`` over the real file journal,
  driving the strict/successor branch of completion scoring (3.6);
- ``FileOrchestrationJournalRepository.candidate_state`` with cold, warm and
  bypassed sanitiser caches (3.7).

The oracle for readiness is the function outside a scope (computed on every
call, as before); for the sanitiser it is the uncached implementation.  Names
the change introduces are reached through the module object inside each test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import redaction
from services.orchestrator import public_evidence as pe
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import source_cycle_raw_manifest as raw
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.test_file_orchestration_journal import _active_job, _latest_view, _write_json
from tests.test_production_scheduler import _model, _write_db_free_raw_manifest_fixture
from tests.test_scheduler_backfill import (
    _IDENTITY_CYCLE_TIMES,
    _IDENTITY_NOW,
    _build_scheduler,
    _completion_status,
    _dt,
    _seed_completed_journal_cycle,
    _set_db_free_cold_start_env,
)
from tests.test_source_cycle_raw_manifest import _write_manifest

# The six environment variables the function reads (independent list).
_READINESS_ENV = (
    "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED",
    "NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST",
    "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT",
    "OBJECT_STORE_ROOT",
    "NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX",
    "OBJECT_STORE_PREFIX",
)
_CYCLE = datetime(2026, 6, 26, 12, tzinfo=UTC)
_OTHER_CYCLE = datetime(2026, 6, 26, 18, tzinfo=UTC)


def _clear_readiness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _READINESS_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Lane enabled over a manifest whose only raw file is missing.

    That readiness carries a nested list (``missing_file_samples``), the part
    a shared mapping would leak.
    """
    _clear_readiness_env(monkeypatch)
    root = tmp_path / "object-store"
    root.mkdir()
    _write_manifest(root)
    (root / "raw/gfs/2026062612/file-a.grib2").unlink()
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(root))
    return root


@pytest.fixture
def computed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every call of the underlying ``nfs_raw_manifest_readiness``."""

    calls: list[dict[str, Any]] = []
    original = raw.nfs_raw_manifest_readiness

    def counting(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return original(**kwargs)

    monkeypatch.setattr(raw, "nfs_raw_manifest_readiness", counting)
    return calls


# --- 3.5 scope unit tests -------------------------------------------------------


def test_scope_computes_once_and_hands_out_copies(lane: Path, computed: list[dict[str, Any]]) -> None:
    """(a)"""

    with raw.nfs_raw_manifest_readiness_scope():
        first = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        second = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        third = raw.nfs_raw_manifest_readiness_from_env("GFS", _CYCLE)
        assert len(computed) == 1
        assert first is not None and second is not None and third is not None
        assert first["reason"] == "raw_files_missing"
        assert first["missing_file_samples"] == ["raw/gfs/2026062612/file-a.grib2"]
        assert first == second == third
        assert first is not second and second is not third and first is not third
        first["missing_file_samples"].append("raw/gfs/2026062612/forged.grib2")
        fourth = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert second["missing_file_samples"] == ["raw/gfs/2026062612/file-a.grib2"]
        assert fourth["missing_file_samples"] == ["raw/gfs/2026062612/file-a.grib2"]
        assert len(computed) == 1


def test_scope_computes_again_for_another_cycle(lane: Path, computed: list[dict[str, Any]]) -> None:
    """(b) another cycle time"""

    with raw.nfs_raw_manifest_readiness_scope():
        raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        other = raw.nfs_raw_manifest_readiness_from_env("gfs", _OTHER_CYCLE)
        assert len(computed) == 2
        assert other is not None and other["cycle_time"] == "2026-06-26T18:00:00Z"
        raw.nfs_raw_manifest_readiness_from_env("gfs", _OTHER_CYCLE)
        assert len(computed) == 2


def test_scope_computes_again_for_another_source(lane: Path, computed: list[dict[str, Any]]) -> None:
    """(b) another source"""

    with raw.nfs_raw_manifest_readiness_scope():
        gfs = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        ifs = raw.nfs_raw_manifest_readiness_from_env("ifs", _CYCLE)
        assert len(computed) == 2
        assert gfs is not None and ifs is not None
        assert (gfs["source_id"], ifs["source_id"]) == ("gfs", "IFS")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED", "1"),
        ("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true"),
        ("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", None),
        ("OBJECT_STORE_ROOT", "/nonexistent/object-store"),
        ("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX", "s3://other"),
        ("OBJECT_STORE_PREFIX", "s3://other"),
    ],
)
def test_scope_computes_again_when_a_read_env_var_changes(
    lane: Path,
    computed: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str | None,
) -> None:
    """(b) a changed value of an environment variable the function reads"""

    if value is None:
        other_root = tmp_path / "other-object-store"
        other_root.mkdir()
        value = str(other_root)
    with raw.nfs_raw_manifest_readiness_scope():
        before = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert len(computed) == 1
        monkeypatch.setenv(name, value)
        after = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert len(computed) == 2
        # What the function computes for the new environment, outside reuse.
        assert after == raw.nfs_raw_manifest_readiness(**computed[-1])
        assert before is not None and after is not None


def test_outside_a_scope_every_call_computes(lane: Path, computed: list[dict[str, Any]]) -> None:
    """(c)"""

    results = [raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE) for _ in range(3)]
    assert len(computed) == 3
    assert results[0] == results[1] == results[2]


def test_a_new_scope_computes_again(lane: Path, computed: list[dict[str, Any]]) -> None:
    """(d)"""

    with raw.nfs_raw_manifest_readiness_scope():
        raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
    assert len(computed) == 1
    with raw.nfs_raw_manifest_readiness_scope():
        raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
    assert len(computed) == 2
    raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
    assert len(computed) == 3


def test_nested_scope_starts_empty_and_restores_the_outer(
    lane: Path, computed: list[dict[str, Any]]
) -> None:
    """(e)"""

    with raw.nfs_raw_manifest_readiness_scope():
        outer = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert len(computed) == 1
        with raw.nfs_raw_manifest_readiness_scope():
            inner = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
            assert len(computed) == 2
            raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
            assert len(computed) == 2
        again = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert len(computed) == 2
        assert outer == inner == again


def test_a_raised_call_is_not_remembered(lane: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(f)"""

    calls: list[dict[str, Any]] = []
    original = raw.nfs_raw_manifest_readiness

    def flaky(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise OSError("transient NFS error")
        return original(**kwargs)

    monkeypatch.setattr(raw, "nfs_raw_manifest_readiness", flaky)
    with raw.nfs_raw_manifest_readiness_scope():
        with pytest.raises(OSError, match="transient NFS error"):
            raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        second = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert len(calls) == 2
        assert second is not None and second["reason"] == "raw_files_missing"
        raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
        assert len(calls) == 2


def test_lane_off_returns_none_and_computes_nothing(
    monkeypatch: pytest.MonkeyPatch, computed: list[dict[str, Any]], tmp_path: Path
) -> None:
    """(g) -- including an unknown source id, which must not reach a key."""

    _clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(tmp_path))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED", "false")
    assert raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE) is None
    assert raw.nfs_raw_manifest_readiness_from_env("not-a-source", _CYCLE) is None
    with raw.nfs_raw_manifest_readiness_scope():
        assert raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE) is None
        assert raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE) is None
        assert raw.nfs_raw_manifest_readiness_from_env("not-a-source", _CYCLE) is None
    assert computed == []


@pytest.mark.parametrize("missing_file", [False, True], ids=["ready", "one_file_missing"])
def test_scoped_result_equals_unscoped_result_for_a_real_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_file: bool
) -> None:
    """(h)"""

    _clear_readiness_env(monkeypatch)
    root = tmp_path / "object-store"
    root.mkdir()
    _write_manifest(
        root,
        entries=[
            {"local_key": "raw/gfs/2026062612/file-a.grib2", "variable": "prcp", "forecast_hour": 0},
            {"local_key": "raw/gfs/2026062612/file-b.grib2", "variable": "prcp", "forecast_hour": 3},
        ],
    )
    if missing_file:
        (root / "raw/gfs/2026062612/file-b.grib2").unlink()
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(root))

    unscoped = raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE)
    with raw.nfs_raw_manifest_readiness_scope():
        scoped = [raw.nfs_raw_manifest_readiness_from_env("gfs", _CYCLE) for _ in range(2)]
    assert unscoped is not None
    assert scoped == [unscoped, unscoped]
    assert unscoped["status"] == ("invalid" if missing_file else "ready")
    assert unscoped["required"] is True


# --- 3.6 scoring on the real file journal and scheduler wiring -------------------

_MODELS = ("model_a", "model_b", "model_c")
_TARGET = "2026-05-21T00:00:00Z"
_SECOND = "2026-05-21T06:00:00Z"


@pytest.fixture
def scoring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[Any, list[dict[str, Any]], list[object]]]:
    _set_db_free_cold_start_env(monkeypatch, tmp_path)
    journal_root = tmp_path / "journal"
    for cycle_time in (_TARGET, _SECOND):
        for model_id in _MODELS:
            _seed_completed_journal_cycle(
                journal_root, cycle_time=cycle_time, init_state_id=None, model_id=model_id
            )
    raw_root = Path(os.environ["NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT"])
    assert raw_root.is_dir()
    for cycle_time in (_TARGET, _SECOND):
        _write_db_free_raw_manifest_fixture({"object_store_root": raw_root}, cycle_time=_dt(cycle_time))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED", "true")
    scheduler = _build_scheduler(
        tmp_path,
        db_free=True,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=FileOrchestrationJournalRepository(journal_root),
        models=[_model(model_id, f"basin_{model_id[-1]}") for model_id in _MODELS],
    )
    # The production branch: strict provider None, successor ready.
    monkeypatch.setattr(scheduler, "_strict_warm_start_for_candidate", lambda _candidate, _cycle: None)
    monkeypatch.setattr(
        scheduler, "_successor_warm_start_state_for_candidate", lambda _candidate, _cycle: {"ready": True}
    )
    calls: list[dict[str, Any]] = []
    original = raw.nfs_raw_manifest_readiness

    def counting(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return original(**kwargs)

    monkeypatch.setattr(raw, "nfs_raw_manifest_readiness", counting)
    states: list[object] = []
    decide: Callable[..., object] = scheduler_module._candidate_state_decision

    def spy(candidate: object, state: object, **kwargs: object) -> object:
        states.append(state)
        return decide(candidate, state, **kwargs)

    monkeypatch.setattr(scheduler_module, "_candidate_state_decision", spy)
    yield scheduler, calls, states
    pe._clear_sanitiser_caches()


def test_completion_scoring_computes_readiness_once_per_cycle(
    scoring: tuple[Any, list[dict[str, Any]], list[object]],
) -> None:
    scheduler, calls, states = scoring

    assert _completion_status(scheduler, _TARGET) == "complete"
    assert len(states) == 3, "the strict/successor branch did not read candidate states"
    assert len(calls) == 1
    readiness = [state["nfs_raw_manifest"] for state in states]  # type: ignore[index]
    assert readiness[0] is not None and readiness[0]["status"] == "ready"
    assert readiness[0] == readiness[1] == readiness[2]
    assert readiness[0] is not readiness[1] and readiness[1] is not readiness[2]

    assert _completion_status(scheduler, _SECOND) == "complete"
    assert len(calls) == 2
    assert len(states) == 6

    # A candidate-state read outside completion scoring computes on its own and
    # carries the same readiness as the scored states (no reuse involved).
    repository = scheduler.active_repository
    target = _dt(_TARGET)
    direct = repository.candidate_state(
        source_id="gfs",
        cycle_time=target,
        model_id="model_b",
        run_id=f"fcst_gfs_{target:%Y%m%d%H}_model_b",
        forcing_version_id=f"forc_gfs_{target:%Y%m%d%H}_model_b",
        candidate_id=f"gfs:{target.isoformat()}:model_b:forecast_gfs_deterministic",
    )
    assert len(calls) == 3
    assert direct is not None and direct["nfs_raw_manifest"] == readiness[0]


# --- 3.7 state parity on the real file journal ----------------------------------


def test_candidate_state_is_the_same_with_cold_warm_and_bypassed_caches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = f"{cycle_time:%Y%m%d%H}"
    run_id = f"fcst_gfs_{stamp}_model_a"
    object_root = tmp_path / "object-store"
    _write_json(
        object_root / f"runs/{run_id}/input/manifest.json",
        {
            "initial_state": {
                "state_id": "state_gfs_model_a_2026062800",
                "quality": "fresh",
                "state_uri": "s3://nhms/states/model_a/2026062800.tar",
            }
        },
    )
    _clear_readiness_env(monkeypatch)
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    job = {
        **_active_job(cycle_time),
        "run_id": run_id,
        "status": "failed",
        "error_message": "upload failed: Authorization: Bearer abcdef0123456789 at /srv/nhms/ws",
        "details": {
            "workspace": "/srv/nhms/workspace/run_1",
            "artifact": "s3://nhms/runs/run_1/out.nc",
            "note": "see s3://nhms/runs/run_1 for details",
        },
    }
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[job])
    latest["hydro_run"]["output_uri"] = "s3://nhms/runs/run_1/output/"
    latest["hydro_run"]["log_uri"] = "published://logs/GFS/2026062800/run_1/job.out"
    journal_root = tmp_path / "journal"
    _write_json(journal_root / f"latest/gfs/{stamp}/model_a.json", latest)

    def read() -> dict[str, Any] | None:
        # A fresh repository per read: the journal's own memo is not the subject.
        return FileOrchestrationJournalRepository(journal_root).candidate_state(
            source_id="gfs",
            cycle_time=cycle_time,
            model_id="model_a",
            run_id=run_id,
            forcing_version_id=f"forc_gfs_{stamp}_model_a",
            candidate_id=f"gfs:{cycle_time.isoformat()}:model_a:forecast_gfs_deterministic",
        )

    def infos() -> tuple[Any, Any, Any]:
        return (
            pe._public_text_cache.cache_info(),
            pe._public_scalar_cache.cache_info(),
            pe._sensitive_key_cache.cache_info(),
        )

    pe._clear_sanitiser_caches()
    try:
        cold = read()
        after_cold = infos()
        warm = read()
        after_warm = infos()
        with monkeypatch.context() as patch:
            patch.setattr(pe, "_sanitize_public_text", pe._sanitize_public_text_uncached)
            patch.setattr(pe, "_sanitize_public_scalar", pe._sanitize_public_scalar_uncached)
            patch.setattr(pe, "_is_sensitive_key", redaction.is_sensitive_key)
            bypassed = read()
        after_bypass = infos()
    finally:
        pe._clear_sanitiser_caches()

    # The warm read was served by the caches; the bypassed read touched none.
    assert all(warm_info.hits > cold_info.hits for warm_info, cold_info in zip(after_warm, after_cold, strict=True))
    assert after_bypass == after_warm

    assert cold is not None
    assert cold == warm == bypassed
    rendered = json.dumps(cold, sort_keys=True)
    # The sanitiser did change this state, so parity is not vacuous.
    assert "abcdef0123456789" not in rendered
    assert "/srv/nhms" not in rendered
    assert "s3://nhms/runs/run_1" not in rendered
    assert "[object-uri]" in rendered
    assert "[local-path]" in rendered
    assert cold["hydro_run"]["output_uri"] == "[object-uri]"
    assert cold["hydro_run"]["log_uri"] == "[object-uri]"
    assert cold["run_manifest_initial_state"]["state_uri"] == "[object-uri]"
    assert cold["run_manifest_initial_state"]["state_id"] == "state_gfs_model_a_2026062800"
