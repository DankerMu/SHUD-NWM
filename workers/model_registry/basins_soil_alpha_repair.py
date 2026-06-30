from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

REPAIR_SCHEMA_VERSION = "basins.soil_alpha_calibration_repair.v1"
SHUD_SOIL_ALPHA_MIN = 0.05
SHUD_SOIL_ALPHA_MAX = 20.0
SHUD_SOIL_ALPHA_TARGET_MAX = 19.999

_SOIL_ALPHA_RE = re.compile(r"^(\s*SOIL_ALPHA\s+)([-+0-9.eE]+)(.*?)(\r?\n?)$")


def repair_soil_alpha_calibration_for_basin(
    *,
    isolated_root: str | Path,
    basin_slug: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Repair calibrated soil Alpha bounds inside a private basin copy."""

    root = Path(isolated_root).expanduser()
    basin_dir = root / basin_slug
    report: dict[str, Any] = {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "basin_slug": basin_slug,
        "isolated_root": str(root),
        "dry_run": bool(dry_run),
        "bounds": {
            "min": SHUD_SOIL_ALPHA_MIN,
            "max": SHUD_SOIL_ALPHA_MAX,
            "repair_target_max": SHUD_SOIL_ALPHA_TARGET_MAX,
        },
        "repairs": [],
        "skipped": [],
        "blocked": [],
    }
    input_parent = basin_dir / "input"
    if not input_parent.is_dir():
        report["skipped"].append({"reason": "input_parent_missing", "path": str(input_parent)})
        return report

    for input_dir in sorted(path for path in input_parent.iterdir() if path.is_dir()):
        cfg_calib = _single_match(input_dir, "*.cfg.calib")
        para_soil = _single_match(input_dir, "*.para.soil")
        if cfg_calib is None or para_soil is None:
            report["skipped"].append(
                {
                    "reason": "required_file_missing_or_ambiguous",
                    "input_dir": str(input_dir),
                    "cfg_calib_count": len(list(input_dir.glob("*.cfg.calib"))),
                    "para_soil_count": len(list(input_dir.glob("*.para.soil"))),
                }
            )
            continue

        multiplier = _read_soil_alpha_multiplier(cfg_calib)
        if multiplier is None:
            report["skipped"].append({"reason": "soil_alpha_multiplier_missing", "cfg_calib": str(cfg_calib)})
            continue
        alpha_values = _read_para_soil_alpha_values(para_soil)
        if not alpha_values:
            report["skipped"].append({"reason": "para_soil_alpha_values_missing", "para_soil": str(para_soil)})
            continue

        raw_min = min(alpha_values)
        raw_max = max(alpha_values)
        if raw_min <= 0 or raw_max <= 0:
            report["blocked"].append(
                {
                    "reason": "non_positive_raw_soil_alpha",
                    "cfg_calib": str(cfg_calib),
                    "para_soil": str(para_soil),
                    "raw_alpha_min": raw_min,
                    "raw_alpha_max": raw_max,
                }
            )
            continue

        current_min = raw_min * multiplier
        current_max = raw_max * multiplier
        if SHUD_SOIL_ALPHA_MIN <= current_min and current_max <= SHUD_SOIL_ALPHA_MAX:
            report["skipped"].append(
                {
                    "reason": "calibrated_soil_alpha_within_bounds",
                    "cfg_calib": str(cfg_calib),
                    "para_soil": str(para_soil),
                    "soil_alpha_multiplier": multiplier,
                    "calibrated_alpha_min": current_min,
                    "calibrated_alpha_max": current_max,
                }
            )
            continue

        repaired_multiplier = multiplier
        if current_max > SHUD_SOIL_ALPHA_MAX:
            repaired_multiplier = min(repaired_multiplier, SHUD_SOIL_ALPHA_TARGET_MAX / raw_max)
        if current_min < SHUD_SOIL_ALPHA_MIN:
            repaired_multiplier = max(repaired_multiplier, SHUD_SOIL_ALPHA_MIN / raw_min)
        repaired_min = raw_min * repaired_multiplier
        repaired_max = raw_max * repaired_multiplier
        if not (SHUD_SOIL_ALPHA_MIN <= repaired_min and repaired_max <= SHUD_SOIL_ALPHA_MAX):
            report["blocked"].append(
                {
                    "reason": "unsatisfiable_calibrated_soil_alpha_bounds",
                    "cfg_calib": str(cfg_calib),
                    "para_soil": str(para_soil),
                    "soil_alpha_multiplier": multiplier,
                    "candidate_multiplier": repaired_multiplier,
                    "raw_alpha_min": raw_min,
                    "raw_alpha_max": raw_max,
                    "candidate_calibrated_alpha_min": repaired_min,
                    "candidate_calibrated_alpha_max": repaired_max,
                }
            )
            continue

        old_sha256 = _sha256_file(cfg_calib)
        if not dry_run:
            _rewrite_soil_alpha_multiplier(cfg_calib, repaired_multiplier)
        report["repairs"].append(
            {
                "status": "would_repair" if dry_run else "repaired",
                "cfg_calib": str(cfg_calib),
                "para_soil": str(para_soil),
                "old_sha256": old_sha256,
                "sha256": old_sha256 if dry_run else _sha256_file(cfg_calib),
                "soil_alpha_multiplier_before": multiplier,
                "soil_alpha_multiplier_after": repaired_multiplier,
                "raw_alpha_min": raw_min,
                "raw_alpha_max": raw_max,
                "calibrated_alpha_min_before": current_min,
                "calibrated_alpha_max_before": current_max,
                "calibrated_alpha_min_after": repaired_min,
                "calibrated_alpha_max_after": repaired_max,
            }
        )
    return report


def repair_performed(report: dict[str, Any]) -> bool:
    return any(item.get("status") == "repaired" for item in report.get("repairs") or [])


def repair_needed(report: dict[str, Any]) -> bool:
    return any(item.get("status") in {"repaired", "would_repair"} for item in report.get("repairs") or [])


def repair_blocked(report: dict[str, Any]) -> bool:
    return bool(report.get("blocked"))


def _single_match(root: Path, pattern: str) -> Path | None:
    matches = sorted(path for path in root.glob(pattern) if path.is_file())
    return matches[0] if len(matches) == 1 else None


def _read_soil_alpha_multiplier(path: Path) -> float | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _SOIL_ALPHA_RE.match(line)
        if match is None:
            continue
        try:
            return float(match.group(2))
        except ValueError:
            return None
    return None


def _read_para_soil_alpha_values(path: Path) -> list[float]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3:
        return []
    header = re.split(r"\s+", lines[1].strip())
    try:
        alpha_index = header.index("Alpha(1_m)")
    except ValueError:
        return []
    values: list[float] = []
    for line in lines[2:]:
        stripped = line.strip()
        if not stripped:
            continue
        columns = re.split(r"\s+", stripped)
        if len(columns) <= alpha_index:
            continue
        try:
            values.append(float(columns[alpha_index]))
        except ValueError:
            continue
    return values


def _rewrite_soil_alpha_multiplier(path: Path, value: float) -> None:
    replacement = f"{value:.15g}"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    repaired_lines: list[str] = []
    changed = False
    for line in lines:
        match = _SOIL_ALPHA_RE.match(line)
        if match is None:
            repaired_lines.append(line)
            continue
        repaired_lines.append(f"{match.group(1)}{replacement}{match.group(3)}{match.group(4)}")
        changed = True
    if not changed:
        return
    path.write_text("".join(repaired_lines), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
