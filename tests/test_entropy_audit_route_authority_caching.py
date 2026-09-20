"""Route authority: bounded work, caching and current-repo floors (#1823 partition).

The per-unique-context memoization of mention context, redirect spans, semantic
keys, list-item ends and paragraph governing text; the large-line and
route-free shapes that must not build a governing context; and the
whole-repository floors (no unallowlisted findings in current docs, the M26
archive evidence, the alias coverage and the scan-scope exclusions).

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    REPO_ROOT,
    _assert_forecast_active_and_hydro_redirect,
    _assert_unallowlisted_budget_counted_report_only_finding,
    _route_authority_findings,
    _route_authority_findings_by_token,
    _route_authority_token_from_finding,
    _write,
)


def test_route_authority_current_runbook_same_list_item_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Open /forecast for current proof,
          /hydro-met -> / redirect alias.
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
    "text",
    [
        "- Open /forecast for current proof /hydro-met -> / redirect alias.\n",
        "- Open /forecast for current proof\n  /hydro-met -> / redirect alias.\n",
    ],
)
def test_route_authority_current_runbook_same_list_item_no_delimiter_redirect_context_is_per_mention(
    tmp_path: Path,
    text: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_current_runbook_same_route_mixed_contexts_keep_active_finding(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "/hydro-met -> / redirect alias; Open /hydro-met for current live browser proof.\n",
    )

    findings = _route_authority_findings(tmp_path)
    hydro_findings = [finding for finding in findings if "/hydro-met" in str(finding["description"])]

    assert len(hydro_findings) == 2
    assert {finding["allowlist_state"] for finding in hydro_findings} == {
        "allowlisted",
        "unallowlisted",
    }
    active = next(finding for finding in hydro_findings if finding["allowlist_state"] == "unallowlisted")
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


@pytest.mark.parametrize(
    ("token", "line"),
    [
        (
            "/forecast",
            "/forecast redirects to / and Visit /forecast for current display proof.",
        ),
        (
            "/forecast",
            "/forecast redirects to / and current route link ?next=/forecast.",
        ),
        (
            "/forecast",
            "/forecast redirects to / and Current route link: ${BASE_URL}/forecast.",
        ),
        (
            "/hydro-met",
            "/hydro-met redirects to / and Open /hydro-met for current live browser proof.",
        ),
        (
            "/hydro-met",
            "/hydro-met redirects to / and --path=/hydro-met current display proof.",
        ),
        (
            "/forecast",
            "/forecast 重定向到 / 且打开 /forecast 做 current browser proof.",
        ),
    ],
)
def test_route_authority_current_runbook_same_token_redirect_first_mixed_line_keeps_active_finding(
    tmp_path: Path,
    token: str,
    line: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)
    token_findings = [finding for finding in findings if token in str(finding["description"])]

    assert len(token_findings) == 2
    by_state = {finding["allowlist_state"]: finding for finding in token_findings}
    assert set(by_state) == {"allowlisted", "unallowlisted"}
    assert by_state["allowlisted"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    _assert_unallowlisted_budget_counted_report_only_finding(by_state["unallowlisted"])


@pytest.mark.parametrize(
    "line",
    [
        "Open /forecast for current live browser proof -> capture the receipt.",
        "Open /forecast for current compatibility/deep-link browser proof.",
    ],
)
def test_route_authority_current_runbook_active_line_with_unrelated_allowlist_words_is_drift(
    tmp_path: Path,
    line: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["allowlist_key"] is None
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False


@pytest.mark.parametrize(
    "context_line",
    [
        "Compatibility context keeps legacy deep links available.",
        "Historical pre-M26 evidence used legacy display aliases.",
    ],
)
def test_route_authority_current_runbook_adjacent_allowlist_context_does_not_allowlist_active_line(
    tmp_path: Path,
    context_line: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"{context_line}\nOpen /forecast.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["allowlist_key"] is None


def test_route_authority_legacy_hydro_met_token_uses_route_boundaries(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Ignore foo/hydro-met and some/path/hydro-met in non-route path examples.\n",
    )

    assert _route_authority_findings(tmp_path) == []

    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /hydro-met for current live browser proof.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/hydro-met" in str(finding["description"])
    assert finding["allowlist_state"] == "unallowlisted"


def test_route_authority_m26_references_preserve_expected_allowlist_keys(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "m26-unified-map-display" / "proposal.md",
        """
        `/hydro-met` and `/forecast` redirect to `/`.
        Delete `HydroMetPage` after preserving historical pre-M26 evidence.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert by_token["/hydro-met"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    hydro_page = next(finding for finding in findings if "HydroMetPage" in str(finding["description"]))
    assert hydro_page["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)


def test_route_authority_current_repo_m26_archive_evidence_uses_complete_marker_allowlist() -> None:
    archive_root = "openspec/changes/archive/2026-06-18-m26-unified-map-display"
    expected_token_counts_by_path = {
        f"{archive_root}/proposal.md": {
            "/basins/:basinId": 3,
            "/forecast": 2,
            "/hydro-met": 3,
            "/meteorology": 2,
            "/overview": 2,
            "/segments/:segmentId": 2,
            "HydroMetPage": 3,
        },
        f"{archive_root}/specs/single-map-shell-routing/spec.md": {
            "/basins/:basinId": 1,
            "/basins/basins_qhh": 1,
            "/forecast": 2,
            "/hydro-met": 2,
            "/meteorology": 3,
            "/overview": 2,
            "/segments/:segmentId": 2,
            "/segments/seg_001": 1,
        },
        f"{archive_root}/tasks.md": {
            "/basins/:basinId": 1,
            "/basins/:id": 1,
            "/forecast": 2,
            "/hydro-met": 2,
            "/meteorology": 2,
            "/overview": 2,
            "/segments/:id": 1,
            "/segments/:segmentId": 2,
            "HydroMetPage": 1,
        },
    }
    actual_token_counts_by_path = {
        evidence_path: {token: 0 for token in expected_tokens}
        for evidence_path, expected_tokens in expected_token_counts_by_path.items()
    }
    findings: list[dict[str, object]] = []
    for finding in _route_authority_findings(REPO_ROOT):
        evidence_path = str(finding["evidence_path"])
        if evidence_path not in expected_token_counts_by_path:
            continue
        token = _route_authority_token_from_finding(finding)
        assert token is not None
        findings.append(finding)
        actual_token_counts_by_path[evidence_path][token] = (
            actual_token_counts_by_path[evidence_path].get(token, 0) + 1
        )

    assert actual_token_counts_by_path == expected_token_counts_by_path
    assert {finding["allowlist_key"] for finding in findings} == {
        "stale-display-route-token:complete-archive-status-marker"
    }
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)


def test_route_authority_large_line_matches_route_tokens_without_prefix_scan_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = ["/forecast", "/hydro-met", "/meteorology", "/overview"]
    large_line = " ".join(f"Open {tokens[index % len(tokens)]} for current proof." for index in range(800))
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{large_line}\n")

    call_count = 0
    original_mention_context = audit_repo_entropy._stale_route_mention_context

    def counting_mention_context(*args: object) -> object:
        nonlocal call_count
        call_count += 1
        return original_mention_context(*args)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_mention_context", counting_mention_context)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == len(tokens)
    assert set(_route_authority_findings_by_token(findings)) == set(tokens)
    assert call_count == len(tokens)


def test_route_authority_duplicate_tokens_dedupe_before_expensive_context_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_count = 400
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open " + " ".join("/forecast" for _index in range(token_count)) + " for current proof\n",
    )
    redirect_span_call_count = 0
    original_redirect_span = audit_repo_entropy._stale_route_mention_redirect_span

    def counting_redirect_span(*args: object) -> str:
        nonlocal redirect_span_call_count
        redirect_span_call_count += 1
        return original_redirect_span(*args)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_mention_redirect_span", counting_redirect_span)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert "/forecast" in str(findings[0]["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    assert redirect_span_call_count <= 1


def test_route_authority_duplicate_tokens_precompute_semantic_work_per_unique_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_count = 400
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open " + " ".join("/forecast" for _index in range(token_count)) + " for current proof\n",
    )
    semantic_key_call_count = 0
    route_valued_call_count = 0
    clause_analysis_call_count = 0
    original_semantic_key = audit_repo_entropy._stale_route_mention_semantic_key
    original_route_valued = audit_repo_entropy._route_token_is_route_valued
    original_clause_analysis = audit_repo_entropy._stale_route_clause_analysis

    def counting_semantic_key(*args: object) -> str:
        nonlocal semantic_key_call_count
        semantic_key_call_count += 1
        return original_semantic_key(*args)

    def counting_route_valued(*args: object) -> bool:
        nonlocal route_valued_call_count
        route_valued_call_count += 1
        return original_route_valued(*args)

    def counting_clause_analysis(
        line: str,
        start: int,
        end: int,
    ) -> audit_repo_entropy._StaleRouteClauseAnalysis:
        nonlocal clause_analysis_call_count
        clause_analysis_call_count += 1
        return original_clause_analysis(line, start, end)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_mention_semantic_key", counting_semantic_key)
    monkeypatch.setattr(audit_repo_entropy, "_route_token_is_route_valued", counting_route_valued)
    monkeypatch.setattr(audit_repo_entropy, "_stale_route_clause_analysis", counting_clause_analysis)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert "/forecast" in str(findings[0]["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    assert semantic_key_call_count == 0
    assert route_valued_call_count == 0
    assert clause_analysis_call_count == 1


def test_route_authority_list_structural_context_is_cached_per_list_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_line_count = 80
    route_lines = [f"  Open /forecast for current proof {index}." for index in range(route_line_count)]
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "- Current operator procedure:\n" + "\n".join(route_lines) + "\n",
    )
    list_item_end_call_count = 0
    original_list_item_end_index = audit_repo_entropy._list_item_end_index

    def counting_list_item_end_index(*args: object) -> int:
        nonlocal list_item_end_call_count
        list_item_end_call_count += 1
        return original_list_item_end_index(*args)

    monkeypatch.setattr(audit_repo_entropy, "_list_item_end_index", counting_list_item_end_index)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == route_line_count
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert list_item_end_call_count <= 2


def test_route_authority_paragraph_structural_context_is_cached_per_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_line_count = 60
    route_lines = [
        f"Open /forecast for current proof {index} and"
        for index in range(route_line_count - 1)
    ]
    route_lines.append(f"Open /forecast for current proof {route_line_count - 1}.")
    _write(tmp_path / "docs" / "runbooks" / "current.md", "\n".join(route_lines) + "\n")
    paragraph_call_count = 0
    original_paragraph_text = audit_repo_entropy._stale_route_paragraph_governing_text_for_range

    def counting_paragraph_text(*args: object) -> str:
        nonlocal paragraph_call_count
        paragraph_call_count += 1
        return original_paragraph_text(*args)

    monkeypatch.setattr(
        audit_repo_entropy,
        "_stale_route_paragraph_governing_text_for_range",
        counting_paragraph_text,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == route_line_count
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert paragraph_call_count == 1


def test_route_authority_blockquote_paragraph_structural_context_is_cached_per_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_line_count = 60
    route_lines = [
        f"> Open /forecast for current proof {index} and"
        for index in range(route_line_count - 1)
    ]
    route_lines.append(f"> Open /forecast for current proof {route_line_count - 1}.")
    _write(tmp_path / "docs" / "runbooks" / "current.md", "\n".join(route_lines) + "\n")
    paragraph_call_count = 0
    original_paragraph_text = audit_repo_entropy._stale_route_paragraph_governing_text_for_range

    def counting_paragraph_text(*args: object) -> str:
        nonlocal paragraph_call_count
        paragraph_call_count += 1
        return original_paragraph_text(*args)

    monkeypatch.setattr(
        audit_repo_entropy,
        "_stale_route_paragraph_governing_text_for_range",
        counting_paragraph_text,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == route_line_count
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert paragraph_call_count == 1


def test_route_authority_route_free_large_markdown_does_not_build_governing_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "\n".join(f"# Current operator procedure {index}\nNo legacy route token here." for index in range(600))
        + "\n",
    )
    call_count = 0
    original_line_context = audit_repo_entropy._stale_route_line_context

    def counting_line_context(*args: object) -> object:
        nonlocal call_count
        call_count += 1
        return original_line_context(*args)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_line_context", counting_line_context)

    findings = _route_authority_findings(tmp_path)

    assert findings == []
    assert call_count == 0


def test_route_authority_current_repo_has_no_unallowlisted_findings_in_current_docs() -> None:
    guarded_entrypoints = {"README.md", "progress.md", "CLAUDE.md", "docs/governance/DOC_STATUS.md"}
    findings = [
        finding
        for finding in _route_authority_findings(REPO_ROOT)
        if finding["check_id"] == "stale-display-route-token"
        and finding["allowlist_state"] == "unallowlisted"
        and (
            str(finding["evidence_path"]).startswith("docs/runbooks/")
            or finding["evidence_path"] in guarded_entrypoints
        )
    ]

    assert findings == []


def test_route_authority_legacy_alias_coverage_includes_all_current_redirect_forms(
    tmp_path: Path,
) -> None:
    expected_tokens = {
        "/overview",
        "/hydro-met",
        "/forecast",
        "/meteorology",
        "/basins/:id",
        "/segments/:id",
        "/basins/demo",
        "/segments/demo",
    }
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "\n".join(f"Open {token} for current live browser proof." for token in sorted(expected_tokens)),
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == len(expected_tokens)
    descriptions = "\n".join(str(finding["description"]) for finding in findings)
    for token in expected_tokens:
        assert token in descriptions


def test_route_authority_expanded_aliases_do_not_scan_frontend_e2e_unless_old_token(
    tmp_path: Path,
) -> None:
    e2e_path = tmp_path / "apps" / "frontend" / "e2e" / "m11-routes.spec.ts"
    _write(
        e2e_path,
        """
        await page.goto('/overview')
        await page.goto('/forecast')
        await page.goto('/basins/demo')
        await page.goto('/segments/demo')
        """,
    )

    assert _route_authority_findings(tmp_path) == []

    _write(
        e2e_path,
        """
        await page.goto('/overview')
        await page.goto('/forecast')
        await page.goto('/basins/demo')
        await page.goto('/segments/demo')
        await page.goto('/hydro-met')
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["evidence_path"] == "apps/frontend/e2e/m11-routes.spec.ts"
    assert "/hydro-met" in finding["description"]
    assert all(
        token not in str(finding["description"])
        for token in ("/overview", "/forecast", "/basins/demo", "/segments/demo")
    )


def test_route_authority_expanded_aliases_do_not_scan_app_route_source_of_truth(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "src" / "App.tsx",
        """
        <Route path="/overview" element={<LegacyRedirect />} />
        <Route path="/forecast" element={<LegacyRedirect />} />
        <Route
          path="/basins/:basinId"
          element={<LegacyRedirect param={{ name: 'basinId', queryKey: 'basinId' }} />}
        />
        <Route
          path="/segments/:segmentId"
          element={<LegacyRedirect param={{ name: 'segmentId', queryKey: 'segmentId' }} />}
        />
        """,
    )

    assert _route_authority_findings(tmp_path) == []


def test_route_authority_expanded_aliases_do_not_scan_frontend_fixtures(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "src" / "fixtures" / "routes.ts",
        """
        export const fixtureRoutes = [
          '/overview',
          '/forecast',
          '/basins/demo',
          '/segments/demo',
        ]
        """,
    )

    assert _route_authority_findings(tmp_path) == []


def test_route_authority_skips_generated_artifact_roots(tmp_path: Path) -> None:
    _write(tmp_path / "artifacts" / "generated.md", "Open /overview for current proof.\n")

    assert _route_authority_findings(tmp_path) == []
