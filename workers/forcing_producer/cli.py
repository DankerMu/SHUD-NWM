from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from packages.common.manifest_index import ManifestValidationError, load_manifest_entry, resolve_task_id
from packages.common.source_identity import normalize_source_id
from packages.common.timescale_write_guard import CompressedChunkGuardError

from .producer import ForcingProducer


def _produce(source_id: str, cycle_time: str, model_id: str, max_lead_hours: int | None = None) -> dict[str, object]:
    source_id = normalize_source_id(source_id)
    producer = ForcingProducer.from_env()
    result = producer.produce(
        source_id=source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        max_lead_hours=max_lead_hours,
    )
    return {
        "status": result.status,
        "forcing_version_id": result.forcing_version_id,
        "forcing_package_uri": result.forcing_package_uri,
        "checksum": result.checksum,
        "station_count": result.station_count,
        "timestep_count": result.timestep_count,
    }


def _resolve_produce_args(
    source_id: str | None,
    cycle_time: str | None,
    model_id: str | None,
    max_lead_hours: int | None,
    manifest_index: str | None,
    task_id: int | None,
) -> tuple[str, str, str, int | None]:
    if manifest_index is not None:
        resolved_task_id = resolve_task_id(task_id)
        entry = load_manifest_entry(manifest_index, resolved_task_id)
        missing = [field for field in ("source_id", "cycle_time", "model_id") if entry.get(field) in (None, "")]
        if missing:
            raise ManifestValidationError(
                "Manifest index entry is missing forcing fields.",
                {"manifest_index_path": manifest_index, "task_id": resolved_task_id, "missing_fields": missing},
            )
        resolved_max_lead_hours = max_lead_hours
        if resolved_max_lead_hours is None and entry.get("max_lead_hours") not in (None, ""):
            resolved_max_lead_hours = int(entry["max_lead_hours"])
        return (
            normalize_source_id(str(entry["source_id"])),
            str(entry["cycle_time"]),
            str(entry["model_id"]),
            resolved_max_lead_hours,
        )

    missing = [
        field
        for field, value in (("cycle_time", cycle_time), ("model_id", model_id))
        if value in (None, "")
    ]
    if missing:
        raise ManifestValidationError(
            "Explicit forcing execution requires --cycle-time and --model-id.",
            {"missing_fields": missing},
        )
    return normalize_source_id(source_id or "gfs"), str(cycle_time), str(model_id), max_lead_hours


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--source-id", default="gfs", show_default=True)
    @click.option("--cycle-time")
    @click.option("--model-id")
    @click.option("--max-lead-hours", type=int, default=None)
    @click.option("--manifest-index")
    @click.option("--task-id", type=int, default=None)
    def produce(
        source_id: str,
        cycle_time: str | None,
        model_id: str | None,
        max_lead_hours: int | None,
        manifest_index: str | None,
        task_id: int | None,
    ) -> None:
        try:
            resolved = _resolve_produce_args(source_id, cycle_time, model_id, max_lead_hours, manifest_index, task_id)
            click.echo(json.dumps(_produce(*resolved), sort_keys=True))
        except ManifestValidationError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error
        except ValueError as error:
            click.echo(f"INVALID_SOURCE_ID: {error}", err=True)
            raise SystemExit(1) from error
        except CompressedChunkGuardError as error:
            click.echo(f"FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED: {error}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-forcing")
    subparsers = parser.add_subparsers(dest="command", required=True)
    produce_parser = subparsers.add_parser("produce")
    produce_parser.add_argument("--source-id", default="gfs")
    produce_parser.add_argument("--cycle-time")
    produce_parser.add_argument("--model-id")
    produce_parser.add_argument("--max-lead-hours", type=int, default=None)
    produce_parser.add_argument("--manifest-index")
    produce_parser.add_argument("--task-id", type=int, default=None)
    args = parser.parse_args(argv)

    if args.command == "produce":
        try:
            resolved = _resolve_produce_args(
                args.source_id,
                args.cycle_time,
                args.model_id,
                args.max_lead_hours,
                args.manifest_index,
                args.task_id,
            )
            print(json.dumps(_produce(*resolved), sort_keys=True))
        except ManifestValidationError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        except ValueError as error:
            print(f"INVALID_SOURCE_ID: {error}", file=sys.stderr)
            return 1
        except CompressedChunkGuardError as error:
            print(f"FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED: {error}", file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
