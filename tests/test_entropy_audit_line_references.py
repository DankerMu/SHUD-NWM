"""The ``hard-line-reference`` hard-gate family (#2648, 16th partition).

A comment or docstring in shipped Python that names a source position as a file
name plus line number rots silently. The family freezes the existing references
in a baseline keyed by (path, reference text) and fails on any reference whose
per-file count exceeds it. These cases drive ``build_report`` on synthetic
trees plus the real repository; the shared fixture builders live in
``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.governance import audit_repo_entropy
from scripts.governance.entropy_audit import check_line_references
from tests.entropy_audit_helpers import (
    REPO_ROOT,
    _repo_report,
    _setup_clean_hard_gate_fixture,
    _write,
)

CHECK_ID = "hard-line-reference"
# Measured with this family's own scanner on the pre-#2648 tree (232 references
# in 44 files), minus the 22 scanned references the #2648 fix replaced with
# symbol names: 20 ``model_registry`` references in
# apps/api/openapi_restored_schemas.py and 2 retry-writer references in
# services/orchestrator/scheduler_state_failure.py. The baseline may shrink
# below this; it may never grow past it.
BASELINE_TOTAL_CAP = 232 - 22
# Built from parts so this file never carries the literal shape it tests for
# (tests/ is outside the scan roots, but grep readbacks are not).
REF = "foo" + ".py" + ":123"


def _hard_gate_line_reference_findings(root: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    report = audit_repo_entropy.build_report(root, mode="hard-gate")
    return report, [finding for finding in report["findings"] if finding["check_id"] == CHECK_ID]


def _write_baseline(root: Path, baseline: dict[str, dict[str, int]]) -> None:
    path = root / check_line_references.LINE_REFERENCE_BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline), encoding="utf-8")


def test_line_reference_family_is_registered_and_gated() -> None:
    assert CHECK_ID == check_line_references.LINE_REFERENCE_CHECK_ID
    assert CHECK_ID in audit_repo_entropy.CHECK_FAMILIES
    assert CHECK_ID in audit_repo_entropy.HARD_GATE_CHECK_IDS


def test_new_line_reference_comment_fails_the_hard_gate_with_the_symbol_message(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "apps/api/new_module.py", f"VALUE = 1\n# see {REF} for the reason\n")

    report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [("apps/api/new_module.py", 2)]
    finding = findings[0]
    assert finding["gate_eligible"] is True
    assert finding["description"] == (
        f"new hard line reference {REF} in apps/api/new_module.py; replace it with a symbol reference "
        "(function/class/constant name)"
    )
    assert "baseline" not in str(finding["description"]).lower()
    assert report["metadata"]["hard_gate_status"] == "fail"
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_each_scan_root_is_covered_and_unscanned_trees_are_not(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    for scan_root in check_line_references.LINE_REFERENCE_SCAN_ROOTS:
        _write(tmp_path / scan_root / "mod.py", f"# {REF}\n")
    _write(tmp_path / "tests/test_mod.py", f"# {REF}\n")
    _write(tmp_path / "docs/notes.md", f"see {REF}\n")

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert sorted(finding["evidence_path"] for finding in findings) == sorted(
        f"{scan_root}/mod.py" for scan_root in ("apps", "packages", "services", "workers", "scripts")
    )


def test_reference_within_the_baseline_count_passes_and_one_above_it_fails(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write_baseline(tmp_path, {"services/mod.py": {REF: 2}})
    _write(tmp_path / "services/mod.py", f"# {REF}\nX = 1\n# again {REF}\n")

    report, findings = _hard_gate_line_reference_findings(tmp_path)
    assert findings == []
    assert audit_repo_entropy._exit_code_for_report(report) == 0

    _write(tmp_path / "services/mod.py", f"# {REF}\nX = 1\n# again {REF}\n# third {REF}\n")
    report, findings = _hard_gate_line_reference_findings(tmp_path)
    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [("services/mod.py", 4)]
    assert audit_repo_entropy._exit_code_for_report(report) == 1
    # The text-keyed baseline cannot tell which occurrence is new, so the
    # finding names every occurrence line rather than pointing only at the last.
    assert "3 occurrences on lines 1, 3, 4" in findings[0]["description"]


def test_reference_new_to_the_file_does_not_list_occurrences(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write_baseline(tmp_path, {})
    _write(tmp_path / "services/mod.py", f"# {REF}\n")

    _, findings = _hard_gate_line_reference_findings(tmp_path)
    assert [finding["line"] for finding in findings] == [1]
    assert "occurrences" not in findings[0]["description"]


def test_deleting_or_moving_a_baseline_reference_passes(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write_baseline(tmp_path, {"services/mod.py": {REF: 2}, "services/gone.py": {REF: 1}})
    # One of two frozen occurrences deleted, the survivor moved to another line,
    # and a whole baseline-listed file deleted: none of it may fail.
    _write(tmp_path / "services/mod.py", f"A = 1\nB = 2\nC = 3\n# moved {REF}\n")

    report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert findings == []
    assert report["metadata"]["hard_gate_status"] == "pass"


def test_baseline_key_is_per_path_not_global(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write_baseline(tmp_path, {"services/mod.py": {REF: 1}})
    _write(tmp_path / "services/other.py", f"# {REF}\n")

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert [finding["evidence_path"] for finding in findings] == ["services/other.py"]


def test_colon_digit_text_that_is_not_a_line_reference_reports_nothing(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(
        tmp_path / "packages/common/geo.py",
        '"""Projected with EPSG:4326 and served from http://h:5432/db."""\n'
        "# EPSG:4326 -> EPSG:3857 via http://h:5432\n"
        'LABEL = f"{7:03d}"\n'
        "def name(x):\n"
        '    return f"{x:03d}"\n',
    )

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert findings == []


def test_reference_inside_a_string_literal_or_code_reports_nothing(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(
        tmp_path / "workers/mod.py",
        "def f():\n"
        "    x = 1\n"
        f'    return "{REF}" + x\n'
        f'MESSAGE = "see `:30 and {REF}"\n',
    )

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert findings == []


def test_module_class_and_function_docstring_references_are_caught_on_their_own_lines(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(
        tmp_path / "scripts/mod.py",
        f'"""Module.\n\nSee {REF}."""\n'
        "class C:\n"
        f'    """Class {REF}."""\n'
        "    def m(self):\n"
        f'        """Method\n\n        uses {REF}\n        """\n'
        "async def g():\n"
        f"    '''Async {REF}.'''\n",
    )

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert sorted(finding["line"] for finding in findings) == [3, 5, 9, 12]


def test_backtick_colon_digit_form_is_a_reference(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "apps/api/mod.py", "# ``_helper``" + ":42 decides this\n")

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert [finding["description"].split(" in ")[0] for finding in findings] == ["new hard line reference `:42"]


def test_range_reference_is_one_reference(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "apps/api/mod.py", "# see services/x.py" + ":10-20 and y.py" + ":3\u20135\n")

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert sorted(finding["description"].split(" in ")[0] for finding in findings) == [
        "new hard line reference services/x.py" + ":10-20",
        "new hard line reference y.py" + ":3\u20135",
    ]


def test_unparseable_file_is_skipped_not_crashed(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "apps/api/broken.py", f"def f(:\n    # {REF}\n")

    _report, findings = _hard_gate_line_reference_findings(tmp_path)

    assert findings == []


def test_the_check_module_does_not_match_its_own_docstrings() -> None:
    source = Path(check_line_references.__file__).read_text(encoding="utf-8")

    assert check_line_references._line_references_in_source(source) == []


def test_current_repository_has_no_new_line_references() -> None:
    report = _repo_report(REPO_ROOT, mode="hard-gate")

    findings = [finding for finding in report["findings"] if finding["check_id"] == CHECK_ID]

    assert findings == []


def test_baseline_total_is_at_or_below_the_measured_cap() -> None:
    baseline = check_line_references._load_line_reference_baseline(REPO_ROOT)
    total = sum(sum(refs.values()) for refs in baseline.values())

    assert 0 < total <= BASELINE_TOTAL_CAP, total


def test_baseline_does_not_carry_the_fixed_stale_groups() -> None:
    baseline = check_line_references._load_line_reference_baseline(REPO_ROOT)

    openapi = baseline.get("apps/api/openapi_restored_schemas.py", {})
    assert not [ref for ref in openapi if ref.startswith("model_registry" + ".py:")], openapi
    failure = baseline.get("services/orchestrator/scheduler_state_failure.py", {})
    assert not [
        ref for ref in failure if ref.startswith(("retry" + ".py:", "file_orchestration_journal" + ".py:"))
    ], failure
