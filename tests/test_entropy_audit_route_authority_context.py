"""Route authority: governing context resolution (#1823 partition).

Route-valued form detection without substring false positives, historical
banners and current headings, the evidence-boundary contexts, blockquote versus
normal heading/table governance, and the inheritance rules that decide whether a
legacy display-route token is allowlisted or reported as drift.

The list/table redirect-context half is in
``tests/test_entropy_audit_route_authority_lists.py`` and the caching/bounded-work
half in ``tests/test_entropy_audit_route_authority_caching.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.entropy_audit_helpers import (
    _assert_unallowlisted_budget_counted_report_only_finding,
    _route_authority_findings,
    _route_authority_findings_by_token,
    _write,
)


def test_route_authority_current_runbook_active_legacy_alias_is_report_only_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current live browser proof.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["check_id"] == "stale-display-route-token"
    assert finding["evidence_path"] == "docs/runbooks/current.md"
    assert "/forecast" in finding["description"]
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["allowlist_key"] is None
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False


def test_route_authority_route_valued_forms_are_detected_without_substring_false_positives(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        BASE_URL=$BASE_URL/forecast
        command --path=/forecast
        callback ?next=/forecast
        Ignore foo/hydro-met and some/path/hydro-met.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 3
    assert {finding["line"] for finding in findings} == {1, 2, 3}
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert all(finding["allowlist_key"] is None for finding in findings)
    assert all(finding["budget_counted"] is True for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_placeholder_url_route_valued_forms_are_detected(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        Current route link: ${BASE_URL}/forecast
        Current route quoted link: "${BASE_URL}/forecast"
        Current route placeholder link: <frontend-base-url>/forecast
        Ignore foo/hydro-met and some/path/hydro-met.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 3
    assert {finding["line"] for finding in findings} == {1, 2, 3}
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert all(finding["allowlist_key"] is None for finding in findings)
    assert all(finding["budget_counted"] is True for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_historical_runbook_banner_allowlists_deep_legacy_evidence(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        """
        > **Historical / superseded by M26**: frozen smoke evidence.
        > Current route authority is the M26 single-map `/` display entrypoint.

        # Historical smoke evidence

        Preserved run output:

        1. Open /hydro-met for current live browser proof.
        2. Visit /forecast for current display proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 2
    assert {finding["evidence_path"] for finding in findings} == {
        "docs/runbooks/historical.md",
    }
    assert {finding["allowlist_reason"] for finding in findings} == {
        "historical plan or pre-M26 display evidence",
    }
    assert {finding["allowlist_key"] for finding in findings} == {
        "stale-display-route-token:historical-plan-or-pre-m26-evidence",
    }
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_historical_banner_does_not_allowlist_later_current_instruction(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        """
        > **Historical / superseded by M26**: frozen smoke evidence.
        > Current route authority is the M26 single-map `/` display entrypoint.

        # Historical smoke evidence

        Frozen receipt: Open /hydro-met for current live browser proof.

        # Current operator procedure

        Open /forecast.
        Use /forecast route.
        Current display route: /forecast.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {6, 10, 11, 12}
    frozen = by_line[6]
    assert "/hydro-met" in str(frozen["description"])
    assert frozen["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert frozen["allowlist_state"] == "allowlisted"
    assert frozen["budget_counted"] is False
    for line_no in (10, 11, 12):
        active = by_line[line_no]
        assert "/forecast" in str(active["description"])
        assert active["allowlist_reason"] is None
        assert active["allowlist_key"] is None
        assert active["allowlist_state"] == "unallowlisted"
        assert active["budget_counted"] is True
        assert active["gate_eligible"] is False


def test_route_authority_current_section_heading_governs_beyond_short_lookback(
    tmp_path: Path,
) -> None:
    lines = [
        "> **Historical / superseded by M26**: frozen smoke evidence.",
        "> Current route authority is the M26 single-map `/` display entrypoint.",
        "",
        "# Historical smoke evidence",
        "",
        "Frozen receipt: Open /hydro-met for current live browser proof.",
        "",
        "# Current operator procedure",
        "",
        *(f"Setup note {index}: prepare operator context." for index in range(1, 14)),
        "Open /forecast.",
        "Use /forecast route.",
    ]
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        "\n".join(lines) + "\n",
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {6, 23, 24}
    frozen = by_line[6]
    assert "/hydro-met" in str(frozen["description"])
    assert frozen["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert frozen["allowlist_state"] == "allowlisted"
    assert frozen["budget_counted"] is False

    for line_no in (23, 24):
        active = by_line[line_no]
        assert "/forecast" in str(active["description"])
        assert active["allowlist_reason"] is None
        assert active["allowlist_key"] is None
        assert active["allowlist_state"] == "unallowlisted"
        assert active["budget_counted"] is True
        assert active["gate_eligible"] is False


def test_route_authority_historical_banner_keeps_later_current_route_values_as_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        """
        > **Historical / superseded by M26**: frozen smoke evidence.
        > Current route authority is the M26 single-map `/` display entrypoint.

        # Historical smoke evidence

        Frozen receipt: Open /hydro-met for current live browser proof.

        # Current operator procedure

        BASE_URL=$BASE_URL/forecast
        command --path=/forecast
        callback ?next=/forecast
        Use /forecast as the current route.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {6, 10, 11, 12, 13}
    frozen = by_line[6]
    assert "/hydro-met" in str(frozen["description"])
    assert frozen["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert frozen["allowlist_state"] == "allowlisted"
    assert frozen["budget_counted"] is False
    for line_no in (10, 11, 12, 13):
        finding = by_line[line_no]
        assert "/forecast" in str(finding["description"])
        assert finding["allowlist_reason"] is None
        assert finding["allowlist_key"] is None
        assert finding["allowlist_state"] == "unallowlisted"
        assert finding["budget_counted"] is True
        assert finding["gate_eligible"] is False


def test_route_authority_evidence_boundary_heading_allowlists_diagnostic_route_references(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        ## #214 evidence boundary

        - `/hydro-met` browser proof 状态以 #214 evidence matrix 为准。
        IFS deterministic `/forecast` browser smoke 标注 144h actual horizon.

        ## Current operator procedure

        Open /meteorology for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/forecast", "/meteorology"}
    for token in ("/hydro-met", "/forecast"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:historical-plan-or-pre-m26-evidence"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
    _assert_unallowlisted_budget_counted_report_only_finding(by_token["/meteorology"])


def test_route_authority_evidence_boundary_active_instruction_is_report_only_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        ## #214 evidence boundary

        - `/hydro-met` browser proof 状态以 #214 evidence matrix 为准。
        Diagnostic `/meteorology` browser evidence remains frozen.
        Open /forecast for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/meteorology", "/forecast"}
    for token in ("/hydro-met", "/meteorology"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:historical-plan-or-pre-m26-evidence"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
        assert by_token[token]["gate_eligible"] is False

    active = by_token["/forecast"]
    assert active["evidence_path"] == "docs/runbooks/current.md"
    assert active["line"] == 5
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


@pytest.mark.parametrize(
    "active_line",
    [
        "Open /forecast.",
        "Current display route: /forecast.",
    ],
)
def test_route_authority_evidence_boundary_terse_active_route_is_report_only_drift(
    tmp_path: Path,
    active_line: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        ## #214 evidence boundary

        - `/hydro-met` browser proof 状态以 #214 evidence matrix 为准。
        Diagnostic `/meteorology` browser evidence remains frozen.
        {active_line}
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/meteorology", "/forecast"}
    for token in ("/hydro-met", "/meteorology"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:historical-plan-or-pre-m26-evidence"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
        assert by_token[token]["gate_eligible"] is False

    active = by_token["/forecast"]
    assert active["evidence_path"] == "docs/runbooks/current.md"
    assert active["line"] == 5
    _assert_unallowlisted_budget_counted_report_only_finding(active)


@pytest.mark.parametrize(
    "line",
    [
        "Current route links: BASE_URL=$BASE_URL/forecast",
        "Current route deep links: --path=/forecast",
        "Current route bookmark: ?next=/forecast",
    ],
)
def test_route_authority_current_route_valued_context_takes_precedence_over_compatibility_words(
    tmp_path: Path,
    line: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_inherited_current_heading_route_value_compatibility_words_are_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Current operator procedure

        Deep links: --path=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_current_child_heading_route_value_is_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Current operator procedure

        ## Deep links
        BASE_URL=$BASE_URL/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 4
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


@pytest.mark.parametrize("route_value", ["--path=/forecast", "?next=/forecast"])
def test_route_authority_inherited_current_parent_list_route_value_is_drift(
    tmp_path: Path,
    route_value: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        - Current route values:
          - {route_value}
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_current_table_row_route_value_compatibility_words_are_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Context | Label | Value |
        |---|---|---|
        | Current route | Deep links | --path=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_blockquoted_current_table_row_route_value_is_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > | Context | Label | Value |
        > |---|---|---|
        > | Current route | Deep links | --path=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_blockquoted_historical_heading_does_not_govern_normal_current_route_value(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > # Historical pre-M26 evidence
        Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_independent_blockquote_does_not_inherit_stale_blockquote_heading(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > # Historical pre-M26 evidence
        > Frozen /hydro-met receipt

        Current operator note outside quote.

        > Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {2, 6}
    historical = by_line[2]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False

    active = by_line[6]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


def test_route_authority_normal_historical_heading_does_not_govern_blockquoted_current_route_value(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Historical pre-M26 evidence
        > Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_normal_historical_heading_restores_after_intervening_blockquote(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Historical pre-M26 evidence
        > preserved quoted note
        Preserved /hydro-met receipt
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/hydro-met" in str(finding["description"])
    assert finding["allowlist_reason"] == "historical plan or pre-M26 display evidence"
    assert finding["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_blockquoted_historical_heading_expires_after_normal_heading(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > # Historical pre-M26 evidence
        > Frozen /hydro-met receipt
        # Current operator procedure
        > Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {2, 4}
    historical = by_line[2]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False

    active = by_line[4]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


def test_route_authority_blockquoted_historical_table_does_not_merge_with_normal_current_table(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > | Context | Value |
        > |---|---|
        > | Historical pre-M26 evidence | /hydro-met |
        | Context | Value |
        |---|---|
        | Current route | ?next=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {3, 6}
    historical = by_line[3]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False
    active = by_line[6]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


def test_route_authority_normal_historical_table_does_not_merge_with_blockquoted_current_table(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Context | Value |
        |---|---|
        | Historical pre-M26 evidence | /hydro-met |
        > | Context | Value |
        > |---|---|
        > | Current route | ?next=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {3, 6}
    historical = by_line[3]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False
    active = by_line[6]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


@pytest.mark.parametrize(
    ("line", "expected_key"),
    [
        ("--path=/hydro-met -> / redirect alias", "stale-display-route-token:m26-route-consolidation-or-redirect"),
        (
            "${BASE_URL}/hydro-met -> / redirect alias",
            "stale-display-route-token:m26-route-consolidation-or-redirect",
        ),
        (
            "<frontend-base-url>/hydro-met redirects to /",
            "stale-display-route-token:m26-route-consolidation-or-redirect",
        ),
        (
            "Historical pre-M26 evidence used --path=/forecast",
            "stale-display-route-token:historical-plan-or-pre-m26-evidence",
        ),
        (
            "Historical pre-M26 evidence used ${BASE_URL}/forecast",
            "stale-display-route-token:historical-plan-or-pre-m26-evidence",
        ),
        (
            "Compatibility context keeps --path=/forecast deep links",
            "stale-display-route-token:legacy-route-compatibility-context",
        ),
        (
            "Compatibility context keeps \"${BASE_URL}/forecast\" deep links",
            "stale-display-route-token:legacy-route-compatibility-context",
        ),
    ],
)
def test_route_authority_explicit_route_valued_allowlist_contexts_still_allowlist(
    tmp_path: Path,
    line: str,
    expected_key: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["allowlist_key"] == expected_key
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False
