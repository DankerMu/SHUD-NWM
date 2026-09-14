"""Requirement-driven tests for the forcing backfill's mutex scope (#2236).

Contract, from the scenarios "the forcing backfill observes an already-present
package under the mutex" and "the forcing backfill plan mode is marked advisory"
in `harden-copyback-mutex-residuals`:

* in `--apply`, the destination inspection, the `already_present` skip, source
  validation, copy and commit or rollback of one package happen inside ONE
  acquisition, taken once per package that reads the destination -- copied,
  skipped or failed alike; a package rejected by checksum grouping takes none;
* so a package a competitor promoted and then rolled back while holding the
  mutex is never reported `already_present`;
* plan mode acquires nothing, creates no lock file, and marks every package and
  the report `observed_under_lock: false`; `--apply` marks them `true`.

The competitor here is this same process: `flock` is per open file description,
so a second acquisition from the test thread contends with the backfill thread
exactly as another host's would. The acquisition counter is installed with
`raising=False`, so a run against the pre-change module fails on the behaviour
assertions rather than on the patch.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import (
    COPYBACK_BATCH_LOCK_NAME,
    COPYBACK_LOCK_TIMEOUT_ENV,
    CopybackLockTimeout,
    acquire_copyback_batch_lock,
    copyback_batch_lock,
    release_copyback_batch_lock,
)
from services.tile_publisher import forcing_copyback_backfill as backfill_module
from services.tile_publisher.forcing_copyback_backfill import run_backfill
from tests.test_forcing_copyback_backfill import (
    CYCLE_TIME_2,
    FORCING_KEY,
    FORCING_KEY_2,
    _base_config,
    _insert_forcing_version,
    _insert_run,
    _seed_valid_candidate,
    _write_forcing_package,
)

CYCLE_TIME_3 = datetime(2024, 6, 3, 12, tzinfo=UTC)
FORCING_KEY_3 = "forcing/gfs/2024060312/basin-1/model-1"


def _count_acquisitions(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    real_acquire = acquire_copyback_batch_lock

    def counting_acquire(root: Path | str, **kwargs: Any) -> int:
        calls.append(Path(root))
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(backfill_module, "acquire_copyback_batch_lock", counting_acquire, raising=False)
    return calls


def _package(report: dict[str, Any], object_key: str) -> dict[str, Any]:
    return next(package for package in report["packages"] if package["object_key"] == object_key)


def _seed_three_packages(tmp_path: Path) -> tuple[Path, Path, Path]:
    """FORCING_KEY already mirrored, FORCING_KEY_2 copyable, FORCING_KEY_3 checksum-inconsistent."""

    engine, db_path, object_store_root, copyback_root, _checksum, _manifest = _seed_valid_candidate(tmp_path)
    _write_forcing_package(copyback_root)

    checksum_2, _ = _write_forcing_package(object_store_root, key=FORCING_KEY_2, forcing_version_id="forcing-2")
    _insert_run(engine, run_id="run-b", forcing_version_id="forcing-2", cycle_time=CYCLE_TIME_2)
    _insert_forcing_version(
        engine, forcing_version_id="forcing-2", package_uri=f"{FORCING_KEY_2}/", checksum=checksum_2
    )

    checksum_3, _ = _write_forcing_package(object_store_root, key=FORCING_KEY_3, forcing_version_id="forcing-3")
    for run_id, forcing_version_id, checksum in (
        ("run-c", "forcing-3", checksum_3),
        ("run-d", "forcing-3b", "0" * 64),
    ):
        _insert_run(engine, run_id=run_id, forcing_version_id=forcing_version_id, cycle_time=CYCLE_TIME_3)
        _insert_forcing_version(
            engine, forcing_version_id=forcing_version_id, package_uri=f"{FORCING_KEY_3}/", checksum=checksum
        )
    return db_path, object_store_root, copyback_root


# --- EF-6: a competitor's promote-then-rollback is not reported as present ------


def test_ef6_a_package_a_competitor_promoted_and_rolled_back_is_not_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, manifest_bytes = _seed_valid_candidate(tmp_path)
    copyback_root.mkdir(parents=True)
    monkeypatch.setenv(COPYBACK_LOCK_TIMEOUT_ENV, "30")
    acquiring = threading.Event()
    real_acquire = acquire_copyback_batch_lock

    def announcing_acquire(root: Path | str, **kwargs: Any) -> int:
        acquiring.set()
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(backfill_module, "acquire_copyback_batch_lock", announcing_acquire, raising=False)
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["report"] = run_backfill(
                _base_config(
                    db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root, apply=True
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            box["error"] = error

    competitor = acquire_copyback_batch_lock(copyback_root, timeout_seconds=10)
    try:
        # The competitor promotes a complete, checksum-consistent tree into a
        # target that did not exist before ...
        _write_forcing_package(copyback_root)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 10
        while not acquiring.is_set() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        # ... then rolls it back (`backup_dir is None`) before releasing.
        shutil.rmtree(copyback_root / FORCING_KEY)
    finally:
        release_copyback_batch_lock(competitor)
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert "error" not in box
    report = box["report"]
    package = _package(report, FORCING_KEY)
    assert package["status"] != "already_present"
    assert report["already_present_checksum_consistent_count"] == 0
    # Observed after the rollback, under the mutex: absent, so it was copied.
    assert package["status"] == "copied"
    assert (copyback_root / FORCING_KEY / "forcing_package.json").read_bytes() == manifest_bytes


# --- EF-7: one acquisition per package, held across the destination read -------


def test_ef7_the_destination_read_and_the_skip_decision_happen_under_the_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, object_store_root, copyback_root = _seed_three_packages(tmp_path)
    acquisitions = _count_acquisitions(monkeypatch)
    held_during: dict[str, list[tuple[str, bool]]] = {"inspect": [], "source": []}

    def is_held() -> bool:
        try:
            probe = acquire_copyback_batch_lock(copyback_root, timeout_seconds=0.2)
        except CopybackLockTimeout:
            return True
        release_copyback_batch_lock(probe)
        return False

    real_inspect = backfill_module._inspect_existing_target
    real_validate_source = backfill_module._validate_source_package

    def probing_inspect(**kwargs: Any) -> dict[str, Any]:
        held_during["inspect"].append((kwargs["object_key"], is_held()))
        return real_inspect(**kwargs)

    def probing_validate_source(publisher: Any, refs: Any, object_key: str) -> dict[str, Any]:
        held_during["source"].append((object_key, is_held()))
        return real_validate_source(publisher, refs, object_key)

    monkeypatch.setattr(backfill_module, "_inspect_existing_target", probing_inspect)
    monkeypatch.setattr(backfill_module, "_validate_source_package", probing_validate_source)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root, apply=True)
    )

    assert _package(report, FORCING_KEY)["status"] == "already_present"
    assert _package(report, FORCING_KEY_2)["status"] == "copied"
    assert _package(report, FORCING_KEY_3)["status"] == "failed"
    assert report["checksum_mismatch_count"] == 1
    # The skipped package's destination read and decision were made holding it,
    # and so were the copyable package's destination read and source validation.
    assert held_during["inspect"] == [(FORCING_KEY, True), (FORCING_KEY_2, True)]
    assert held_during["source"] == [(FORCING_KEY_2, True)]
    # Exactly one acquisition per package that read the destination -- the
    # `already_present` one included -- and none for the checksum-rejected one.
    assert [Path(root) for root in acquisitions] == [Path(copyback_root), Path(copyback_root)]
    # Released between packages and at the end.
    freed = acquire_copyback_batch_lock(copyback_root, timeout_seconds=0.2)
    release_copyback_batch_lock(freed)


def test_ef7_a_lock_failure_is_that_packages_failure_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, object_store_root, copyback_root = _seed_three_packages(tmp_path)
    monkeypatch.setenv(COPYBACK_LOCK_TIMEOUT_ENV, "0.2")
    inspected: list[str] = []
    real_inspect = backfill_module._inspect_existing_target

    def recording_inspect(**kwargs: Any) -> dict[str, Any]:
        inspected.append(kwargs["object_key"])
        return real_inspect(**kwargs)

    monkeypatch.setattr(backfill_module, "_inspect_existing_target", recording_inspect)

    with copyback_batch_lock(copyback_root, timeout_seconds=10):
        report = run_backfill(
            _base_config(
                db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root, apply=True
            )
        )

    assert report["status"] == "completed"
    # Nothing was read from the destination without the mutex.
    assert inspected == []
    for key in (FORCING_KEY, FORCING_KEY_2):
        assert _package(report, key)["status"] == "failed"
    categories = {failure["object_key"]: failure["category"] for failure in report["failures"]}
    assert categories[FORCING_KEY] == "copyback_lock_unavailable"
    assert categories[FORCING_KEY_2] == "copyback_lock_unavailable"
    assert report["already_present_checksum_consistent_count"] == 0
    assert report["copied_count"] == 0
    assert not (copyback_root / FORCING_KEY_2).exists()


# --- EF-8: plan mode is unlocked and says so -----------------------------------


def test_ef8_plan_mode_acquires_nothing_and_marks_every_observation_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, object_store_root, copyback_root = _seed_three_packages(tmp_path)
    acquisitions = _count_acquisitions(monkeypatch)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root, apply=False)
    )

    assert acquisitions == []
    assert not (copyback_root / COPYBACK_BATCH_LOCK_NAME).exists()
    assert report["observed_under_lock"] is False
    assert len(report["packages"]) == 3
    assert [package["observed_under_lock"] for package in report["packages"]] == [False, False, False]
    assert _package(report, FORCING_KEY)["status"] == "already_present"
    assert _package(report, FORCING_KEY_2)["status"] == "copyable"


def test_ef8_apply_marks_every_observation_as_made_under_the_mutex(tmp_path: Path) -> None:
    db_path, object_store_root, copyback_root = _seed_three_packages(tmp_path)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root, apply=True)
    )

    assert report["observed_under_lock"] is True
    assert [package["observed_under_lock"] for package in report["packages"]] == [True, True, True]
