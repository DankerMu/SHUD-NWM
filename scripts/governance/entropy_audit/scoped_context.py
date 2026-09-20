"""The ``scoped-agent-context`` check over the governed scope instructions.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. For each
configured scope, checks its ``AGENTS.md`` exists and carries the required
glossary terms, references and verification commands, and renders the summary,
findings and markdown section."""

from __future__ import annotations

from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    SCOPED_AGENT_CONTEXT_CHECK_ID,
    SCOPED_AGENT_CONTEXT_GLOSSARY_PATH,
    SCOPED_AGENT_CONTEXT_SCHEMA_VERSION,
    STRUCTURAL_FILE_BUDGET_TOP_LIMIT,
)
from scripts.governance.entropy_audit.repo_files import _module_for_relative, _read_repo_text
from scripts.governance.entropy_audit.schema import SCOPED_AGENT_CONTEXT_CONFIGS, FindingSpec, _ScopedAgentContextConfig


def _scoped_agent_context_summary(root: Path) -> dict[str, object]:
    scopes: list[dict[str, object]] = []
    signals: list[dict[str, object]] = []
    for config in SCOPED_AGENT_CONTEXT_CONFIGS:
        scope_record, scope_signals = _scoped_agent_context_scope_record(root, config)
        scopes.append(scope_record)
        signals.extend(scope_signals)
    return {
        "schema_version": SCOPED_AGENT_CONTEXT_SCHEMA_VERSION,
        "mode": "report-only",
        "governed_scope_count": len(SCOPED_AGENT_CONTEXT_CONFIGS),
        "missing_instruction_count": sum(
            1 for signal in signals if signal["signal_type"] == "missing-scoped-instruction"
        ),
        "stale_context_count": sum(
            1 for signal in signals if signal["signal_type"] == "stale-scoped-context"
        ),
        "missing_glossary_link_count": sum(
            1 for signal in signals if signal["signal_type"] == "missing-glossary-linkage"
        ),
        "signal_count": len(signals),
        "scopes": scopes,
        "signals": signals,
    }


def _scoped_agent_context_scope_record(
    root: Path,
    config: _ScopedAgentContextConfig,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    instruction = root / config.instruction_path
    present = instruction.is_file()
    text = _read_repo_text(root, instruction) if present else ""
    signals: list[dict[str, object]] = []
    if not present:
        signals.append(
            _scoped_agent_context_signal(
                config,
                signal_type="missing-scoped-instruction",
                detail=f"`{config.instruction_path}` is missing.",
                missing_items=(config.instruction_path,),
            )
        )
        return (
            _scoped_agent_context_record(
                config,
                present=False,
                status="missing",
                missing_references=(),
                missing_verification_commands=(),
                missing_glossary_terms=(),
                has_glossary_link=False,
            ),
            signals,
        )

    missing_references = _missing_text_needles(text, config.required_references)
    missing_verification_commands = _missing_text_needles(text, config.required_verification_commands)
    absent_glossary_terms = _missing_text_needles(text, config.required_glossary_terms)
    has_glossary_link = _text_contains_needle(text, SCOPED_AGENT_CONTEXT_GLOSSARY_PATH)
    missing_glossary_terms = () if has_glossary_link else absent_glossary_terms

    if missing_references or missing_verification_commands:
        missing_items = (*missing_references, *missing_verification_commands)
        signals.append(
            _scoped_agent_context_signal(
                config,
                signal_type="stale-scoped-context",
                detail="Scoped instruction is missing current references or verification commands.",
                missing_items=missing_items,
                line=1,
            )
        )
    if (not has_glossary_link) and absent_glossary_terms:
        missing_items = (SCOPED_AGENT_CONTEXT_GLOSSARY_PATH, *missing_glossary_terms)
        signals.append(
            _scoped_agent_context_signal(
                config,
                signal_type="missing-glossary-linkage",
                detail="Scoped instruction must link the glossary or reuse required glossary terms.",
                missing_items=missing_items,
                line=1,
            )
        )

    status = "pass" if not signals else "incomplete"
    return (
        _scoped_agent_context_record(
            config,
            present=True,
            status=status,
            missing_references=missing_references,
            missing_verification_commands=missing_verification_commands,
            missing_glossary_terms=missing_glossary_terms,
            has_glossary_link=has_glossary_link,
        ),
        signals,
    )


def _scoped_agent_context_record(
    config: _ScopedAgentContextConfig,
    *,
    present: bool,
    status: str,
    missing_references: tuple[str, ...],
    missing_verification_commands: tuple[str, ...],
    missing_glossary_terms: tuple[str, ...],
    has_glossary_link: bool,
) -> dict[str, object]:
    return {
        "scope_path": config.scope_path,
        "instruction_path": config.instruction_path,
        "owner_area": config.owner_area,
        "present": present,
        "status": status,
        "has_glossary_link": has_glossary_link,
        "missing_references": list(missing_references),
        "missing_verification_commands": list(missing_verification_commands),
        "missing_glossary_terms": list(missing_glossary_terms),
        "required_references": list(config.required_references),
        "required_verification_commands": list(config.required_verification_commands),
        "required_glossary_terms": list(config.required_glossary_terms),
    }


def _scoped_agent_context_signal(
    config: _ScopedAgentContextConfig,
    *,
    signal_type: str,
    detail: str,
    missing_items: tuple[str, ...],
    line: int | None = None,
) -> dict[str, object]:
    return {
        "signal_type": signal_type,
        "scope_path": config.scope_path,
        "instruction_path": config.instruction_path,
        "owner_area": config.owner_area,
        "line": line,
        "detail": detail,
        "missing_items": list(missing_items),
        "owner_action": _scoped_agent_context_owner_action(signal_type, config.instruction_path),
    }


def _scoped_agent_context_owner_action(signal_type: str, instruction_path: str) -> str:
    if signal_type == "missing-scoped-instruction":
        return f"Add `{instruction_path}` with local ownership rules and focused verification commands."
    if signal_type == "stale-scoped-context":
        return f"Refresh `{instruction_path}` with current spec/runbook references and verification commands."
    return f"Link `{SCOPED_AGENT_CONTEXT_GLOSSARY_PATH}` from `{instruction_path}` and reuse glossary terms."


def _scoped_agent_context_findings(summary: dict[str, object]) -> list[FindingSpec]:
    raw_signals = summary.get("signals", [])
    if not isinstance(raw_signals, list):
        return []
    findings: list[FindingSpec] = []
    for signal in raw_signals:
        if not isinstance(signal, dict):
            continue
        instruction_path = str(signal.get("instruction_path", ""))
        signal_type = str(signal.get("signal_type", ""))
        missing_items = signal.get("missing_items", [])
        missing_text = ", ".join(str(item) for item in missing_items) if isinstance(missing_items, list) else ""
        line = signal.get("line")
        findings.append(
            FindingSpec(
                check_id=SCOPED_AGENT_CONTEXT_CHECK_ID,
                title=_scoped_agent_context_finding_title(signal_type),
                axis="context",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path=instruction_path,
                line=line if isinstance(line, int) else None,
                severity="medium",
                priority="P2",
                owner_area=str(signal.get("owner_area", "governance/scoped agent context")),
                module=_module_for_relative(str(signal.get("scope_path", instruction_path))),
                description=f"{signal_type}: {signal.get('detail', '')} Missing: {missing_text}",
                recommendation=str(signal.get("owner_action", "")),
            )
        )
    return findings


def _scoped_agent_context_finding_title(signal_type: str) -> str:
    if signal_type == "missing-scoped-instruction":
        return "High-entropy directory lacks scoped agent instructions"
    if signal_type == "stale-scoped-context":
        return "Scoped agent instructions lack freshness references"
    if signal_type == "missing-glossary-linkage":
        return "Scoped agent instructions lack glossary linkage"
    return "Scoped agent context coverage is incomplete"


def _scoped_agent_context_markdown_lines(payload: object) -> list[str]:
    lines = ["", "## Scoped Agent Context", ""]
    if not isinstance(payload, dict):
        lines.append("- Scoped agent context summary unavailable.")
        return lines
    lines.extend(
        [
            "- Mode: `report-only`",
            f"- Governed scopes: `{payload.get('governed_scope_count', 0)}`",
            f"- Missing scoped instructions: `{payload.get('missing_instruction_count', 0)}`",
            f"- Stale scoped contexts: `{payload.get('stale_context_count', 0)}`",
            f"- Missing glossary linkages: `{payload.get('missing_glossary_link_count', 0)}`",
        ]
    )
    signals = payload.get("signals", [])
    if isinstance(signals, list) and signals:
        lines.extend(["", "Scoped context signals:"])
        for signal in signals[:STRUCTURAL_FILE_BUDGET_TOP_LIMIT]:
            if not isinstance(signal, dict):
                continue
            missing_items = signal.get("missing_items", [])
            missing_text = ", ".join(str(item) for item in missing_items) if isinstance(missing_items, list) else ""
            lines.append(
                (
                    "- `{signal_type}` for `{instruction_path}`: {detail} "
                    "Missing: {missing}; action: {owner_action}"
                ).format(
                    signal_type=signal.get("signal_type", "unknown"),
                    instruction_path=signal.get("instruction_path", "unknown"),
                    detail=signal.get("detail", ""),
                    missing=missing_text or "none",
                    owner_action=signal.get("owner_action", ""),
                )
            )
    return lines


def _missing_text_needles(text: str, needles: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(needle for needle in needles if not _text_contains_needle(text, needle))


def _text_contains_needle(text: str, needle: str) -> bool:
    return _normalize_scoped_context_text(needle) in _normalize_scoped_context_text(text)


def _normalize_scoped_context_text(text: str) -> str:
    return " ".join(text.casefold().split())
