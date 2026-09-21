"""The structural file-budget summary and the comparison base it is taken against.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Composes the
mandatory-governance and yellow-zone budget classes from the source inventory,
resolves the base ref (env override, origin/master merge-base, ``HEAD^``, local
fallback) and renders the markdown section."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    STRUCTURAL_BUDGET_BASE_REF_ENV,
    STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES,
    STRUCTURAL_FILE_BUDGET_SCHEMA_VERSION,
    STRUCTURAL_FILE_BUDGET_TOP_LIMIT,
    STRUCTURAL_FILE_BUDGET_YELLOW_MIN_LINES,
)
from scripts.governance.entropy_audit.repo_files import _git_merge_base, _git_resolve_commit, _module_for_relative
from scripts.governance.entropy_audit.schema import (
    StructuralBudgetClass,
    _StructuralBudgetExemption,
    _StructuralBudgetFile,
    _StructuralComparisonBase,
    _StructuralOwnershipGrowthSignal,
    _StructuralUnknownLineCountFile,
)
from scripts.governance.entropy_audit.structural_growth import (
    _structural_budget_exemption_record,
    _structural_budget_file_record,
    _structural_budget_owner_action,
    _structural_budget_review_reason,
    _structural_growth_signal_record,
    _structural_ownership_growth_signals,
    _structural_top_oversized_files_by_module,
    _structural_top_oversized_modules,
    _structural_unknown_line_count_file_record,
)
from scripts.governance.entropy_audit.structural_sources import (
    _read_structural_analysis_text,
    _read_structural_header_text,
    _structural_import_families,
    _structural_physical_line_count,
    _structural_source_exemption,
    _structural_source_rejection_reason,
    _structural_tracked_source_paths,
)
from scripts.governance.entropy_audit.structural_surface import _structural_ownership_surface_signals


def _structural_file_budget_summary(
    root: Path,
    *,
    structural_base_ref: str | None = None,
) -> dict[str, object]:
    comparison_base = _structural_comparison_base(root, structural_base_ref)
    mandatory_files: list[_StructuralBudgetFile] = []
    yellow_zone_files: list[_StructuralBudgetFile] = []
    governed_exemptions: list[_StructuralBudgetExemption] = []
    unknown_line_count_files: list[_StructuralUnknownLineCountFile] = []
    ownership_growth_signals: list[_StructuralOwnershipGrowthSignal] = []

    for relative_path in _structural_tracked_source_paths(root):
        path = root / relative_path
        if _structural_source_rejection_reason(root, path) is not None:
            continue
        line_count_result = _structural_physical_line_count(path)
        if line_count_result is None:
            continue
        module = _module_for_relative(relative_path)
        if (
            line_count_result.line_count_is_truncated
            and line_count_result.line_count_lower_bound
            <= STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES
        ):
            unknown_line_count_files.append(
                _StructuralUnknownLineCountFile(
                    relative_path=relative_path,
                    line_count=line_count_result.line_count,
                    line_count_is_truncated=line_count_result.line_count_is_truncated,
                    line_count_lower_bound=line_count_result.line_count_lower_bound,
                    size_bytes=line_count_result.size_bytes,
                    module=module,
                    review_reason=(
                        "line-count scan hit the byte cap before observing enough "
                        "physical lines to classify structural budget threshold"
                    ),
                    owner_action=(
                        "Inspect physical line count manually before applying "
                        "mandatory-governance or yellow-zone ownership rules."
                    ),
                )
            )
            continue
        line_count = line_count_result.line_count
        if line_count < STRUCTURAL_FILE_BUDGET_YELLOW_MIN_LINES:
            continue
        header_text = _read_structural_header_text(path) or ""
        exemption = _structural_source_exemption(relative_path, header_text)
        if exemption is not None:
            governed_exemptions.append(
                _StructuralBudgetExemption(
                    relative_path=relative_path,
                    line_count=line_count,
                    line_count_is_truncated=line_count_result.line_count_is_truncated,
                    line_count_lower_bound=line_count_result.line_count_lower_bound,
                    size_bytes=line_count_result.size_bytes,
                    module=module,
                    exemption_family=exemption.family,
                    exemption_reason=exemption.reason,
                )
            )
            continue

        text = _read_structural_analysis_text(path)
        import_families = _structural_import_families(relative_path, text) if text is not None else ()
        ownership_surface_signals = (
            _structural_ownership_surface_signals(relative_path, text, import_families)
            if text is not None
            else ()
        )
        budget_class: StructuralBudgetClass = (
            "mandatory-governance"
            if line_count > STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES
            else "yellow-zone"
        )
        record = _StructuralBudgetFile(
            relative_path=relative_path,
            line_count=line_count,
            line_count_is_truncated=line_count_result.line_count_is_truncated,
            line_count_lower_bound=line_count_result.line_count_lower_bound,
            size_bytes=line_count_result.size_bytes,
            module=module,
            budget_class=budget_class,
            import_families=import_families,
            ownership_surface_signals=ownership_surface_signals,
            review_reason=_structural_budget_review_reason(
                budget_class,
                line_count,
                ownership_surface_signals,
            ),
            owner_action=_structural_budget_owner_action(budget_class),
        )
        if budget_class == "mandatory-governance":
            mandatory_files.append(record)
            ownership_growth_signals.extend(
                _structural_ownership_growth_signals(root, record, comparison_base.resolved)
            )
        else:
            yellow_zone_files.append(record)

    mandatory_files.sort(key=lambda item: (-item.line_count, item.relative_path))
    yellow_zone_files.sort(key=lambda item: (-item.line_count, item.relative_path))
    governed_exemptions.sort(key=lambda item: (-item.line_count, item.relative_path))
    unknown_line_count_files.sort(key=lambda item: (-item.line_count_lower_bound, item.relative_path))
    ownership_growth_signals.sort(key=lambda item: (item.relative_path, item.signal_type, item.detail))

    return {
        "schema_version": STRUCTURAL_FILE_BUDGET_SCHEMA_VERSION,
        "mode": "report-only",
        "comparison_base_ref": _structural_comparison_base_record(comparison_base),
        "thresholds": {
            "yellow_zone_min_physical_lines": STRUCTURAL_FILE_BUDGET_YELLOW_MIN_LINES,
            "yellow_zone_max_physical_lines": STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES,
            "mandatory_governance_over_physical_lines": STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES,
        },
        "mandatory_governance_count": len(mandatory_files),
        "yellow_zone_count": len(yellow_zone_files),
        "governed_exemption_count": len(governed_exemptions),
        "unknown_line_count_count": len(unknown_line_count_files),
        "ownership_growth_signal_count": len(ownership_growth_signals),
        "oversized_files": [_structural_budget_file_record(item) for item in mandatory_files],
        "yellow_zone_files": [_structural_budget_file_record(item) for item in yellow_zone_files],
        "governed_exemptions": [
            _structural_budget_exemption_record(item) for item in governed_exemptions
        ],
        "unknown_line_count_files": [
            _structural_unknown_line_count_file_record(item) for item in unknown_line_count_files
        ],
        "ownership_growth_signals": [
            _structural_growth_signal_record(item) for item in ownership_growth_signals
        ],
        "top_oversized_modules": _structural_top_oversized_modules(mandatory_files),
        "top_oversized_files_by_module": _structural_top_oversized_files_by_module(mandatory_files),
    }


def _structural_comparison_base(
    root: Path,
    explicit_base_ref: str | None,
) -> _StructuralComparisonBase:
    requested_ref = explicit_base_ref or os.environ.get(STRUCTURAL_BUDGET_BASE_REF_ENV)
    requested_source = (
        "argument"
        if explicit_base_ref
        else "environment"
        if requested_ref
        else "auto"
    )
    fallback_reason: str | None = None
    if requested_ref:
        resolved = _git_resolve_commit(root, requested_ref)
        if resolved is not None:
            return _StructuralComparisonBase(
                requested=requested_ref,
                requested_source=requested_source,
                resolved=resolved,
                ref_kind="explicit",
                status="resolved",
            )
        fallback_reason = f"requested ref `{requested_ref}` could not be resolved"

    origin_master_base = _git_merge_base(root, "HEAD", "origin/master")
    if origin_master_base is not None:
        return _StructuralComparisonBase(
            requested=requested_ref,
            requested_source=requested_source,
            resolved=origin_master_base,
            ref_kind="origin-master-merge-base",
            status="fallback" if fallback_reason else "resolved",
            fallback_reason=fallback_reason,
        )

    head_parent = _git_resolve_commit(root, "HEAD^")
    if head_parent is not None:
        return _StructuralComparisonBase(
            requested=requested_ref,
            requested_source=requested_source,
            resolved=head_parent,
            ref_kind="head-parent",
            status="fallback" if fallback_reason else "resolved",
            fallback_reason=fallback_reason,
        )

    if os.environ.get("CI"):
        return _StructuralComparisonBase(
            requested=requested_ref,
            requested_source=requested_source,
            resolved=None,
            ref_kind="unavailable",
            status="unavailable",
            fallback_reason=(
                fallback_reason
                or "CI structural comparison requires an explicit base ref, origin/master, or HEAD^"
            ),
        )

    head = _git_resolve_commit(root, "HEAD")
    if head is not None:
        return _StructuralComparisonBase(
            requested=requested_ref,
            requested_source=requested_source,
            resolved=head,
            ref_kind="head",
            status="fallback" if fallback_reason else "resolved",
            fallback_reason=fallback_reason,
        )

    return _StructuralComparisonBase(
        requested=requested_ref,
        requested_source=requested_source,
        resolved=None,
        ref_kind="unavailable",
        status="unavailable",
        fallback_reason=fallback_reason or "no usable git comparison ref was available",
    )


def _structural_comparison_base_record(base: _StructuralComparisonBase) -> dict[str, object]:
    record: dict[str, object] = {
        "requested": base.requested,
        "requested_source": base.requested_source,
        "resolved": base.resolved,
        "ref_kind": base.ref_kind,
        "status": base.status,
    }
    if base.fallback_reason:
        record["fallback_reason"] = base.fallback_reason
    return record


def _structural_file_budget_markdown_lines(payload: object) -> list[str]:
    lines = ["", "## Structural File Budget", ""]
    if not isinstance(payload, dict):
        lines.append("- Structural file budget summary unavailable.")
        return lines
    lines.extend(
        [
            "- Mode: `report-only`",
            "- Thresholds: `500-1000` physical lines => `yellow-zone`; "
            "`>1000` physical lines => `mandatory-governance`",
            f"- Mandatory-governance files: `{payload.get('mandatory_governance_count', 0)}`",
            f"- Yellow-zone files: `{payload.get('yellow_zone_count', 0)}`",
            f"- Governed exemptions: `{payload.get('governed_exemption_count', 0)}`",
            f"- Unknown line-count files: `{payload.get('unknown_line_count_count', 0)}`",
            f"- Ownership-growth signals: `{payload.get('ownership_growth_signal_count', 0)}`",
        ]
    )
    comparison_base = payload.get("comparison_base_ref")
    if isinstance(comparison_base, dict):
        resolved = comparison_base.get("resolved") or "unavailable"
        lines.append(
            "- Comparison base: `{resolved}` ({ref_kind}; status `{status}`)".format(
                resolved=resolved,
                ref_kind=comparison_base.get("ref_kind", "unknown"),
                status=comparison_base.get("status", "unknown"),
            )
        )
    top_modules = payload.get("top_oversized_modules", [])
    if isinstance(top_modules, list) and top_modules:
        lines.extend(
            [
                "",
                "| Module | Oversized Files | Max Lines | Total Lines | Top Paths |",
                "|---|---:|---:|---:|---|",
            ]
        )
        for row in top_modules[:STRUCTURAL_FILE_BUDGET_TOP_LIMIT]:
            if not isinstance(row, dict):
                continue
            paths = row.get("paths", [])
            path_text = ", ".join(f"`{path}`" for path in paths) if isinstance(paths, list) else ""
            lines.append(
                "| {module} | {count} | {max_lines} | {total_lines} | {paths} |".format(
                    module=row.get("module", "unknown"),
                    count=row.get("oversized_file_count", 0),
                    max_lines=row.get("max_line_count", 0),
                    total_lines=row.get("total_line_count", 0),
                    paths=path_text or "none",
                )
            )
    else:
        lines.append("- No oversized implementation files detected.")

    growth_signals = payload.get("ownership_growth_signals", [])
    if isinstance(growth_signals, list) and growth_signals:
        lines.extend(["", "Ownership-growth signals:"])
        for signal in growth_signals[:STRUCTURAL_FILE_BUDGET_TOP_LIMIT]:
            if not isinstance(signal, dict):
                continue
            lines.append(
                "- `{path}` `{signal_type}`: {detail}; action: {owner_action}".format(
                    path=signal.get("path", "unknown"),
                    signal_type=signal.get("signal_type", "unknown"),
                    detail=signal.get("detail", ""),
                    owner_action=signal.get("owner_action", ""),
                )
            )
    return lines
