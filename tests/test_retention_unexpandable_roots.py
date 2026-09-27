"""Object-store retention refuses an unexpandable ``~user`` root with a typed skip (#2596).

``Path("~nosuchuser/...").expanduser()`` raises an errno-less ``RuntimeError``.
Before this change it escaped ``_sanitize_root_candidate`` and the published
protected root: the ``cleanup`` CLI printed a traceback and exited 1, and one bad
additional root turned every scheduler-pass sweep -- the valid primary included --
into ``status: error``.  Now:

- an unexpandable primary is skipped as ``primary_root_unexpandable``;
- an unexpandable additional root is skipped as ``extra_root_unexpandable`` and
  drops only itself;
- an unexpandable published root is recorded as ``published_root_unexpandable``
  and the pass plans nothing, because no deletion may run without its
  protection root.

The ``cleanup`` entrypoints print the structured result and exit 0, the way a
``primary_root_not_absolute`` rejection already surfaces.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from services.orchestrator import cli
from services.orchestrator.retention import (
    EXTRA_ROOT_UNEXPANDABLE_REASON,
    PRIMARY_ROOT_UNEXPANDABLE_REASON,
    PUBLISHED_ROOT_UNEXPANDABLE_REASON,
    RUNS_PREFIX,
    RetentionConfig,
    run_retention,
)
from tests.retention_test_helpers import EXTRA_CONFIG, NOW, _cycle_name, _pass_scheduler, _seed_pass_env, _write

# A user name no test host has, so ``expanduser()`` can not find a home for it.
_NO_USER = "~nosuchuser_nhms_2596"
_DELETING = RetentionConfig(enabled=True, dry_run=False, retention_days=14)
_AGED = _cycle_name(NOW - timedelta(days=20))


def _real(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


def _store_with_an_aged_cycle(tmp_path: Path) -> Path:
    store = _real(tmp_path) / "object-store"
    _write(store, f"raw/gfs/{_AGED}/gfs.f000.nc")
    return store


def _triples(entries: list[dict]) -> list[tuple[str, str, str]]:
    return [(entry["key"], entry["root"], entry["reason"]) for entry in entries]


def test_the_tokens_are_the_documented_spellings() -> None:
    assert PRIMARY_ROOT_UNEXPANDABLE_REASON == "primary_root_unexpandable"
    assert EXTRA_ROOT_UNEXPANDABLE_REASON == "extra_root_unexpandable"
    assert PUBLISHED_ROOT_UNEXPANDABLE_REASON == "published_root_unexpandable"


def test_an_unexpandable_primary_root_is_a_typed_skip(tmp_path: Path) -> None:
    raw = f"{_NO_USER}/store"

    result = run_retention(object_store_root=raw, now=NOW, config=_DELETING)

    assert _triples(result.skipped) == [("", raw, PRIMARY_ROOT_UNEXPANDABLE_REASON)]
    assert (result.planned, result.deleted, result.failed) == ([], [], [])


def test_an_unexpandable_additional_root_drops_only_itself(tmp_path: Path) -> None:
    store = _store_with_an_aged_cycle(tmp_path)
    raw = f"{_NO_USER}/workspace"

    control = run_retention(object_store_root=str(store), now=NOW, config=EXTRA_CONFIG, runs_only_roots=())
    result = run_retention(object_store_root=str(store), now=NOW, config=EXTRA_CONFIG, runs_only_roots=(raw,))

    assert control.planned and [entry["key"] for entry in control.planned] == [f"raw/gfs/{_AGED}"]
    assert result.planned == control.planned
    assert _triples(result.skipped) == [*_triples(control.skipped), (RUNS_PREFIX, raw, EXTRA_ROOT_UNEXPANDABLE_REASON)]
    assert result.extra_roots == []


def test_an_unexpandable_published_root_plans_and_deletes_nothing(tmp_path: Path) -> None:
    store = _store_with_an_aged_cycle(tmp_path)
    published = f"{_NO_USER}/published"
    extra = f"{_NO_USER}/workspace"
    config = replace(EXTRA_CONFIG, dry_run=False)

    result = run_retention(
        object_store_root=str(store),
        now=NOW,
        config=config,
        published_artifact_root=published,
        runs_only_roots=(extra,),
    )

    assert result.planned == []
    assert result.deleted == []
    assert result.freed_bytes == 0
    # Both typed skips reach the receipt: the protection root's own, and the
    # extra root's, which the early return must not lose.
    assert _triples(result.skipped) == [
        ("", published, PUBLISHED_ROOT_UNEXPANDABLE_REASON),
        (RUNS_PREFIX, extra, EXTRA_ROOT_UNEXPANDABLE_REASON),
    ]
    assert (store / f"raw/gfs/{_AGED}/gfs.f000.nc").exists()


def test_an_unexpandable_copyback_root_with_the_gate_off_does_not_stop_the_sweep(tmp_path: Path) -> None:
    """The copyback lock root is sanitised on every non-dry run, whatever the extra-roots gate says."""

    store = _store_with_an_aged_cycle(tmp_path)

    result = run_retention(
        object_store_root=str(store),
        now=NOW,
        config=_DELETING,
        runs_only_roots=(f"{_NO_USER}/copyback",),
        copyback_root=f"{_NO_USER}/copyback",
    )

    assert [entry["key"] for entry in result.deleted] == [f"raw/gfs/{_AGED}"]
    assert result.skipped == []
    assert not (store / f"raw/gfs/{_AGED}").exists()


@pytest.fixture
def cleanup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = _real(tmp_path) / "workspace"
    evidence_dir = workspace / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(evidence_dir))
    monkeypatch.setenv("OBJECT_STORE_ROOT", f"{_NO_USER}/store")
    for name in (
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS",
        "NHMS_RETENTION_EXTRA_ROOTS_ENABLED",
        "NHMS_RETENTION_EXTRA_ROOTS_DAYS",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    return evidence_dir


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_the_cleanup_cli_reports_an_unexpandable_primary_without_a_traceback(
    cleanup_env: Path, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    run = cli._click_main if entrypoint == "click" else cli._argparse_main

    rc = run(["cleanup", "--retention-days", "14", "--execute"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "Traceback" not in captured.err
    payload = json.loads(captured.out.strip())
    assert _triples(payload["skipped"]) == [("", f"{_NO_USER}/store", PRIMARY_ROOT_UNEXPANDABLE_REASON)]
    assert payload["deleted"] == []


def test_the_pass_reports_an_unexpandable_raw_primary_as_completed(tmp_path: Path, monkeypatch) -> None:
    """db-free config: the raw constructor value reaches retention un-normalized."""

    _seed_pass_env(monkeypatch)
    workspace = _real(tmp_path) / "ws"
    (workspace / "journal").mkdir(parents=True)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    raw = f"{_NO_USER}/store"

    scheduler = _pass_scheduler(
        workspace_root=str(workspace),
        object_store_root=raw,
        scheduler_db_free_required=True,
        scheduler_journal_root=str(workspace / "journal"),
    )
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert _triples(payload["skipped"]) == [("", raw, PRIMARY_ROOT_UNEXPANDABLE_REASON)]
    assert payload["planned"] == []


def test_the_pass_sweeps_the_valid_primary_past_an_unexpandable_copyback_root(tmp_path: Path, monkeypatch) -> None:
    _seed_pass_env(monkeypatch)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    store = _store_with_an_aged_cycle(tmp_path)
    copyback = f"{_NO_USER}/copyback"

    scheduler = _pass_scheduler(
        workspace_root=str(_real(tmp_path) / "ws"),
        object_store_root=str(store),
        object_store_copyback_root=copyback,
    )
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert _triples(payload["skipped"]) == [(RUNS_PREFIX, copyback, EXTRA_ROOT_UNEXPANDABLE_REASON)]
    assert [entry["key"] for entry in payload["deleted"]] == [f"raw/gfs/{_AGED}"]
    assert not (store / f"raw/gfs/{_AGED}").exists()
