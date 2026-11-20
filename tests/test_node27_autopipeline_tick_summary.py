"""#3224: ``--summary-mode tick`` -- one digest line per tick, the full summary only when the tick is not quiet.

Expected values come from the ``node27-ingest-boundary`` delta of openspec change
``cheapen-idle-autopipe-tick``: the digest schema name and keys, the quiet
predicate and the preflight exception. The tick set-up reuses the handoff
suite's ``_prepare_autopipe`` / ``_run_main`` / ``_run_main_captured`` helpers.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_autopipeline as autopipe
import tests.test_node27_autopipeline_handoff as handoff

DIGEST_SCHEMA = "nhms.node27.ingest_tick_digest.v1"
STATE_FILE_NAME = "pipeline-job-provenance-import-state.json"
DIGEST_RUNS_KEYS = {"already_ingested", "published", "processed", "ingested", "skipped", "failed", "declined"}


def _quiet_autopipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **prepare: Any,
) -> tuple[Path, list[dict[str, Any]]]:
    """An idle tick: every run already ingested, every provenance run unchanged."""

    object_store_root, _calls, _published = handoff._prepare_autopipe(
        monkeypatch, tmp_path, runs={handoff.RUN_A: True}, **prepare
    )
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {handoff.RUN_A})
    importer_calls: list[dict[str, Any]] = []

    def unchanged_import(**kwargs: Any) -> dict[str, Any]:
        importer_calls.append(kwargs)
        return {
            "status": "imported",
            "runs": [],
            "imported": 0,
            "unavailable": 0,
            "failed": 0,
            "skipped_unchanged": len(list(kwargs["run_ids"])),
            "skip_state": "loaded",
            "skip_state_saved": True,
        }

    monkeypatch.setattr(autopipe, "import_discovered_pipeline_job_provenance", unchanged_import)
    return object_store_root, importer_calls


def _tick_stdout(capsys: pytest.CaptureFixture[str], object_store_root: Path, *extra: str) -> tuple[int, str]:
    rc, captured = handoff._run_main_captured(capsys, object_store_root, "--summary-mode", "tick", *extra)
    return rc, captured.out


def _split_tick(out: str) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    first, _newline, rest = out.partition("\n")
    digest = json.loads(first)
    return digest, first, json.loads(rest) if rest else None


def test_tick_mode_prints_exactly_one_digest_line_on_a_quiet_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root, _importer_calls = _quiet_autopipe(monkeypatch, tmp_path)

    rc, out = _tick_stdout(capsys, object_store_root)

    assert rc == 0
    assert out.endswith("\n")
    lines = out.splitlines()
    assert len(lines) == 1
    digest = json.loads(lines[0])
    assert digest["schema"] == DIGEST_SCHEMA
    assert digest["quiet"] is True
    assert digest["return_code"] == 0
    assert digest["status"] == "completed"
    assert '"declines_active": 0' in lines[0]
    assert digest["discovered_runs"] == 1
    assert set(digest["runs"]) == DIGEST_RUNS_KEYS
    assert digest["runs"]["already_ingested"] == 1
    assert digest["seed"] == {"seeded": 0, "already_seeded": 1, "failed": 0}
    assert digest["job_provenance"] == {"imported": 0, "unavailable": 0, "failed": 0, "skipped_unchanged": 1}
    assert digest["stats_guard"] == {"status": "not_triggered", "authority_status": "completed"}
    # One line, sorted keys, the default separators.
    assert lines[0] == json.dumps(digest, ensure_ascii=False, sort_keys=True)


def test_tick_mode_prints_the_full_summary_when_a_run_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(argv: list[str], _env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            return 0, "{}", ""
        if "workers.output_parser.cli" in command:
            return 1, "", "parse exploded"
        raise AssertionError(f"unexpected command: {argv}")

    object_store_root, _importer_calls = _quiet_autopipe(monkeypatch, tmp_path, command_handler=handler)
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: set())

    rc, out = _tick_stdout(capsys, object_store_root)
    digest, _line, summary = _split_tick(out)

    assert rc == 1
    assert digest["quiet"] is False
    assert digest["runs"]["failed"] == 1
    assert summary is not None
    assert summary["schema"] == autopipe.INGEST_SUMMARY_SCHEMA
    failed = summary["runs"]["failed_runs"]
    assert [entry["run_id"] for entry in failed] == [handoff.RUN_A]
    assert failed[0]["stage"] == "parse"
    assert failed[0]["error"]
    assert out.count("\n") > 2, "the full summary after the digest is pretty-printed"


@pytest.fixture
def quiet_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    """The full summary of a real quiet tick, as ``main`` builds it."""

    object_store_root, _importer_calls = _quiet_autopipe(monkeypatch, tmp_path)
    rc, summary = handoff._run_main(capsys, object_store_root)
    assert rc == 0
    assert autopipe._ingest_tick_digest(summary)["quiet"] is True
    return summary


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(summary: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target = summary
        for key in parents:
            target = target[key]
        target[leaf] = value

    return apply


@pytest.mark.parametrize(
    "cause",
    [
        pytest.param(_set("runs.declined", 1), id="declined-run"),
        pytest.param(_set("seed.seeded", ["qhh"]), id="seeded-basin"),
        pytest.param(_set("seed.failed", [{"basin": "qhh", "stage": "seed", "error": "boom"}]), id="seed-failure"),
        pytest.param(_set("runs.job_provenance.imported", 1), id="provenance-imported"),
        pytest.param(_set("runs.job_provenance.unavailable", 1), id="provenance-unavailable"),
        pytest.param(_set("runs.job_provenance.failed", 1), id="provenance-failed"),
        pytest.param(_set("declines_active", 1), id="declines-active-1"),
        pytest.param(_set("declines_active", None), id="declines-active-unknown"),
        pytest.param(_set("stats_guard.authority.status", "failed"), id="authority-failed"),
        pytest.param(
            _set("stats_guard.authority.analyzed", [{"relation": "core.x", "status": "ok"}]),
            id="authority-analyzed",
        ),
        # The remaining terms of the pinned predicate, one at a time.
        pytest.param(_set("return_code", 1), id="return-code"),
        pytest.param(_set("runs.ingested", 1), id="run-ingested"),
        pytest.param(_set("runs.skipped", 1), id="run-skipped"),
        pytest.param(_set("runs.failed", 1), id="run-failed"),
        pytest.param(
            _set("seed.activation_skipped_other_active", [{"basin": "qhh", "model_id": "m", "active_model_id": "n"}]),
            id="activation-skipped",
        ),
        pytest.param(_set("declines_active", False), id="declines-active-not-an-integer"),
        pytest.param(_set("stats_guard.status", "completed"), id="frontier-leg-ran"),
        pytest.param(_set("stats_guard.authority.deferred", [{"relation": "core.x"}]), id="authority-deferred"),
    ],
)
def test_tick_mode_is_not_quiet_for_each_single_cause(
    quiet_summary: dict[str, Any], cause: Callable[[dict[str, Any]], None]
) -> None:
    summary = copy.deepcopy(quiet_summary)
    cause(summary)

    assert autopipe._ingest_tick_digest(summary)["quiet"] is False


@pytest.mark.parametrize("extra", [(), ("--summary-mode", "full")], ids=["default", "explicit-full"])
def test_full_mode_stdout_is_one_json_document_without_a_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    object_store_root, _importer_calls = _quiet_autopipe(monkeypatch, tmp_path)

    rc, captured = handoff._run_main_captured(capsys, object_store_root, *extra)
    document = json.loads(captured.out)

    assert rc == 0
    assert document["schema"] == autopipe.INGEST_SUMMARY_SCHEMA
    assert DIGEST_SCHEMA not in captured.out
    assert captured.out == json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    provenance = document["runs"]["job_provenance"]
    assert provenance["skipped_unchanged"] == 1
    assert provenance["runs"] == []


def test_autopipeline_hands_the_importer_a_state_path_under_the_work_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root, importer_calls = _quiet_autopipe(monkeypatch, tmp_path)

    rc, _summary = handoff._run_main(capsys, object_store_root)

    assert rc == 0
    assert len(importer_calls) == 1
    assert importer_calls[0]["state_path"] == tmp_path / "autopipe-work" / STATE_FILE_NAME
    assert importer_calls[0]["connect"] is autopipe._attributed_connect
    assert list(importer_calls[0]["run_ids"]) == [handoff.RUN_A]


@pytest.mark.parametrize("blocked_by", ["missing-database-url", "database-url-argv"])
def test_preflight_blocked_summary_is_printed_in_full_in_tick_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], blocked_by: str
) -> None:
    object_store_root, importer_calls = _quiet_autopipe(monkeypatch, tmp_path)
    extra: tuple[str, ...] = ()
    if blocked_by == "missing-database-url":
        monkeypatch.delenv("DATABASE_URL")
    else:
        extra = ("--database-url", handoff.NODE27_DATABASE_URL)

    rc, out = _tick_stdout(capsys, object_store_root, *extra)
    document = json.loads(out)

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert document["status"] == "preflight_blocked"
    assert DIGEST_SCHEMA not in out
    assert out == json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert importer_calls == []


def test_tick_mode_seed_only_pass_prints_a_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root, importer_calls = _quiet_autopipe(monkeypatch, tmp_path)

    rc, out = _tick_stdout(capsys, object_store_root, "--seed-only")
    digest, _line, summary = _split_tick(out)

    assert rc == 0
    assert importer_calls == []
    assert digest["schema"] == DIGEST_SCHEMA
    assert digest["job_provenance"] == {"imported": 0, "unavailable": 0, "failed": 0, "skipped_unchanged": 0}
    if summary is not None:
        assert summary["runs"]["job_provenance"] == {"imported": 0, "unavailable": 0, "failed": 0, "runs": []}


def test_tick_mode_digest_of_the_importer_exception_fallback_is_not_quiet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root, _importer_calls = _quiet_autopipe(monkeypatch, tmp_path)

    def boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("importer exploded")

    monkeypatch.setattr(autopipe, "import_discovered_pipeline_job_provenance", boom)

    rc, out = _tick_stdout(capsys, object_store_root)
    digest, _line, summary = _split_tick(out)

    assert rc == 0
    assert digest["quiet"] is False
    assert digest["job_provenance"] == {"imported": 0, "unavailable": 0, "failed": 1, "skipped_unchanged": 0}
    assert summary is not None
    assert summary["runs"]["job_provenance"] == {
        "status": "failed",
        "imported": 0,
        "unavailable": 0,
        "failed": 1,
        "runs": [{"run_id": handoff.RUN_A, "status": "failed", "reason": "RuntimeError"}],
    }


def test_tick_mode_digest_identifies_the_ingest_plane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    password = "Pw3224DigestOnly"
    object_store_root, _importer_calls = _quiet_autopipe(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://node27_writer:{password}@127.0.0.1:55432/nhms")

    rc, out = _tick_stdout(capsys, object_store_root)
    digest, line, summary = _split_tick(out)

    assert rc == 0
    assert summary is None
    assert digest["quiet"] is True
    assert digest["ingest"]["role"] == autopipe.INGEST_ROLE
    assert digest["ingest"]["stage_shape"] == list(autopipe.INGEST_STAGE_SHAPE)
    assert digest["ingest"]["config_source"] == "pytest"
    database = digest["ingest"]["database"]
    assert database["host"] == "127.0.0.1"
    assert database["port"] == 55432
    assert database["database"] == "nhms"
    assert digest["object_store_root"] == str(object_store_root)
    assert digest["discovered_runs"] == 1
    assert digest["runs"]["processed"] == 0
    assert digest["return_code"] == 0
    assert password not in line
    assert "postgresql://" not in line
