"""Route authority: list, table and archive contexts (#1823 partition).

Archive markers over route tokens (section, expanded and archived-openspec
forms), the markdown table/list/wrapped allowlist contexts, and the
per-mention redirect rules -- sibling lists, nested children, table cells and
same-item continuations -- that decide which mention on a mixed line stays an
active finding.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _assert_forecast_active_and_hydro_redirect,
    _assert_unallowlisted_budget_counted_report_only_finding,
    _complete_archive_status_front_matter,
    _route_authority_findings,
    _route_authority_findings_by_token,
    _route_authority_token_from_finding,
    _write,
)


def test_archive_route_token_without_complete_marker_remains_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "archived" / "m26.md", "Current route link ?next=/hydro-met.\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/archived/m26.md"
    assert _route_authority_token_from_finding(findings[0]) == "/hydro-met"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_complete_archive_marker_allowlists_section_route_token_without_global_archive_ignore(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "archive" / "m26-route-notes.md",
        """
        Archive status:
        - status: superseded
        - current_authority: docs/governance/DOC_STATUS.md#display-route-authority-m26-single-map
        - superseded_by: openspec/specs/single-map-shell-routing/spec.md
        - status_since: 2026-06-24
        - archive_scope: section
        - retained_for: compatibility evidence

        # Preserved route evidence

        Current route link ?next=/hydro-met.

        # Current-looking appendix

        Current page component HydroMetPage is active.
        """,
    )

    by_token = _route_authority_findings_by_token(_route_authority_findings(tmp_path))

    assert set(by_token) == {"/hydro-met", "HydroMetPage"}
    archived = by_token["/hydro-met"]
    assert archived["allowlist_reason"] == audit_repo_entropy.COMPLETE_ARCHIVE_STATUS_ALLOWLIST_REASON
    assert archived["allowlist_key"] == "stale-display-route-token:complete-archive-status-marker"
    assert archived["allowlist_state"] == "allowlisted"
    assert archived["budget_counted"] is False
    assert archived["gate_eligible"] is False
    _assert_unallowlisted_budget_counted_report_only_finding(by_token["HydroMetPage"])


def test_fenced_archive_status_example_does_not_allowlist_current_route_token(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        ```text
        Archive status:
        - status: superseded
        - current_authority: docs/governance/DOC_STATUS.md#display-route-authority-m26-single-map
        - superseded_by: openspec/specs/single-map-shell-routing/spec.md
        - status_since: 2026-06-24
        - archive_scope: section
        - retained_for: compatibility evidence
        ```

        Current route link ?next=/hydro-met.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/runbooks/current.md"
    assert _route_authority_token_from_finding(findings[0]) == "/hydro-met"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_archive_expanded_route_token_without_marker_remains_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "archived" / "m26.md", "Current route link ?next=/forecast.\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/archived/m26.md"
    assert _route_authority_token_from_finding(findings[0]) == "/forecast"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_complete_archive_marker_allowlists_expanded_archive_route_token(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "archived" / "m26.md",
        _complete_archive_status_front_matter("Current route link ?next=/meteorology.\n"),
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/archived/m26.md"
    assert _route_authority_token_from_finding(findings[0]) == "/meteorology"
    assert findings[0]["allowlist_key"] == "stale-display-route-token:complete-archive-status-marker"
    assert findings[0]["allowlist_state"] == "allowlisted"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False


def test_archived_openspec_expanded_route_token_without_marker_remains_budget_counted(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "archive" / "m26" / "tasks.md",
        "Current route link ?next=/forecast.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "openspec/changes/archive/m26/tasks.md"
    assert _route_authority_token_from_finding(findings[0]) == "/forecast"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_complete_archive_marker_allowlists_archived_openspec_expanded_route_token(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "archive" / "m26" / "tasks.md",
        _complete_archive_status_front_matter("Current route link ?next=/meteorology.\n"),
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "openspec/changes/archive/m26/tasks.md"
    assert _route_authority_token_from_finding(findings[0]) == "/meteorology"
    assert findings[0]["allowlist_key"] == "stale-display-route-token:complete-archive-status-marker"
    assert findings[0]["allowlist_state"] == "allowlisted"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False


def test_current_active_doc_route_tokens_without_archive_marker_remain_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "current.md", "Current route link ?next=/hydro-met.\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/current.md"
    assert _route_authority_token_from_finding(findings[0]) == "/hydro-met"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_route_authority_current_runbook_allowlist_contexts_are_distinct(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        /hydro-met -> / redirect alias
        Compatibility context keeps /meteorology deep links
        Historical pre-M26 evidence used /overview
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/meteorology", "/overview"}
    redirect = by_token["/hydro-met"]
    compatibility = by_token["/meteorology"]
    historical = by_token["/overview"]
    assert redirect["allowlist_reason"] == "M26 route-consolidation redirect alias"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert compatibility["allowlist_reason"] == "legacy route compatibility context"
    assert compatibility["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert historical["allowlist_reason"] == "historical plan or pre-M26 display evidence"
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert len({finding["allowlist_reason"] for finding in findings}) == 3
    assert len({finding["allowlist_key"] for finding in findings}) == 3
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_markdown_table_list_and_wrapped_contexts_allowlist_governed_mentions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "README.md",
        """
        legacy redirect aliases (compatibility only; not active independent pages):

        | Old route | Target |
        |---|---|
        | `/overview`, `/hydro-met`, `/forecast` | `/` |

        - Legacy compatibility aliases:
          `/meteorology`

        Current route authority: `/` is active display proof. `/basins/:id` and
        `/segments/:id` only belong to legacy redirect /
        compatibility context.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {
        "/overview",
        "/hydro-met",
        "/forecast",
        "/meteorology",
        "/basins/:id",
        "/segments/:id",
    }
    assert by_token["/hydro-met"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    assert by_token["/meteorology"]["allowlist_key"] == (
        "stale-display-route-token:legacy-route-compatibility-context"
    )
    assert by_token["/basins/:id"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)


@pytest.mark.parametrize(
    "continuation",
    [
        "  all `replace` redirect to `/` with semantic query parameters.",
        "  all old aliases redirect to `/` with semantic query parameters.",
        "  全 `replace` 重定向到 `/` + 语义参数。",
    ],
)
def test_route_authority_wrapped_list_redirect_continuation_allowlists_route_list(
    tmp_path: Path,
    continuation: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        - Legacy display routes:
          (`/hydro-met`/`/overview`/`/forecast`/`/meteorology`)
        {continuation}
        - Open /basins/demo for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {
        "/hydro-met",
        "/overview",
        "/forecast",
        "/meteorology",
        "/basins/demo",
    }
    for token in ("/hydro-met", "/overview", "/forecast", "/meteorology"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:m26-route-consolidation-or-redirect"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
    _assert_unallowlisted_budget_counted_report_only_finding(by_token["/basins/demo"])


def test_route_authority_top_level_sibling_list_context_does_not_allowlist_active_route(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Compatibility context keeps /hydro-met deep links.
        - Open /forecast.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {1, 2}
    legacy = by_line[1]
    assert "/hydro-met" in str(legacy["description"])
    assert legacy["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert legacy["allowlist_state"] == "allowlisted"
    assert legacy["budget_counted"] is False

    active = by_line[2]
    assert "/forecast" in str(active["description"])
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


def test_route_authority_blockquoted_sibling_list_context_does_not_allowlist_active_route(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > - Historical compatibility redirect keeps /hydro-met deep links.
        > - Open /forecast for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {1, 2}
    legacy = by_line[1]
    assert "/hydro-met" in str(legacy["description"])
    assert legacy["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert legacy["allowlist_state"] == "allowlisted"
    assert legacy["budget_counted"] is False

    active = by_line[2]
    assert "/forecast" in str(active["description"])
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


def test_route_authority_top_level_sibling_list_context_does_not_allowlist_route_value(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Historical pre-M26 evidence used /hydro-met.
        - BASE_URL=$BASE_URL/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {1, 2}
    legacy = by_line[1]
    assert "/hydro-met" in str(legacy["description"])
    assert legacy["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert legacy["allowlist_state"] == "allowlisted"
    assert legacy["budget_counted"] is False

    active = by_line[2]
    assert "/forecast" in str(active["description"])
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("- Compatibility context keeps /forecast deep links.\n", "/forecast"),
        ("- Compatibility context keeps legacy deep links:\n  /forecast\n", "/forecast"),
    ],
)
def test_route_authority_same_item_and_continuation_list_contexts_still_allowlist_route_mentions(
    tmp_path: Path,
    text: str,
    token: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert token in str(finding["description"])
    assert finding["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_blockquoted_same_item_continuation_inherits_list_context(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > - Compatibility context keeps legacy deep links:
        >   /forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


@pytest.mark.parametrize(
    "text",
    [
        """
        > - Compatibility context keeps legacy deep links:
        >   - /forecast
        """,
        """
        - Compatibility context keeps legacy deep links:
          - /forecast
        """,
    ],
)
def test_route_authority_nested_child_list_inherits_parent_context(
    tmp_path: Path,
    text: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_current_runbook_mixed_active_and_redirect_contexts_are_distinct(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current live browser proof; /hydro-met -> / redirect alias.\n",
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def test_route_authority_current_runbook_comma_sibling_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current proof, /hydro-met -> / redirect alias.\n",
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def test_route_authority_current_runbook_no_delimiter_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current proof /hydro-met -> / redirect alias.\n",
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_inherited_parent_list_redirect_context_does_not_allowlist_active_child_mixed_line(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Legacy redirect aliases:
          - Open /forecast for current proof /hydro-met -> / redirect alias.
        """,
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_blockquoted_parent_list_redirect_context_does_not_allowlist_active_child_mixed_line(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > - Legacy redirect aliases:
        >   - Open /forecast for current proof /hydro-met -> / redirect alias.
        """,
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


@pytest.mark.parametrize(
    "text",
    [
        """
        # Legacy redirect aliases

        Open /forecast for current proof /hydro-met -> / redirect alias.
        """,
        """
        Legacy redirect aliases:

        | Proof |
        |---|
        | Open /forecast for current proof /hydro-met -> / redirect alias |
        """,
    ],
)
def test_route_authority_inherited_heading_or_table_redirect_context_does_not_allowlist_active_mixed_line(
    tmp_path: Path,
    text: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_current_runbook_table_cell_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Active proof | Redirect alias |
        |---|---|
        | Open /forecast for current proof | /hydro-met -> / redirect alias |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


@pytest.mark.parametrize(
    "row",
    [
        "| `/hydro-met` | 重定向到 `/`（旧别名行为不变） |",
        "| `/hydro-met` | redirects to `/`, legacy alias behaviour unchanged |",
    ],
    ids=["chinese-redirect-cell", "english-redirect-cell"],
)
def test_route_authority_current_runbook_table_row_sees_redirect_wording_in_the_next_cell(
    tmp_path: Path,
    row: str,
) -> None:
    # A receipt records the route in one column and its disposition in the next, so the
    # redirect wording never lands in the mention's own clause.
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        路由 smoke：

        | 路径 | 结果 |
        |---|---|
        {row}
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 5
    assert _route_authority_token_from_finding(finding) == "/hydro-met"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["allowlist_reason"] == "M26 route-consolidation redirect alias"
    assert finding["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert finding["budget_counted"] is False


def test_route_authority_table_row_redirect_cell_does_not_launder_a_sibling_row(
    tmp_path: Path,
) -> None:
    # Narrowness pin: the row is the span, not the table. A neighbouring row's redirect
    # wording must not allowlist a current route value in a different row.
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | 路径 | 结果 |
        |---|---|
        | Deep links | --path=/forecast |
        | `/hydro-met` | 重定向到 `/`（旧别名行为不变） |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    drift = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert drift["line"] == 3
    assert drift["allowlist_state"] == "unallowlisted"
    assert drift["allowlist_reason"] is None
    assert drift["budget_counted"] is True
    assert redirect["line"] == 4
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def test_route_authority_current_runbook_same_table_cell_no_delimiter_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Proof |
        |---|
        | Open /forecast for current proof /hydro-met -> / redirect alias |
        """,
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))
