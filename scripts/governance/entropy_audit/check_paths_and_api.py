"""The placeholder-path, Makefile, OpenAPI, Slurm-gateway and layer checks.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Holds the
retired-path token and tracked-path checks, the Makefile toolchain discipline
scan with its shell tokenizer, the OpenAPI/frontend type drift check, the
AST-based Slurm gateway route-leakage check, the agent-artifact ownership check
and the apps/api layer-inversion check."""

from __future__ import annotations

import ast
import fnmatch
import re
import shlex
from pathlib import Path

from scripts.governance.entropy_audit.archive_status import (
    _complete_archive_status_marker_ranges,
    _line_has_complete_archive_status_marker,
)
from scripts.governance.entropy_audit.constants import (
    COMPLETE_ARCHIVE_STATUS_ALLOWLIST_REASON,
    RETIRED_ACTIVE_TREE_PREFIXES,
)
from scripts.governance.entropy_audit.repo_files import (
    _artifact_fingerprint_pair_reason,
    _git_tracked_paths,
    _iter_python_files,
    _iter_text_files,
    _matching_lines,
    _module_for_path,
    _module_for_relative,
    _read_repo_text,
    _rel,
)
from scripts.governance.entropy_audit.schema import FindingSpec, _ArchiveStatusMarkerRange


def _check_placeholder_paths(root: Path) -> list[FindingSpec]:
    placeholder_patterns = RETIRED_ACTIVE_TREE_PREFIXES
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, [root / "docs", root / "openspec", root / "services", root / "infra"]):
        rel = _rel(root, path)
        text = _read_repo_text(root, path)
        lines = text.splitlines()
        archive_status_markers = _complete_archive_status_marker_ranges(lines)
        for line_no, line in _matching_lines(text, placeholder_patterns):
            token = next(token for token in placeholder_patterns if token in line)
            allowlist = _placeholder_path_allowlist_reason(
                rel,
                line,
                token,
                line_no=line_no,
                archive_status_markers=archive_status_markers,
            )
            findings.append(
                FindingSpec(
                    check_id="placeholder-path-token",
                    title="Placeholder or retired path token remains",
                    axis="semantics",
                    governance_face="legacy/dead-code",
                    role="shared_contract",
                    evidence_path=rel,
                    line=line_no,
                    severity="low" if allowlist else "medium",
                    priority="P3" if allowlist else "P2",
                    owner_area="docs/modules",
                    module=_module_for_path(root, path),
                    allowlist_reason=allowlist,
                    description=f"Reference to retired placeholder path `{token}` remains in active scan scope.",
                    recommendation=(
                        "Use canonical underscore package paths or mark the reference as historical inventory with "
                        "a narrow reason."
                    ),
                )
            )
    findings.extend(_check_tracked_retired_paths(root))
    return findings


def _check_tracked_retired_paths(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for tracked_path in _git_tracked_paths(root, RETIRED_ACTIVE_TREE_PREFIXES):
        retired_prefix = _retired_active_tree_prefix_for(tracked_path)
        if retired_prefix is None:
            continue
        findings.append(
            FindingSpec(
                check_id="placeholder-path-exists",
                title="Tracked retired path returned to active tree",
                axis="structure",
                governance_face="legacy/dead-code",
                role="shared_contract",
                evidence_path=tracked_path,
                severity="medium",
                priority="P2",
                owner_area="repo structure",
                module=_module_for_relative(tracked_path),
                description=(
                    f"Tracked file `{tracked_path}` returned under retired active-tree prefix "
                    f"`{retired_prefix}`."
                ),
                recommendation=(
                    "Remove the tracked retired path or move the implementation to the canonical active "
                    "underscore/package path."
                ),
            )
        )
    return findings


def _retired_active_tree_prefix_for(relative_path: str) -> str | None:
    for prefix in RETIRED_ACTIVE_TREE_PREFIXES:
        if relative_path == prefix or relative_path.startswith(f"{prefix}/"):
            return prefix
    return None


def _check_makefile_toolchain(root: Path) -> list[FindingSpec]:
    makefile = root / "Makefile"
    if not makefile.exists():
        return []
    findings: list[FindingSpec] = []
    for line_no, line in enumerate(_read_repo_text(root, makefile).splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _makefile_line_has_unmanaged_python_tool(stripped):
            findings.append(
                FindingSpec(
                    check_id="makefile-toolchain-discipline",
                    title="Makefile command bypasses repository-managed Python environment",
                    axis="protocol",
                    governance_face="entropy automation/control",
                    role="shared_contract",
                    evidence_path="Makefile",
                    line=line_no,
                    severity="medium",
                    priority="P2",
                    owner_area="developer tooling",
                    module="Makefile",
                    description="Makefile line invokes system Python tooling instead of `uv run`.",
                    recommendation="Route Python, pytest, and ruff commands through `uv run` from the repository root.",
                )
            )
    return findings


def _check_openapi_frontend_type_drift(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    openapi = root / "openapi" / "nhms.v1.yaml"
    frontend_types = root / "apps" / "frontend" / "src" / "api" / "types.ts"
    drift_test = root / "tests" / "test_openapi_drift.py"
    if not openapi.exists() or not frontend_types.exists():
        return [
            FindingSpec(
                check_id="openapi-frontend-types-presence",
                title="OpenAPI or generated frontend types are missing",
                axis="protocol",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path="openapi/nhms.v1.yaml",
                severity="high",
                priority="P1",
                owner_area="api contract",
                module="openapi",
                description="Could not find both static OpenAPI spec and generated frontend type file.",
                recommendation=(
                    "Restore the OpenAPI spec and generated frontend type artifact before enabling CI drift checks."
                ),
            )
        ]
    if drift_test.exists():
        findings.append(
            FindingSpec(
                check_id="openapi-frontend-types-delegated",
                title="OpenAPI/frontend type drift delegated to existing contract checks",
                axis="protocol",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path="tests/test_openapi_drift.py",
                severity="low",
                priority="P3",
                owner_area="api contract",
                module="openapi",
                allowlist_reason="existing OpenAPI drift tests are the enforced contract oracle",
                description=(
                    "Static OpenAPI and generated frontend types are present; this report records delegation to the "
                    "existing contract-drift test lane."
                ),
                recommendation="Keep running `tests/test_openapi_drift.py` and frontend API type generation checks.",
            )
        )
    fingerprint_reason, fingerprint_available = _artifact_fingerprint_pair_reason(root, openapi, frontend_types)
    findings.append(
        FindingSpec(
            check_id="openapi-frontend-types-signal",
            title="OpenAPI/frontend type artifacts have comparable fingerprints",
            axis="control",
            governance_face="entropy automation/control",
            role="shared_contract",
            evidence_path="apps/frontend/src/api/types.ts",
            severity="low",
            priority="P3",
            owner_area="api contract",
            module="openapi",
            allowlist_reason=fingerprint_reason,
            description=(
                "Report-only drift signal records both artifacts without asserting byte-level generation parity."
                if fingerprint_available
                else (
                    "Report-only drift signal records artifact presence but skipped unsafe or oversized "
                    "fingerprinting."
                )
            ),
            recommendation=(
                "Use the existing OpenAPI drift test and frontend `check:api-types` command as hard oracles."
            ),
        )
    )
    return findings


def _makefile_line_has_unmanaged_python_tool(line: str) -> bool:
    for segment in _shell_command_segments(line):
        command_index, command_name = _shell_command_name(segment)
        if command_name in {"python", "pytest", "ruff"}:
            return True
        if command_name == "pip" and command_index + 1 < len(segment) and segment[command_index + 1] == "install":
            return True
    return False


def _shell_command_segments(line: str) -> list[list[str]]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return [[line]]

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in ";&|" for char in token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _shell_command_name(tokens: list[str]) -> tuple[int, str]:
    index = 0
    while index < len(tokens):
        token = tokens[index].lstrip("@-+") if index == 0 else tokens[index]
        if not token or _is_shell_assignment(token):
            index += 1
            continue
        if token == "env":
            index += 1
            while index < len(tokens) and (tokens[index].startswith("-") or _is_shell_assignment(tokens[index])):
                index += 1
            continue
        return index, token
    return len(tokens), ""


def _is_shell_assignment(token: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None


def _check_slurm_gateway_route_leakage(root: Path) -> list[FindingSpec]:
    path = root / "services" / "slurm_gateway" / "app.py"
    if not path.exists():
        return []
    findings: list[FindingSpec] = []
    text = _read_repo_text(root, path)
    for line_no, reason in _slurm_gateway_leakage_lines(text):
        findings.append(
            FindingSpec(
                check_id="slurm-gateway-route-leakage",
                title="Standalone Slurm gateway references business route surface",
                axis="structure",
                governance_face="role boundary",
                role="slurm_gateway",
                evidence_path=_rel(root, path),
                line=line_no,
                severity="high",
                priority="P1",
                owner_area="slurm gateway",
                module=_module_for_path(root, path),
                description=(
                    "Standalone Slurm gateway source contains a token associated with "
                    f"business/static routes: {reason}."
                ),
                recommendation=(
                    "Keep the standalone gateway limited to `/health` and `/api/v1/slurm/*`; expose business "
                    "routes only from `apps.api.main`."
                ),
            )
        )
    return findings


def _slurm_gateway_leakage_lines(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    findings: dict[int, str] = {}
    docstring_lines = _docstring_line_numbers(tree)
    for node in ast.walk(tree):
        line_no = getattr(node, "lineno", None)
        if not isinstance(line_no, int) or line_no in docstring_lines:
            continue
        if isinstance(node, ast.Call):
            reason = _slurm_gateway_forbidden_call_reason(node)
            if reason:
                findings.setdefault(line_no, reason)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            reason = _slurm_gateway_forbidden_path_reason(node.value)
            if reason:
                findings.setdefault(line_no, reason)
    return sorted(findings.items())


def _docstring_line_numbers(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        start = getattr(first, "lineno", None)
        end = getattr(first, "end_lineno", start)
        if isinstance(start, int) and isinstance(end, int):
            lines.update(range(start, end + 1))
    return lines


def _slurm_gateway_forbidden_call_reason(node: ast.Call) -> str | None:
    func_name = _call_name(node.func)
    if func_name.endswith(".include_router"):
        for arg in node.args:
            arg_name = _name_for_ast(arg)
            if _slurm_gateway_router_name_is_forbidden(arg_name):
                return f"business router registration `{arg_name}`"
    if func_name.endswith(".mount"):
        return "static/frontend mount call"
    if func_name == "StaticFiles" or func_name.endswith(".StaticFiles"):
        return "StaticFiles registration"
    if _call_is_route_decorator(node):
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                reason = _slurm_gateway_forbidden_path_reason(arg.value)
                if reason:
                    return f"direct route decorator for {reason}"
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _name_for_ast(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name_for_ast(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _slurm_gateway_router_name_is_forbidden(name: str) -> bool:
    normalized = name.lower()
    return any(token in normalized for token in ("forecast", "model", "pipeline", "static", "frontend"))


def _call_is_route_decorator(node: ast.Call) -> bool:
    func_name = _call_name(node.func)
    return any(
        func_name.endswith(f".{method}")
        for method in ("get", "post", "put", "delete", "patch", "options", "head", "api_route")
    )


def _slurm_gateway_forbidden_path_reason(value: str) -> str | None:
    if not value.startswith("/"):
        return None
    normalized = value.rstrip("/") or "/"
    segments = [segment for segment in re.split(r"[/?#]+", normalized.strip("/")) if segment]
    forbidden_segment_tokens = ("forecast", "model", "pipeline", "static", "assets", "frontend")
    for segment in segments:
        if segment == "slurm":
            continue
        if any(token in segment.lower() for token in forbidden_segment_tokens):
            return f"path literal `{value}`"
    forbidden_prefixes = (
        "/api/v1/forecast",
        "/api/v1/models",
        "/api/v1/model",
        "/api/v1/pipeline",
        "/forecast",
        "/models",
        "/model",
        "/pipeline",
        "/static",
        "/assets",
        "/frontend",
    )
    for prefix in forbidden_prefixes:
        if normalized == prefix or normalized.startswith(f"{prefix}/") or normalized.startswith(f"{prefix}{{"):
            return f"path literal `{value}`"
    return None


def _check_agent_artifact_ownership(root: Path) -> list[FindingSpec]:
    doc_status = root / "docs" / "governance" / "DOC_STATUS.md"
    findings: list[FindingSpec] = []
    required_terms = (
        ".agents/skills/**",
        ".codex/tmp/",
        ".codex/cache/",
        ".codex/evidence/",
        "apps/frontend/artifacts/**",
        "Root `artifacts/`",
        ".dockerignore",
    )
    text = _read_repo_text(root, doc_status) if doc_status.exists() else ""
    for term in required_terms:
        if term not in text:
            findings.append(
                FindingSpec(
                    check_id="agent-artifact-ownership-policy",
                    title="DOC_STATUS ownership policy misses governed artifact term",
                    axis="context",
                    governance_face="docs alignment",
                    role="shared_contract",
                    evidence_path=_rel(root, doc_status),
                    severity="medium",
                    priority="P2",
                    owner_area="governance docs",
                    module="docs/governance",
                    description=f"`DOC_STATUS.md` does not mention expected ownership term `{term}`.",
                    recommendation=(
                        "Update the ownership policy before relying on generated agent/artifact path handling."
                    ),
                )
            )
    ignore_text = _read_repo_text(root, root / ".gitignore")
    dockerignore_text = _read_repo_text(root, root / ".dockerignore")
    ignore_expectations = {
        ".codex/": ignore_text,
        "artifacts/": ignore_text,
        "apps/frontend/artifacts/": ignore_text,
        ".agents": dockerignore_text,
        ".codex": dockerignore_text,
        "apps/frontend/artifacts": dockerignore_text,
    }
    for token, haystack in ignore_expectations.items():
        if token not in haystack:
            findings.append(
                FindingSpec(
                    check_id="agent-artifact-ignore-policy",
                    title="Generated agent/artifact path is not covered by ignore policy",
                    axis="control",
                    governance_face="entropy automation/control",
                    role="shared_contract",
                    evidence_path=(
                        ".gitignore" if "artifacts" in token or token.startswith(".codex") else ".dockerignore"
                    ),
                    severity="medium",
                    priority="P2",
                    owner_area="repo hygiene",
                    module="repo policy",
                    description=f"Expected ignore token `{token}` was not found in the relevant ignore file.",
                    recommendation="Align ignore files with `docs/governance/DOC_STATUS.md` ownership policy.",
                )
            )
    tracked = _git_tracked_paths(root)
    unexpected = [
        path
        for path in tracked
        if path.startswith((".codex/tmp/", ".codex/cache/", ".codex/evidence/", "artifacts/"))
        or (
            path.startswith("apps/frontend/artifacts/")
            and not fnmatch.fnmatch(path, "apps/frontend/artifacts/m11-*.png")
        )
    ]
    for path in unexpected:
        findings.append(
            FindingSpec(
                check_id="tracked-generated-artifact",
                title="Generated artifact path appears tracked",
                axis="control",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path=path,
                severity="medium",
                priority="P2",
                owner_area="repo hygiene",
                module=_module_for_relative(path),
                description="A generated agent/artifact path conflicts with the documented ownership policy.",
                recommendation=(
                    "Remove the generated artifact from tracking or promote it with explicit issue-scoped review."
                ),
            )
        )
    return findings


def _check_apps_api_layer_inversion(root: Path) -> list[FindingSpec]:
    scan_roots = [root / "packages", root / "services", root / "workers"]
    scan_files = [
        root / "services" / "slurm_gateway" / "models.py",
        root / "services" / "production_closure" / "ops_validation.py",
    ]
    findings: list[FindingSpec] = []
    for path in sorted({*list(_iter_python_files(root, scan_roots)), *[file for file in scan_files if file.exists()]}):
        rel = _rel(root, path)
        if rel.startswith("apps/api/"):
            continue
        try:
            tree = ast.parse(_read_repo_text(root, path), filename=rel)
        except SyntaxError as exc:
            findings.append(
                FindingSpec(
                    check_id="apps-api-layer-parse-error",
                    title="Could not parse Python file for apps.api layer inversion scan",
                    axis="structure",
                    governance_face="role boundary",
                    role="shared_contract",
                    evidence_path=rel,
                    line=exc.lineno,
                    severity="low",
                    priority="P3",
                    owner_area="layering",
                    module=_module_for_path(root, path),
                    description="The AST import scan skipped a file because it could not be parsed.",
                    recommendation="Fix parse errors or exclude generated files from the source tree.",
                )
            )
            continue
        for module in sorted(_normalized_apps_api_import_modules(tree)):
            findings.append(
                FindingSpec(
                    check_id="apps-api-layer-inversion",
                    title="Non-API layer imports apps.api",
                    axis="structure",
                    governance_face="role boundary",
                    role="shared_contract",
                    evidence_path=rel,
                    severity="high",
                    priority="P1",
                    owner_area="layering",
                    module=_module_for_path(root, path),
                    description=f"Shared/service/worker source imports `{module}` from the API layer.",
                    recommendation=(
                        "Move shared contracts to packages/common or inject API-only behavior from apps/api."
                    ),
                )
            )
    return findings


def _normalized_apps_api_import_modules(tree: ast.AST) -> frozenset[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "apps.api" or alias.name.startswith("apps.api."):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                continue
            if module == "apps":
                for alias in node.names:
                    if alias.name == "api":
                        modules.add("apps.api")
                    elif alias.name == "*":
                        modules.add("apps.api.*")
            elif module == "apps.api":
                for alias in node.names:
                    modules.add("apps.api.*" if alias.name == "*" else f"apps.api.{alias.name}")
            elif module.startswith("apps.api."):
                modules.add(f"{module}.*" if any(alias.name == "*" for alias in node.names) else module)
    return frozenset(modules)


def _placeholder_path_allowlist_reason(
    relative_path: str,
    line: str = "",
    token: str = "",
    *,
    line_no: int | None = None,
    archive_status_markers: tuple[_ArchiveStatusMarkerRange, ...] = (),
) -> str | None:
    if relative_path == "docs/governance/LEGACY_DEAD_CODE_INVENTORY.md":
        return "governance inventory documents retired placeholder paths"
    if line_no is not None and _line_has_complete_archive_status_marker(line_no, archive_status_markers):
        return COMPLETE_ARCHIVE_STATUS_ALLOWLIST_REASON
    if relative_path.startswith("openspec/changes/governance-2-legacy-dead-code-retirement/"):
        return "governed completed OpenSpec evidence documents retired placeholder paths"
    if relative_path.startswith("openspec/changes/governance-5-e1-entropy-baseline-burndown/"):
        return "governed Governance-5 E1 fixture evidence documents retired placeholder paths"
    if (
        relative_path == "services/slurm_gateway/config.py"
        and token == "workers/sbatch_templates"
        and "retired" in line.lower()
    ):
        return "source comment documents retired Slurm template path"
    return None


def _path_has_label(relative_path: str, labels: set[str]) -> bool:
    parts = re.split(r"[^a-z0-9]+", relative_path.lower())
    return any(part in labels for part in parts)
