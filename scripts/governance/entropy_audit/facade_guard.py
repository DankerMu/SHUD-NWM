"""The ``compatibility-facade-growth`` check over the two governed facades.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Parses a
governed facade's AST into its exposed surface (imported symbols, assignment
aliases, forwarding definitions), classifies each exposed name against its
owner, and checks the governing inventory document covers the signal with owner,
retention, removal-condition and verification semantics."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    COMPATIBILITY_FACADE_GUARD_CHECK_ID,
    COMPATIBILITY_FACADE_GUARD_SCHEMA_VERSION,
    COMPATIBILITY_FACADE_PROJECT_IMPORT_ROOTS,
    STRUCTURAL_FILE_BUDGET_TOP_LIMIT,
)
from scripts.governance.entropy_audit.repo_files import _module_for_relative
from scripts.governance.entropy_audit.schema import (
    COMPATIBILITY_FACADE_CONFIGS,
    FindingSpec,
    _CompatibilityFacadeAlias,
    _CompatibilityFacadeConfig,
    _CompatibilityFacadeDefinition,
    _CompatibilityFacadeImportedSymbol,
    _CompatibilityFacadeSignal,
    _CompatibilityFacadeSurface,
)
from scripts.governance.entropy_audit.structural_budget import (
    _structural_comparison_base,
    _structural_comparison_base_record,
)
from scripts.governance.entropy_audit.structural_growth import _git_blob_text
from scripts.governance.entropy_audit.structural_sources import (
    _read_structural_analysis_text,
    _structural_import_families,
)


def _compatibility_facade_guard_summary(
    root: Path,
    *,
    structural_base_ref: str | None = None,
) -> dict[str, object]:
    comparison_base = _structural_comparison_base(root, structural_base_ref)
    signals: list[_CompatibilityFacadeSignal] = []
    facade_records: list[dict[str, object]] = []
    for config in COMPATIBILITY_FACADE_CONFIGS:
        facade_signals = _compatibility_facade_signals(root, config, comparison_base.resolved)
        signals.extend(facade_signals)
        facade_records.append(
            {
                "name": config.name,
                "path": config.relative_path,
                "inventory_path": config.inventory_path,
                "signal_count": len(facade_signals),
                "status": "inventory-update-required" if facade_signals else "ok",
            }
        )
    signals.sort(
        key=lambda item: (
            item.relative_path,
            item.signal_type,
            item.line or 0,
            item.detail,
        )
    )
    return {
        "schema_version": COMPATIBILITY_FACADE_GUARD_SCHEMA_VERSION,
        "mode": "report-only",
        "comparison_base_ref": _structural_comparison_base_record(comparison_base),
        "governed_facade_count": len(COMPATIBILITY_FACADE_CONFIGS),
        "signal_count": len(signals),
        "facades": facade_records,
        "signals": [_compatibility_facade_signal_record(signal) for signal in signals],
    }


def _compatibility_facade_signals(
    root: Path,
    config: _CompatibilityFacadeConfig,
    comparison_base_ref: str | None,
) -> list[_CompatibilityFacadeSignal]:
    if comparison_base_ref is None:
        return []
    current_text = _read_structural_analysis_text(root / config.relative_path)
    if current_text is None:
        return []
    base_text = _git_blob_text(root, comparison_base_ref, config.relative_path) or ""
    current_surface = _compatibility_facade_surface(root, config.relative_path, current_text)
    base_surface = _compatibility_facade_surface(root, config.relative_path, base_text)
    inventory_text = _read_structural_analysis_text(root / config.inventory_path) or ""
    signals: list[_CompatibilityFacadeSignal] = []

    base_import_families = set(base_surface.import_families)
    for import_family in sorted(set(current_surface.import_families) - base_import_families):
        signal = _compatibility_facade_signal(
            config,
            signal_type="new-import-family",
            inventory_tokens=(import_family,),
            line=None,
            detail=f"new import family `{import_family}` in `{config.relative_path}`",
        )
        if not _compatibility_inventory_covers_signal(inventory_text, signal):
            signals.append(signal)

    base_imported_keys = {item.key for item in base_surface.imported_symbols}
    for item in current_surface.imported_symbols:
        if item.key in base_imported_keys:
            continue
        signal = _compatibility_facade_signal(
            config,
            signal_type=_compatibility_symbol_signal_type(item.exposed_name, item.imported_name),
            inventory_tokens=(item.exposed_name,),
            line=item.line,
            detail=(
                f"new imported facade symbol `{item.exposed_name}` from "
                f"`{item.module}.{item.imported_name}`"
            ),
        )
        if not _compatibility_inventory_covers_signal(inventory_text, signal):
            signals.append(signal)

    base_alias_keys = {item.key for item in base_surface.aliases}
    for item in current_surface.aliases:
        if item.key in base_alias_keys:
            continue
        signal = _compatibility_facade_signal(
            config,
            signal_type=_compatibility_symbol_signal_type(item.exposed_name, item.owner_attr),
            inventory_tokens=(item.exposed_name,),
            line=item.line,
            detail=(
                f"new owner-module alias `{item.exposed_name}` forwarding to "
                f"`{item.owner_module}.{item.owner_attr}`"
            ),
        )
        if not _compatibility_inventory_covers_signal(inventory_text, signal):
            signals.append(signal)

    base_definitions_by_key = {item.key: item for item in base_surface.definitions}
    for item in current_surface.definitions:
        base_item = base_definitions_by_key.get(item.key)
        if base_item is not None:
            if base_item.forwarding == item.forwarding:
                continue
            if item.forwarding:
                signal_type = _compatibility_symbol_signal_type(item.simple_name, item.simple_name)
                detail = (
                    "existing non-forwarding facade implementation changed to forwarding "
                    f"facade path `{item.qualified_name}` in `{config.relative_path}`"
                )
            else:
                signal_type = "new-non-forwarding-implementation"
                detail = (
                    "existing forwarding facade path changed to non-forwarding "
                    f"facade implementation `{item.qualified_name}` in `{config.relative_path}`"
                )
            signal = _compatibility_facade_signal(
                config,
                signal_type=signal_type,
                inventory_tokens=(item.qualified_name, item.simple_name),
                line=item.line,
                detail=detail,
            )
            if not _compatibility_inventory_covers_signal(inventory_text, signal):
                signals.append(signal)
            continue
        signal_type = (
            _compatibility_symbol_signal_type(item.simple_name, item.simple_name)
            if item.forwarding
            else "new-non-forwarding-implementation"
        )
        behavior = "forwarding facade path" if item.forwarding else "non-forwarding facade implementation"
        signal = _compatibility_facade_signal(
            config,
            signal_type=signal_type,
            inventory_tokens=(item.qualified_name, item.simple_name),
            line=item.line,
            detail=f"new {behavior} `{item.qualified_name}` in `{config.relative_path}`",
        )
        if not _compatibility_inventory_covers_signal(inventory_text, signal):
            signals.append(signal)

    return signals


def _compatibility_facade_surface(
    root: Path,
    relative_path: str,
    text: str,
) -> _CompatibilityFacadeSurface:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _CompatibilityFacadeSurface(
            import_families=(),
            imported_symbols=(),
            aliases=(),
            definitions=(),
        )
    module_aliases = _compatibility_facade_module_aliases(root, tree)
    return _CompatibilityFacadeSurface(
        import_families=tuple(
            family
            for family in _structural_import_families(relative_path, text)
            if _compatibility_project_import_family(family)
        ),
        imported_symbols=_compatibility_facade_imported_symbols(root, tree),
        aliases=_compatibility_facade_aliases(tree, module_aliases),
        definitions=_compatibility_facade_definitions(tree, module_aliases),
    )


def _compatibility_facade_module_aliases(root: Path, tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _compatibility_project_module(alias.name):
                    continue
                exposed_name = alias.asname or alias.name
                aliases[exposed_name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if not _compatibility_project_module(node.module):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not _compatibility_imported_name_is_module(root, node.module, alias.name):
                    continue
                exposed_name = alias.asname or alias.name
                aliases[exposed_name] = f"{node.module}.{alias.name}"
    return aliases


def _compatibility_facade_imported_symbols(
    root: Path,
    tree: ast.Module,
) -> tuple[_CompatibilityFacadeImportedSymbol, ...]:
    symbols: list[_CompatibilityFacadeImportedSymbol] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module == "__future__" or not _compatibility_project_module(node.module):
            continue
        for alias in node.names:
            if alias.name == "*" or _compatibility_imported_name_is_module(root, node.module, alias.name):
                continue
            symbols.append(
                _CompatibilityFacadeImportedSymbol(
                    exposed_name=alias.asname or alias.name,
                    imported_name=alias.name,
                    module=node.module,
                    line=node.lineno,
                )
            )
    return tuple(sorted(symbols, key=lambda item: item.key))


def _compatibility_facade_aliases(
    tree: ast.Module,
    module_aliases: dict[str, str],
) -> tuple[_CompatibilityFacadeAlias, ...]:
    aliases: list[_CompatibilityFacadeAlias] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            aliases.extend(
                _compatibility_facade_assignment_aliases(
                    tuple(node.targets),
                    node.value,
                    node.lineno,
                    module_aliases,
                )
            )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            aliases.extend(
                _compatibility_facade_assignment_aliases(
                    (node.target,),
                    node.value,
                    node.lineno,
                    module_aliases,
                )
            )
    return tuple(sorted(aliases, key=lambda item: item.key))


def _compatibility_facade_assignment_aliases(
    targets: tuple[ast.expr, ...],
    value: ast.expr,
    line: int,
    module_aliases: dict[str, str],
) -> tuple[_CompatibilityFacadeAlias, ...]:
    aliases: list[_CompatibilityFacadeAlias] = []
    for target in targets:
        aliases.extend(
            _compatibility_facade_aliases_for_target_value(
                target,
                value,
                line,
                module_aliases,
            )
        )
    return tuple(aliases)


def _compatibility_facade_aliases_for_target_value(
    target: ast.expr,
    value: ast.expr,
    line: int,
    module_aliases: dict[str, str],
) -> tuple[_CompatibilityFacadeAlias, ...]:
    alias = _compatibility_facade_alias(target, value, line, module_aliases)
    if alias is not None:
        return (alias,)

    target_elements = _compatibility_sequence_elements(target)
    value_elements = _compatibility_sequence_elements(value)
    if (
        target_elements is None
        or value_elements is None
        or len(target_elements) != len(value_elements)
    ):
        return ()

    aliases: list[_CompatibilityFacadeAlias] = []
    for target_element, value_element in zip(target_elements, value_elements, strict=True):
        alias = _compatibility_facade_alias(
            target_element,
            value_element,
            line,
            module_aliases,
        )
        if alias is not None:
            aliases.append(alias)
    return tuple(aliases)


def _compatibility_sequence_elements(expression: ast.expr) -> tuple[ast.expr, ...] | None:
    if not isinstance(expression, ast.Tuple | ast.List):
        return None
    return tuple(expression.elts)


def _compatibility_facade_alias(
    target: ast.expr,
    value: ast.expr,
    line: int,
    module_aliases: dict[str, str],
) -> _CompatibilityFacadeAlias | None:
    if not isinstance(target, ast.Name):
        return None
    owner_attribute = _compatibility_facade_owner_attribute(value, module_aliases)
    if owner_attribute is None:
        return None
    owner_module, owner_attr = owner_attribute
    return _CompatibilityFacadeAlias(
        exposed_name=target.id,
        owner_module=owner_module,
        owner_attr=owner_attr,
        line=line,
    )


def _compatibility_facade_owner_attribute(
    expression: ast.expr,
    module_aliases: dict[str, str],
) -> tuple[str, str] | None:
    parts = _compatibility_attribute_parts(expression)
    if len(parts) < 2:
        return None
    module_parts = parts[:-1]
    owner_attr = parts[-1]
    for prefix_length in range(len(module_parts), 0, -1):
        module_prefix = ".".join(module_parts[:prefix_length])
        owner_module = module_aliases.get(module_prefix)
        if owner_module is None:
            continue
        remaining_parts = module_parts[prefix_length:]
        if remaining_parts:
            owner_module = ".".join((owner_module, *remaining_parts))
        return owner_module, owner_attr
    return None


def _compatibility_attribute_parts(expression: ast.expr) -> tuple[str, ...]:
    parts: list[str] = []
    current = expression
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ()
    parts.append(current.id)
    return tuple(reversed(parts))


def _compatibility_facade_definitions(
    tree: ast.Module,
    module_aliases: dict[str, str],
) -> tuple[_CompatibilityFacadeDefinition, ...]:
    definitions: list[_CompatibilityFacadeDefinition] = []
    current_top_level_class_names = {
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            definitions.append(
                _CompatibilityFacadeDefinition(
                    qualified_name=node.name,
                    simple_name=node.name,
                    kind="function",
                    line=node.lineno,
                    forwarding=_compatibility_function_is_forwarding(node, module_aliases),
                )
            )
        elif isinstance(node, ast.ClassDef):
            definitions.append(
                _CompatibilityFacadeDefinition(
                    qualified_name=node.name,
                    simple_name=node.name,
                    kind="class",
                    line=node.lineno,
                    forwarding=False,
                )
            )
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    definitions.append(
                        _CompatibilityFacadeDefinition(
                            qualified_name=f"{node.name}.{child.name}",
                            simple_name=child.name,
                            kind="method",
                            line=child.lineno,
                            forwarding=_compatibility_function_is_forwarding(child, module_aliases),
                        )
                    )
    return tuple(
        sorted(
            definitions,
            key=lambda item: (
                item.kind,
                item.qualified_name,
                item.line,
                item.simple_name in current_top_level_class_names,
            ),
        )
    )


def _compatibility_function_is_forwarding(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module_aliases: dict[str, str],
) -> bool:
    body = _compatibility_function_effective_body(node.body)
    if len(body) != 1:
        return False
    statement = body[0]
    if isinstance(statement, ast.Return):
        return _compatibility_expression_is_owner_call(statement.value, module_aliases)
    if isinstance(statement, ast.Expr):
        return _compatibility_expression_is_owner_call(statement.value, module_aliases)
    return False


def _compatibility_function_effective_body(statements: list[ast.stmt]) -> list[ast.stmt]:
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return statements[1:]
    return statements


def _compatibility_expression_is_owner_call(
    expression: ast.expr | None,
    module_aliases: dict[str, str],
) -> bool:
    if isinstance(expression, ast.Await):
        expression = expression.value
    if not isinstance(expression, ast.Call):
        return False
    return _compatibility_facade_owner_attribute(expression.func, module_aliases) is not None


def _compatibility_project_module(module: str) -> bool:
    first = module.split(".", maxsplit=1)[0]
    return first in COMPATIBILITY_FACADE_PROJECT_IMPORT_ROOTS


def _compatibility_project_import_family(import_family: str) -> bool:
    first = import_family.split("/", maxsplit=1)[0]
    return first in COMPATIBILITY_FACADE_PROJECT_IMPORT_ROOTS


def _compatibility_imported_name_is_module(root: Path, module: str, name: str) -> bool:
    parts = [*module.split("."), name]
    module_file = root.joinpath(*parts).with_suffix(".py")
    package_file = root.joinpath(*parts, "__init__.py")
    return module_file.exists() or package_file.exists()


def _compatibility_symbol_signal_type(exposed_name: str, owner_name: str) -> str:
    if exposed_name.startswith("_") or owner_name.startswith("_"):
        return "new-monkeypatch-alias"
    return "new-facade-reexport"


def _compatibility_facade_signal(
    config: _CompatibilityFacadeConfig,
    *,
    signal_type: str,
    inventory_tokens: tuple[str, ...],
    line: int | None,
    detail: str,
) -> _CompatibilityFacadeSignal:
    return _CompatibilityFacadeSignal(
        facade_name=config.name,
        relative_path=config.relative_path,
        inventory_path=config.inventory_path,
        signal_type=signal_type,
        message_key=f"{COMPATIBILITY_FACADE_GUARD_CHECK_ID}.{signal_type}.inventory-required",
        inventory_tokens=inventory_tokens,
        line=line,
        detail=detail,
        owner_action=(
            "Update the matching compatibility inventory with the new symbol/import family, "
            "real owner, retention reason, removal condition, verification command, "
            "and follow-up rationale; "
            "otherwise move the implementation to the owning module."
        ),
    )


def _compatibility_inventory_covers_signal(
    inventory_text: str,
    signal: _CompatibilityFacadeSignal,
) -> bool:
    guard_text = _compatibility_inventory_guard_hook_text(inventory_text)
    return any(
        _compatibility_inventory_line_covers_signal(line, signal)
        for line in guard_text.splitlines()
    )


def _compatibility_inventory_line_covers_signal(
    line: str,
    signal: _CompatibilityFacadeSignal,
) -> bool:
    if not any(_compatibility_inventory_contains_token(line, token) for token in signal.inventory_tokens):
        return False
    return _compatibility_inventory_line_has_metadata(line, signal)


def _compatibility_inventory_line_has_metadata(line: str, signal: _CompatibilityFacadeSignal) -> bool:
    normalized = line.casefold()
    if signal.signal_type in {"new-facade-reexport", "new-monkeypatch-alias"}:
        required_metadata = (
            _compatibility_inventory_has_owner_semantics(normalized)
            and _compatibility_inventory_has_retention_semantics(normalized)
            and _compatibility_inventory_has_removal_condition_semantics(normalized)
        )
        if signal.facade_name in {"scheduler", "chain"}:
            required_metadata = required_metadata and _compatibility_inventory_has_verification_semantics(
                normalized
            )
        return required_metadata
    if signal.signal_type == "new-non-forwarding-implementation":
        return (
            _compatibility_inventory_has_owner_hosting_rationale_semantics(normalized)
            and _compatibility_inventory_has_follow_up_issue_semantics(normalized)
            and _compatibility_inventory_has_removal_condition_semantics(normalized)
        )
    if signal.signal_type == "new-import-family":
        return (
            _compatibility_inventory_has_justification_semantics(normalized)
            and _compatibility_inventory_has_no_ownership_inversion_semantics(normalized)
        )
    return False


def _compatibility_inventory_has_owner_semantics(text: str) -> bool:
    return re.search(r"(?<![a-z0-9_])owner(?![a-z0-9_])", text) is not None


def _compatibility_inventory_has_retention_semantics(text: str) -> bool:
    return "retention" in text or "retain" in text


def _compatibility_inventory_has_removal_condition_semantics(text: str) -> bool:
    return "removal-condition" in text or "removal condition" in text


def _compatibility_inventory_has_verification_semantics(text: str) -> bool:
    if "verification command" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "`uv run pytest",
            "`uv run ruff",
            "`openspec validate",
            "`git diff --check",
            "`corepack pnpm",
            "`pnpm ",
            "`cd apps/frontend && pnpm",
        )
    )


def _compatibility_inventory_has_owner_hosting_rationale_semantics(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "owner module cannot host",
            "owner cannot host",
            "cannot host",
            "not hostable in owner",
        )
    )


def _compatibility_inventory_has_follow_up_issue_semantics(text: str) -> bool:
    follow_up_pattern = r"(?:follow-up|follow up)"
    issue_ref_pattern = r"(?:#[0-9]+|/?issues/[0-9]+)"
    return (
        re.search(rf"{follow_up_pattern}.{{0,40}}{issue_ref_pattern}", text) is not None
        or re.search(rf"{issue_ref_pattern}.{{0,40}}{follow_up_pattern}", text) is not None
    )


def _compatibility_inventory_has_justification_semantics(text: str) -> bool:
    return "justified" in text or "justification" in text or "justify" in text


def _compatibility_inventory_has_no_ownership_inversion_semantics(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "does not invert ownership",
            "do not invert ownership",
            "doesn't invert ownership",
            "no ownership inversion",
            "without ownership inversion",
        )
    )


def _compatibility_inventory_guard_hook_text(inventory_text: str) -> str:
    match = re.search(r"(?im)^##\s+Guard Hook Seed\s*$", inventory_text)
    if match is None:
        return ""
    section = inventory_text[match.end() :]
    next_heading = re.search(r"(?m)^##\s+", section)
    if next_heading is not None:
        section = section[: next_heading.start()]
    return section


def _compatibility_inventory_contains_token(inventory_text: str, token: str) -> bool:
    token = token.strip()
    if not token:
        return False
    variants = {token, token.replace(".", "/"), token.replace("/", ".")}
    for variant in variants:
        if "/" in variant or "." in variant or "-" in variant:
            if variant in inventory_text:
                return True
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(variant)}(?![A-Za-z0-9_])", inventory_text):
            return True
    return False


def _compatibility_facade_signal_record(signal: _CompatibilityFacadeSignal) -> dict[str, object]:
    record: dict[str, object] = {
        "path": signal.relative_path,
        "facade": signal.facade_name,
        "inventory_path": signal.inventory_path,
        "signal_type": signal.signal_type,
        "message_key": signal.message_key,
        "inventory_tokens": list(signal.inventory_tokens),
        "detail": signal.detail,
        "owner_action": signal.owner_action,
    }
    if signal.line is not None:
        record["line"] = signal.line
    return record


def _compatibility_facade_guard_findings(
    summary: dict[str, object],
) -> list[FindingSpec]:
    signals = summary.get("signals", [])
    if not isinstance(signals, list):
        return []
    findings: list[FindingSpec] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        relative_path = str(signal.get("path", ""))
        line = signal.get("line")
        findings.append(
            FindingSpec(
                check_id=COMPATIBILITY_FACADE_GUARD_CHECK_ID,
                title="Compatibility facade growth lacks inventory coverage",
                axis="structure",
                governance_face="compatibility facade governance",
                role="facade growth guard",
                evidence_path=relative_path,
                line=line if isinstance(line, int) else None,
                severity="medium",
                priority="P2",
                owner_area="governance/structural entropy",
                module=_module_for_relative(relative_path),
                description=f"{signal.get('message_key', '')}: {signal.get('detail', '')}",
                recommendation=str(signal.get("owner_action", "")),
            )
        )
    return findings


def _compatibility_facade_guard_markdown_lines(payload: object) -> list[str]:
    lines = ["", "## Compatibility Facade Guard", ""]
    if not isinstance(payload, dict):
        lines.append("- Compatibility facade guard summary unavailable.")
        return lines
    lines.extend(
        [
            "- Mode: `report-only`",
            f"- Governed facades: `{payload.get('governed_facade_count', 0)}`",
            f"- Facade growth signals: `{payload.get('signal_count', 0)}`",
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
    signals = payload.get("signals", [])
    if isinstance(signals, list) and signals:
        lines.extend(["", "Facade growth signals:"])
        for signal in signals[:STRUCTURAL_FILE_BUDGET_TOP_LIMIT]:
            if not isinstance(signal, dict):
                continue
            location = signal.get("path", "unknown")
            if signal.get("line"):
                location = f"{location}:{signal['line']}"
            lines.append(
                "- `{message_key}` at `{location}`: {detail}; action: {owner_action}".format(
                    message_key=signal.get("message_key", "unknown"),
                    location=location,
                    detail=signal.get("detail", ""),
                    owner_action=signal.get("owner_action", ""),
                )
            )
    return lines
