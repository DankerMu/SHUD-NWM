#!/usr/bin/env python3
"""Explicit maintainer-only entropy baseline writer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    from scripts.governance import audit_repo_entropy
except ModuleNotFoundError:  # pragma: no cover - exercised by direct script execution.
    import audit_repo_entropy  # type: ignore[no-redef]

BASELINE_DIR_NAME = ".entropy-baseline"
LATEST_BASELINE_NAME = "latest.json"
ARCHIVE_COLLISION_LIMIT = 1000
TEMP_LATEST_NAME = ".latest.json.tmp"
MAX_ARCHIVED_LATEST_BYTES = 2_097_152
COPY_CHUNK_BYTES = 1_048_576
MAX_BASELINE_INVENTORY_FILES = 50_000
V1_SOURCE_MODULE_ROOTS = frozenset(
    {
        "apps",
        "config",
        "db",
        "infra",
        "packages",
        "schemas",
        "scripts",
        "services",
        "workers",
    }
)
V1_FRONTEND_EXCLUDED_PARTS = frozenset({"artifacts", "e2e"})
V1_SUMMARY_EXCLUDED_ROOTS = frozenset({".agents", ".codex", "docs", "openapi"})
V1_SUMMARY_ROOT_DOCUMENT_SUFFIXES = frozenset({".md", ".rst", ".txt"})

STRUCTURAL_HOTSPOT_MODULES: dict[str, dict[str, object]] = {
    "services/orchestrator": {
        "priority": "P1",
        "structure": {
            "score": "high",
            "hotspots": [
                "services/orchestrator/scheduler.py",
                "services/orchestrator/chain.py",
            ],
        },
        "behavior": {"score": "high"},
        "context": {"score": "medium"},
        "protocol": {"score": "medium"},
        "control": {"score": "medium"},
    },
    "services/production_closure": {
        "priority": "P1",
        "structure": {
            "score": "high",
            "hotspots": [
                "services/production_closure/two_node_e2e_evidence.py",
                "services/production_closure/readiness_validation.py",
            ],
        },
        "behavior": {"score": "medium"},
        "context": {"score": "medium"},
        "protocol": {"score": "medium"},
        "control": {"score": "medium"},
    },
}

STRUCTURAL_HIGH_SPREAD_PATTERNS = (
    {
        "description": "orchestrator mixed responsibilities in scheduler.py and chain.py",
        "occurrences": 2,
        "files": [
            "services/orchestrator/scheduler.py",
            "services/orchestrator/chain.py",
        ],
        "axis": "structure,behavior",
        "spread_risk": "high",
        "top_priority": "P1",
        "top_severity": "high",
    },
)

STRUCTURAL_CLEANUP_PRIORITIES = (
    {
        "target": "Stage large decomposition of services/orchestrator/scheduler.py and services/orchestrator/chain.py",
        "impact": "high",
        "effort": "high",
        "axis": "structure/behavior",
    },
)


class BaselineWriteError(RuntimeError):
    """Raised for stable CLI failures during baseline writes."""


@dataclass(frozen=True)
class BaselineWriteResult:
    baseline_path: Path
    archive_path: Path | None
    baseline_bytes: bytes


@dataclass(frozen=True)
class BaselineFileInventory:
    relative_paths: tuple[str, ...]
    file_fingerprints: tuple[tuple[str, int, int, int], ...]
    total_source_files: int
    v1_summary_source_files: int
    total_test_files: int
    total_instruction_files: int
    module_file_counts: dict[str, int]


@dataclass(frozen=True)
class SnapshotIdentity:
    repo: str
    branch: str
    commit: str
    report_file_fingerprints: tuple[tuple[str, int, int, int], ...]
    inventory_paths: tuple[str, ...]
    inventory_file_fingerprints: tuple[tuple[str, int, int, int], ...]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the current repository entropy report to .entropy-baseline/latest.json."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root to audit. Defaults to the root discovered from the current directory.",
    )
    args = parser.parse_args(argv)

    try:
        root = audit_repo_entropy.repo_root_from(args.repo_root)
        result = write_entropy_baseline(root)
    except BaselineWriteError as exc:
        print(f"ERROR: entropy baseline write failed: {exc}", file=sys.stderr)
        return 1

    payload = {
        "baseline_path": _repo_relative_or_absolute(root, result.baseline_path),
        "archive_path": _repo_relative_or_absolute(root, result.archive_path) if result.archive_path else None,
        "baseline_written": True,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def write_entropy_baseline(repo_root: Path, *, now: datetime | None = None) -> BaselineWriteResult:
    root = audit_repo_entropy.repo_root_from(repo_root)
    timestamp = now or datetime.now(UTC)
    latest_path = root / BASELINE_DIR_NAME / LATEST_BASELINE_NAME
    if _path_exists_or_is_symlink(latest_path):
        _validate_existing_latest_for_archive(latest_path)

    before_inventory = _baseline_file_inventory(root)
    before_snapshot = _snapshot_identity(root, file_inventory=before_inventory)
    report = audit_repo_entropy.build_report(root, mode="report")
    file_inventory = _baseline_file_inventory(root)
    baseline = build_baseline_snapshot(
        root,
        report,
        timestamp=timestamp,
        file_inventory=file_inventory,
        snapshot=before_snapshot,
    )
    after_snapshot = _snapshot_identity(root, file_inventory=file_inventory)
    if after_snapshot != before_snapshot:
        raise BaselineWriteError("repository snapshot changed during baseline generation")
    baseline_bytes = (json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    baseline_dir = _prepare_baseline_dir(root)
    archive_path: Path | None = None

    _write_temporary_latest(baseline_dir, baseline_bytes)
    if _path_exists_or_is_symlink(latest_path):
        try:
            archive_path = _archive_existing_latest(baseline_dir, latest_path, timestamp=timestamp)
        except BaselineWriteError:
            _unlink_file_if_present(baseline_dir / TEMP_LATEST_NAME)
            raise
    try:
        _replace_latest_with_temporary(baseline_dir, latest_path)
    except BaselineWriteError:
        if archive_path is not None:
            _unlink_file_if_present(archive_path)
        raise

    return BaselineWriteResult(
        baseline_path=latest_path,
        archive_path=archive_path,
        baseline_bytes=baseline_bytes,
    )


def build_baseline_snapshot(
    repo_root: Path,
    report: dict[str, object],
    *,
    timestamp: datetime | None = None,
    file_inventory: BaselineFileInventory | None = None,
    snapshot: SnapshotIdentity | None = None,
) -> dict[str, object]:
    generated_at = timestamp or datetime.now(UTC)
    metadata = _dict_value(report, "metadata")
    findings = _list_value(report, "findings")
    module_heatmap = _list_value(report, "module_heatmap")
    high_spread_patterns = _list_value(report, "high_spread_patterns")
    file_inventory = file_inventory or _baseline_file_inventory(repo_root)
    snapshot = snapshot or _snapshot_identity(repo_root, file_inventory=file_inventory)

    return {
        "version": 1,
        "timestamp": _baseline_timestamp(generated_at),
        "repo": snapshot.repo,
        "branch": snapshot.branch,
        "commit": snapshot.commit,
        "summary": _baseline_summary(metadata, module_heatmap, file_inventory),
        "metadata": {
            "source_report_schema": metadata.get("schema_version"),
            "source_report_generated_at": metadata.get("generated_at"),
            "source_report_mode": metadata.get("mode"),
            "summary_counts": metadata.get("summary_counts", {}),
            "skipped_path_families": metadata.get("skipped_path_families", []),
        },
        "modules": _baseline_modules(module_heatmap, file_inventory),
        "high_spread_patterns": _baseline_high_spread_patterns(high_spread_patterns, findings),
        "cleanup_priorities": _baseline_cleanup_priorities(findings),
    }


def _prepare_baseline_dir(root: Path) -> Path:
    baseline_dir = root / BASELINE_DIR_NAME
    if _path_exists_or_is_symlink(baseline_dir):
        try:
            file_stat = baseline_dir.lstat()
        except OSError as exc:
            raise BaselineWriteError("unable to inspect .entropy-baseline directory") from exc
        if baseline_dir.is_symlink() or not baseline_dir.is_dir():
            raise BaselineWriteError(".entropy-baseline exists but is not a regular directory")
        if file_stat.st_mode == 0:
            raise BaselineWriteError("unable to access .entropy-baseline directory")
        return baseline_dir
    try:
        baseline_dir.mkdir(mode=0o755)
    except OSError as exc:
        raise BaselineWriteError("unable to create .entropy-baseline directory") from exc
    return baseline_dir


def _archive_existing_latest(
    baseline_dir: Path,
    latest_path: Path,
    *,
    timestamp: datetime,
) -> Path:
    _validate_existing_latest_for_archive(latest_path)
    archive_path = _next_archive_path(baseline_dir, timestamp)
    try:
        _bounded_copy_file(latest_path, archive_path)
    except OSError as exc:
        _unlink_file_if_present(archive_path)
        raise BaselineWriteError("unable to archive existing latest baseline") from exc
    return archive_path


def _validate_existing_latest_for_archive(latest_path: Path) -> None:
    try:
        latest_stat = latest_path.lstat()
    except OSError as exc:
        raise BaselineWriteError("unable to inspect existing latest baseline") from exc
    if latest_path.is_symlink() or not latest_path.is_file():
        raise BaselineWriteError("existing latest baseline is not a regular file")
    if latest_stat.st_size < 0:
        raise BaselineWriteError("existing latest baseline has invalid size")
    if latest_stat.st_size > MAX_ARCHIVED_LATEST_BYTES:
        raise BaselineWriteError("existing latest baseline exceeds archive size limit")


def _bounded_copy_file(source_path: Path, destination_path: Path) -> None:
    copied = 0
    try:
        with source_path.open("rb") as source, destination_path.open("xb") as destination:
            while True:
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_ARCHIVED_LATEST_BYTES:
                    raise OSError("existing latest baseline exceeds archive size limit")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError as exc:
        raise exc


def _write_temporary_latest(baseline_dir: Path, baseline_bytes: bytes) -> None:
    temp_path = baseline_dir / TEMP_LATEST_NAME
    created_temp = False
    try:
        with temp_path.open("xb") as handle:
            created_temp = True
            handle.write(baseline_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if created_temp:
            _unlink_file_if_present(temp_path)
        raise BaselineWriteError("unable to write temporary latest baseline") from exc


def _replace_latest_with_temporary(baseline_dir: Path, latest_path: Path) -> None:
    temp_path = baseline_dir / TEMP_LATEST_NAME
    try:
        os.replace(temp_path, latest_path)
    except OSError as exc:
        _unlink_file_if_present(temp_path)
        raise BaselineWriteError("unable to replace latest baseline") from exc
    _fsync_directory_best_effort(baseline_dir)


def _next_archive_path(baseline_dir: Path, timestamp: datetime) -> Path:
    stem = _archive_timestamp(timestamp)
    for index in range(ARCHIVE_COLLISION_LIMIT):
        suffix = "" if index == 0 else f"-{index:02d}"
        candidate = baseline_dir / f"{stem}{suffix}.json"
        if not _path_exists_or_is_symlink(candidate):
            return candidate
    raise BaselineWriteError("unable to allocate timestamped baseline archive path")


def _baseline_summary(
    metadata: dict[str, object],
    module_heatmap: list[object],
    file_inventory: BaselineFileInventory,
) -> dict[str, object]:
    modules = _baseline_modules(module_heatmap, file_inventory)
    return {
        "total_source_files": file_inventory.v1_summary_source_files,
        "total_test_files": file_inventory.total_test_files,
        "total_instruction_files": file_inventory.total_instruction_files,
        "total_modules": len(modules),
        "modules_with_high_entropy": sum(
            1 for row in modules.values() if _module_row_has_high_entropy(row)
        ),
        "overall_trend": "baseline",
        "governance_finding_count": metadata.get("finding_count", 0),
        "budget_counted_count": metadata.get("budget_counted_count", 0),
        "gate_eligible_count": metadata.get("gate_eligible_count", 0),
        "check_family_count": metadata.get("check_family_count", 0),
    }


def _baseline_modules(
    module_heatmap: list[object],
    file_inventory: BaselineFileInventory,
) -> dict[str, object]:
    modules: dict[str, object] = {}
    for item in module_heatmap:
        if not isinstance(item, dict):
            continue
        module = str(item.get("module", "unknown"))
        modules[module] = {
            "file_count": file_inventory.module_file_counts.get(module, 0),
            "finding_count": item.get("finding_count", 0),
            "priority": item.get("priority", "P3"),
            "structure": {"score": item.get("structure", "low")},
            "semantics": {"score": item.get("semantics", "low")},
            "behavior": {"score": item.get("behavior", "low")},
            "context": {"score": item.get("context", "low")},
            "protocol": {"score": item.get("protocol", "low")},
            "control": {"score": item.get("control", "low")},
        }
    for module, overlay in STRUCTURAL_HOTSPOT_MODULES.items():
        if module in file_inventory.module_file_counts:
            row = modules.setdefault(
                module,
                {
                    "file_count": file_inventory.module_file_counts.get(module, 0),
                    "finding_count": 0,
                    "priority": "P3",
                    "structure": {"score": "low"},
                    "semantics": {"score": "low"},
                    "behavior": {"score": "low"},
                    "context": {"score": "low"},
                    "protocol": {"score": "low"},
                    "control": {"score": "low"},
                },
            )
            _merge_module_overlay(row, overlay)
    return dict(sorted(modules.items()))


def _merge_module_overlay(row: dict[str, object], overlay: dict[str, object]) -> None:
    priority = overlay.get("priority")
    if isinstance(priority, str) and (
        priority not in audit_repo_entropy.PRIORITY_RANK
        or audit_repo_entropy.PRIORITY_RANK[priority]
        > audit_repo_entropy.PRIORITY_RANK[str(row.get("priority", "P3"))]
    ):
        row["priority"] = priority
    for axis in audit_repo_entropy.AXES:
        overlay_axis = overlay.get(axis)
        if not isinstance(overlay_axis, dict):
            continue
        current_axis = row.get(axis)
        if not isinstance(current_axis, dict):
            current_axis = {"score": "low"}
        merged_axis = dict(current_axis)
        score = overlay_axis.get("score")
        if isinstance(score, str) and (
            score not in audit_repo_entropy.SCORE_RANK
            or audit_repo_entropy.SCORE_RANK[score]
            > audit_repo_entropy.SCORE_RANK[str(merged_axis.get("score", "low"))]
        ):
            merged_axis["score"] = score
        if "hotspots" in overlay_axis:
            merged_axis["hotspots"] = overlay_axis["hotspots"]
        row[axis] = merged_axis


def _baseline_file_inventory(root: Path) -> BaselineFileInventory:
    source_count = 0
    v1_summary_source_count = 0
    test_count = 0
    instruction_count = 0
    module_file_counts: dict[str, int] = {}

    inventory_paths = _baseline_inventory_relative_paths(root)
    file_fingerprints = _baseline_file_fingerprints(root, inventory_paths)
    for relative_path in inventory_paths:
        if _baseline_path_is_file_count_skipped(relative_path):
            continue
        if _baseline_path_is_v1_source_counted(relative_path):
            module = audit_repo_entropy._module_for_relative(relative_path)  # noqa: SLF001
            module_file_counts[module] = module_file_counts.get(module, 0) + 1
        path = root / relative_path
        if audit_repo_entropy._repo_text_rejection_reason(root, path) is not None:  # noqa: SLF001
            continue

        if _baseline_path_is_instruction(relative_path):
            instruction_count += 1
            continue
        if _baseline_path_is_test(relative_path):
            test_count += 1
            continue

        source_count += 1
        if _baseline_path_is_v1_summary_source_counted(relative_path):
            v1_summary_source_count += 1

    return BaselineFileInventory(
        relative_paths=tuple(inventory_paths),
        file_fingerprints=tuple(file_fingerprints),
        total_source_files=source_count,
        v1_summary_source_files=v1_summary_source_count,
        total_test_files=test_count,
        total_instruction_files=instruction_count,
        module_file_counts=dict(sorted(module_file_counts.items())),
    )


def _baseline_inventory_relative_paths(root: Path) -> list[str]:
    tracked_paths = audit_repo_entropy._git_tracked_paths(root)  # noqa: SLF001
    if tracked_paths:
        return _bounded_baseline_inventory_paths(_filter_baseline_inventory_paths(tracked_paths))

    return _bounded_baseline_inventory_paths(_fallback_inventory_relative_paths(root))


def _fallback_inventory_relative_paths(root: Path) -> list[str]:
    root_resolved = root.resolve(strict=False)
    relative_paths: set[str] = set()
    if not root.exists():
        return []
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname != BASELINE_DIR_NAME
            and audit_repo_entropy._is_scannable_dir(root, current_path / dirname)  # noqa: SLF001
        ]
        for filename in filenames:
            path = current_path / filename
            if audit_repo_entropy._repo_text_rejection_reason(root, path) is not None:  # noqa: SLF001
                continue
            try:
                relative_paths.add(path.resolve(strict=False).relative_to(root_resolved).as_posix())
            except ValueError:
                continue
    return _filter_baseline_inventory_paths(relative_paths)


def _filter_baseline_inventory_paths(relative_paths: object) -> list[str]:
    filtered: set[str] = set()
    for relative_path in relative_paths:
        if not isinstance(relative_path, str):
            continue
        if _baseline_path_is_file_count_skipped(relative_path):
            continue
        filtered.add(relative_path)
    return sorted(filtered)


def _bounded_baseline_inventory_paths(relative_paths: list[str]) -> list[str]:
    if len(relative_paths) > MAX_BASELINE_INVENTORY_FILES:
        raise BaselineWriteError(
            f"baseline inventory file count {len(relative_paths)} exceeds limit "
            f"{MAX_BASELINE_INVENTORY_FILES}"
        )
    return relative_paths


def _baseline_file_fingerprints(
    root: Path,
    relative_paths: list[str],
) -> list[tuple[str, int, int, int]]:
    fingerprints: list[tuple[str, int, int, int]] = []
    for relative_path in relative_paths:
        try:
            file_stat = (root / relative_path).lstat()
        except OSError:
            continue
        fingerprints.append((relative_path, file_stat.st_size, file_stat.st_mtime_ns, file_stat.st_ctime_ns))
    return fingerprints


def _baseline_path_is_file_count_skipped(relative_path: str) -> bool:
    return Path(relative_path).parts[:1] == (BASELINE_DIR_NAME,)


def _baseline_path_is_instruction(relative_path: str) -> bool:
    return Path(relative_path).name in {"AGENTS.md", "CLAUDE.md", "CODEX.md"}


def _baseline_path_is_test(relative_path: str) -> bool:
    path = Path(relative_path)
    parts = set(path.parts)
    name = path.name
    return (
        bool(parts & {"test", "tests", "__tests__", "e2e"})
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _baseline_path_is_v1_source_counted(relative_path: str) -> bool:
    path = Path(relative_path)
    parts = path.parts
    if not parts:
        return False
    if parts[0] not in V1_SOURCE_MODULE_ROOTS:
        return False
    if parts[:2] == ("apps", "frontend"):
        if V1_FRONTEND_EXCLUDED_PARTS & set(parts):
            return False
        if "config" in path.name:
            return False
    return True


def _baseline_path_is_v1_summary_source_counted(relative_path: str) -> bool:
    path = Path(relative_path)
    parts = path.parts
    if not parts:
        return False
    if parts[0] in V1_SUMMARY_EXCLUDED_ROOTS:
        return False
    if len(parts) == 1 and path.suffix in V1_SUMMARY_ROOT_DOCUMENT_SUFFIXES:
        return False
    return (
        not _baseline_path_is_instruction(relative_path)
        and not _baseline_path_is_test(relative_path)
    )


def _baseline_high_spread_patterns(
    high_spread_patterns: list[object],
    findings: list[object],
) -> list[dict[str, object]]:
    axis_by_check_id = {
        str(finding["check_id"]): str(finding["governance_face"])
        for finding in findings
        if isinstance(finding, dict) and "check_id" in finding and "governance_face" in finding
    }
    patterns: list[dict[str, object]] = []
    for item in high_spread_patterns:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", "unknown"))
        occurrence_count = item.get("occurrence_count", 0)
        module_count = item.get("module_count", 0)
        patterns.append(
            {
                "pattern": pattern,
                "description": pattern,
                "occurrences": occurrence_count,
                "occurrence_count": occurrence_count,
                "module_count": module_count,
                "modules": item.get("modules", []),
                "roles": item.get("roles", []),
                "governance_faces": item.get("governance_faces", []),
                "axis": axis_by_check_id.get(pattern, "unknown"),
                "spread_risk": _spread_risk(item),
                "top_priority": item.get("top_priority", "P3"),
                "top_severity": item.get("top_severity", "low"),
            }
        )
    patterns.extend(dict(pattern) for pattern in STRUCTURAL_HIGH_SPREAD_PATTERNS)
    return patterns


def _baseline_cleanup_priorities(findings: list[object]) -> list[dict[str, object]]:
    priorities: list[dict[str, object]] = _cleanup_priority_targets(findings)
    for priority in STRUCTURAL_CLEANUP_PRIORITIES:
        if not any(item.get("target") == priority["target"] for item in priorities):
            priorities.append(dict(priority))
    return sorted(
        priorities,
        key=lambda item: (
            -_impact_rank(str(item.get("impact", "low"))),
            _effort_rank(str(item.get("effort", "low"))),
            str(item.get("target", "")),
        ),
    )


def _cleanup_priority_targets(findings: list[object]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in findings:
        if not isinstance(item, dict):
            continue
        grouped.setdefault(str(item.get("check_id", "")), []).append(item)

    priorities: list[dict[str, object]] = []
    for check_id, group in grouped.items():
        if not group:
            continue
        target = _cleanup_target(check_id)
        if target is None:
            continue
        axis = _cleanup_axis(check_id)
        top_priority = max(
            (str(item.get("priority", "P3")) for item in group),
            key=lambda item: audit_repo_entropy.PRIORITY_RANK.get(item, 0),
        )
        top_severity = max(
            (str(item.get("severity", "low")) for item in group),
            key=lambda item: audit_repo_entropy.SEVERITY_RANK.get(item, 0),
        )
        priorities.append(
            {
                "target": target,
                "impact": _cleanup_impact(check_id, top_priority, top_severity, len(group)),
                "effort": _cleanup_effort(check_id, top_priority),
                "axis": axis,
            }
        )
    return priorities


def _cleanup_target(check_id: str) -> str | None:
    if check_id == "stale-display-route-token":
        return "Align current display runbooks with M26 single-map route authority"
    if check_id == "agent-artifact-ownership-policy":
        return "Resolve gate-eligible broad E2E API mocks and DOC_STATUS artifact ownership term"
    if check_id == "broad-e2e-api-mock":
        return "Keep mocked Playwright regression separated from live display evidence"
    return None


def _cleanup_axis(check_id: str) -> str:
    if check_id == "stale-display-route-token":
        return "context"
    if check_id == "agent-artifact-ownership-policy":
        return "protocol/control"
    if check_id == "broad-e2e-api-mock":
        return "behavior/context"
    return "context"


def _cleanup_impact(check_id: str, priority: str, severity: str, occurrence_count: int) -> str:
    if check_id in {"agent-artifact-ownership-policy", "stale-display-route-token"}:
        return "high"
    if priority in {"P0", "P1"} or severity == "high" or occurrence_count >= 10:
        return "high"
    if priority == "P2" or severity == "medium" or occurrence_count >= 2:
        return "medium"
    return "low"


def _spread_risk(pattern: dict[str, object]) -> str:
    priority = str(pattern.get("top_priority", "P3"))
    severity = str(pattern.get("top_severity", "low"))
    module_count = int(pattern.get("module_count", 0))
    occurrence_count = int(pattern.get("occurrence_count", 0))
    if priority in {"P0", "P1"} or severity == "high":
        return "high"
    if occurrence_count >= 10 or module_count >= 10:
        return "high"
    if priority == "P2" or severity == "medium" or module_count >= 3:
        return "medium"
    return "low"


def _cleanup_effort(check_id: str, priority: str) -> str:
    if check_id in {"stale-display-route-token", "agent-artifact-ownership-policy"}:
        return "low"
    if check_id == "broad-e2e-api-mock":
        return "medium"
    if priority in {"P0", "P1"}:
        return "high"
    if priority == "P2":
        return "medium"
    return "low"


def _impact_rank(impact: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(impact, 0)


def _effort_rank(effort: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(effort, 0)


def _module_row_has_high_entropy(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    for axis in audit_repo_entropy.AXES:
        value = row.get(axis)
        if value == "high":
            return True
        if isinstance(value, dict) and value.get("score") == "high":
            return True
    return False


def _git_remote_url(root: Path) -> str:
    remote = _git_output(root, "config", "--get", "remote.origin.url")
    return _redact_remote_url(remote) if remote else "unknown"


def _git_branch(root: Path) -> str:
    branch = _git_output(root, "branch", "--show-current")
    if branch:
        return branch
    head = _git_output(root, "rev-parse", "--abbrev-ref", "HEAD")
    return head if head and head != "HEAD" else "unknown"


def _git_commit(root: Path) -> str:
    return _git_output(root, "rev-parse", "--short=12", "HEAD") or "unknown"


def _snapshot_identity(
    root: Path,
    *,
    file_inventory: BaselineFileInventory | None = None,
) -> SnapshotIdentity:
    inventory = file_inventory or _baseline_file_inventory(root)
    return SnapshotIdentity(
        repo=_git_remote_url(root),
        branch=_git_branch(root),
        commit=_git_commit(root),
        report_file_fingerprints=tuple(_snapshot_file_fingerprints(root, file_inventory=inventory)),
        inventory_paths=inventory.relative_paths,
        inventory_file_fingerprints=inventory.file_fingerprints,
    )


def _snapshot_file_fingerprints(
    root: Path,
    *,
    file_inventory: BaselineFileInventory | None = None,
) -> list[tuple[str, int, int, int]]:
    if file_inventory is not None:
        relative_paths = file_inventory.relative_paths
    else:
        relative_paths = tuple(_baseline_inventory_relative_paths(root))
    return _baseline_file_fingerprints(root, list(relative_paths))


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _redact_remote_url(remote: str) -> str:
    if "://" not in remote:
        return _redact_scp_like_remote(remote)
    try:
        parts = urlsplit(remote)
    except ValueError:
        return "unknown"
    if not parts.scheme or not parts.netloc:
        return "unknown"
    host = parts.netloc.rsplit("@", 1)[-1]
    if not host:
        return "unknown"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _redact_scp_like_remote(remote: str) -> str:
    if any(separator in remote for separator in ("?", "#")):
        return "unknown"
    host_and_path = remote.rsplit("@", 1)[-1]
    if ":" not in host_and_path:
        return "unknown"
    host, path = host_and_path.split(":", 1)
    if not host or not path:
        return "unknown"
    if any(separator in host for separator in ("/", "\\")):
        return "unknown"
    return f"{host}:{path}"


def _dict_value(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise BaselineWriteError(f"audit report field `{key}` is not an object")
    return value


def _list_value(data: dict[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise BaselineWriteError(f"audit report field `{key}` is not a list")
    return value


def _baseline_timestamp(timestamp: datetime) -> str:
    normalized = timestamp.astimezone(UTC)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _archive_timestamp(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _path_exists_or_is_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _fsync_directory_best_effort(path: Path) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        return
    finally:
        os.close(dir_fd)


def _repo_relative_or_absolute(root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _unlink_file_if_present(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
