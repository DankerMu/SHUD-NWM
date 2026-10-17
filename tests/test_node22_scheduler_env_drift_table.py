"""#2917 -- the node-22 scheduler env live-drift table matches the loaders.

``infra/env/README.md`` classifies the seven keys the live node-22
``compute.scheduler-dbfree.env`` lacks. Each row states the value the loader
applies when only that key is absent. These cases parse the table out of the
real README and call the real loaders with that key removed from the process
environment, so a changed loader default (or a hand-edited table cell) reds
here instead of leaving operators with a stale classification.
"""

from __future__ import annotations

import dataclasses
import importlib
import re
from pathlib import Path

import pytest

from scripts import node22_scheduler_evidence_retention as retention
from services.orchestrator import scheduler_lease

# The scheduler module re-exports the dataclass defined in scheduler_config/config.py;
# importing config.py first trips the package's import cycle.
from services.orchestrator.scheduler import ProductionSchedulerConfig
from tests.test_env_templates import parse_env_template

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "infra/env/README.md"
TEMPLATE = REPO_ROOT / "infra/env/compute.scheduler-dbfree.env.example"
RECOVERY_RUNBOOK = REPO_ROOT / "docs/runbooks/node22-control-plane-manual-recovery.md"

HEADING = "## compute.scheduler-dbfree.env live-drift classification (2026-10-16)"
HEADER_ROW = "| key | status | effective value when unset | operator action |"
LEGACY_LEASE_KEY = "NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE"
KEEP_UNSET = "保持未设置"

# The spec's seven keys, with the status cell each one must carry verbatim.
EXPECTED_STATUS: dict[str, str] = {
    "NHMS_SCHEDULER_LEASE_GUARD_MODE": "optional, example differs (example: atomic, default: flock)",
    "NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS": "optional (effective default: 300)",
    "NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT": "optional (effective default: 3)",
    "NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES": "optional (effective default: 3)",
    "NHMS_SCHEDULER_TIMING_LEVEL": "optional (effective default: stage)",
    "NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS": "optional (effective default: 90)",
    "NHMS_SCHEDULER_EVIDENCE_MAX_MB": "optional (effective default: 512)",
}
CONFIG_FIELD_FOR_KEY: dict[str, str] = {
    "NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS": "restart_reconcile_absence_seconds",
    "NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT": "identity_blocked_streak_limit",
    "NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES": "no_progress_circuit_passes",
    "NHMS_SCHEDULER_TIMING_LEVEL": "timing_level",
}
RETENTION_KEYS = ("NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS", "NHMS_SCHEDULER_EVIDENCE_MAX_MB")

OPTIONAL_DEFAULT = re.compile(r"^optional \(effective default: (?P<default>[^()]+)\)$")
OPTIONAL_DIFFERS = re.compile(
    r"^optional, example differs \(example: (?P<example>[^(),]+), default: (?P<default>[^()]+)\)$"
)
ROW_KEY = re.compile(r"^`(?P<key>[A-Z][A-Z0-9_]*)`$")
ACTION_ASSIGNMENT = re.compile(r"^`(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>[^`#\s]+)`$")


@dataclasses.dataclass(frozen=True)
class DriftRow:
    key: str
    status: str
    effective: str
    action: str


def drift_section(markdown: str) -> list[str]:
    """Return the lines after the drift heading, up to the next heading or EOF."""
    lines = markdown.splitlines()
    starts = [index for index, line in enumerate(lines) if line == HEADING]
    assert len(starts) == 1, f"expected exactly one {HEADING!r} heading, found {len(starts)}"
    section: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("#"):
            break
        section.append(line)
    return section


def parse_drift_table(markdown: str) -> list[DriftRow]:
    table = [line.strip() for line in drift_section(markdown) if line.strip().startswith("|")]
    assert table[:2] == [HEADER_ROW, "| --- | --- | --- | --- |"], table[:2]
    rows: list[DriftRow] = []
    for line in table[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 4, f"malformed drift row: {line!r}"
        key_match = ROW_KEY.match(cells[0])
        assert key_match is not None, f"key cell must be one backticked KEY: {cells[0]!r}"
        rows.append(DriftRow(key_match.group("key"), cells[1], cells[2].strip("`"), cells[3]))
    return rows


def stated_default(status: str) -> str | None:
    """Return the effective default a status cell states; ``None`` for ``required``."""
    if status == "required":
        return None
    for pattern in (OPTIONAL_DEFAULT, OPTIONAL_DIFFERS):
        match = pattern.match(status)
        if match is not None:
            return match.group("default").strip()
    raise AssertionError(f"status is none of the three allowed forms: {status!r}")


def required_rows_have_runbook_step(rows: list[DriftRow], runbook_text: str) -> list[str]:
    """Return one violation per ``required`` row whose ``KEY=`` step the runbook lacks."""
    return [
        f"{row.key}: required, but the recovery runbook has no `{row.key}=` step"
        for row in rows
        if row.status == "required" and f"{row.key}=" not in runbook_text
    ]


@pytest.fixture(scope="module")
def rows() -> list[DriftRow]:
    return parse_drift_table(README.read_text(encoding="utf-8"))


def _row(rows: list[DriftRow], key: str) -> DriftRow:
    return next(row for row in rows if row.key == key)


def test_table_carries_exactly_the_spec_keys_once_and_none_required(rows: list[DriftRow]) -> None:
    keys = [row.key for row in rows]
    assert len(keys) == len(set(keys)), f"duplicate rows: {keys}"
    assert set(keys) == set(EXPECTED_STATUS)
    assert [row.key for row in rows if row.status == "required"] == []


@pytest.mark.parametrize("key", sorted(EXPECTED_STATUS))
def test_status_cell_is_the_pinned_literal_and_agrees_with_the_effective_column(rows: list[DriftRow], key: str) -> None:
    row = _row(rows, key)
    assert row.status == EXPECTED_STATUS[key]
    assert stated_default(row.status) == row.effective


@pytest.mark.parametrize("key", sorted(EXPECTED_STATUS))
def test_example_value_matches_the_status_cell(rows: list[DriftRow], key: str) -> None:
    # parse_env_template drops the trailing `# ...` commentary three of these lines carry.
    example = parse_env_template(TEMPLATE.read_text(encoding="utf-8"))
    assert key in example, f"{key} is not in {TEMPLATE.name}"
    row = _row(rows, key)
    differs = OPTIONAL_DIFFERS.match(row.status)
    if differs is None:
        assert example[key] == row.effective
    else:
        assert example[key] == differs.group("example")
        assert example[key] != row.effective


def test_lease_guard_mode_unset_with_legacy_key_unset_yields_flock(
    rows: list[DriftRow], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NHMS_SCHEDULER_LEASE_GUARD_MODE", raising=False)
    monkeypatch.delenv(LEGACY_LEASE_KEY, raising=False)

    assert scheduler_lease._file_lock_guard_mode() == _row(rows, "NHMS_SCHEDULER_LEASE_GUARD_MODE").effective


@pytest.mark.parametrize("key", sorted(CONFIG_FIELD_FOR_KEY))
def test_scheduler_config_field_default_matches_the_table(
    rows: list[DriftRow], key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(key, raising=False)
    config_module = importlib.import_module("services.orchestrator.scheduler_config.config")
    assert ProductionSchedulerConfig is config_module.ProductionSchedulerConfig
    fields = {field.name: field for field in dataclasses.fields(ProductionSchedulerConfig)}
    field = fields[CONFIG_FIELD_FOR_KEY[key]]
    assert field.default_factory is not dataclasses.MISSING, f"{field.name} has no default_factory"

    # default_factory() skips __post_init__, whose path resolution needs a full environment.
    assert str(field.default_factory()) == _row(rows, key).effective


@pytest.mark.parametrize("key", RETENTION_KEYS)
def test_evidence_retention_default_matches_the_table(
    rows: list[DriftRow], key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(key, raising=False)

    config, blockers = retention.config_from_env(
        retention.build_parser().parse_args(["--evidence-root", str(tmp_path)])
    )

    assert blockers == []
    assert config is not None
    effective = int(_row(rows, key).effective)
    if key == "NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS":
        assert config.retention_days == effective
    else:
        assert config.max_bytes == effective * 1024 * 1024


@pytest.mark.parametrize("key", sorted(EXPECTED_STATUS))
def test_operator_action_is_a_bare_assignment_or_keep_unset(rows: list[DriftRow], key: str) -> None:
    action = _row(rows, key).action
    assignment = ACTION_ASSIGNMENT.match(action)
    if assignment is not None:
        assert assignment.group("key") == key
    else:
        assert action.startswith(KEEP_UNSET), action
    assert "example" not in action.lower(), "never tell operators to copy a template line"


def test_lease_action_records_and_defers_without_setting_the_legacy_key(rows: list[DriftRow]) -> None:
    action = _row(rows, "NHMS_SCHEDULER_LEASE_GUARD_MODE").action
    assert action.startswith(KEEP_UNSET)
    assert "另行决策" in action
    assert "legacy" in action
    assert LEGACY_LEASE_KEY not in action


def test_required_rows_have_a_recovery_runbook_step(rows: list[DriftRow]) -> None:
    assert required_rows_have_runbook_step(rows, RECOVERY_RUNBOOK.read_text(encoding="utf-8")) == []


def test_runbook_step_helper_reports_a_required_row_without_a_step() -> None:
    required = DriftRow("NHMS_SYNTHETIC_DRIFT_KEY", "required", "-", "`NHMS_SYNTHETIC_DRIFT_KEY=1`")
    optional = DriftRow("NHMS_SYNTHETIC_OTHER_KEY", "optional (effective default: 1)", "1", KEEP_UNSET)
    runbook_without_step = "set `NHMS_SYNTHETIC_OTHER_KEY=1` and restart"

    assert required_rows_have_runbook_step([required, optional], runbook_without_step) == [
        "NHMS_SYNTHETIC_DRIFT_KEY: required, but the recovery runbook has no `NHMS_SYNTHETIC_DRIFT_KEY=` step"
    ]
    assert required_rows_have_runbook_step([required], "echo NHMS_SYNTHETIC_DRIFT_KEY=1 >> env") == []
