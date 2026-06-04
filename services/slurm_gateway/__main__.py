"""CLI entrypoint for the standalone Slurm gateway.

Run with ``python -m services.slurm_gateway``. Host/port are derived from
``SLURM_GATEWAY_URL`` (default ``http://127.0.0.1:8081``). This entrypoint serves
only the bounded gateway app from :func:`services.slurm_gateway.app.create_gateway_app`;
it never starts the full business API.
"""

from __future__ import annotations

import argparse
import os
from urllib.parse import urlsplit

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8081"


def _resolve_host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 8081
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.slurm_gateway",
        description="Run the standalone NHMS Slurm gateway HTTP service.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("SLURM_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        help="Listen URL (default: $SLURM_GATEWAY_URL or http://127.0.0.1:8081).",
    )
    parser.add_argument("--host", default=None, help="Override the host parsed from --url.")
    parser.add_argument("--port", type=int, default=None, help="Override the port parsed from --url.")
    args = parser.parse_args(argv)

    host, port = _resolve_host_port(args.url)
    if args.host:
        host = args.host
    if args.port:
        port = args.port

    import uvicorn

    from services.slurm_gateway.app import create_gateway_app

    uvicorn.run(create_gateway_app(), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
