from __future__ import annotations

import argparse
import json
from typing import Sequence

from .producer import ForcingProducer


def _produce(source_id: str, cycle_time: str, model_id: str, max_lead_hours: int | None = None) -> dict[str, object]:
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


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--source-id", default="gfs", show_default=True)
    @click.option("--cycle-time", required=True)
    @click.option("--model-id", required=True)
    @click.option("--max-lead-hours", type=int, default=None)
    def produce(source_id: str, cycle_time: str, model_id: str, max_lead_hours: int | None) -> None:
        click.echo(json.dumps(_produce(source_id, cycle_time, model_id, max_lead_hours), sort_keys=True))

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-forcing")
    subparsers = parser.add_subparsers(dest="command", required=True)
    produce_parser = subparsers.add_parser("produce")
    produce_parser.add_argument("--source-id", default="gfs")
    produce_parser.add_argument("--cycle-time", required=True)
    produce_parser.add_argument("--model-id", required=True)
    produce_parser.add_argument("--max-lead-hours", type=int, default=None)
    args = parser.parse_args(argv)

    if args.command == "produce":
        print(json.dumps(_produce(args.source_id, args.cycle_time, args.model_id, args.max_lead_hours), sort_keys=True))
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
