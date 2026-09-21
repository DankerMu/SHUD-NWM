"""The role-env boundary check plus the QHH, paused-workflow and e2e-mock checks.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Grouped by
shape rather than by domain: each of these four families is a token or pattern
scan over a small fixed root set, with a narrow path/context classifier and no
shared state beyond the scan helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Literal

from scripts.governance.entropy_audit.check_paths_and_api import _path_has_label
from scripts.governance.entropy_audit.constants import BROAD_E2E_API_MOCK_PATTERN
from scripts.governance.entropy_audit.repo_files import (
    _iter_text_files,
    _matching_lines,
    _module_for_path,
    _read_repo_text,
    _rel,
)
from scripts.governance.entropy_audit.schema import FindingSpec


def _check_display_env_boundaries(root: Path) -> list[FindingSpec]:
    compute_only_tokens = (
        "WORKSPACE_ROOT",
        "SHUD_EXECUTABLE",
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_PARTITION",
        "SLURM_ACCOUNT",
    )
    findings: list[FindingSpec] = []
    for path in _infra_role_env_scan_files(root):
        text = _read_repo_text(root, path)
        lines = text.splitlines()
        for line_no, line in _matching_lines(text, compute_only_tokens):
            if line.lstrip().startswith("#"):
                continue
            if not _is_display_boundary_context(root, path, lines, line_no):
                continue
            token = next(token for token in compute_only_tokens if token in line)
            findings.append(
                FindingSpec(
                    check_id="role-env-boundary",
                    title="Display configuration references compute-only environment",
                    axis="protocol",
                    governance_face="role boundary",
                    role="display_readonly",
                    evidence_path=_rel(root, path),
                    line=line_no,
                    severity="high",
                    priority="P1",
                    owner_area="infra/runtime",
                    module=_module_for_path(root, path),
                    description=(
                        f"Display-facing env or compose file references `{token}`, which is part of the "
                        "compute/control-plane boundary inventory."
                    ),
                    recommendation=(
                        "Keep display config limited to read-only runtime identity and public display inputs; "
                        "move compute-only values to compute env/compose files."
                    ),
                )
            )
    return findings


def _infra_role_env_scan_files(root: Path) -> Iterable[Path]:
    roots = [root / "infra", root / "apps" / "frontend"]
    for path in _iter_text_files(root, roots):
        if (
            path.name == ".env"
            or path.name.startswith(".env.")
            or path.suffix in {".env", ".example", ".yaml", ".yml"}
        ):
            yield path


def _is_display_boundary_context(root: Path, path: Path, lines: list[str], line_no: int) -> bool:
    rel = _rel(root, path).lower()
    env_like = path.name == ".env" or path.name.startswith(".env.") or path.suffix in {".env", ".example"}
    if env_like:
        if _path_has_display_hint(rel) and not _path_has_compute_hint(rel):
            return True
        return _line_has_display_env_section(lines, line_no)
    if path.suffix in {".yaml", ".yml"}:
        if _path_has_display_hint(rel) and not _path_has_compute_hint(rel):
            return True
        return any(start <= line_no <= end for start, end in _display_yaml_line_ranges(lines))
    if _path_has_display_hint(rel) and not _path_has_compute_hint(rel):
        return True
    return False


def _path_has_display_hint(relative: str) -> bool:
    parts = re.split(r"[/_.-]+", relative)
    return any(part in {"display", "frontend", "webui"} for part in parts)


def _path_has_compute_hint(relative: str) -> bool:
    parts = re.split(r"[/_.-]+", relative)
    return any(part in {"api", "backend", "compute", "gateway", "slurm", "worker"} for part in parts)


def _display_yaml_line_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    services_indent: int | None = None
    service_indent: int | None = None
    current_name: str | None = None
    current_start: int | None = None
    current_lines: list[str] = []

    def close_current(end_line: int) -> None:
        nonlocal current_name, current_start, current_lines
        if current_name is not None and current_start is not None:
            if _yaml_service_looks_display_facing(current_name, current_lines):
                ranges.append((current_start, end_line))
        current_name = None
        current_start = None
        current_lines = []

    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            if current_name is not None:
                current_lines.append(line)
            continue
        if re.match(r"services\s*:\s*(?:#.*)?$", stripped):
            close_current(index - 1)
            services_indent = indent
            service_indent = None
            continue
        if services_indent is None:
            continue
        if indent <= services_indent:
            close_current(index - 1)
            services_indent = None
            service_indent = None
            continue

        key_match = re.match(r"['\"]?([A-Za-z0-9_.-]+)['\"]?\s*:\s*(?:#.*)?$", stripped)
        if key_match:
            if service_indent is None:
                service_indent = indent
            if indent == service_indent:
                close_current(index - 1)
                current_name = key_match.group(1)
                current_start = index
                current_lines = [line]
                continue
        if current_name is not None:
            current_lines.append(line)

    close_current(len(lines))
    return ranges


def _yaml_service_looks_display_facing(service_name: str, service_lines: list[str]) -> bool:
    name = service_name.lower()
    service_text = "\n".join(service_lines).lower()
    if any(hint in name for hint in ("display", "frontend", "webui")):
        return True
    if any(hint in name for hint in ("compute", "slurm", "gateway", "api", "worker", "db", "postgres", "redis")):
        return False
    return any(
        hint in service_text
        for hint in (
            "apps/frontend",
            "frontend",
            "display",
            "nginx",
            "caddy",
            "vite",
            "web-ui",
            "webui",
        )
    )


def _line_has_display_env_section(lines: list[str], line_no: int) -> bool:
    for index in range(line_no - 2, max(-1, line_no - 12), -1):
        stripped = lines[index].strip().lower()
        if not stripped:
            break
        if "compute" in stripped or "slurm" in stripped or "backend" in stripped:
            return False
        if "display" in stripped or "frontend" in stripped or "web-ui" in stripped or "webui" in stripped:
            return True
    return False


def _check_qhh_diagnostic_tokens(root: Path) -> list[FindingSpec]:
    tokens = (
        "DIAGNOSTIC-ONLY",
        "run_qhh_cycle",
        "run_qhh_continuous",
        "run_qhh_backend_smoke",
        "create_qhh_shud_manifest",
        "scripts/run_qhh_cycle.sh",
        "scripts/run_qhh_continuous.py",
        "scripts/run_qhh_backend_smoke.sh",
        "scripts/create_qhh_shud_manifest.py",
    )
    production_roots = [
        root / "services" / "orchestrator",
        root / "services" / "production_closure",
        root / "workers",
    ]
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, production_roots):
        text = _read_repo_text(root, path)
        for line_no, line in _matching_lines(text, tokens):
            token = next(token for token in tokens if token in line)
            findings.append(
                FindingSpec(
                    check_id="qhh-diagnostic-token",
                    title="Production path references QHH diagnostic token",
                    axis="behavior",
                    governance_face="legacy/dead-code",
                    role="compute_control",
                    evidence_path=_rel(root, path),
                    line=line_no,
                    severity="high" if _rel(root, path).startswith("services/orchestrator/") else "medium",
                    priority="P1",
                    owner_area="production scheduler",
                    module=_module_for_path(root, path),
                    description=f"Production-adjacent source references diagnostic token `{token}`.",
                    recommendation=(
                        "Keep QHH diagnostic runners in scripts/diagnostic evidence lanes; production scheduling "
                        "should use the generic orchestrator and standalone Slurm gateway path."
                    ),
                )
            )
    return findings


def _check_paused_workflows(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, [root / ".github" / "workflows"]):
        text = _read_repo_text(root, path)
        for line_no, line in _matching_lines(text, ("&& false",)):
            findings.append(
                FindingSpec(
                    check_id="paused-workflow-condition",
                    title="Workflow condition is paused with hidden false branch",
                    axis="control",
                    governance_face="entropy automation/control",
                    role="shared_contract",
                    evidence_path=_rel(root, path),
                    line=line_no,
                    severity="medium",
                    priority="P2",
                    owner_area="ci",
                    module=_module_for_path(root, path),
                    description="A workflow line contains `&& false`, which can hide disabled validation.",
                    recommendation=(
                        "Replace hidden false conditions with explicit workflow_dispatch, path filters, or a "
                        "documented non-blocking job state."
                    ),
                )
            )
    return findings


def _check_broad_e2e_mocks(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, [root / "apps" / "frontend"]):
        rel = _rel(root, path)
        if _path_is_frontend_generated_artifact(rel):
            continue
        if not ("/e2e/" in rel or rel.endswith(".spec.ts") or _path_has_label(rel, {"live"})):
            continue
        text = _read_repo_text(root, path)
        for line_no in _broad_e2e_mock_line_numbers(text):
            classification = _classify_broad_e2e_mock_path(rel)
            findings.append(
                FindingSpec(
                    check_id="broad-e2e-api-mock",
                    title=classification["title"],
                    axis="behavior",
                    governance_face="docs alignment",
                    role="display_readonly",
                    evidence_path=rel,
                    line=line_no,
                    severity=classification["severity"],
                    priority=classification["priority"],
                    owner_area="frontend e2e",
                    module=_module_for_path(root, path),
                    allowlist_reason=classification["allowlist_reason"],
                    description="Broad `page.route('**/api/v1/**')` mocks can be mistaken for live display evidence.",
                    recommendation=(
                        "Keep broad API mocks in deterministic mocked regressions and label live evidence specs so "
                        "they use real API calls or narrowly scoped mocks."
                    ),
                )
            )
    return findings


def _broad_e2e_mock_line_numbers(text: str) -> list[int]:
    return [text.count("\n", 0, match.start("glob")) + 1 for match in BROAD_E2E_API_MOCK_PATTERN.finditer(text)]


def _path_is_frontend_generated_artifact(relative: str) -> bool:
    return relative.startswith("apps/frontend/artifacts/")


def _classify_broad_e2e_mock_path(
    relative: str,
) -> dict[str, Literal["medium", "high", "P1", "P2"] | str | None]:
    if _path_has_label(relative, {"live"}):
        return {
            "title": "Live-labeled frontend E2E path uses broad API mock",
            "severity": "high",
            "priority": "P1",
            "allowlist_reason": None,
        }
    if _path_has_label(relative, {"deterministic", "fixture", "fixtures", "mock", "mocked", "preview", "visual"}):
        return {
            "title": "Deterministic frontend E2E path uses broad API mock",
            "severity": "medium",
            "priority": "P2",
            "allowlist_reason": "deterministic mocked/preview/visual e2e broad mock",
        }
    return {
        "title": "Frontend E2E path uses broad API mock",
        "severity": "medium",
        "priority": "P2",
        "allowlist_reason": None,
    }
