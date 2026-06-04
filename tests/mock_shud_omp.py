from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path


def _read_cfg(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timesteps(cfg: dict[str, str]) -> int:
    start = _parse_time(cfg["START_TIME"])
    end = _parse_time(cfg["END_TIME"])
    interval_minutes = int(cfg.get("MODEL_OUTPUT_INTERVAL", "1440"))
    return int((end - start).total_seconds() // (interval_minutes * 60))


def main() -> int:
    parser = argparse.ArgumentParser(prog="mock_shud_omp")
    parser.add_argument("cfg_path")
    parser.add_argument("--basin", default=None)
    args = parser.parse_args()

    cfg_path = Path(args.cfg_path)
    cfg = _read_cfg(cfg_path)
    output_dir = Path(cfg["OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    basin = args.basin or cfg_path.stem.replace(".cfg", "")
    segment_count = int(cfg.get("SEGMENT_COUNT", "1"))
    interval_minutes = int(cfg.get("MODEL_OUTPUT_INTERVAL", "1440"))
    start = _parse_time(cfg["START_TIME"])

    rows = []
    rows.append(",".join(["time", *[f"seg_{index:04d}" for index in range(1, segment_count + 1)]]))
    for step in range(_timesteps(cfg)):
        timestamp = (start + timedelta(minutes=step * interval_minutes)).isoformat()
        values = [f"{100.0 + index + step:.3f}" for index in range(1, segment_count + 1)]
        rows.append(",".join([timestamp, *values]))
    (output_dir / f"{basin}.rivqdown").write_text("\n".join(rows) + "\n", encoding="utf-8")
    # Emit a structurally valid minimal SHUD .cfg.ic restart state so warm-start
    # state-variable QC (header counts + per-element non-negative state columns) and
    # the runtime header-minute readers all parse it. Header layout:
    #   <mesh_count> <river_count> <minute-time>   (minute-time at token index 2)
    # followed by one mesh row (id + canopy/snow/surface/unsat/groundwater) and one
    # river row (id + river_stage) per element, all non-negative.
    state_time = start + timedelta(minutes=_timesteps(cfg) * interval_minutes)
    minute_time = state_time.timestamp() / 60.0
    mesh_count = segment_count
    ic_lines = [f"{mesh_count}\t{segment_count}\t{minute_time:.6f}"]
    for index in range(1, mesh_count + 1):
        ic_lines.append(f"{index}\t0.010\t0.000\t0.050\t0.200\t0.500")
    for index in range(1, segment_count + 1):
        ic_lines.append(f"{index}\t0.300")
    (output_dir / f"{basin}.cfg.ic").write_text("\n".join(ic_lines) + "\n", encoding="utf-8")
    print(f"mock_shud_omp wrote {len(rows) - 1} timesteps to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
