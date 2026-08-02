"""Tests for the serial six-basin replay driver (#1164 change 2, tasks.md 3.3).

Fixtures are production-shaped on the three points round 1 got wrong:

* state-index entries carry ``created_at: null`` (1753/1753 on node-22), so the
  convergence oracle is checksum-vs-reset-receipt, never a since-gate;
* the journal keeps the ORIGINAL run's terminal completion record across the
  state-scope reset, so the completion probe baselines job ids pre-pass;
* run manifests carry no ``outputs.variables``; key consistency is asserted over
  ``river_network_version_id``, the output segment count and the output file
  inventory.

Covered, one test per required property:

* receipt row completeness -- the forcing/model package checksums are recorded
  UNCONDITIONALLY, including on a row with no prior run;
* the replay-sequence ORIGIN cycle's new half must be the packaged-IC bootstrap
  shape, otherwise the driver halts -- and a resumed mid-window run does not
  mistake its first cycle for the origin;
* key drift halts the driver;
* convergence needs a replaced successor checksum AND terminal-job evidence --
  either a new job id, or a prior pass's own record of the replacement carried in
  the receipt this pass resumes from (the scheduler refuses to resubmit a model
  whose state exists, so demanding a new id deadlocks every resume after a
  partially successful cycle); with no ``--resume-from`` there is no prior
  evidence at all and an already-replayed world dead-ends in the timeout;
* the receipt is on disk after every cycle, not only at the end;
* an unverified staging result halts before submission (repair cycles excepted);
* resumption never blind-skips: a recorded row is skipped only when the live
  index still carries the recorded checksum;
* the repair parameter set is switched on for GFS 2026070712 and nothing else;
* the prior state fields come from the reset receipt; a receipt that cannot
  supply them, or that did not complete, is a refusal, never a silent null.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import replay_capture, replay_driver
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
    output_segment_count: int = 412,
    marker: str = "prior",
) -> dict[str, Any]:
    """The real forecast run manifest shape (``chain_manifests``).

    There is deliberately NO ``outputs.variables`` key: none of the 36 sampled
    production manifests has one, which is exactly why the old key-consistency
    assertion never fired (round-1 B-P1-3).
    """

    return {
        "marker": marker,
        "run_type": "forecast",
        "model": {
            "river_network_version_id": river_network_version_id,
            "model_package_checksum": "sha256:model",
            "segment_count": output_segment_count,
            "output_segment_count": output_segment_count,
        },
        "runtime": {"init_mode": init_mode, "output_interval_minutes": 60},
        "initial_state": {
            "state_id": f"state-{marker}",
            "checksum": f"sha256:{marker}",
            "quality": quality,
            "packaged_ic_checksum": packaged_ic_checksum,
        },
        "outputs": {
            "output_uri": "s3://nhms/runs/output",
            "output_segment_count": output_segment_count,
            "gis_segment_count": output_segment_count,
        },
    }


class _Site:
    """A throwaway node-22-shaped object store plus the driver's inputs."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        cycles: Sequence[datetime],
        models: Sequence[str] = MODELS,
    ) -> None:
        self.root = tmp_path
        self.models = tuple(models)
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

    def write_run(
        self,
        cycle: datetime,
        model_id: str,
        manifest: Mapping[str, Any],
        *,
        output_files: Sequence[str] = ("discharge.csv", "stage.csv"),
    ) -> None:
        root = self.run_root(cycle, model_id)
        _write_json(root / "input" / "manifest.json", manifest)
        output_root = root / "output"
        if output_root.exists():
            shutil.rmtree(output_root)
        output_root.mkdir(parents=True, exist_ok=True)
        for name in output_files:
            # A replay legitimately rewrites every output byte; the file SET is
            # what must not drift.  Names may be nested (``state_checkpoints/...``).
            target = output_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"{manifest.get('marker')},{cycle.isoformat()},{model_id}\n", encoding="utf-8"
            )

    def write_forcing(self, cycle: datetime, model_id: str, *, source: str = SOURCE) -> Path:
        package = (
            self.nfs_root
            / "forcing"
            / source.lower()
            / cycle.strftime("%Y%m%d%H")
            / BASIN_VERSION
            / model_id
        )
        package.mkdir(parents=True, exist_ok=True)
        (package / "forcing.csv").write_text(f"{model_id}:{cycle.isoformat()}\n", encoding="utf-8")
        (package / "manifest.json").write_text(json.dumps({"model_id": model_id}), encoding="utf-8")
        return package

    def populate(
        self,
        *,
        manifest_marker: str = "prior",
        output_files: Sequence[str] = ("discharge.csv", "stage.csv"),
    ) -> None:
        for cycle in self.cycles:
            for model_id in self.models:
                self.write_run(cycle, model_id, _manifest(marker=manifest_marker), output_files=output_files)
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

    def clear_index(self) -> None:
        """Rewrite a valid, empty index -- an operator repairing an unreadable one."""

        self._write_index([])

    def index_entries(self) -> list[dict[str, Any]]:
        payload = json.loads(self.state_index.read_text(encoding="utf-8"))
        return list(payload["entries"])

    def add_state_entries(
        self,
        cycle: datetime,
        *,
        checksum_prefix: str,
        model_ids: Sequence[str] | None = None,
    ) -> None:
        """Upsert one successor entry per model, production shape.

        ``created_at`` is ``None``: the state-index writer never sets it (1753 of
        1753 entries in both node-22 lanes), which is why the convergence oracle
        may not use a since-gate (round-1 B-P1-1).
        """

        targets = tuple(model_ids) if model_ids is not None else self.models
        valid_time = cycle + timedelta(hours=12)
        entries = [
            entry
            for entry in self.index_entries()
            if not (
                str(entry.get("model_id")) in targets
                and str(entry.get("valid_time")) == _format_time(valid_time)
            )
        ]
        for model_id in targets:
            entries.append(
                {
                    "model_id": model_id,
                    "source_id": SOURCE,
                    "valid_time": _format_time(valid_time),
                    "created_at": None,
                    "state_id": f"{checksum_prefix}-{model_id}-{cycle.strftime('%Y%m%d%H')}",
                    "checksum": f"sha256:{checksum_prefix}-{model_id}-{cycle.strftime('%Y%m%d%H')}",
                }
            )
        self._write_index(entries)

    # -- reset receipt -----------------------------------------------------

    def write_reset_receipt(
        self,
        *,
        removed: Sequence[Mapping[str, Any]] | None = None,
        outcome: str = "completed",
        scopes: Sequence[Mapping[str, Any]] | None = None,
        source: str = SOURCE,
    ) -> None:
        if scopes is None:
            scopes = [{"model_id": model_id, "source_id": source} for model_id in self.models]
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
                for model_id in self.models
            ]
        _write_json(
            self.reset_receipt,
            {
                "schema_version": "nhms.replay_state_scope_reset.v1",
                "outcome": outcome,
                "enforced": True,
                "scopes": [dict(scope) for scope in scopes],
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
            "model_ids": self.models,
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


class _JournalTerminals:
    """Journal completion probe: terminal job ids per cycle.

    The state-scope reset does not touch the journal, so the ORIGINAL run's
    terminal completion record is present before the replay pass starts — a
    boolean "has a completion" probe is therefore true from the outset
    (round-1 B-P2-7).  ``record_replay_terminal`` is what a real replayed cycle
    adds: a NEW job id alongside the original one.
    """

    def __init__(self, *, original_terminal: bool = True) -> None:
        self.original_terminal = original_terminal
        self.replayed: dict[str, list[str]] = {}

    def __call__(self, config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, list[str]]:
        token = cycle_time.strftime("%Y%m%d%H")
        ids: list[str] = []
        if self.original_terminal:
            ids.append(f"job-original-{token}")
        ids.extend(self.replayed.get(token) or [])
        return {model_id: list(ids) for model_id in config.model_ids}

    def record_replay_terminal(self, cycle_time: datetime) -> None:
        """Append a NEW terminal job id, keeping the earlier attempts' ids.

        The journal is never reset between attempts, so a resumed attempt's
        completion evidence must be a job id nobody has seen before -- not the
        one the interrupted attempt already left behind.
        """

        token = cycle_time.strftime("%Y%m%d%H")
        recorded = self.replayed.setdefault(token, [])
        recorded.append(f"job-replay-{token}" if not recorded else f"job-replay-{token}-{len(recorded) + 1}")


class _SubmitRecorder:
    """Fake pass: records the invocation and mutates the site like a real pass."""

    def __init__(
        self,
        site: _Site,
        *,
        new_manifest: Mapping[str, Any] | None = None,
        new_output_files: Sequence[str] = ("discharge.csv", "stage.csv"),
        returncode: int = 0,
        publish_entries: bool = True,
        publish_terminal: bool = True,
        journal: _JournalTerminals | None = None,
    ) -> None:
        self.site = site
        self.new_manifest = new_manifest
        self.new_output_files = tuple(new_output_files)
        self.returncode = returncode
        self.publish_entries = publish_entries
        self.publish_terminal = publish_terminal
        self.journal = journal if journal is not None else _JournalTerminals()
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((list(argv), dict(env)))
        cycle = _cycle_from_argv(argv)
        if self.returncode == 0:
            manifest = dict(self.new_manifest or _manifest(marker="replayed"))
            for model_id in self.site.models:
                self.site.write_run(cycle, model_id, manifest, output_files=self.new_output_files)
            if self.publish_entries:
                self.site.add_state_entries(cycle, checksum_prefix="new")
            if self.publish_terminal:
                self.journal.record_replay_terminal(cycle)
        return {"returncode": self.returncode, "stdout_tail": "", "stderr_tail": ""}


def _cycle_from_argv(argv: Sequence[str]) -> datetime:
    index = list(argv).index("--cycle-time")
    return datetime.fromisoformat(str(argv[index + 1]).replace("Z", "+00:00"))


def _original_terminals_only(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, list[str]]:
    """Pre-pass journal: only the original run's terminal record exists."""

    return _JournalTerminals()(config, cycle_time)


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
        journal_probe=_original_terminals_only,
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
        journal_probe=_original_terminals_only,
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
        journal_probe=_original_terminals_only,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    census = receipt["inventory_census"]
    assert census["enumeration_status"] == "present"
    assert census["frontier_cycle"] == CYCLE_2.strftime("%Y%m%d%H")
    assert {scope["model_id"] for scope in census["scopes"]} == set(MODELS)
    for scope in census["scopes"]:
        assert scope["run_cycles"] == [cycle.strftime("%Y%m%d%H") for cycle in site.cycles]
        # the scope was cleared before the replay: the live index holds nothing
        assert scope["state_index_entry_count"] == 0


def test_inventory_census_enumeration_is_not_truncated_to_the_requested_window(site: _Site) -> None:
    """B-P2-8: the census is on-site, so a run past the requested range shows up.

    A frontier that advanced after the survey must be visible in the receipt
    rather than silently clipped to ``--end-cycle``.
    """

    advanced = datetime(2026, 7, 21, 12, tzinfo=UTC)
    for model_id in MODELS:
        site.write_run(advanced, model_id, _manifest(marker="post-survey"))

    receipt = run_replay(
        site.config(),
        submit_pass=_refuse_to_submit,
        journal_probe=_original_terminals_only,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    census = receipt["inventory_census"]
    assert census["frontier_cycle"] == "2026072112"
    assert census["requested_frontier_cycle"] == CYCLE_2.strftime("%Y%m%d%H")
    for scope in census["scopes"]:
        assert "2026072112" in scope["run_cycles"]
        assert scope["run_cycle_count"] == len(site.cycles) + 1


# ---------------------------------------------------------------------------
# prior state provenance
# ---------------------------------------------------------------------------


def test_prior_state_comes_from_the_reset_receipt_not_the_cleared_index(site: _Site) -> None:
    receipt = run_replay(
        site.config(),
        submit_pass=_refuse_to_submit,
        journal_probe=_original_terminals_only,
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
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_has_no_removed_entries"
    assert not site.receipt_path.exists()


@pytest.mark.parametrize("outcome", ["commit_uncertain", "refused"])
def test_a_reset_receipt_that_did_not_complete_refuses(site: _Site, outcome: str) -> None:
    """B-P2-11: an irreversible replacement may not start off a half-committed reset.

    ``commit_uncertain`` means the reset's own read-back could not confirm the
    write; proceeding would overwrite production runs on top of a state scope
    whose content is unknown.
    """

    site.write_reset_receipt(outcome=outcome)

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_not_completed"
    assert excinfo.value.details["outcome"] == outcome
    assert not site.receipt_path.exists()


def test_a_missing_reset_receipt_refuses(site: _Site) -> None:
    site.reset_receipt.unlink()

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_unreadable"


def test_retention_must_be_disabled_before_anything_runs(site: _Site) -> None:
    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(env={"NHMS_RETENTION_ENABLED": "true"}),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
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
        journal_probe=submit.journal,
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
            journal_probe=submit.journal,
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


def test_a_resumed_mid_window_run_does_not_treat_its_first_cycle_as_the_origin(tmp_path: Path) -> None:
    """B-P1-2: the bootstrap assertion is bound to 2026070500, not ``cycles[0]``.

    Phase 2 resumes from a later ``--start-cycle``; its first cycle is a WARM
    cycle (``init_mode=3``, ``quality=fresh``, no packaged IC), which the
    old ``cycles[0]`` binding would have failed on an assertion that cannot hold.
    """

    warm_cycles = (datetime(2026, 7, 7, 12, tzinfo=UTC), datetime(2026, 7, 8, 0, tzinfo=UTC))
    fixture = _Site(tmp_path, cycles=warm_cycles)
    fixture.populate()
    warm_manifest = _manifest(
        marker="replayed",
        init_mode=3,
        quality="fresh",
        packaged_ic_checksum="",
    )
    submit = _SubmitRecorder(fixture, new_manifest=warm_manifest)

    receipt = run_replay(
        fixture.config(execute=True),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    assert {row["bootstrap_assertion"]["status"] for row in receipt["rows"]} == {"not_required"}
    assert all(row["bootstrap_assertion"]["required"] is False for row in receipt["rows"])


def test_the_origin_cycle_keeps_its_bootstrap_assertion_when_it_is_not_first(tmp_path: Path) -> None:
    """The origin is asserted wherever it appears in the requested range."""

    fixture = _Site(tmp_path, cycles=(datetime(2026, 7, 4, 12, tzinfo=UTC), CYCLE_1))
    fixture.populate()
    submit = _SubmitRecorder(
        fixture,
        new_manifest=_manifest(marker="replayed", init_mode=1, quality="cold_start", packaged_ic_checksum=""),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            fixture.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    assert receipt["interruption"]["reason"] == "first_cycle_bootstrap_assertion_failed"
    assert receipt["interruption"]["cycle"] == CYCLE_1.strftime("%Y%m%d%H")
    # the earlier cycle ran through untouched by the origin assertion
    assert len(submit.calls) == 2


def test_the_receipt_is_written_after_every_cycle(site: _Site) -> None:
    """B-P1-4: a crash mid-sequence must not lose the completed rows."""

    submit = _SubmitRecorder(site)
    on_disk_at_submission: list[Any] = []

    def _observing_submit(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        on_disk_at_submission.append(
            json.loads(site.receipt_path.read_text(encoding="utf-8"))
            if site.receipt_path.exists()
            else None
        )
        return submit(argv, env)

    receipt = run_replay(
        site.config(execute=True),
        submit_pass=_observing_submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    # The pre-flight receipt is on disk before the first submission (round-2
    # B2-2), carrying no rows yet.
    preflight = on_disk_at_submission[0]
    jsonschema.validate(preflight, _SCHEMA)
    assert preflight["outcome"] == "in_progress"
    assert preflight["rows"] == []
    checkpoint = on_disk_at_submission[1]
    jsonschema.validate(checkpoint, _SCHEMA)
    assert checkpoint["outcome"] == "in_progress"
    assert checkpoint["finished_at"] is None
    assert {row["cycle"] for row in checkpoint["rows"]} == {CYCLE_1.strftime("%Y%m%d%H")}
    assert {row["status"] for row in checkpoint["rows"]} == {"completed"}
    assert receipt["outcome"] == "completed"
    assert json.loads(site.receipt_path.read_text(encoding="utf-8"))["outcome"] == "completed"


# ---------------------------------------------------------------------------
# staging gate
# ---------------------------------------------------------------------------


def test_an_absent_forcing_source_halts_before_submission(site: _Site) -> None:
    """A-P2-6/B-P2-5: an unstaged package must never reach a submission."""

    package = (
        site.nfs_root
        / "forcing"
        / SOURCE.lower()
        / CYCLE_1.strftime("%Y%m%d%H")
        / BASIN_VERSION
        / MODELS[1]
    )
    for path in sorted(package.iterdir()):
        path.unlink()
    package.rmdir()
    submit = _SubmitRecorder(site)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "forcing_source_absent"
    assert receipt["interruption"]["detail"]["rows"][0]["model_id"] == MODELS[1]
    assert submit.calls == []
    assert {row["status"] for row in receipt["rows"]} == {"halted"}


def test_a_failed_staging_copy_halts_before_submission(site: _Site) -> None:
    (site.scratch_root / "forcing").write_text("not a directory", encoding="utf-8")
    submit = _SubmitRecorder(site)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "forcing_staging_unverified"
    assert {row["status"] for row in receipt["interruption"]["detail"]["rows"]} == {"stage_failed"}
    assert submit.calls == []


def test_the_repair_cycle_tolerates_an_absent_forcing_source(tmp_path: Path) -> None:
    """GFS 2026070712 legitimately has no NFS forcing; the repair set rebuilds it."""

    repair_cycle = datetime(2026, 7, 7, 12, tzinfo=UTC)
    fixture = _Site(tmp_path, cycles=(repair_cycle,))
    for model_id in MODELS:
        run_id = f"fcst_gfs_{repair_cycle.strftime('%Y%m%d%H')}_{model_id}"
        _write_json(fixture.nfs_root / "runs" / run_id / "input" / "manifest.json", _manifest())
        (fixture.nfs_root / "runs" / run_id / "output").mkdir(parents=True, exist_ok=True)
        (fixture.nfs_root / "runs" / run_id / "output" / "discharge.csv").write_text("prior\n", encoding="utf-8")
    fixture.write_reset_receipt(
        removed=[
            {
                "model_id": model_id,
                "source_id": "gfs",
                "valid_time": _format_time(repair_cycle + timedelta(hours=12)),
                "created_at": None,
                "state_id": f"old-{model_id}",
                "checksum": f"sha256:old-{model_id}",
            }
            for model_id in MODELS
        ],
        source="gfs",
    )
    config = ReplayDriverConfig(**{**fixture.config(execute=True).__dict__, "source_id": "gfs"})
    journal = _JournalTerminals()

    def _submit(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        cycle = _cycle_from_argv(argv)
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
                    "created_at": None,
                    "state_id": f"new-{model_id}",
                    "checksum": f"sha256:new-{model_id}",
                }
            )
        fixture._write_index(entries)
        journal.record_replay_terminal(cycle)
        return {"returncode": 0}

    receipt = run_replay(
        config,
        submit_pass=_submit,
        journal_probe=journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    for row in receipt["rows"]:
        assert row["staging"]["status"] == "source_absent"
        assert row["staging"]["repair_cycle_exemption"] is True
        assert row["submission"]["repair_parameter_set"] is True


def test_key_drift_halts_the_driver(site: _Site) -> None:
    submit = _SubmitRecorder(
        site,
        new_manifest=_manifest(marker="replayed", river_network_version_id="rn-2027-DRIFT"),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    drifted = [row for row in receipt["rows"] if row["key_consistency"] is not None]
    assert drifted and all(row["key_consistency"]["status"] == "drift" for row in drifted)
    assert len(submit.calls) == 1


def test_output_inventory_drift_halts_the_driver(site: _Site) -> None:
    """B-P1-3: the assertion runs on evidence the run tree really carries."""

    submit = _SubmitRecorder(site, new_output_files=("discharge.csv",))

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    drifted = [row for row in receipt["rows"] if (row["key_consistency"] or {}).get("status") == "drift"]
    assert drifted
    assert drifted[0]["key_consistency"]["drifted_axes"] == ["output_file_names"]
    assert drifted[0]["key_consistency"]["prior_output_file_names"] == ["discharge.csv", "stage.csv"]
    assert drifted[0]["key_consistency"]["new_output_file_names"] == ["discharge.csv"]


#: A run tree's output files, checkpoint directory included -- the shape the
#: attempt-4 inventories really had.
_RUN_OUTPUT_FILES = (
    "discharge.csv",
    "stage.csv",
    "state_checkpoints/state_checkpoints.json",
    "state_checkpoints/CJ-DTH-LS.f012.cfg.ic.update",
)
#: What the run's OWN ``state_save_qc`` writes next to a checkpoint whose IC
#: content needed canonicalizing (``state_cli._normalized_checkpoint_ic_file``).
_NORMALIZATION_SIDECAR = "state_checkpoints/.CJ-DTH-LS.f012.cfg.ic.update.normalized"


def test_normalization_sidecar_present_on_one_half_only_is_not_key_drift(site: _Site) -> None:
    """Attempt-4: prior 14 files, new 15 -- the extra one is the IC normalization cache.

    ``save_state_for_run`` writes ``.{ic}.normalized`` beside the checkpoint only
    when that half's IC needed it (retimed header or clamped residual), so the
    file tracks IC content, not the key set: the replayed dth_ls IC needed
    normalizing where its July original did not.  The comparison drops it from
    both halves and RECORDS what it dropped.
    """

    site.populate(output_files=_RUN_OUTPUT_FILES)
    submit = _SubmitRecorder(site, new_output_files=(*_RUN_OUTPUT_FILES, _NORMALIZATION_SIDECAR))

    receipt = run_replay(
        site.config(execute=True),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    consistency = receipt["rows"][0]["key_consistency"]
    assert consistency["status"] == "consistent"
    assert consistency["drifted_axes"] == []
    assert "output_file_names" in consistency["compared_axes"]
    # the full inventories stay on the receipt: the check narrows loudly
    assert _NORMALIZATION_SIDECAR in consistency["new_output_file_names"]
    assert _NORMALIZATION_SIDECAR not in consistency["prior_output_file_names"]
    assert consistency["excluded_normalization_sidecars"] == {
        "prior": [],
        "new": [_NORMALIZATION_SIDECAR],
    }


def test_normalization_sidecar_only_on_the_prior_half_is_not_key_drift(site: _Site) -> None:
    """The mirror case: the OLD run's IC needed normalizing and the replayed one did not."""

    site.populate(output_files=(*_RUN_OUTPUT_FILES, _NORMALIZATION_SIDECAR))
    submit = _SubmitRecorder(site, new_output_files=_RUN_OUTPUT_FILES)

    receipt = run_replay(
        site.config(execute=True),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert receipt["outcome"] == "completed"
    consistency = receipt["rows"][0]["key_consistency"]
    assert consistency["status"] == "consistent"
    assert consistency["excluded_normalization_sidecars"] == {
        "prior": [_NORMALIZATION_SIDECAR],
        "new": [],
    }


def test_real_output_drift_under_a_normalization_sidecar_still_halts(site: _Site) -> None:
    """The exclusion is surgical: any NON-sidecar difference still drifts."""

    site.populate(output_files=(*_RUN_OUTPUT_FILES, _NORMALIZATION_SIDECAR))
    submit = _SubmitRecorder(
        site,
        new_output_files=(
            *(name for name in _RUN_OUTPUT_FILES if name != "discharge.csv"),
            _NORMALIZATION_SIDECAR,
        ),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    drifted = next(row for row in receipt["rows"] if (row["key_consistency"] or {}).get("status") == "drift")
    consistency = drifted["key_consistency"]
    assert consistency["drifted_axes"] == ["output_file_names"]
    assert "discharge.csv" in consistency["prior_output_file_names"]
    assert "discharge.csv" not in consistency["new_output_file_names"]
    assert consistency["excluded_normalization_sidecars"] == {
        "prior": [_NORMALIZATION_SIDECAR],
        "new": [_NORMALIZATION_SIDECAR],
    }


def test_a_differing_checkpoint_set_under_differing_sidecars_still_halts(site: _Site) -> None:
    """The real checkpoint files are the backstop: both halves may carry a sidecar.

    Excluding the sidecars must not also hide that the halves checkpointed at
    DIFFERENT lead hours -- the ``.cfg.ic.update`` files themselves are compared.
    """

    prior_checkpoint = "state_checkpoints/CJ-DTH-LS.f006.cfg.ic.update"
    new_checkpoint = "state_checkpoints/CJ-DTH-LS.f012.cfg.ic.update"
    site.populate(
        output_files=(
            "discharge.csv",
            "stage.csv",
            "state_checkpoints/state_checkpoints.json",
            prior_checkpoint,
            f"state_checkpoints/.{prior_checkpoint.rsplit('/', 1)[1]}.normalized",
        )
    )
    submit = _SubmitRecorder(
        site,
        new_output_files=(
            "discharge.csv",
            "stage.csv",
            "state_checkpoints/state_checkpoints.json",
            new_checkpoint,
            f"state_checkpoints/.{new_checkpoint.rsplit('/', 1)[1]}.normalized",
        ),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    drifted = next(row for row in receipt["rows"] if (row["key_consistency"] or {}).get("status") == "drift")
    consistency = drifted["key_consistency"]
    assert consistency["drifted_axes"] == ["output_file_names"]
    assert prior_checkpoint in consistency["prior_output_file_names"]
    assert new_checkpoint in consistency["new_output_file_names"]
    assert consistency["excluded_normalization_sidecars"] == {
        "prior": [f"state_checkpoints/.{prior_checkpoint.rsplit('/', 1)[1]}.normalized"],
        "new": [f"state_checkpoints/.{new_checkpoint.rsplit('/', 1)[1]}.normalized"],
    }


def test_only_checkpoint_normalization_sidecars_are_excluded() -> None:
    """The exclusion follows the sidecar naming rule, not a basin name list."""

    def inventory(*names: str) -> dict[str, Any]:
        return {"status": "present", "files": list(names)}

    look_alikes = (
        "state_checkpoints/state.normalized",  # no dot prefix -> a real output
        "state_checkpoints/.normalized",  # no ``{ic}`` stem
        ".CJ-DTH-LS.f012.cfg.ic.update.normalized",  # outside state_checkpoints/
        "state_checkpoints/.CJ-DTH-LS.f012.cfg.ic.update",  # not the cache
    )
    for name in look_alikes:
        consistency = replay_capture.key_consistency(
            {"output_inventory": inventory("discharge.csv")},
            {"output_inventory": inventory("discharge.csv", name)},
        )
        assert consistency["status"] == "drift", name
        assert "excluded_normalization_sidecars" not in consistency, name

    nested = replay_capture.key_consistency(
        {"output_inventory": inventory("discharge.csv")},
        {"output_inventory": inventory("discharge.csv", "output/state_checkpoints/.HHe.f012.cfg.ic.update.normalized")},
    )
    assert nested["status"] == "consistent"
    assert nested["excluded_normalization_sidecars"]["new"] == [
        "output/state_checkpoints/.HHe.f012.cfg.ic.update.normalized"
    ]


def test_output_segment_count_drift_halts_the_driver(site: _Site) -> None:
    submit = _SubmitRecorder(
        site,
        new_manifest=_manifest(marker="replayed", output_segment_count=999),
    )

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    drifted = next(row for row in receipt["rows"] if (row["key_consistency"] or {}).get("status") == "drift")
    assert drifted["key_consistency"]["drifted_axes"] == ["output_segment_count"]
    assert drifted["prior"]["output_segment_count"] == 412
    assert drifted["new"]["output_segment_count"] == 999


def test_key_consistency_compares_real_manifest_axes_only(site: _Site) -> None:
    """The consistent case names the axes it compared -- no dead ``variables`` key."""

    submit = _SubmitRecorder(site)
    receipt = run_replay(
        site.config(execute=True),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    consistency = receipt["rows"][0]["key_consistency"]
    assert consistency["status"] == "consistent"
    assert consistency["compared_axes"] == [
        "output_file_names",
        "output_segment_count",
        "river_network_version_id",
    ]
    assert consistency["drifted_axes"] == []


def test_submission_failure_halts_with_the_interruption_recorded(site: _Site) -> None:
    submit = _SubmitRecorder(site, returncode=7)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=submit,
            journal_probe=submit.journal,
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


def test_convergence_rejects_a_successor_the_reset_receipt_already_recorded(site: _Site) -> None:
    """B-P1-1: freshness is checksum-vs-reset-receipt, not ``created_at``.

    The entry below carries the very checksum the reset receipt archived and
    ``created_at: null`` (the production shape).  It must not satisfy the wait.
    """

    site.add_state_entries(CYCLE_1, checksum_prefix="old")
    submit = _SubmitRecorder(site, publish_entries=False)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    # a new terminal job id DID appear; only the unchanged state entry blocked it
    assert receipt["interruption"]["detail"]["journal_terminal"] == sorted(MODELS)
    assert receipt["interruption"]["detail"]["state_entries"] == []
    assert receipt["interruption"]["detail"]["unreplaced_successors"] == sorted(MODELS)
    # ... and the unchanged checksum keeps them out of the prior-pass leg too
    assert receipt["interruption"]["detail"]["prior_satisfied"] == []
    assert [entry["created_at"] for entry in site.index_entries()] == [None] * len(MODELS)
    assert all(row["convergence"]["state_entry_present"] is False for row in receipt["rows"])


def test_convergence_accepts_a_successor_whose_checksum_differs_from_the_reset_receipt(site: _Site) -> None:
    site.add_state_entries(CYCLE_1, checksum_prefix="old")
    submit = _SubmitRecorder(site)

    receipt = run_replay(
        site.config(execute=True, cycle_timeout_seconds=0),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert receipt["outcome"] == "completed"
    first_cycle_rows = [row for row in receipt["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")]
    assert all(row["convergence"]["state_entry_present"] is True for row in first_cycle_rows)
    assert all(row["new"]["state_checksum"].startswith("sha256:new-") for row in first_cycle_rows)
    assert all(row["convergence"]["state_index_status"] == "present" for row in first_cycle_rows)


def test_a_pre_existing_terminal_record_alone_does_not_count_as_completion(site: _Site) -> None:
    """B-P2-7: the ORIGINAL run's terminal record survives the state-scope reset.

    "The journal holds a completion for this cycle" is therefore true before the
    replay starts and can never be completion evidence on its own -- not even
    now that the terminal leg accepts prior-pass evidence, because that leg also
    demands a REPLACED successor.  Here the index still carries exactly the
    checksum the reset receipt archived, so the driver must keep waiting.
    """

    site.add_state_entries(CYCLE_1, checksum_prefix="old")
    submit = _SubmitRecorder(site, publish_entries=False, publish_terminal=False)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert receipt["interruption"]["detail"]["journal_terminal"] == []
    assert receipt["interruption"]["detail"]["prior_satisfied"] == []
    assert receipt["interruption"]["detail"]["unreplaced_successors"] == sorted(MODELS)
    assert receipt["interruption"]["detail"]["prior_terminal_job_ids"] == {
        model_id: [f"job-original-{CYCLE_1.strftime('%Y%m%d%H')}"] for model_id in MODELS
    }
    for row in receipt["rows"]:
        assert row["prior"]["terminal_job_ids"] == [f"job-original-{CYCLE_1.strftime('%Y%m%d%H')}"]
        assert row["convergence"]["journal_terminal"] is False
        assert row["convergence"]["new_terminal_job_ids"] == []
        assert row["convergence"]["terminal_evidence"] is None
        assert row["convergence"]["state_entry_present"] is False


def test_an_unreadable_state_index_halts_with_its_own_typed_reason(site: _Site) -> None:
    """Three-way: an index that cannot be read is undecidable, never converged."""

    submit = _SubmitRecorder(site, publish_entries=False)
    site.state_index.write_text("{not json", encoding="utf-8")

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=submit.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "state_index_undeterminable"
    assert receipt["interruption"]["detail"]["state_index_detail"] == "state_index_unreadable"
    assert all(row["convergence"]["state_index_status"] == "undeterminable" for row in receipt["rows"])


def test_a_non_terminal_journal_halts_even_when_the_index_is_fresh(site: _Site) -> None:
    """A fresh successor without ANY journal attribution is not completion.

    The second model's cycle has no terminal record at all -- neither this
    pass's nor an earlier one's -- so neither leg of the terminal predicate can
    fire and the fresh index alone must not close the cycle.
    """

    submit = _SubmitRecorder(site)

    def _one_model_pending(config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, list[str]]:
        token = cycle_time.strftime("%Y%m%d%H")
        # only the first model records a terminal job, and only after submission
        return {
            MODELS[0]: [f"job-original-{token}"] + ([f"job-replay-{token}"] if submit.calls else []),
            MODELS[1]: [],
        }

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=_one_model_pending,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert receipt["interruption"]["detail"]["prior_satisfied"] == []
    evidence = {row["model_id"]: row["convergence"]["terminal_evidence"] for row in receipt["rows"]}
    assert evidence == {MODELS[0]: "new_job", MODELS[1]: None}


# ---------------------------------------------------------------------------
# convergence on a resume chain's prior evidence
# ---------------------------------------------------------------------------


SIX_MODELS = ("dg_alpha", "dg_beta", "dg_gamma", "dg_delta", "dg_epsilon", "dg_zeta")


def _six_model_site(tmp_path: Path) -> _Site:
    fixture = _Site(tmp_path, cycles=(CYCLE_1,), models=SIX_MODELS)
    fixture.populate()
    return fixture


class _CampaignJournal:
    """One journal for the whole campaign: never reset, never rewound.

    Every model starts with the ORIGINAL run's terminal record -- it survived the
    state-scope reset -- and each attempt appends what its cohort earns, so
    attempt 2's pre-pass baseline contains attempt 1's completions.
    """

    def __init__(self, site: _Site) -> None:
        self.token = CYCLE_1.strftime("%Y%m%d%H")
        self.terminal: dict[str, list[str]] = {
            model_id: [f"job-original-{self.token}"] for model_id in site.models
        }

    def record_cohort(self, model_ids: Sequence[str], attempt: str) -> None:
        """Attribute ONE cohort completion job to every requested model.

        The real record carries ``model_id: null`` and is attributed to the whole
        cohort, which is why a terminal job id says nothing about which model
        actually produced state (the failed model gets it too).
        """

        for model_id in model_ids:
            self.terminal[model_id].append(f"job-{attempt}-cohort-{self.token}")

    def record(self, model_id: str, attempt: str) -> None:
        self.terminal[model_id].append(f"job-{attempt}-{self.token}-{model_id}")

    def __call__(self, config: ReplayDriverConfig, cycle_time: datetime) -> dict[str, list[str]]:
        return {model_id: list(self.terminal.get(model_id) or []) for model_id in config.model_ids}


class _AttemptOne:
    """Attempt 1: ``replayed`` models publish a successor, the rest produce nothing.

    The cohort's completion job is attributed to EVERY model of the pass, the
    failed one included -- that is the production shape and the reason
    ``journal_terminal`` alone can never be the per-model discriminator.
    """

    def __init__(
        self,
        site: _Site,
        journal: _CampaignJournal,
        *,
        replayed: Sequence[str],
        record_terminal: bool = True,
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        self.site = site
        self.journal = journal
        self.replayed = tuple(replayed)
        self.record_terminal = record_terminal
        self.manifest = dict(manifest or _manifest(marker="replayed"))
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((list(argv), dict(env)))
        cycle = _cycle_from_argv(argv)
        for model_id in self.replayed:
            self.site.write_run(cycle, model_id, self.manifest)
        if self.replayed:
            self.site.add_state_entries(cycle, checksum_prefix="new", model_ids=self.replayed)
        if self.record_terminal:
            self.journal.record_cohort(self.site.models, "attempt1")
        return {"returncode": 0, "stdout_tail": "", "stderr_tail": ""}


def _run_attempt_one(
    site: _Site,
    journal: _CampaignJournal,
    *,
    replayed: Sequence[str],
    record_terminal: bool = True,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Run attempt 1 and return its receipt plus the halt reason, if any."""

    submit = _AttemptOne(
        site, journal, replayed=replayed, record_terminal=record_terminal, manifest=manifest
    )
    try:
        receipt = run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=submit,
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )
    except ReplayHalted as error:
        return error.receipt, str(error.receipt["interruption"]["reason"])
    return receipt, None


def _attempt_one_receipt(
    site: _Site,
    journal: _CampaignJournal,
    *,
    replayed: Sequence[str],
    record_terminal: bool = True,
    manifest: Mapping[str, Any] | None = None,
    expect_halt: str | None = "convergence_timeout",
) -> Path:
    """Run attempt 1 and archive the receipt attempt 2 will resume from."""

    receipt, reason = _run_attempt_one(
        site, journal, replayed=replayed, record_terminal=record_terminal, manifest=manifest
    )
    assert reason == expect_halt
    resume_from = site.root / "attempt-1-receipt.json"
    _write_json(resume_from, receipt)
    return resume_from


class _AttemptTwo:
    """Attempt 2: the scheduler refuses the finished models, ``pending`` runs.

    The submission itself publishes nothing -- a real pass returns as soon as the
    jobs are queued -- and the pending models reach terminal during the wait.
    """

    def __init__(self, site: _Site, journal: _CampaignJournal, *, pending: Sequence[str] = ()) -> None:
        self.site = site
        self.journal = journal
        self.pending = tuple(pending)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((list(argv), dict(env)))
        return {"returncode": 0, "stdout_tail": "", "stderr_tail": ""}

    def sleep(self, _seconds: float) -> None:
        for model_id in self.pending:
            self.site.write_run(CYCLE_1, model_id, _manifest(marker="replayed"))
            self.journal.record(model_id, "attempt2")
        if self.pending:
            self.site.add_state_entries(CYCLE_1, checksum_prefix="new", model_ids=self.pending)


def _no_sleep(_seconds: float) -> None:
    raise AssertionError("an already-converged cycle must not wait")


def _rows_by_model(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["model_id"]: row for row in payload["rows"]}


def _strip_post_fix_convergence_fields(path: Path) -> dict[str, dict[str, Any]]:
    """Rewrite a receipt into the shape the pre-fix driver wrote.

    The campaign's real receipts have no ``terminal_evidence`` / ``prior_eligible``
    / ``successor_checksum``: those rows must still be judged, on the fields they
    do carry.
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload["rows"]:
        convergence = row.get("convergence") or {}
        for field in ("terminal_evidence", "prior_eligible", "successor_checksum"):
            convergence.pop(field, None)
    _write_json(path, payload)
    return {row["model_id"]: row for row in payload["rows"]}


def test_resume_converges_on_prior_pass_evidence_for_the_models_already_done(tmp_path: Path) -> None:
    """The mid-cycle partial failure the campaign actually hit.

    Attempt 1 finished 5 of 6 models and halted on the 6th (OOM); attempt 2 may
    not demand a NEW terminal job id for the 5 -- the scheduler refuses to
    resubmit them, so no new id can ever appear and the wait would deadlock.
    Attempt 1's rows record both halves for those 5, and that is the evidence
    attempt 2 converges on.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(site, journal, replayed=SIX_MODELS[:5])
    world = _AttemptTwo(site, journal, pending=SIX_MODELS[5:])

    receipt = run_replay(
        site.config(execute=True, cycle_timeout_seconds=600, resume_from=resume_from),
        submit_pass=world,
        journal_probe=journal,
        clock=_Clock(),
        sleep=world.sleep,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    assert {row["status"] for row in receipt["rows"]} == {"completed"}
    evidence = {row["model_id"]: row["convergence"]["terminal_evidence"] for row in receipt["rows"]}
    assert evidence == {
        **{model_id: "prior_pass" for model_id in SIX_MODELS[:5]},
        SIX_MODELS[5]: "new_job",
    }
    for row in receipt["rows"]:
        prior_done = row["model_id"] in SIX_MODELS[:5]
        # the 6th model's attempt-1 row records no replacement, so it is not
        # prior-eligible and converges on its NEW job id alone
        assert row["convergence"]["prior_eligible"] is prior_done
        assert row["convergence"]["journal_terminal"] is not prior_done
        assert bool(row["convergence"]["new_terminal_job_ids"]) is not prior_done
        assert row["convergence"]["state_entry_present"] is True
        assert row["convergence"]["successor_checksum"] == row["new"]["state_checksum"]
        assert row["prior"]["prior_source"] == "resumed_receipt"


def test_a_fully_prior_evidenced_cycle_converges_without_any_new_job(tmp_path: Path) -> None:
    """Attempt 1 converged the whole cycle, then halted on the drift assertion.

    Attempt 2 is refused for every model -- their state exists -- so no new job
    id can appear at all; attempt 1's rows are the whole terminal leg and the
    wait closes on its first poll, before the drift assertion re-fires against
    the original pre-image.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(
        site,
        journal,
        replayed=SIX_MODELS,
        manifest=_manifest(marker="replayed", river_network_version_id="rn-2026b"),
        expect_halt="key_consistency_drift",
    )
    assert all(
        row["convergence"]["terminal_evidence"] == "new_job"
        for row in _rows_by_model(resume_from).values()
    )
    world = _AttemptTwo(site, journal)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=600, resume_from=resume_from),
            submit_pass=world,
            journal_probe=journal,
            clock=_Clock(),
            sleep=_no_sleep,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    # the WAIT converged on prior evidence alone; the halt is the re-fired drift
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    for row in receipt["rows"]:
        assert row["convergence"]["terminal_evidence"] == "prior_pass"
        assert row["convergence"]["prior_eligible"] is True
        assert row["convergence"]["journal_terminal"] is False
        assert row["convergence"]["new_terminal_job_ids"] == []
        assert row["convergence"]["state_entry_present"] is True


def test_a_rerun_without_resume_from_never_converges_on_prior_evidence(tmp_path: Path) -> None:
    """Re-running an already-replayed cycle with no resume chain must dead-end.

    There is no pre-image left on disk -- the run trees were overwritten -- so a
    pass that "converged" here would record its own output as the prior half.
    Prior evidence lives in the resume receipt, and without ``--resume-from``
    there is none: the misoperation halts instead of fabricating a receipt.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    completed, reason = _run_attempt_one(site, journal, replayed=SIX_MODELS)
    assert reason is None and completed["outcome"] == "completed"
    world = _AttemptTwo(site, journal)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=world,
            journal_probe=journal,
            clock=_Clock(),
            sleep=world.sleep,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    detail = receipt["interruption"]["detail"]
    assert receipt["outcome"] == "halted"
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert receipt.get("resume_from") is None
    # every successor IS replaced -- and none of it counts without a resume chain
    assert detail["state_entries"] == sorted(SIX_MODELS)
    assert detail["prior_eligible"] == []
    assert detail["prior_satisfied"] == []
    for row in receipt["rows"]:
        assert row["convergence"]["terminal_evidence"] is None
        assert row["convergence"]["prior_eligible"] is False


def test_a_resumed_row_that_recorded_no_replacement_is_not_prior_evidence(tmp_path: Path) -> None:
    """The failed model's own shape: cohort terminal job, no successor.

    Its attempt-1 row carries ``journal_terminal: true`` exactly like the five
    that succeeded -- the cohort job is attributed to every model -- and only
    ``state_entry_present: false`` tells them apart.  It must stay outside the
    prior-eligible set and keep the cycle waiting.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(site, journal, replayed=SIX_MODELS[:5])
    failed_row = _rows_by_model(resume_from)[SIX_MODELS[5]]
    assert failed_row["convergence"]["journal_terminal"] is True
    assert failed_row["convergence"]["state_entry_present"] is False
    assert failed_row["convergence"]["successor_checksum"] is None
    world = _AttemptTwo(site, journal)

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0, resume_from=resume_from),
            submit_pass=world,
            journal_probe=journal,
            clock=_Clock(),
            sleep=world.sleep,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    detail = receipt["interruption"]["detail"]
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert detail["prior_eligible"] == sorted(SIX_MODELS[:5])
    assert detail["prior_satisfied"] == sorted(SIX_MODELS[:5])
    assert SIX_MODELS[5] not in detail["prior_eligible"]
    assert detail["journal_terminal"] == []
    evidence = {row["model_id"]: row["convergence"]["terminal_evidence"] for row in receipt["rows"]}
    assert evidence[SIX_MODELS[5]] is None
    assert set(evidence[model_id] for model_id in SIX_MODELS[:5]) == {"prior_pass"}


def test_a_successor_replaced_mid_wait_still_needs_a_new_terminal_job(tmp_path: Path) -> None:
    """A replacement THIS pass produced is not prior evidence.

    Attempt 1 published nothing, so its receipt evidences nothing.  The
    successors that appear during attempt 2's wait are attempt 2's own runs
    writing their state -- those runs may still fail afterwards -- so they close
    the cycle only once their terminal job ids land.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(site, journal, replayed=())

    def _publish_state_during_the_wait(_seconds: float) -> None:
        site.add_state_entries(CYCLE_1, checksum_prefix="new")

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=3, resume_from=resume_from),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=_publish_state_during_the_wait,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    detail = receipt["interruption"]["detail"]
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    # the successors ARE replaced by now -- and still not completion evidence
    assert detail["state_entries"] == sorted(SIX_MODELS)
    assert detail["prior_eligible"] == []
    assert detail["prior_satisfied"] == []
    assert detail["journal_terminal"] == []
    for row in receipt["rows"]:
        assert row["convergence"]["prior_eligible"] is False
        assert row["convergence"]["terminal_evidence"] is None
        assert row["convergence"]["state_entry_present"] is True


def test_an_undeterminable_first_poll_does_not_admit_this_passs_own_successors(tmp_path: Path) -> None:
    """An unreadable index at the first poll may not launder this pass's state.

    The index is repaired mid-wait and then holds successors THIS pass wrote,
    with no new terminal job id and nothing in the resume receipt to evidence
    them; the cycle must still time out.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(site, journal, replayed=())
    site.state_index.write_text("{not json", encoding="utf-8")

    def _repair_index_during_the_wait(_seconds: float) -> None:
        if not site.state_index.read_text(encoding="utf-8").startswith("{not json"):
            return
        site.clear_index()
        site.add_state_entries(CYCLE_1, checksum_prefix="new")

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=3, resume_from=resume_from),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=_repair_index_during_the_wait,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    detail = receipt["interruption"]["detail"]
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert detail["state_index_status"] == "present"
    assert detail["state_entries"] == sorted(SIX_MODELS)
    assert detail["prior_eligible"] == []
    for row in receipt["rows"]:
        assert row["convergence"]["terminal_evidence"] is None


def test_a_pre_fix_attempt_one_row_still_evidences_the_replacement(tmp_path: Path) -> None:
    """The campaign's real attempt-1 receipt predates ``terminal_evidence``.

    Its rows carry ``state_entry_present`` and ``journal_terminal`` and nothing
    else about the terminal leg, so both halves are read off those older fields:
    a resume from the receipt the operator actually holds must converge.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(site, journal, replayed=SIX_MODELS[:5])
    stripped = _strip_post_fix_convergence_fields(resume_from)
    assert all("terminal_evidence" not in row["convergence"] for row in stripped.values())
    assert stripped[SIX_MODELS[0]]["convergence"]["journal_terminal"] is True
    assert stripped[SIX_MODELS[0]]["convergence"]["state_entry_present"] is True
    world = _AttemptTwo(site, journal, pending=SIX_MODELS[5:])

    receipt = run_replay(
        site.config(execute=True, cycle_timeout_seconds=600, resume_from=resume_from),
        submit_pass=world,
        journal_probe=journal,
        clock=_Clock(),
        sleep=world.sleep,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    evidence = {row["model_id"]: row["convergence"]["terminal_evidence"] for row in receipt["rows"]}
    assert set(evidence[model_id] for model_id in SIX_MODELS[:5]) == {"prior_pass"}
    assert evidence[SIX_MODELS[5]] == "new_job"


def test_a_pre_fix_zero_submission_receipt_carries_no_prior_evidence(tmp_path: Path) -> None:
    """A deadlocked diagnostic pass evidenced nothing, and its receipt says so.

    ``journal_terminal`` is PASS-RELATIVE: the zero-submission attempt 2 wrote
    ``false`` for every model even though five of them had state.  Resuming from
    that receipt must NOT admit them -- the operator has to resume from attempt
    1's receipt instead, whose prior halves are identical (rows carry verbatim).
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    attempt_one = _attempt_one_receipt(site, journal, replayed=SIX_MODELS[:5])
    attempt_two = site.root / "attempt-2-receipt.json"
    with pytest.raises(ReplayHalted) as halted:
        run_replay(
            site.config(
                execute=True,
                cycle_timeout_seconds=0,
                resume_from=attempt_one,
                receipt_path=site.root / "attempt-2-live.json",
            ),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )
    _write_json(attempt_two, halted.value.receipt)
    stripped = _strip_post_fix_convergence_fields(attempt_two)
    assert stripped[SIX_MODELS[0]]["convergence"]["journal_terminal"] is False
    assert stripped[SIX_MODELS[0]]["convergence"]["state_entry_present"] is True

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0, resume_from=attempt_two),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert receipt["interruption"]["detail"]["prior_eligible"] == []
    assert all(row["convergence"]["terminal_evidence"] is None for row in receipt["rows"])


def test_a_row_whose_pass_adjudicated_no_terminal_evidence_is_not_prior_evidence(
    tmp_path: Path,
) -> None:
    """A post-fix row that saw state but closed nothing is not evidence either.

    ``terminal_evidence: null`` beside ``state_entry_present: true`` is exactly
    the "state was written, nothing adjudicated it terminal" shape; the field is
    present, so it -- not ``journal_terminal`` -- is what the next resume reads.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    first = _attempt_one_receipt(site, journal, replayed=())
    site.add_state_entries(CYCLE_1, checksum_prefix="new")
    second = site.root / "attempt-2-receipt.json"
    with pytest.raises(ReplayHalted) as halted:
        run_replay(
            site.config(
                execute=True,
                cycle_timeout_seconds=0,
                resume_from=first,
                receipt_path=site.root / "attempt-2-live.json",
            ),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )
    _write_json(second, halted.value.receipt)
    rows = _rows_by_model(second)
    assert rows[SIX_MODELS[0]]["convergence"]["state_entry_present"] is True
    assert rows[SIX_MODELS[0]]["convergence"]["terminal_evidence"] is None

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0, resume_from=second),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    assert receipt["interruption"]["detail"]["prior_eligible"] == []


def test_a_successor_checksum_that_no_longer_matches_is_not_accepted(tmp_path: Path) -> None:
    """Prior evidence describes ONE successor; a different one needs a new job.

    The index still holds a replaced successor for all five models, but not the
    one attempt 1 saw -- something re-published it -- so the rows stay eligible
    and are still refused.
    """

    site = _six_model_site(tmp_path)
    journal = _CampaignJournal(site)
    resume_from = _attempt_one_receipt(site, journal, replayed=SIX_MODELS[:5])
    recorded = _rows_by_model(resume_from)[SIX_MODELS[0]]["convergence"]["successor_checksum"]
    site.add_state_entries(CYCLE_1, checksum_prefix="renewed", model_ids=SIX_MODELS[:5])
    assert site.index_entries()[0]["checksum"] != recorded

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0, resume_from=resume_from),
            submit_pass=_AttemptTwo(site, journal),
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    detail = receipt["interruption"]["detail"]
    assert receipt["interruption"]["reason"] == "convergence_timeout"
    # eligible by receipt, refused by identity: the split is visible in the row
    assert detail["prior_eligible"] == sorted(SIX_MODELS[:5])
    assert detail["prior_satisfied"] == []
    assert detail["state_entries"] == sorted(SIX_MODELS[:5])
    for row in receipt["rows"]:
        assert row["convergence"]["terminal_evidence"] is None
        assert row["convergence"]["prior_eligible"] is (row["model_id"] in SIX_MODELS[:5])


# ---------------------------------------------------------------------------
# resumption
# ---------------------------------------------------------------------------


def _completed_run(site: _Site) -> dict[str, Any]:
    submit = _SubmitRecorder(site)
    return run_replay(
        site.config(execute=True),
        submit_pass=submit,
        journal_probe=submit.journal,
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
        journal_probe=submit.journal,
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
        journal_probe=submit.journal,
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
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert {row["status"] for row in receipt["rows"]} == {"completed"}
    assert len(submit.calls) == len(site.cycles)


def test_resume_keeps_the_interrupted_cycle_prior_from_the_resume_receipt(site: _Site) -> None:
    """B2-1: the run trees have no archive; a re-captured prior is the replay itself.

    Attempt 1 overwrites cycle 2's run tree and then halts before convergence,
    so its rows are ``halted`` -- the pre-change loader dropped exactly those,
    and attempt 2 re-captured the prior half from the already-replayed tree.
    """

    original_manifest_sha256 = hashlib.sha256(
        (site.run_root(CYCLE_2, MODELS[0]) / "input" / "manifest.json").read_bytes()
    ).hexdigest()
    journal = _JournalTerminals()

    def _submit_but_only_publish_cycle_1(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        cycle = _cycle_from_argv(argv)
        for model_id in MODELS:
            site.write_run(cycle, model_id, _manifest(marker="replayed"))
        if cycle == CYCLE_1:
            site.add_state_entries(cycle, checksum_prefix="new")
            journal.record_replay_terminal(cycle)
        return {"returncode": 0}

    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, cycle_timeout_seconds=0),
            submit_pass=_submit_but_only_publish_cycle_1,
            journal_probe=journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )
    first = excinfo.value.receipt
    assert first["interruption"]["cycle"] == CYCLE_2.strftime("%Y%m%d%H")
    halted_rows = [row for row in first["rows"] if row["cycle"] == CYCLE_2.strftime("%Y%m%d%H")]
    assert {row["status"] for row in halted_rows} == {"halted"}
    assert {row["prior"]["run_manifest_sha256"] for row in halted_rows} == {original_manifest_sha256}
    assert {row["prior"]["prior_source"] for row in halted_rows} == {"captured"}

    # attempt 1's receipt is the resume source and must survive attempt 2 intact
    resume_from = site.receipt_path
    resume_bytes_before = resume_from.read_bytes()
    # the cycle-2 tree on disk is now the interrupted replay, not the pre-image
    assert (
        hashlib.sha256((site.run_root(CYCLE_2, MODELS[0]) / "input" / "manifest.json").read_bytes()).hexdigest()
        != original_manifest_sha256
    )

    submit = _SubmitRecorder(site, journal=journal)
    receipt = run_replay(
        site.config(
            execute=True,
            resume_from=resume_from,
            receipt_path=site.root / "attempt-2-receipt.json",
        ),
        submit_pass=submit,
        journal_probe=journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert receipt["outcome"] == "completed"
    resumed_rows = [row for row in receipt["rows"] if row["cycle"] == CYCLE_2.strftime("%Y%m%d%H")]
    assert len(resumed_rows) == len(MODELS)
    for row in resumed_rows:
        assert row["status"] == "completed"
        assert row["prior"]["prior_source"] == "resumed_receipt"
        # the ONLY pre-image that ever existed, not a re-read of the replayed tree
        assert row["prior"]["run_manifest_sha256"] == original_manifest_sha256
    # cycle 1 was verified-skipped from the same receipt
    assert {row["status"] for row in receipt["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")} == {
        "verified_skip"
    }
    assert resume_from.read_bytes() == resume_bytes_before
    jsonschema.validate(receipt, _SCHEMA)


class _DriftLastModelSubmit(_SubmitRecorder):
    """Replay pass whose LAST model comes back on a different river network.

    A model package pinned to the wrong river-network version reproduces the
    drift on every attempt -- which is the point: a resumed attempt must judge
    the ORIGINAL pre-image and halt again, not skip past the drift.
    """

    def __init__(self, site: _Site, *, drift_cycle: datetime, **kwargs: Any) -> None:
        super().__init__(site, **kwargs)
        self.drift_cycle = drift_cycle

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((list(argv), dict(env)))
        cycle = _cycle_from_argv(argv)
        for model_id in MODELS:
            drifts = model_id == MODELS[-1] and cycle == self.drift_cycle
            self.site.write_run(
                cycle,
                model_id,
                _manifest(
                    marker="replayed",
                    river_network_version_id="rn-2026b" if drifts else "rn-2026a",
                ),
            )
        self.site.add_state_entries(cycle, checksum_prefix="new")
        self.journal.record_replay_terminal(cycle)
        return {"returncode": 0}


def test_resume_reruns_a_drifted_cycle_and_halts_again_on_the_original_prior(site: _Site) -> None:
    """B3-1: the skip gate needs the status/assertion filter, not just a checksum.

    The last model of cycle 2 comes back on a different river network, so the
    driver halts on ``key_consistency_drift``.  That row is ``planned`` and
    carries the failed assertion, yet its recorded ``new.state_checksum`` DOES
    match the live index -- the checksum-only gate skipped the whole cycle on
    resume and shipped the drift as "verified".
    """

    drifting = _DriftLastModelSubmit(site, drift_cycle=CYCLE_2)
    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=drifting,
            journal_probe=drifting.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )
    first = excinfo.value.receipt
    assert first["interruption"]["reason"] == "key_consistency_drift"
    assert first["interruption"]["cycle"] == CYCLE_2.strftime("%Y%m%d%H")
    drifted_row = next(
        row
        for row in first["rows"]
        if row["cycle"] == CYCLE_2.strftime("%Y%m%d%H") and row["model_id"] == MODELS[-1]
    )
    original_prior_sha256 = drifted_row["prior"]["run_manifest_sha256"]
    # the premise of the defect: the live index really does carry what that row
    # recorded, so a checksum-only gate would have skipped the cycle
    assert drifted_row["new"]["state_checksum"] in {
        entry["checksum"] for entry in site.index_entries()
    }

    resume_from = site.root / "attempt-1-receipt.json"
    _write_json(resume_from, first)
    resumed = _DriftLastModelSubmit(site, drift_cycle=CYCLE_2, journal=drifting.journal)

    with pytest.raises(ReplayHalted) as second_excinfo:
        run_replay(
            site.config(
                execute=True,
                resume_from=resume_from,
                receipt_path=site.root / "attempt-2-receipt.json",
            ),
            submit_pass=resumed,
            journal_probe=resumed.journal,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    receipt = second_excinfo.value.receipt
    jsonschema.validate(receipt, _SCHEMA)
    # the drift is re-judged against the original pre-image and halts again
    assert receipt["interruption"]["reason"] == "key_consistency_drift"
    assert receipt["interruption"]["cycle"] == CYCLE_2.strftime("%Y%m%d%H")
    assert [_cycle_from_argv(argv) for argv, _env in resumed.calls] == [CYCLE_2]
    cycle_2_rows = [row for row in receipt["rows"] if row["cycle"] == CYCLE_2.strftime("%Y%m%d%H")]
    assert "verified_skip" not in {row["status"] for row in cycle_2_rows}
    for row in cycle_2_rows:
        assert row["prior"]["prior_source"] == "resumed_receipt"
    rerun_row = next(row for row in cycle_2_rows if row["model_id"] == MODELS[-1])
    assert rerun_row["prior"]["run_manifest_sha256"] == original_prior_sha256
    assert rerun_row["key_consistency"]["status"] == "drift"
    # cycle 1 finished cleanly and is still allowed the verified skip
    assert {row["status"] for row in receipt["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")} == {
        "verified_skip"
    }
    assert receipt["resume_from"] == {
        "path": str(resume_from),
        "sha256": hashlib.sha256(resume_from.read_bytes()).hexdigest(),
    }


def test_a_three_hop_resume_chain_never_loses_a_cycles_pre_image(site: _Site) -> None:
    """B3-2: a middle attempt with a narrower range must not break the chain.

    Attempt 2 resumes for cycle 2 only.  Pre-change its receipt named cycle 2
    alone, so attempt 3 -- resuming from it -- found no row for cycle 1 and
    re-captured that "prior" from the tree attempt 1 had already replayed, then
    labelled the re-capture ``captured``.  Every row is now carried forward
    verbatim, so the original pre-image survives an arbitrarily long chain.
    """

    first = _completed_run(site)
    original_priors = {
        (row["cycle"], row["model_id"]): row["prior"]["run_manifest_sha256"] for row in first["rows"]
    }
    hop_1 = site.root / "hop-1-receipt.json"
    _write_json(hop_1, first)

    # attempt 2: cycle 2 only, exactly as a narrowed --start-cycle resume
    hop_2 = site.root / "hop-2-receipt.json"
    second = run_replay(
        site.config(execute=True, cycles=(CYCLE_2,), resume_from=hop_1, receipt_path=hop_2),
        submit_pass=_refuse_to_submit,
        journal_probe=_original_terminals_only,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )
    jsonschema.validate(second, _SCHEMA)
    carried = [row for row in second["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")]
    assert len(carried) == len(MODELS)
    assert carried == [row for row in first["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")]

    # attempt 3: the whole range again, with cycle 1's state evidence drifted so
    # it must genuinely re-run rather than verified-skip
    entries = site.index_entries()
    for entry in entries:
        if entry["valid_time"] == _format_time(CYCLE_1 + timedelta(hours=12)):
            entry["checksum"] = "sha256:index-drifted"
    site._write_index(entries)
    submit = _SubmitRecorder(site)
    third = run_replay(
        site.config(
            execute=True,
            resume_from=hop_2,
            receipt_path=site.root / "hop-3-receipt.json",
        ),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(third, _SCHEMA)
    assert third["outcome"] == "completed"
    assert [_cycle_from_argv(argv) for argv, _env in submit.calls] == [CYCLE_1]
    rerun_rows = [row for row in third["rows"] if row["cycle"] == CYCLE_1.strftime("%Y%m%d%H")]
    assert len(rerun_rows) == len(MODELS)
    for row in rerun_rows:
        assert row["status"] == "completed"
        # the pre-image from attempt 1, three hops back -- never the replayed tree
        assert row["prior"]["prior_source"] == "resumed_receipt"
        assert row["prior"]["run_manifest_sha256"] == original_priors[(row["cycle"], row["model_id"])]
    assert third["resume_from"]["path"] == str(hop_2)
    assert third["resume_from"]["sha256"] == hashlib.sha256(hop_2.read_bytes()).hexdigest()


def test_a_three_hop_chain_through_a_HALT_never_loses_a_cycles_pre_image(site: _Site) -> None:
    """B4-1: the middle attempt covers the whole window and stops early.

    This is the branch the static in-scope exclusion could not see.  Attempt 2
    asks for cycles 1 AND 2, halts on cycle 1's submission, and therefore never
    produces a cycle-2 row -- yet cycle 2 was in its window, so the round-3
    ``carried_rows()`` dropped attempt 1's rows for it.  Attempt 3, resuming
    from that holed receipt, then re-captured cycle 2's "prior" from the tree
    attempt 1 had already replayed and labelled the re-capture ``captured``:
    the only pre-image that ever existed, gone, with the key-consistency
    assertion degenerating to replay-vs-replay.
    """

    first = _completed_run(site)
    original_priors = {
        (row["cycle"], row["model_id"]): row["prior"]["run_manifest_sha256"] for row in first["rows"]
    }
    cycle_1_token = CYCLE_1.strftime("%Y%m%d%H")
    cycle_2_token = CYCLE_2.strftime("%Y%m%d%H")
    first_cycle_2_rows = [row for row in first["rows"] if row["cycle"] == cycle_2_token]
    hop_1 = site.root / "hop-1-receipt.json"
    _write_json(hop_1, first)

    # every cycle's live state drifted away from what attempt 1 recorded, so
    # nothing is verified-skippable and both attempts really re-run
    entries = site.index_entries()
    for entry in entries:
        entry["checksum"] = "sha256:index-drifted"
    site._write_index(entries)

    # attempt 2: the FULL window, stopped at the first cycle by a rejected pass
    calls: list[datetime] = []

    def _submission_rejected(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        calls.append(_cycle_from_argv(argv))
        return {"returncode": 1, "stdout_tail": "", "stderr_tail": "slurm refused"}

    hop_2 = site.root / "hop-2-receipt.json"
    with pytest.raises(ReplayHalted) as excinfo:
        run_replay(
            site.config(execute=True, resume_from=hop_1, receipt_path=hop_2),
            submit_pass=_submission_rejected,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    second = excinfo.value.receipt
    assert calls == [CYCLE_1]
    assert second["outcome"] == "halted"
    assert second["interruption"]["reason"] == "submission_failed"
    assert second["interruption"]["cycle"] == cycle_1_token
    jsonschema.validate(second, _SCHEMA)
    # the halted receipt on disk is what attempt 3 will read
    persisted = json.loads(hop_2.read_text(encoding="utf-8"))
    assert persisted["rows"] == second["rows"]
    # THE POINT: the cycle attempt 2 never reached keeps attempt 1's rows verbatim
    assert [row for row in persisted["rows"] if row["cycle"] == cycle_2_token] == first_cycle_2_rows
    assert {row["status"] for row in persisted["rows"] if row["cycle"] == cycle_1_token} == {"halted"}

    # attempt 3: resumes from the halted receipt and re-runs both cycles
    submit = _SubmitRecorder(site)
    third = run_replay(
        site.config(
            execute=True,
            resume_from=hop_2,
            receipt_path=site.root / "hop-3-receipt.json",
        ),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(third, _SCHEMA)
    assert third["outcome"] == "completed"
    assert [_cycle_from_argv(argv) for argv, _env in submit.calls] == [CYCLE_1, CYCLE_2]
    rerun_rows = [row for row in third["rows"] if row["cycle"] == cycle_2_token]
    assert len(rerun_rows) == len(MODELS)
    for row in rerun_rows:
        assert row["status"] == "completed"
        assert row["prior"]["prior_source"] == "resumed_receipt"
        assert row["prior"]["run_manifest_sha256"] == original_priors[(row["cycle"], row["model_id"])]
    assert third["resume_from"]["path"] == str(hop_2)


def test_a_resume_receipt_from_the_other_source_is_refused(site: _Site) -> None:
    """B4-2: ``(cycle, model)`` keys collide exactly across IFS and GFS.

    Same windows, same six model ids, receipts written side by side in one
    directory.  Adopting the other source's rows hands this pass foreign
    pre-images under ``prior_source="resumed_receipt"`` while every row's own
    ``source_id`` still says GFS, and key consistency passes because both
    sources share the river network -- the real pre-image is destroyed by the
    replay with nothing left to notice.
    """

    ifs_receipt = _completed_run(site)
    assert ifs_receipt["source_id"] == "IFS"
    resume_from = site.root / "ifs-receipt.json"
    _write_json(resume_from, ifs_receipt)
    # this pass is the GFS one: its own reset receipt covers the GFS scope
    site.write_reset_receipt(source="GFS")
    gfs_receipt_path = site.root / "gfs-receipt.json"

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(
                execute=True,
                source_id="GFS",
                resume_from=resume_from,
                receipt_path=gfs_receipt_path,
            ),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "resume_receipt_scope_mismatch"
    assert excinfo.value.details["receipt_source_id"] == "IFS"
    assert excinfo.value.details["config_source_id"] == "gfs"
    # refused before the pre-flight write: nothing submitted, nothing written
    assert not gfs_receipt_path.exists()
    assert resume_from.read_bytes() == json.dumps(ifs_receipt, indent=2, sort_keys=True).encode("utf-8")


def test_a_resume_receipt_missing_one_of_this_passs_models_is_refused(site: _Site) -> None:
    """A model this pass replays but the receipt never covered has no pre-image.

    "Never covered" means the ROWS are absent, not merely that the declared
    ``model_ids`` field is narrow: since the full-row carry a narrowed pass
    still carries every model's rows, and refusing on the declaration alone
    rejects a receipt that holds the pre-image (round-5 B5-1).
    """

    first = _completed_run(site)
    narrowed = {
        **first,
        "model_ids": [MODELS[0]],
        "rows": [row for row in first["rows"] if row["model_id"] != MODELS[1]],
    }
    resume_from = site.root / "narrow-receipt.json"
    _write_json(resume_from, narrowed)

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(execute=True, resume_from=resume_from, receipt_path=site.root / "next.json"),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "resume_receipt_scope_mismatch"
    assert excinfo.value.details["missing_model_ids"] == [MODELS[1]]


def test_a_narrowed_model_resume_from_a_superset_receipt_still_runs(site: _Site) -> None:
    """The subset direction is legitimate: a narrowed resume must keep working."""

    first = _completed_run(site)
    resume_from = site.root / "full-receipt.json"
    _write_json(resume_from, first)

    receipt = run_replay(
        site.config(
            execute=True,
            model_ids=(MODELS[0],),
            resume_from=resume_from,
            receipt_path=site.root / "narrowed-receipt.json",
        ),
        submit_pass=_refuse_to_submit,
        journal_probe=_original_terminals_only,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(receipt, _SCHEMA)
    assert receipt["outcome"] == "completed"
    assert {row["status"] for row in receipt["rows"] if row["model_id"] == MODELS[0]} == {"verified_skip"}
    # the model this pass dropped keeps attempt 1's rows verbatim
    assert [row for row in receipt["rows"] if row["model_id"] == MODELS[1]] == [
        row for row in first["rows"] if row["model_id"] == MODELS[1]
    ]


def test_widening_the_model_set_back_resumes_from_the_narrowed_pass(site: _Site) -> None:
    """B5-1: narrow -> fix -> widen back is the runbook's own recovery path.

    §3 tells the operator to drop a model from the scope when its forcing is
    absent, and to resume from "上一份" every time.  The narrowed pass's receipt
    DECLARES only the narrowed model while carrying every model's rows verbatim
    (that is the whole point of the full-row carry), so judging coverage by the
    declaration refused the widen-back hop -- with the pre-image sitting in the
    receipt it just refused.  The only way past a refusal is resuming from an
    older receipt, which is exactly the pre-image loss B4-1 closed.
    """

    first = _completed_run(site)
    original_priors = {
        (row["cycle"], row["model_id"]): row["prior"]["run_manifest_sha256"] for row in first["rows"]
    }
    hop_1 = site.root / "widen-hop-1.json"
    _write_json(hop_1, first)

    # hop 2: the narrowed pass, exactly as the runbook prescribes
    hop_2 = site.root / "widen-hop-2.json"
    second = run_replay(
        site.config(
            execute=True,
            model_ids=(MODELS[0],),
            resume_from=hop_1,
            receipt_path=hop_2,
        ),
        submit_pass=_refuse_to_submit,
        journal_probe=_original_terminals_only,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )
    # the premise: declared scope is narrow, carried rows are not
    assert second["model_ids"] == [MODELS[0]]
    assert {row["model_id"] for row in second["rows"]} == set(MODELS)

    # the dropped model's forcing has been restored, so hop 3 widens back; the
    # live state drifted meanwhile, so both cycles genuinely re-run
    entries = site.index_entries()
    for entry in entries:
        entry["checksum"] = "sha256:index-drifted"
    site._write_index(entries)
    submit = _SubmitRecorder(site)

    third = run_replay(
        site.config(
            execute=True,
            resume_from=hop_2,
            receipt_path=site.root / "widen-hop-3.json",
        ),
        submit_pass=submit,
        journal_probe=submit.journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    jsonschema.validate(third, _SCHEMA)
    assert third["outcome"] == "completed"
    assert [_cycle_from_argv(argv) for argv, _env in submit.calls] == list(site.cycles)
    widened_rows = [row for row in third["rows"] if row["model_id"] == MODELS[1]]
    assert len(widened_rows) == len(site.cycles)
    for row in widened_rows:
        assert row["status"] == "completed"
        # the pre-image travelled through the narrowed hop untouched
        assert row["prior"]["prior_source"] == "resumed_receipt"
        assert row["prior"]["run_manifest_sha256"] == original_priors[(row["cycle"], row["model_id"])]


def test_a_run_without_resume_records_no_resume_source(site: _Site) -> None:
    receipt = _completed_run(site)

    jsonschema.validate(receipt, _SCHEMA)
    assert "resume_from" not in receipt
    assert "resume_from" not in json.loads(site.receipt_path.read_text(encoding="utf-8"))


def test_the_replacement_receipt_is_readable_by_the_node_27_consumer(site: _Site) -> None:
    """C3-1: all four receipts are loaded on node-27 under a different uid.

    ``safe_fs``'s 0600 default is right for node-22-only receipts; this one is
    shared evidence, and the runbook's ``test -r`` precondition depends on it.
    """

    run_replay(
        site.config(),
        submit_pass=_refuse_to_submit,
        journal_probe=_original_terminals_only,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert stat.S_IMODE(site.receipt_path.stat().st_mode) == 0o644


def test_writing_the_receipt_to_the_resume_source_refuses(site: _Site) -> None:
    """B2-1: the first per-cycle write would overwrite the only pre-image record."""

    first = _completed_run(site)
    resume_from = site.root / "first-receipt.json"
    _write_json(resume_from, first)
    before = resume_from.read_bytes()
    (site.root / "sub").mkdir()

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(
                execute=True,
                resume_from=resume_from,
                receipt_path=site.root / "sub" / ".." / "first-receipt.json",
            ),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "receipt_path_is_resume_source"
    assert resume_from.read_bytes() == before


def test_an_unreadable_resume_receipt_refuses(site: _Site) -> None:
    resume_from = site.root / "not-a-receipt.json"
    resume_from.write_text("{}", encoding="utf-8")

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(execute=True, resume_from=resume_from),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "resume_receipt_unreadable"


# ---------------------------------------------------------------------------
# receipt resilience (round-2 B2-2) and reset-receipt scope (round-2 B2-4)
# ---------------------------------------------------------------------------


def test_an_unwritable_receipt_path_refuses_before_any_submission(site: _Site) -> None:
    """B2-2: the write must be attempted BEFORE the first cycle is overwritten."""

    locked_dir = site.root / "locked"
    locked_dir.mkdir()
    locked_dir.chmod(0o500)
    try:
        with pytest.raises(ReplayDriverRefused) as excinfo:
            run_replay(
                site.config(execute=True, receipt_path=locked_dir / "receipt.json"),
                submit_pass=_refuse_to_submit,
                journal_probe=_original_terminals_only,
                clock=_Clock(),
                sleep=lambda _seconds: None,
            )
    finally:
        locked_dir.chmod(0o700)

    assert excinfo.value.reason == "receipt_path_unwritable"
    assert not (locked_dir / "receipt.json").exists()


def test_a_mid_run_receipt_write_failure_halts_instead_of_printing(site: _Site) -> None:
    """B2-2: a swallowed write left the sequence running with no receipt at all."""

    receipt_dir = site.root / "receipts"
    receipt_dir.mkdir()
    submit = _SubmitRecorder(site)

    def _submit_then_lock(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
        result = submit(argv, env)
        receipt_dir.chmod(0o500)
        return result

    try:
        with pytest.raises(ReplayHalted) as excinfo:
            run_replay(
                site.config(execute=True, receipt_path=receipt_dir / "receipt.json"),
                submit_pass=_submit_then_lock,
                journal_probe=submit.journal,
                clock=_Clock(),
                sleep=lambda _seconds: None,
            )
    finally:
        receipt_dir.chmod(0o700)

    assert excinfo.value.reason == "receipt_write_failed"
    assert excinfo.value.receipt["outcome"] == "in_progress"
    # exactly one cycle was submitted before the halt; the sequence stopped
    assert len(submit.calls) == 1


def test_a_reset_receipt_scoped_to_another_source_refuses(site: _Site) -> None:
    """B2-4: a globally non-empty receipt from the wrong scope is not evidence.

    Every ``prior.state`` would be null and ``replaced_successor_entries`` would
    then treat an empty prior checksum as "any checksum counts as replaced" --
    the convergence oracle silently collapses to its journal leg over a scope
    that was never cleared.
    """

    site.write_reset_receipt(scopes=[{"model_id": model_id, "source_id": "ifs"} for model_id in MODELS])
    config = ReplayDriverConfig(**{**site.config(execute=True).__dict__, "source_id": "gfs"})

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            config,
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_scope_mismatch"
    assert excinfo.value.details["uncovered_scopes"] == [
        {"model_id": model_id, "source_id": "gfs"} for model_id in sorted(MODELS)
    ]


def test_a_reset_receipt_missing_one_model_scope_refuses(site: _Site) -> None:
    site.write_reset_receipt(scopes=[{"model_id": MODELS[0], "source_id": SOURCE}])

    with pytest.raises(ReplayDriverRefused) as excinfo:
        run_replay(
            site.config(execute=True),
            submit_pass=_refuse_to_submit,
            journal_probe=_original_terminals_only,
            clock=_Clock(),
            sleep=lambda _seconds: None,
        )

    assert excinfo.value.reason == "reset_receipt_scope_mismatch"
    assert excinfo.value.details["uncovered_scopes"] == [{"model_id": MODELS[1], "source_id": SOURCE}]


# ---------------------------------------------------------------------------
# repair parameter set
# ---------------------------------------------------------------------------


def test_the_repair_parameter_set_applies_only_to_the_pinned_repair_cycles(tmp_path: Path) -> None:
    base = _Site(tmp_path, cycles=(CYCLE_1,)).config()
    gfs = ReplayDriverConfig(**{**base.__dict__, "source_id": "gfs"})
    ifs = ReplayDriverConfig(**{**base.__dict__, "source_id": "IFS"})

    for config, cycle, stamp in (
        (gfs, datetime(2026, 7, 7, 12, tzinfo=UTC), "2026-07-07T12:00:00Z"),
        (ifs, datetime(2026, 7, 10, 12, tzinfo=UTC), "2026-07-10T12:00:00Z"),
    ):
        repaired = submit_env(config, cycle)
        assert repaired[REPAIR_ENV] == "1"
        assert repaired[REPAIR_CYCLE_ENV] == stamp

    for config, cycle in (
        (gfs, datetime(2026, 7, 7, 0, tzinfo=UTC)),
        (gfs, datetime(2026, 7, 8, 12, tzinfo=UTC)),
        (ifs, datetime(2026, 7, 7, 12, tzinfo=UTC)),
        (gfs, datetime(2026, 7, 10, 12, tzinfo=UTC)),
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
            fixture.write_forcing(cycle, model_id, source="gfs")

    calls: list[tuple[str, str | None]] = []
    journal = _JournalTerminals()

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
                    "created_at": None,
                    "state_id": f"new-{model_id}",
                    "checksum": f"sha256:new-{model_id}",
                }
            )
        fixture._write_index(entries)
        journal.record_replay_terminal(cycle)
        return {"returncode": 0}

    fixture.write_reset_receipt(
        removed=[
            {
                "model_id": model_id,
                "source_id": "gfs",
                "valid_time": _format_time(cycle + timedelta(hours=12)),
                "created_at": None,
                "state_id": f"old-{model_id}",
                "checksum": f"sha256:old-{model_id}",
            }
            for cycle in fixture.cycles
            for model_id in MODELS
        ],
        source="gfs",
    )

    run_replay(
        config,
        submit_pass=_submit,
        journal_probe=journal,
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )

    assert calls == [("2026070712", "1"), ("2026070800", "false")]


# ---------------------------------------------------------------------------
# terminal completion probe shape (round-2 B2-6)
# ---------------------------------------------------------------------------


def test_cohort_terminal_job_is_attributed_to_every_requested_model() -> None:
    """Production shape: the completion record is cohort scoped, ``model_id`` null."""

    jobs = [
        {
            "job_id": "job_cycle_ifs_2026070500_state_save_qc_cohort_abc123_state_save_qc",
            "run_id": "cycle_ifs_2026070500_state_save_qc_cohort_abc123",
            "model_id": None,
            "status": "succeeded",
            "stage": "state_save_qc",
        }
    ]

    assert replay_capture.terminal_completion_job_ids(jobs, model_ids=MODELS) == {
        model_id: ["job_cycle_ifs_2026070500_state_save_qc_cohort_abc123_state_save_qc"]
        for model_id in MODELS
    }


def test_a_model_scoped_terminal_job_is_attributed_to_that_model_only() -> None:
    jobs = [
        {"job_id": "job-alpha", "model_id": MODELS[0], "status": "succeeded", "stage": "state_save_qc"},
    ]

    assert replay_capture.terminal_completion_job_ids(jobs, model_ids=MODELS) == {
        MODELS[0]: ["job-alpha"],
        MODELS[1]: [],
    }


@pytest.mark.parametrize(
    "job",
    [
        pytest.param(
            {"job_id": "job-running", "model_id": None, "status": "running", "stage": "state_save_qc"},
            id="non-terminal-status",
        ),
        pytest.param(
            {"job_id": "job-forecast", "model_id": None, "status": "succeeded", "stage": "forecast"},
            id="non-terminal-stage",
        ),
        pytest.param(
            {"job_id": "", "model_id": None, "status": "succeeded", "stage": "state_save_qc"},
            id="empty-job-id",
        ),
        pytest.param(
            {"job_id": "job-other", "model_id": "dg_not_in_scope", "status": "succeeded", "stage": "state_save_qc"},
            id="out-of-scope-model",
        ),
    ],
)
def test_non_terminal_or_out_of_scope_jobs_are_not_completion_evidence(job: Mapping[str, Any]) -> None:
    assert replay_capture.terminal_completion_job_ids([job], model_ids=MODELS) == {
        model_id: [] for model_id in MODELS
    }


def test_duplicate_terminal_job_ids_are_deduped_and_sorted() -> None:
    jobs = [
        {"job_id": "job-b", "model_id": None, "status": "succeeded", "stage": "state_save_qc"},
        {"job_id": "job-b", "model_id": MODELS[0], "status": "published", "stage": "publish"},
        {"job_id": "job-a", "model_id": None, "status": "complete", "stage": "parse"},
    ]

    assert replay_capture.terminal_completion_job_ids(jobs, model_ids=MODELS) == {
        model_id: ["job-a", "job-b"] for model_id in MODELS
    }


def test_default_journal_probe_reads_a_real_cohort_record(site: _Site) -> None:
    """End-to-end: the probe's shape assumption is pinned against the real writer."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from workers.data_adapters.base import cycle_id_for

    repository = FileOrchestrationJournalRepository(str(site.journal_root))
    cycle_id = cycle_id_for(SOURCE, CYCLE_1)
    stamp = CYCLE_1.strftime("%Y%m%d%H")
    cohort_run_id = f"cycle_{SOURCE.lower()}_{stamp}_forecast_cohort_abc123"
    repository.upsert_pipeline_job(
        {
            "job_id": f"job_{cohort_run_id}_state_save_qc",
            "idempotency_key": f"{SOURCE.lower()}:{cycle_id}:cohort:state_save_qc:abc123",
            "run_id": cohort_run_id,
            "cycle_id": cycle_id,
            "source_id": SOURCE.lower(),
            "cycle_time": _format_time(CYCLE_1),
            "job_type": "run_state_save_qc",
            "slurm_job_id": "7002",
            "model_id": None,
            "status": "succeeded",
            "stage": "state_save_qc",
            "submitted_at": _format_time(CYCLE_1),
            "created_at": _format_time(CYCLE_1),
        }
    )

    probe = replay_driver.default_journal_probe(site.config(), CYCLE_1)

    assert set(probe) == set(MODELS)
    for model_id in MODELS:
        assert probe[model_id] == [f"job_{cohort_run_id}_state_save_qc"]
    # a cycle with no records is empty, not "done"
    assert replay_driver.default_journal_probe(site.config(), CYCLE_2) == {model_id: [] for model_id in MODELS}


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
        journal_probe=submit.journal,
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
