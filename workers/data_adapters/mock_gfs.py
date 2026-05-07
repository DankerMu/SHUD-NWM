from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from packages.common.mock_grib import build_mock_payload, encode_mock_grib2
from packages.common.object_store import LocalObjectStore

from .base import DownloadManifest, ManifestEntry, format_cycle_time, parse_cycle_time, valid_time_for
from .gfs_adapter import GFSAdapter, GFSAdapterConfig


@dataclass(frozen=True)
class MockGFSResult:
    manifest: DownloadManifest
    file_count: int


def generate_mock_gfs(
    *,
    cycle_time: str,
    output_root: str | Path,
    source_id: str = "gfs",
    object_store_prefix: str = "",
) -> MockGFSResult:
    parsed_cycle_time = parse_cycle_time(cycle_time)
    config = GFSAdapterConfig(
        source_id=source_id,
        workspace_root=output_root,
        object_store_prefix=object_store_prefix,
        poll_interval_seconds=0,
        max_wait_seconds=0,
    )
    object_store = LocalObjectStore(output_root, object_store_prefix=object_store_prefix)
    adapter = GFSAdapter(config=config, object_store=object_store)
    compact_cycle = format_cycle_time(parsed_cycle_time)
    entries: list[ManifestEntry] = []

    for forecast_hour in config.forecast_hours():
        for variable in config.variables:
            filename = adapter.raw_filename(parsed_cycle_time, forecast_hour, variable)
            local_key = f"raw/{source_id}/{compact_cycle}/{filename}"
            payload = build_mock_payload(parsed_cycle_time, variable, forecast_hour)
            object_store.write_bytes_atomic(local_key, encode_mock_grib2(payload))
            entries.append(
                ManifestEntry(
                    remote_url=f"mock://gfs/{compact_cycle}/{forecast_hour:03d}/{variable}",
                    local_key=local_key,
                    variable=variable,
                    forecast_hour=forecast_hour,
                    expected_checksum=object_store.checksum(local_key),
                    expected_size_bytes=object_store.size(local_key),
                    metadata={
                        "cycle_time": parsed_cycle_time.isoformat(),
                        "valid_time": valid_time_for(parsed_cycle_time, forecast_hour).isoformat(),
                    },
                )
            )

    manifest_key = f"raw/{source_id}/{compact_cycle}/manifest.json"
    manifest = DownloadManifest(
        source_id=source_id,
        cycle_time=parsed_cycle_time,
        manifest_uri=object_store.uri_for_key(manifest_key),
        entries=tuple(entries),
        metadata={
            "cycle_time": parsed_cycle_time.isoformat(),
            "first_forecast_hour": config.forecast_start_hour,
            "last_forecast_hour": config.forecast_end_hour,
            "variable_count": len(config.variables),
            "total_file_count": len(entries),
            "mock": True,
        },
    )
    object_store.write_bytes_atomic(
        manifest_key,
        json.dumps(manifest.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
    )
    return MockGFSResult(manifest=manifest, file_count=len(entries))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic mock GFS GRIB2-framed test data.")
    parser.add_argument("--cycle-time", required=True, help="Cycle time as YYYYMMDDHH or ISO-8601.")
    parser.add_argument("--output-root", default=".nhms-workspace")
    parser.add_argument("--source-id", default="gfs")
    parser.add_argument("--object-store-prefix", default="")
    args = parser.parse_args(argv)

    result = generate_mock_gfs(
        cycle_time=args.cycle_time,
        output_root=args.output_root,
        source_id=args.source_id,
        object_store_prefix=args.object_store_prefix,
    )
    print(json.dumps({"manifest_uri": result.manifest.manifest_uri, "file_count": result.file_count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
