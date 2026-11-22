"""Scoped instruction state (#1823 partition).

The `scoped_agent_context` cases cover the four governed `AGENTS.md` scopes --
missing instructions, stale references, glossary linkage and wrapped
verification commands. #3274 deleted the two guard-hook seed cases together with
the inventory files they read.

This is the one partition that is not a byte-for-byte move. #1823 repointed the
verification-command literals off the deleted monolith onto
``tests/test_entropy_audit_*.py``, in lockstep with
``_ScopedAgentContextConfig.required_verification_commands`` and the four scoped
``AGENTS.md``. The commands are substring needles
(``audit_repo_entropy._missing_text_needles``), so the fixture text and the
config must migrate together or the scoped-context cases red on a command they
themselves stopped naming.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    REPO_ROOT,
    _assert_unallowlisted_budget_counted_report_only_finding,
    _findings_by_check,
    _init_git,
    _scoped_agent_context,
    _scoped_agent_context_scope,
    _scoped_agent_context_signals,
    _write,
)


def test_scoped_agent_context_current_repo_matches_scoped_instruction_state() -> None:
    context = _scoped_agent_context(REPO_ROOT)
    signals = context["signals"]
    assert isinstance(signals, list)
    scopes = context["scopes"]
    assert isinstance(scopes, list)
    expected_scoped_instruction_paths = {
        "services/orchestrator/AGENTS.md",
        "services/production_closure/AGENTS.md",
        "apps/api/AGENTS.md",
        "apps/frontend/AGENTS.md",
    }
    configured_paths = {
        config.instruction_path for config in audit_repo_entropy.SCOPED_AGENT_CONTEXT_CONFIGS
    }
    assert configured_paths == expected_scoped_instruction_paths
    assert context["governed_scope_count"] == 4

    expected_missing = {
        path
        for path in expected_scoped_instruction_paths
        if not (REPO_ROOT / path).is_file()
    }
    actual_missing = {
        str(signal["instruction_path"])
        for signal in signals
        if isinstance(signal, dict) and signal["signal_type"] == "missing-scoped-instruction"
    }

    assert actual_missing == expected_missing
    assert context["missing_instruction_count"] == len(expected_missing)
    if expected_missing == expected_scoped_instruction_paths:
        assert context["missing_instruction_count"] == 4
        assert context["stale_context_count"] == 0
        assert context["missing_glossary_link_count"] == 0
    for scope in scopes:
        assert isinstance(scope, dict)
        instruction_path = str(scope["instruction_path"])
        if instruction_path in expected_missing:
            assert scope["status"] == "missing"
            continue
        assert scope["status"] == "pass"


def test_scoped_agent_context_reports_missing_high_entropy_directory_instructions(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)

    context = _scoped_agent_context(tmp_path)
    findings = _findings_by_check(tmp_path, audit_repo_entropy.SCOPED_AGENT_CONTEXT_CHECK_ID)

    assert context["missing_instruction_count"] == len(audit_repo_entropy.SCOPED_AGENT_CONTEXT_CONFIGS)
    assert context["stale_context_count"] == 0
    assert context["missing_glossary_link_count"] == 0
    assert len(findings) == len(audit_repo_entropy.SCOPED_AGENT_CONTEXT_CONFIGS)
    assert {finding["evidence_path"] for finding in findings} == {
        config.instruction_path for config in audit_repo_entropy.SCOPED_AGENT_CONTEXT_CONFIGS
    }
    for finding in findings:
        _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_scoped_agent_context_reports_stale_scoped_context(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "services" / "orchestrator" / "AGENTS.md",
        """
        # Orchestrator Instructions

        See `openspec/glossary.md`.
        See `openspec/changes/governance-7-structural-entropy-controls/specs/scoped-agent-context-governance/spec.md`.

        Use the glossary terms active entrypoint, compatibility facade, current authority,
        and budget-counted finding when describing local ownership.

        Verification:
        - `uv run pytest -q tests/test_entropy_audit_*.py`
        - `openspec validate --all --strict --no-interactive`
        """,
    )

    signals = _scoped_agent_context_signals(tmp_path, "stale-scoped-context")
    orchestrator = [
        signal
        for signal in signals
        if signal["instruction_path"] == "services/orchestrator/AGENTS.md"
    ]

    assert len(orchestrator) == 1
    missing_items = set(orchestrator[0]["missing_items"])
    assert missing_items == {"docs/runbooks/two-node-deployment-overview.md"}
    assert not [
        signal
        for signal in _scoped_agent_context_signals(tmp_path, "missing-glossary-linkage")
        if signal["instruction_path"] == "services/orchestrator/AGENTS.md"
    ]


def test_scoped_agent_context_reports_missing_glossary_linkage(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "apps" / "api" / "AGENTS.md",
        f"""
        # API Instructions

        Current references:
        - `{audit_repo_entropy.SCOPED_AGENT_CONTEXT_SPEC_PATH}`
        - `docs/governance/ROLE_BOUNDARY.md`
        - `docs/runbooks/qhh-backend-smoke.md`

        Verification:
        - `uv run pytest -q tests/test_entropy_audit_*.py tests/test_runtime_mode.py tests/test_api.py`
        - `openspec validate --all --strict --no-interactive`
        """,
    )

    glossary_signals = _scoped_agent_context_signals(tmp_path, "missing-glossary-linkage")
    api = [
        signal
        for signal in glossary_signals
        if signal["instruction_path"] == "apps/api/AGENTS.md"
    ]

    assert len(api) == 1
    missing_items = set(api[0]["missing_items"])
    assert audit_repo_entropy.SCOPED_AGENT_CONTEXT_GLOSSARY_PATH in missing_items
    assert {"active entrypoint", "budget-counted finding", "gate-eligible finding"} <= missing_items
    assert not [
        signal
        for signal in _scoped_agent_context_signals(tmp_path, "stale-scoped-context")
        if signal["instruction_path"] == "apps/api/AGENTS.md"
    ]


def test_scoped_agent_context_accepts_glossary_link_without_repeated_terms(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "apps" / "api" / "AGENTS.md",
        f"""
        # API Instructions

        Use the canonical vocabulary in `openspec/glossary.md`.

        Current references:
        - `{audit_repo_entropy.SCOPED_AGENT_CONTEXT_SPEC_PATH}`
        - `docs/governance/ROLE_BOUNDARY.md`
        - `docs/runbooks/qhh-backend-smoke.md`

        Verification:
        - `uv run pytest -q tests/test_entropy_audit_*.py tests/test_runtime_mode.py tests/test_api.py`
        - `openspec validate --all --strict --no-interactive`
        """,
    )

    assert not [
        signal
        for signal in _scoped_agent_context_signals(tmp_path, "missing-glossary-linkage")
        if signal["instruction_path"] == "apps/api/AGENTS.md"
    ]
    api_scope = _scoped_agent_context_scope(tmp_path, "apps/api/AGENTS.md")
    assert api_scope["has_glossary_link"] is True
    assert api_scope["missing_glossary_terms"] == []


def test_scoped_agent_context_accepts_required_terms_without_glossary_link(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "apps" / "api" / "AGENTS.md",
        f"""
        # API Instructions

        Use the terms active entrypoint, current authority, budget-counted finding,
        and gate-eligible finding for local governance concepts.

        Current references:
        - `{audit_repo_entropy.SCOPED_AGENT_CONTEXT_SPEC_PATH}`
        - `docs/governance/ROLE_BOUNDARY.md`
        - `docs/runbooks/qhh-backend-smoke.md`

        Verification:
        - `uv run pytest -q tests/test_entropy_audit_*.py tests/test_runtime_mode.py tests/test_api.py`
        - `openspec validate --all --strict --no-interactive`
        """,
    )

    assert not [
        signal
        for signal in _scoped_agent_context_signals(tmp_path, "missing-glossary-linkage")
        if signal["instruction_path"] == "apps/api/AGENTS.md"
    ]
    api_scope = _scoped_agent_context_scope(tmp_path, "apps/api/AGENTS.md")
    assert api_scope["has_glossary_link"] is False
    assert api_scope["missing_glossary_terms"] == []


def test_scoped_agent_context_matches_wrapped_verification_command(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "apps" / "api" / "AGENTS.md",
        f"""
        # API Instructions

        See `openspec/glossary.md`.

        Current references:
        - `{audit_repo_entropy.SCOPED_AGENT_CONTEXT_SPEC_PATH}`
        - `docs/governance/ROLE_BOUNDARY.md`
        - `docs/runbooks/qhh-backend-smoke.md`

        Verification:
        - `uv run pytest -q tests/test_entropy_audit_*.py
          tests/test_runtime_mode.py tests/test_api.py`
        - `openspec validate --all --strict --no-interactive`
        """,
    )

    assert not [
        signal
        for signal in _scoped_agent_context_signals(tmp_path, "stale-scoped-context")
        if signal["instruction_path"] == "apps/api/AGENTS.md"
    ]
