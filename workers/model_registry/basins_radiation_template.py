from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

REPAIR_SCHEMA_VERSION = "basins.missing_tsd_rl_template_repair.v1"


def repair_missing_tsd_rl_for_basin(
    *,
    isolated_root: str | Path,
    basin_slug: str,
    template_search_root: str | Path | None,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> dict[str, Any]:
    """Repair missing ``<project>.tsd.rl`` files inside a private basin copy."""

    root = Path(isolated_root).expanduser()
    basin_dir = root / basin_slug
    template_root = Path(template_search_root).expanduser() if template_search_root not in (None, "") else root
    report: dict[str, Any] = {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "basin_slug": basin_slug,
        "isolated_root": str(root),
        "template_search_root": str(template_root),
        "repairs": [],
        "skipped": [],
    }
    input_parent = basin_dir / "input"
    if not input_parent.is_dir():
        report["skipped"].append({"reason": "input_parent_missing", "path": str(input_parent)})
        return report

    for input_dir in sorted(path for path in input_parent.iterdir() if path.is_dir()):
        project_name = input_dir.name
        target = input_dir / f"{project_name}.tsd.rl"
        if target.exists():
            report["skipped"].append({"reason": "target_exists", "target": str(target)})
            continue
        lai = input_dir / f"{project_name}.tsd.lai"
        if not lai.is_file():
            report["skipped"].append({"reason": "lai_missing", "target": str(target), "lai": str(lai)})
            continue
        desired_header = _first_line(lai)
        template = _matching_template(template_root=template_root, desired_header=desired_header, target=target)
        if template is None:
            report["skipped"].append(
                {
                    "reason": "matching_template_missing",
                    "target": str(target),
                    "lai_header": desired_header,
                }
            )
            continue
        _require_under_root(target, root)
        if copy_file is None:
            shutil.copy2(template, target)
        else:
            copy_file(template, target)
        report["repairs"].append(
            {
                "status": "repaired",
                "target": str(target),
                "template": str(template),
                "sha256": _sha256_file(target),
                "template_sha256": _sha256_file(template),
                "header": desired_header,
            }
        )
    return report


def repair_performed(report: dict[str, Any]) -> bool:
    return any(item.get("status") == "repaired" for item in report.get("repairs") or [])


def _matching_template(*, template_root: Path, desired_header: str, target: Path) -> Path | None:
    matches: list[Path] = []
    for candidate in sorted(template_root.rglob("*.tsd.rl")):
        if "@eaDir" in candidate.parts or candidate.is_symlink() or not candidate.is_file():
            continue
        if _same_path(candidate, target):
            continue
        try:
            if _first_line(candidate) == desired_header:
                matches.append(candidate)
        except OSError:
            continue
    return matches[0] if matches else None


def _first_line(path: Path) -> str:
    with path.open(encoding="utf-8") as handle:
        return handle.readline().strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _require_under_root(path: Path, root: Path) -> None:
    path.resolve().relative_to(root.resolve())
