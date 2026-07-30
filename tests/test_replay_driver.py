"""Tests for the serial six-basin replay driver (#1164 change 2, tasks.md 3.3).

Covered, one test per required property:

* receipt row completeness -- the forcing/model package checksums are recorded
  UNCONDITIONALLY, including on a row with no prior run;
* the first replayed cycle's new half must be the packaged-IC bootstrap shape,
  otherwise the driver halts;
* key drift (``river_network_version_id`` / variable key set) halts the driver;
* the wait condition really is "state index entry created after this pass
  started" -- a stale entry with the right ``valid_time`` does not satisfy it;
* resumption never blind-skips: a recorded row is skipped only when the live
  index still carries the recorded checksum;
* the repair parameter set is switched on for GFS 2026070712 and nothing else;
* the prior state fields come from the reset receipt; a receipt that cannot
  supply them is a refusal, never a silent null.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import replay_driver
from scripts.replay_capture import ReplayDriverRefused
from scripts.replay_driver import (
    REPAIR_CYCLE_ENV,
    REPAIR_ENV,
    ReplayDriverConfig,
    ReplayHalted,
    run_replay,
    submit_env,
)

SOURCE = "IFS"
MODELS = ("dg_alpha", "dg_beta")
CYCLE_1 = datetime(2026, 7, 5, 0, tzinfo=UTC)
CYCLE_2 = datetime(2026, 7, 5, 12, tzinfo=UTC)
BASE_TIME = datetime(2026, 8, 1, tzinfo=UTC)
BASIN_VERSION = "bv-2026a"

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas/production_replay_replacement_receipt.schema.json").read_text(
        encoding="utf-8"
    )
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _manifest(
    *,
    init_mode: int = 3,
    quality: str = "packaged_calibrated_state",
    packaged_ic_checksum: str = "sha256:ic",
    river_network_version_id: str = "rn-2026a",
    variables: Sequence[str] = ("discharge", "stage"),
    marker: str = "prior",
) -> dict[str, Any]:
    return {
        "marker": marker,
        "model": {
            "river_network_version_id": river_network_version_id,
            "model_package_checksum": "sha256:model",
        },
        "runtime": {"init_mode": init_mode},
        "initial_state": {
            "state_id": f"state-{marker}",
            "checksum": f"sha256:{marker}",
            "quality": quality,
            "packaged_ic_checksum": packaged_ic_checksum,
        },
        "outputs": {"variables": {name: {"unit": "m3/s"} for name in variables}},
    }


class _Site:
    """A throwaway node-22-shaped object store plus the driver's inputs."""

    def __init__(self, tmp_path: Path, *, cycles: Sequence[datetime]) -> None:
        self.root = tmp_path
        self.nfs_root = tmp_path / "nfs"
        self.scratch_root = tmp_path / "scratch"
        self.state_index = tmp_path / "index" / "index-last.json"
        self.reset_receipt = tmp_path / "reset-receipt.json"
        self.receipt_path = tmp_path / "replacement-receipt.json"
        self.journal_root = tmp_path / "journal"
        self.cycles = tuple(cycles)
        self.journal_root.mkdir(parents=True, exist_ok=True)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self._write_index([])
        self.write_reset_receipt()

    # -- object store ------------------------------------------------------

    def run_root(self, cycle: datetime, model_id: str) -> Path:
        run_id = f"fcst_{SOURCE.lower()}_{cycle.strftime('%Y%m%d%H')}_{model_id}"
        return self.nfs_root / "runs" / run_id

    def write_run(self, cycle: datetime, model_id: str, manifest: Mapping[str, Any]) -> None:
        root = self.run_root(cycle, model_id)
        _write_json(root / "input" / "manifest.json", manifest)
        (root / "output").mkdir(parents=True, exist_ok=True)
        (root / "output" / "discharge.csv").write_text(
            f"{manifest.get('marker')},{cycle.isoformat()},{model_id}\n", encoding="utf-8"
        )

    def write_forcing(self, cycle: datetime, model_id: str) -> Path:
        package = (
            self.nfs_root
            / "forcing"
            / SOURCE.lower()
            / cycle.strftime("%Y%m%d%H")
            / BASIN_VERSION
            / model_id
        )
        package.mkdir(parents=True, exist_ok=True)
        (package / "forcing.csv").write_text(f"{model_id}:{cycle.isoformat()}\n", encoding="utf-8")
        (package / "manifest.json").write_text(json.dumps({"model_id": model_id}), encoding="utf-8")
        return package

    def populate(self, *, manifest_marker: str = "prior") -> None:
        for cycle in self.cycles:
            for model_id in MODELS:
                self.write_run(cycle, model_id, _manifest(marker=manifest_marker))
                self.write_forcing(cycle, model_id)

    # -- state index -------------------------------------------------------

    def _write_index(self, entries: Sequence[Mapping[str, Any]]) -> None:
        _write_json(
            self.state_index,
            {
                "schema_version": "nhms.scheduler.file_state_snapshot_index.v1",
                "generated_at": _format_time(BASE_TIME),
                "entries": [dict(entry) for entry in entries],
            },
        )

    def index_entries(self) -> list[dict[str, Any]]:
        payload = json.loads(self.state_index.read_text(encoding="utf-8"))
        return list(payload["entries"])

    def add_state_entries(self, cycle: datetime, *, created_at: datetime, checksum_prefix: str) -> None:
        entries = self.index_entries()
        valid_time = cycle + timedelta(hours=12)
        for model_id in MODELS:
            entries.append(
                {
                    "model_id": model_id,
                    "source_id": SOURCE,
                    "valid_time": _format_time(valid_time),
                    "created_at": _format_time(created_at),
                    "state_id": f"{checksum_prefix}-{model_id}-{cycle.strftime('%Y%m%d%H')}",
                    "checksum": f"sha256:{checksum_prefix}-{model_id}-{cycle.strftime('%Y%m%d%H')}",
                }
            )
        self._write_index(entries)

    # -- reset receipt -----------------------------------------------------

    def write_reset_receipt(self, *, removed: Sequence[Mapping[str, Any]] | None = None) -> None:
        if removed is None:
            removed = [
                {
                    "model_id": model_id,
                    "source_id": SOURCE,
                    "valid_time": _format_time(cycle + timedelta(hours=12)),
                    "created_at": _format_time(cycle),
                    "state_id": f"old-{model_id}-{cycle.strftime('%Y%m%d%H')}",
                    "checksum": f"sha256:old-{model_id}-{cycle.strftime('%Y%m%d%H')}",
                }
                for cycle in self.cycles
                for model_id in MODELS
            ]
        _write_json(
            self.reset_receipt,
            {
                "schema_version": "nhms.replay_state_scope_reset.v1",
                "outcome": "completed",
                "enforced": True,
                "lanes": [
                    {"lane": "scratch", "removed_entries": [dict(entry) for entry in removed]},
                    {"lane": "nfs", "removed_entries": [dict(entry) for entry in removed]},
                ],
            },
        )

    # -- driver config -----------------------------------------------------

    def config(self, **overrides: Any) -> ReplayDriverConfig:
        kwargs: dict[str, Any] = {
            "source_id": SOURCE,
            "model_ids": MODELS,
            "cycles": self.cycles,
            "nfs_root": self.nfs_root,
            "scratch_root": self.scratch_root,
            "state_index": self.state_index,
            "journal_root": self.journal_root,
            "reset_receipt": self.reset_receipt,
            "receipt_path": self.receipt_path,
            "env": {"NHMS_RETENTION_ENABLED": "false"},
        }
        kwargs.update(overrides)
        return ReplayDriverConfig(**kwargs)


class _Clock:
    def __init__(self, *, step_seconds: float = 1.0) -> None:
        self.now = BASE_TIME
        self.step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self.now
        self.now = self.now + self.step
        return current


class _SubmitRecorder:
    """Fake pass: records the invocation and mutates the site like a real pass."""

    def __init__(
        self,
        site: _Site,
        *,
        new_manifest: Mapping[str, Any] | None = None,
        entry_created_at: datetime | None = None,
        returncode: int = 0,
        publish_entries: bool = True,
    ) -> None:
        self.site = site
        self.new_manifest = new_manifest
        self.entry_created_at = entry_created_at
        self.returncode = returncode
        self.publish_entries = publish_entries
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((list(argv), dict(env)))
        cycle = _cycle_from_argv(argv)
        if self.returncode == 0:
            manifest = dict(self.new_manifest or _manifest(marker="replayed"))
            for model_id in MODELS:
                self.site.write_run(cycle, model_id, manifest)
            if self.publish_entries:
                self.site.add_state_entries(
                    cycle,
                    created_at=self.entry_created_at or (BASE_TIME + timedelta(hours=1)),
                    checksum_prefix="new",
                )
        return {"returncode": self.returncode, "stdout_tail": "", "stderr_tail": ""}


def _cycle_from_argv(argv: Sequence[str]) -> datetime:
    index = list(argv).index("--cycle-time")
    return datetime.fromisoformat(str(argv[index + 1]).replace("Z", "+00:00"))


def _all_terminal(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, bool]:
    return dict.fromkeys(config.model_ids, True)


def _refuse_to_submit(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
    raise AssertionError(f"a dry run must not submit anything (argv={list(argv)})")


@pytest.fixture
def site(tmp_path: Path) -> _Site:
    fixture = _Site(tmp_path, cycles=(CYCLE_1, CYCLE_2))
    fixture.populate()
    return fixture


# ---------------------------------------------------------------------------
# receipt completeness
# ---------------------------------------------------------------------------


def test_dry_run_plans_every_row_submits_nothing_and_matches_the_schema(site: _Site) -> None:
    receipt = run_replay(
        site.config(),
        submit_pass=_refuse_to_submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["executed"] is False
    assert receipt["outcome"] == "completed"
    assert len(receipt["rows"]) == len(site.cycles) * len(MODELS)
    assert {row["status"] for row in receipt["rows"]} == {"planned"}
    assert all(row["staging"]["status"] == "stage_planned" for row in receipt["rows"])
    assert all(row["submission"] is None for row in receipt["rows"])
    # zero mutation: no forcing was copied into scratch, no run tree touched
    assert not (site.scratch_root / "forcing").exists()
    assert json.loads(site.receipt_path.read_text(encoding="utf-8"))["rows"] == receipt["rows"]


def test_input_checksums_are_recorded_even_on_a_row_with_no_prior_run(tmp_path: Path) -> None:
    """tasks.md 3.3: input checksums are unconditional, not "when a run exists"."""

    fixture = _Site(tmp_path, cycles=(CYCLE_1,))
    for model_id in MODELS:
        fixture.write_forcing(CYCLE_1, model_id)  # forcing only: the GFS 070712 shape
    registry = tmp_path / "registry.json"
    package_dir = fixture.nfs_root / "models" / "dg_alpha" / "v1"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "model.tar").write_text("model-bytes", encoding="utf-8")
    _write_json(
        registry,
        {"models": [{"model_id": "dg_alpha", "model_package_uri": "models/dg_alpha/v1"}]},
    )

    receipt = run_replay(
        fixture.config(registry_manifest=registry),
        submit_pass=_refuse_to_submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    rows = {row["model_id"]: row for row in receipt["rows"]}
    for row in rows.values():
        assert row["prior"]["no_prior_run"] is True
        assert row["prior"]["run_manifest_sha256"] is None
        assert row["inputs"]["forcing_package"]["status"] == "present"
        assert row["inputs"]["forcing_package"]["sha256"] is not None
    assert rows["dg_alpha"]["inputs"]["model_package"]["status"] == "present"
    assert rows["dg_alpha"]["inputs"]["model_package"]["sha256"] is not None


def test_inventory_census_is_taken_on_site(site: _Site) -> None:
    receipt = run_replay(
        site.config(),
        submit_pass=_refuse_to_submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    census = receipt["inventory_census"]
    assert census["frontier_cycle"] == CYCLE_2.strftime("%Y%m%d%H")
    assert {scope["model_id"] for scope in census["scopes"]} == set(MODELS)
    for scope in census["scopes"]:
        assert scope["run_cycles"] == [cycle.strftime("%Y%m%d%H") for cycle in site.cycles]
        # the scope was cleared before the replay: the live index holds nothing
        assert scope["state_index_entry_count"] == 0


# ---------------------------------------------------------------------------
# prior state provenance
# ---------------------------------------------------------------------------


def test_prior_state_comes_from_the_reset_receipt_not_the_cleared_index(site: _Site) -> None:
    receipt = run_replay(
        site.config(),
        submit_pass=_refuse_to_submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    row = next(row for row in receipt["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H"))
    assert row["prior"]["state_source"] == "reset_receipt"
    assert row["prior"]["state"]["source"] == "reset_receipt"
    assert row["prior"]["state"]["state_id"] == f"old-{row['model_id']}-{CYCLE_1.strftime('%Y%m%d%H')}"
    assert row["prior"]["state"]["checksum"].startswith("sha256:old-")
    assert receipt["reset_receipt"]["removed_entry_count"] == len(site.cycles) * len(MODELS)


def test_a_reset_receipt_without_removed_entries_refuses_instead_of_recording_nulls(site: _Site) -> None:
    site.write_reset_receipt(removed=[])

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(),
            submit_pass=_refuse_to_submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_has_no_removed_entries"
    assert not site.receipt_path.exists()


def test_a_missing_reset_receipt_refuses(site: _Site) -> None:
    site.reset_receipt.unlink()

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(),
            submit_pass=_refuse_to_submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_unreadable"


def test_retention_must_be_disabled_before_anything_runs(site: _Site) -> None:
    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(env={"NHMS_RETENTION_ENABLED": "true"}),
            submit_pass=_refuse_to_submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "retention_not_disabled"
    assert not site.receipt_path.exists()


# ---------------------------------------------------------------------------
# execution, staging and halting
# ---------------------------------------------------------------------------


def test_execute_stages_forcing_and_completes_every_row(site: _Site) -> None:
    submit = _SubmitRecorder(site)

    receipt = run_replay(
        site.config(execute=True),
        submit_pass=submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    assert {row["status"] for row in receipt["rows"]} == {"completed"}
    assert [_cycle_from_argv(argv) for argv, _env in submit.calls] == list(site.cycles)
    assert all("--max-passes" not in argv for argv, _env in submit.calls)
    for row in receipt["rows"]:
        assert row["staging"]["status"] == "staged"
        assert row["staging"]["verified"] is True
        assert row["new"]["run_manifest_sha256"] != row["prior"]["run_manifest_sha256"]
        assert row["new"]["state_checksum"].startswith("sha256:new-")
        assert row["key_consistency"]["status"] == "consistent"
    staged = site.scratch_root / "forcing" / SOURCE.lower() / CYCLE_1.strftime("%Y%m%d%H") / BASIN_VERSION
    assert (staged / MODELS[0] / "forcing.csv").read_text(encoding="utf-8") == (
        f"{MODELS[0]}:{CYCLE_1.isoformat()}\n"
    )


def test_first_cycle_without_the_bootstrap_shape_halts_the_driver(site: _Site) -> None:
    submit = _SubmitRecorder(
        site,
        new_manifest=_manifest(
            marker="replayed",
            init_mode=1,
            quality="cold_start",
            packaged_ic_checksum="",
        ),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "halted"
    assert receipt["interruption"]["reason"] == "first_cycle_bootstrap_assertion_failed"
    assert receipt["interruption"]["cycle"] == CYCLE_1.strftime("%Y%m%d%H")
    # halted at the FIRST cycle: the second cycle was never submitted
    assert len(submit.calls) == 1
    assert json.loads(site.receipt_path.read_text(encoding="utf-8"))["outcome"] == "halted"


def test_key_drift_halts_the_driver(site: _Site) -> None:
    submit = _SubmitRecorder(
        site,
        new_manifest=_manifest(marker="replayed", river_network_version_id="rn-2027-DRIFT"),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    drifted = [row for row in receipt["rows"] if row["key_consistency"] is not None]
    assert drifted and all(row["key_consistency"]["status"] == "drift" for row in drifted)
    assert len(submit.calls) == 1


def test_variable_key_drift_also_halts_the_driver(site: _Site) -> None:
    submit = _SubmitRecorder(
        site,
        new_manifest=_manifest(marker="replayed", variables=("discharge",)),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.receipt["interruption"]["reason"] == "key_consistency_drift"


def test_submission_failure_halts_with_the_interruption_recorded(site: _Site) -> None:
    submit = _SubmitRecorder(site, returncode=7)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "submission_failed"
    assert receipt["interruption"]["detail"]["returncode"] == 7
    assert {row["status"] for row in receipt["rows"]} == {"halted"}
    assert len(submit.calls) == 1


# ---------------------------------------------------------------------------
# convergence gate
# ---------------------------------------------------------------------------


def test_convergence_requires_index_entries_created_after_the_pass_started(site: _Site) -> None:
    """A pre-existing entry with the right valid_time must NOT satisfy the wait."""

    site.add_state_entries(CYCLE_1, created_at=BASE_TIME - timedelta(days=1), checksum_prefix="stale")
    submit = _SubmitRecorder(site, publish_entries=False)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    # the journal was terminal and the entry exists -- only its age blocked it
    assert receipt["interruption"]["detail"]["journal_terminal"] == sorted(MODELS)
    assert receipt["interruption"]["detail"]["state_entries"] == []
    assert len(site.index_entries()) == len(MODELS)
    assert all(row["convergence"]["state_entry_present"] is False for row in receipt["rows"])


def test_convergence_accepts_entries_created_after_the_pass_started(site: _Site) -> None:
    site.add_state_entries(CYCLE_1, created_at=BASE_TIME - timedelta(days=1), checksum_prefix="stale")
    submit = _SubmitRecorder(site, entry_created_at=BASE_TIME + timedelta(hours=1))

    receipt = run_replay(
        site.config(execute=True, cycle_timeout_seconds=0),
        submit_pass=submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert receipt["outcome"] == "completed"
    first_cycle_rows = [row for row in receipt["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")]
    assert all(row["convergence"]["state_entry_present"] is True for row in first_cycle_rows)
    assert all(row["new"]["state_checksum"].startswith("sha256:new-") for row in first_cycle_rows)


def test_a_non_terminal_journal_halts_even_when_the_index_is_fresh(site: _Site) -> None:
    submit = _SubmitRecorder(site)

    def _one_model_pending(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, bool]:
        return {MODELS[0]: True, MODELS[1]: False}

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=_one_model_pending,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.receipt["interruption"]["reason"] == "convergence_timeout"


# ---------------------------------------------------------------------------
# resumption
# ---------------------------------------------------------------------------


def _completed_run(site: _Site) -> dict[str, Any]:
    return run_replay(
        site.config(execute=True),
        submit_pass=_SubmitRecorder(site),
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )


def test_resume_skips_only_cycles_whose_state_evidence_still_matches(site: _Site) -> None:
    first = _completed_run(site)
    resume_from = site.root / "first-receipt.json"
    _write_json(resume_from, first)
    submit = _SubmitRecorder(site)

    receipt = run_replay(
        site.config(execute=True, resume_from=resume_from),
        submit_pass=submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert {row["status"] for row in receipt["rows"]} == {"verified_skip"}
    assert submit.calls == []


def test_resume_reruns_a_cycle_whose_recorded_checksum_no_longer_matches(site: _Site) -> None:
    """Resumption is evidence-verified, never a blind skip on receipt status."""

    first = _completed_run(site)
    resume_from = site.root / "first-receipt.json"
    _write_json(resume_from, first)
    # the live index drifted away from what the receipt recorded
    entries = site.index_entries()
    for entry in entries:
        entry["checksum"] = "sha256:something-else"
    site._write_index(entries)
    submit = _SubmitRecorder(site)

    receipt = run_replay(
        site.config(execute=True, resume_from=resume_from),
        submit_pass=submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert {row["status"] for row in receipt["rows"]} == {"completed"}
    assert [_cycle_from_argv(argv) for argv, _env in submit.calls] == list(site.cycles)


def test_resume_reruns_a_cycle_whose_state_entry_disappeared(site: _Site) -> None:
    first = _completed_run(site)
    resume_from = site.root / "first-receipt.json"
    _write_json(resume_from, first)
    site._write_index([])
    submit = _SubmitRecorder(site)

    receipt = run_replay(
        site.config(execute=True, resume_from=resume_from),
        submit_pass=submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert {row["status"] for row in receipt["rows"]} == {"completed"}
    assert len(submit.calls) == len(site.cycles)


def test_an_unreadable_resume_receipt_refuses(site: _Site) -> None:
    resume_from = site.root / "not-a-receipt.json"
    resume_from.write_text("{}", encoding="utf-8")

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(execute=True, resume_from=resume_from),
            submit_pass=_refuse_to_submit,
            journal_probe=_all_terminal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "resume_receipt_unreadable"


# ---------------------------------------------------------------------------
# repair parameter set
# ---------------------------------------------------------------------------


def test_the_repair_parameter_set_applies_only_to_gfs_2026070712(tmp_path: Path) -> None:
    base = _Site(tmp_path, cycles=(CYCLE_1,)).config()
    gfs = ReplayDriverConfig(**{**base.__dict__, "source_id": "gfs"})
    ifs = ReplayDriverConfig(**{**base.__dict__, "source_id": "IFS"})
    repair_cycle = datetime(2026, 7, 7, 12, tzinfo=UTC)

    repaired = submit_env(gfs, repair_cycle)
    assert repaired[REPAIR_ENV] == "1"
    assert repaired[REPAIR_CYCLE_ENV] == "2026-07-07T12:00:00Z"

    for config, cycle in (
        (gfs, datetime(2026, 7, 7, 0, tzinfo=UTC)),
        (gfs, datetime(2026, 7, 8, 12, tzinfo=UTC)),
        (ifs, repair_cycle),
    ):
        env = submit_env(config, cycle)
        assert env[REPAIR_ENV] == "false"
        assert REPAIR_CYCLE_ENV not in env


def test_the_repair_env_reaches_the_submitted_pass_for_that_cycle_only(tmp_path: Path) -> None:
    fixture = _Site(tmp_path, cycles=(datetime(2026, 7, 7, 12, tzinfo=UTC), datetime(2026, 7, 8, 0, tzinfo=UTC)))
    fixture.populate()
    # the GFS run tree/reset receipt are keyed by source; rebuild them for gfs
    config = ReplayDriverConfig(**{**fixture.config(execute=True).__dict__, "source_id": "gfs"})
    for cycle in fixture.cycles:
        for model_id in MODELS:
            run_id = f"fcst_gfs_{cycle.strftime('%Y%m%d%H')}_{model_id}"
            root = fixture.nfs_root / "runs" / run_id
            _write_json(root / "input" / "manifest.json", _manifest())
            (root / "output").mkdir(parents=True, exist_ok=True)
            (root / "output" / "discharge.csv").write_text("prior\n", encoding="utf-8")

    calls: list[tuple[str, str | None]] = []

    def _submit(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        cycle = _cycle_from_argv(argv)
        calls.append((cycle.strftime("%Y%m%d%H"), env.get(REPAIR_ENV)))
        for model_id in MODELS:
            run_id = f"fcst_gfs_{cycle.strftime('%Y%m%d%H')}_{model_id}"
            _write_json(
                fixture.nfs_root / "runs" / run_id / "input" / "manifest.json",
                _manifest(marker="replayed"),
            )
        entries = fixture.index_entries()
        for model_id in MODELS:
            entries.append(
                {
                    "model_id": model_id,
                    "source_id": "gfs",
                    "valid_time": _format_time(cycle + timedelta(hours=12)),
                    "created_at": _format_time(BASE_TIME + timedelta(hours=1)),
                    "state_id": f"new-{model_id}",
                    "checksum": f"sha256:new-{model_id}",
                }
            )
        fixture._write_index(entries)
        return {"returncode": 0}

    fixture.write_reset_receipt(
        removed=[
            {
                "model_id": model_id,
                "source_id": "gfs",
                "valid_time": _format_time(cycle + timedelta(hours=12)),
                "created_at": _format_time(cycle),
                "state_id": f"old-{model_id}",
                "checksum": f"sha256:old-{model_id}",
            }
            for cycle in fixture.cycles
            for model_id in MODELS
        ]
    )

    run_replay(
        config,
        submit_pass=_submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert calls == [("2026070712", "1"), ("2026070800", "false")]


# ---------------------------------------------------------------------------
# submission shape
# ---------------------------------------------------------------------------


def test_submit_argv_pins_one_cycle_and_disables_backfill(site: _Site) -> None:
    argv = replay_driver.submit_argv(site.config(), CYCLE_1)

    assert argv[argv.index("--cycle-time") + 1] == "2026-07-05T00:00:00Z"
    assert "--disable-backfill" in argv
    assert "--submit" in argv
    assert "--continuous" not in argv
    assert "--max-passes" not in argv
    assert [argv[index + 1] for index, token in enumerate(argv) if token == "--model-id"] == list(MODELS)


def test_staging_verifies_every_copied_file(site: _Site) -> None:
    submit = _SubmitRecorder(site)
    run_replay(
        site.config(execute=True),
        submit_pass=submit,
        journal_probe=_all_terminal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    for cycle in site.cycles:
        for model_id in MODELS:
            source = (
                site.nfs_root
                / "forcing"
                / SOURCE.lower()
                / cycle.strftime("%Y%m%d%H")
                / BASIN_VERSION
                / model_id
                / "forcing.csv"
            )
            staged = site.scratch_root / source.relative_to(site.nfs_root)
            assert hashlib.sha256(staged.read_bytes()).hexdigest() == (
                hashlib.sha256(source.read_bytes()).hexdigest()
            )
