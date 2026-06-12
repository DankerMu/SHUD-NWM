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

try:
    from scripts.governance import audit_repo_entropy
except ModuleNotFoundError:  # pragma: no cover - exercised by direct script execution.
    import audit_repo_entropy  # type: ignore[no-redef]

BASELINE_DIR_NAME = ".entropy-baseline"
LATEST_BASELINE_NAME = "latest.json"
ARCHIVE_COLLISION_LIMIT = 1000
TEMP_LATEST_NAME = ".latest.json.tmp"


class BaselineWriteError(RuntimeError):
    """Raised for stable CLI failures during baseline writes."""


@dataclass(frozen=True)
class BaselineWriteResult:
    baseline_path: Path
    archive_path: Path | None
    baseline_bytes: bytes


@dataclass(frozen=True)
class BaselineFileInventory:
    total_source_files: int
    total_test_files: int
    total_instruction_files: int
    module_file_counts: dict[str, int]


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
    report = audit_repo_entropy.build_report(root, mode="report")
    baseline = build_baseline_snapshot(root, report, timestamp=timestamp)
    baseline_bytes = (json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    baseline_dir = _prepare_baseline_dir(root)
    latest_path = baseline_dir / LATEST_BASELINE_NAME
    archive_path: Path | None = None

    _write_temporary_latest(baseline_dir, baseline_bytes)
    if _path_exists_or_is_symlink(latest_path):
        try:
            archive_path = _archive_existing_latest(baseline_dir, latest_path, timestamp=timestamp)
        except BaselineWriteError:
            _unlink_file_if_present(baseline_dir / TEMP_LATEST_NAME)
            raise
    _replace_latest_with_temporary(baseline_dir, latest_path)

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
) -> dict[str, object]:
    generated_at = timestamp or datetime.now(UTC)
    metadata = _dict_value(report, "metadata")
    findings = _list_value(report, "findings")
    module_heatmap = _list_value(report, "module_heatmap")
    high_spread_patterns = _list_value(report, "high_spread_patterns")
    file_inventory = _baseline_file_inventory(repo_root)

    return {
        "version": 1,
        "timestamp": _baseline_timestamp(generated_at),
        "repo": _git_remote_url(repo_root),
        "branch": _git_branch(repo_root),
        "commit": _git_commit(repo_root),
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
    try:
        latest_stat = latest_path.lstat()
    except OSError as exc:
        raise BaselineWriteError("unable to inspect existing latest baseline") from exc
    if latest_path.is_symlink() or not latest_path.is_file():
        raise BaselineWriteError("existing latest baseline is not a regular file")
    if latest_stat.st_size < 0:
        raise BaselineWriteError("existing latest baseline has invalid size")
    try:
        previous_bytes = latest_path.read_bytes()
    except OSError as exc:
        raise BaselineWriteError("unable to read existing latest baseline") from exc

    archive_path = _next_archive_path(baseline_dir, timestamp)
    created_archive = False
    try:
        with archive_path.open("xb") as handle:
            created_archive = True
            handle.write(previous_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if created_archive:
            _unlink_file_if_present(archive_path)
        raise BaselineWriteError("unable to archive existing latest baseline") from exc
    return archive_path


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
    return {
        "total_source_files": file_inventory.total_source_files,
        "total_test_files": file_inventory.total_test_files,
        "total_instruction_files": file_inventory.total_instruction_files,
        "total_modules": len(module_heatmap),
        "modules_with_high_entropy": sum(
            1 for row in module_heatmap if _module_row_has_high_entropy(row)
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
    return dict(sorted(modules.items()))


def _baseline_file_inventory(root: Path) -> BaselineFileInventory:
    source_count = 0
    test_count = 0
    instruction_count = 0
    module_file_counts: dict[str, int] = {}

    for relative_path in _baseline_inventory_relative_paths(root):
        if _baseline_path_is_file_count_skipped(relative_path):
            continue
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
        module = audit_repo_entropy._module_for_relative(relative_path)  # noqa: SLF001
        module_file_counts[module] = module_file_counts.get(module, 0) + 1

    return BaselineFileInventory(
        total_source_files=source_count,
        total_test_files=test_count,
        total_instruction_files=instruction_count,
        module_file_counts=dict(sorted(module_file_counts.items())),
    )


def _baseline_inventory_relative_paths(root: Path) -> list[str]:
    tracked_paths = audit_repo_entropy._git_tracked_paths(root)  # noqa: SLF001
    if tracked_paths:
        return sorted(set(tracked_paths))

    root_resolved = root.resolve(strict=False)
    relative_paths: set[str] = set()
    for path in audit_repo_entropy._iter_text_files(root, [root]):  # noqa: SLF001
        try:
            relative_paths.add(path.resolve(strict=False).relative_to(root_resolved).as_posix())
        except ValueError:
            continue
    return sorted(relative_paths)


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


def _baseline_high_spread_patterns(
    high_spread_patterns: list[object],
    findings: list[object],
) -> list[dict[str, object]]:
    axis_by_check_id = {
        str(finding["check_id"]): str(finding["axis"])
        for finding in findings
        if isinstance(finding, dict) and "check_id" in finding and "axis" in finding
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
    return patterns


def _baseline_cleanup_priorities(findings: list[object]) -> list[dict[str, object]]:
    priorities: list[dict[str, object]] = []
    for item in findings[:20]:
        if not isinstance(item, dict):
            continue
        priorities.append(
            {
                "target": item.get("evidence_path"),
                "check_id": item.get("check_id"),
                "title": item.get("title"),
                "module": item.get("module"),
                "axis": item.get("axis"),
                "priority": item.get("priority"),
                "severity": item.get("severity"),
                "line": item.get("line"),
                "budget_counted": item.get("budget_counted"),
                "gate_eligible": item.get("gate_eligible"),
                "allowlist_state": item.get("allowlist_state"),
                "impact": item.get("description"),
                "effort": _cleanup_effort(str(item.get("priority", "P3"))),
                "recommendation": item.get("recommendation"),
            }
        )
    return priorities


def _spread_risk(pattern: dict[str, object]) -> str:
    priority = str(pattern.get("top_priority", "P3"))
    severity = str(pattern.get("top_severity", "low"))
    module_count = int(pattern.get("module_count", 0))
    if priority in {"P0", "P1"} or severity == "high":
        return "high"
    if priority == "P2" or severity == "medium" or module_count >= 3:
        return "medium"
    return "low"


def _cleanup_effort(priority: str) -> str:
    if priority in {"P0", "P1"}:
        return "high"
    if priority == "P2":
        return "medium"
    return "low"


def _module_row_has_high_entropy(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    return any(row.get(axis) == "high" for axis in audit_repo_entropy.AXES)


def _git_remote_url(root: Path) -> str:
    remote = _git_output(root, "config", "--get", "remote.origin.url")
    return remote or "unknown"


def _git_branch(root: Path) -> str:
    branch = _git_output(root, "branch", "--show-current")
    if branch:
        return branch
    head = _git_output(root, "rev-parse", "--abbrev-ref", "HEAD")
    return head if head and head != "HEAD" else "unknown"


def _git_commit(root: Path) -> str:
    return _git_output(root, "rev-parse", "--short=12", "HEAD") or "unknown"


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
