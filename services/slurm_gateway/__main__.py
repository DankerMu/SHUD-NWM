"""CLI entrypoint for the standalone Slurm gateway.

Run with ``python -m services.slurm_gateway``. Host/port are derived from
``SLURM_GATEWAY_URL`` (default ``http://127.0.0.1:8081``). This entrypoint serves
only the bounded gateway app from :func:`services.slurm_gateway.app.create_gateway_app`;
it never starts the full business API.

The entrypoint FAILS CLOSED before uvicorn starts when the resolved or
overridden bind host is not a loopback address (``127.0.0.0/8`` or ``::1``).
This is the deployable user-level equivalent of the loopback-only listening
requirement: node-22's user has no noninteractive sudo to install a host
packet-filter, so the process itself rejects ``0.0.0.0``, ``::``, hostname, or
any non-loopback IP before binding. A root-managed host packet-filter/ACL
remains an optional stronger defense and is not required for the process to be
safe from bind drift.
"""

from __future__ import annotations

import argparse
import os
import sys
from ipaddress import ip_address
from urllib.parse import urlsplit

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8081"


def _resolve_host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 8081
    return host, int(port)


def _validate_bind_host(host: str) -> None:
    """Reject every non-loopback bind host with a stable stderr + nonzero exit.

    Accepts only ``127.0.0.0/8`` and ``::1``. Rejects ``0.0.0.0``, ``::``,
    hostnames, wildcards, and non-loopback IPs by raising ``SystemExit`` after
    writing a stable error line to stderr. The guard runs before uvicorn is
    imported/started, so a wrong bind never opens a socket.
    """
    try:
        address = ip_address(host)
    except ValueError as error:
        print(
            "services.slurm_gateway: non-loopback or unresolvable bind host is not "
            f"allowed: {host!r} (loopback IPs only: 127.0.0.0/8, ::1)",
            file=sys.stderr,
        )
        raise SystemExit(2) from error
    if not (address.is_loopback and not address.is_unspecified):
        print(
            "services.slurm_gateway: non-loopback bind host is not allowed: "
            f"{host!r} (loopback IPs only: 127.0.0.0/8, ::1)",
            file=sys.stderr,
        )
        raise SystemExit(2)


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

    # Deployable loopback-only control: reject non-loopback before any import
    # or bind (node-22 user has no noninteractive sudo for a host firewall).
    _validate_bind_host(host)

    import uvicorn

    from services.slurm_gateway.app import create_gateway_app

    uvicorn.run(create_gateway_app(), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
