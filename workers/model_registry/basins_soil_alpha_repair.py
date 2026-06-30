from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

REPAIR_SCHEMA_VERSION = "basins.calibration_repair.v1"
SHUD_SOIL_ALPHA_MIN = 0.05
SHUD_SOIL_ALPHA_MAX = 20.0
SHUD_SOIL_ALPHA_TARGET_MAX = 19.999
SHUD_GEOL_DMAC_MIN = 0.0
SHUD_GEOL_DMAC_MAX = 4.0
SHUD_GEOL_DMAC_TARGET_MAX = 4.0

_CFG_MULTIPLIER_RE = re.compile(r"^(\s*{parameter}\s+)([-+0-9.eE]+)(.*?)(\r?\n?)$")


def repair_soil_alpha_calibration_for_basin(
    *,
    isolated_root: str | Path,
    basin_slug: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Repair known SHUD calibration bounds inside a private basin copy."""

    root = Path(isolated_root).expanduser()
    basin_dir = root / basin_slug
    report: dict[str, Any] = {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "basin_slug": basin_slug,
        "isolated_root": str(root),
        "dry_run": bool(dry_run),
        "bounds": {
            "soil_alpha": {
                "min": SHUD_SOIL_ALPHA_MIN,
                "max": SHUD_SOIL_ALPHA_MAX,
                "repair_target_max": SHUD_SOIL_ALPHA_TARGET_MAX,
            },
            "geol_dmac": {
                "min": SHUD_GEOL_DMAC_MIN,
                "max": SHUD_GEOL_DMAC_MAX,
                "repair_target_max": SHUD_GEOL_DMAC_TARGET_MAX,
            },
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
        if cfg_calib is None:
            report["skipped"].append(
                {
                    "reason": "cfg_calib_missing_or_ambiguous",
                    "input_dir": str(input_dir),
                    "cfg_calib_count": len(list(input_dir.glob("*.cfg.calib"))),
                }
            )
            continue

        para_soil = _single_match(input_dir, "*.para.soil")
        if para_soil is None:
            report["skipped"].append(
                {
                    "reason": "para_soil_missing_or_ambiguous",
                    "input_dir": str(input_dir),
                    "para_soil_count": len(list(input_dir.glob("*.para.soil"))),
                }
            )
        else:
            _repair_soil_alpha(report, cfg_calib=cfg_calib, para_soil=para_soil, dry_run=dry_run)

        para_geol = _single_match(input_dir, "*.para.geol")
        if para_geol is None:
            report["skipped"].append(
                {
                    "reason": "para_geol_missing_or_ambiguous",
                    "input_dir": str(input_dir),
                    "para_geol_count": len(list(input_dir.glob("*.para.geol"))),
                }
            )
        else:
            _repair_geol_dmac(report, cfg_calib=cfg_calib, para_geol=para_geol, dry_run=dry_run)
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


def _repair_soil_alpha(report: dict[str, Any], *, cfg_calib: Path, para_soil: Path, dry_run: bool) -> None:
    multiplier = _read_cfg_multiplier(cfg_calib, "SOIL_ALPHA")
    if multiplier is None:
        report["skipped"].append({"reason": "soil_alpha_multiplier_missing", "cfg_calib": str(cfg_calib)})
        return
    alpha_values = _read_tabular_column_values(para_soil, "Alpha(1_m)")
    if not alpha_values:
        report["skipped"].append({"reason": "para_soil_alpha_values_missing", "para_soil": str(para_soil)})
        return

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
        return

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
        return

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
        return

    old_sha256 = _sha256_file(cfg_calib)
    if not dry_run:
        _rewrite_cfg_multiplier(cfg_calib, "SOIL_ALPHA", repaired_multiplier)
    report["repairs"].append(
        {
            "status": "would_repair" if dry_run else "repaired",
            "parameter": "SOIL_ALPHA",
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


def _repair_geol_dmac(report: dict[str, Any], *, cfg_calib: Path, para_geol: Path, dry_run: bool) -> None:
    multiplier = _read_cfg_multiplier(cfg_calib, "GEOL_DMAC")
    if multiplier is None:
        report["skipped"].append({"reason": "geol_dmac_multiplier_missing", "cfg_calib": str(cfg_calib)})
        return
    dmac_values = _read_tabular_column_values(para_geol, "Dmac(m)")
    if not dmac_values:
        report["skipped"].append({"reason": "para_geol_dmac_values_missing", "para_geol": str(para_geol)})
        return

    raw_min = min(dmac_values)
    raw_max = max(dmac_values)
    current_min = raw_min * multiplier
    current_max = raw_max * multiplier
    if SHUD_GEOL_DMAC_MIN <= current_min and current_max <= SHUD_GEOL_DMAC_MAX:
        report["skipped"].append(
            {
                "reason": "calibrated_geol_dmac_within_bounds",
                "cfg_calib": str(cfg_calib),
                "para_geol": str(para_geol),
                "geol_dmac_multiplier": multiplier,
                "calibrated_dmac_min": current_min,
                "calibrated_dmac_max": current_max,
            }
        )
        return

    if raw_max <= 0 or raw_min < 0:
        report["blocked"].append(
            {
                "reason": "unsatisfiable_raw_geol_dmac_bounds",
                "cfg_calib": str(cfg_calib),
                "para_geol": str(para_geol),
                "geol_dmac_multiplier": multiplier,
                "raw_dmac_min": raw_min,
                "raw_dmac_max": raw_max,
                "calibrated_dmac_min": current_min,
                "calibrated_dmac_max": current_max,
            }
        )
        return

    repaired_multiplier = multiplier
    if current_max > SHUD_GEOL_DMAC_MAX:
        repaired_multiplier = min(repaired_multiplier, SHUD_GEOL_DMAC_TARGET_MAX / raw_max)
    if current_min < SHUD_GEOL_DMAC_MIN:
        repaired_multiplier = max(repaired_multiplier, SHUD_GEOL_DMAC_MIN / raw_min)
    repaired_min = raw_min * repaired_multiplier
    repaired_max = raw_max * repaired_multiplier
    if not (SHUD_GEOL_DMAC_MIN <= repaired_min and repaired_max <= SHUD_GEOL_DMAC_MAX):
        report["blocked"].append(
            {
                "reason": "unsatisfiable_calibrated_geol_dmac_bounds",
                "cfg_calib": str(cfg_calib),
                "para_geol": str(para_geol),
                "geol_dmac_multiplier": multiplier,
                "candidate_multiplier": repaired_multiplier,
                "raw_dmac_min": raw_min,
                "raw_dmac_max": raw_max,
                "candidate_calibrated_dmac_min": repaired_min,
                "candidate_calibrated_dmac_max": repaired_max,
            }
        )
        return

    old_sha256 = _sha256_file(cfg_calib)
    if not dry_run:
        _rewrite_cfg_multiplier(cfg_calib, "GEOL_DMAC", repaired_multiplier)
    report["repairs"].append(
        {
            "status": "would_repair" if dry_run else "repaired",
            "parameter": "GEOL_DMAC",
            "cfg_calib": str(cfg_calib),
            "para_geol": str(para_geol),
            "old_sha256": old_sha256,
            "sha256": old_sha256 if dry_run else _sha256_file(cfg_calib),
            "geol_dmac_multiplier_before": multiplier,
            "geol_dmac_multiplier_after": repaired_multiplier,
            "raw_dmac_min": raw_min,
            "raw_dmac_max": raw_max,
            "calibrated_dmac_min_before": current_min,
            "calibrated_dmac_max_before": current_max,
            "calibrated_dmac_min_after": repaired_min,
            "calibrated_dmac_max_after": repaired_max,
        }
    )


def _read_cfg_multiplier(path: Path, parameter: str) -> float | None:
    pattern = re.compile(_CFG_MULTIPLIER_RE.pattern.format(parameter=re.escape(parameter)))
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        try:
            return float(match.group(2))
        except ValueError:
            return None
    return None


def _read_tabular_column_values(path: Path, column_name: str) -> list[float]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3:
        return []
    header = re.split(r"\s+", lines[1].strip())
    try:
        value_index = header.index(column_name)
    except ValueError:
        return []
    values: list[float] = []
    for line in lines[2:]:
        stripped = line.strip()
        if not stripped:
            continue
        columns = re.split(r"\s+", stripped)
        if len(columns) <= value_index:
            continue
        try:
            values.append(float(columns[value_index]))
        except ValueError:
            continue
    return values


def _rewrite_cfg_multiplier(path: Path, parameter: str, value: float) -> None:
    replacement = f"{value:.15g}"
    pattern = re.compile(_CFG_MULTIPLIER_RE.pattern.format(parameter=re.escape(parameter)))
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    repaired_lines: list[str] = []
    changed = False
    for line in lines:
        match = pattern.match(line)
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
