#!/usr/bin/env python
"""Create root-owned synthetic evidence for the #1894 disposable Docker oracle.

This is not a production health collector.  It is a narrow root prerequisite
for the opt-in isolated test: it writes descriptor-bound JSON envelopes whose
command, hostname, subject, member identities, mode and owner are consumed by
the unchanged production evidence parser.  The caller must use direct argv,
for example:

``sudo -n /path/to/uv run --no-sync python scripts/node27_cold_tablespace_root_evidence_setup.py ...``

The helper refuses non-#1894 work roots and production path prefixes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
from datetime import UTC, datetime
from pathlib import Path

from packages.common.node27_cold_tablespace_identity import (
    INTEGRATION_PREFIX,
    IdentityContractError,
    make_disposable_identity,
)


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be absolute")
    return path


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write(path: Path, document: dict) -> None:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(encoded, encoding="utf-8")
    path.chmod(0o600)


def _envelope(*, hostname: str, argv: list[str], subject: dict, output: str) -> dict:
    return {
        "schema_version": "1.0",
        "captured_at": _iso_now(),
        "hostname": hostname,
        "command": {"argv": argv},
        "subject": subject,
        "output": output,
    }


def _validate_identity(args: argparse.Namespace) -> None:
    try:
        make_disposable_identity(
            container_name=args.container_name,
            prior_container_name=args.prior_container_name,
            host_port=args.host_port,
            work_root=args.work_root,
            host_path=args.cold_path,
            image_id=args.image_id,
            image_ref=args.image_ref,
        )
    except IdentityContractError as error:
        raise RuntimeError("unsafe disposable identity") from error
    if args.hostname != f"nhms-1894-{socket.gethostname()}":
        raise RuntimeError("synthetic evidence hostname is invalid")
    if args.pgdata.parent != args.work_root:
        raise RuntimeError("synthetic PGDATA must be directly under the owned work root")
    if args.evidence_root.parent != args.work_root:
        raise RuntimeError("synthetic evidence root must be directly under the owned work root")
    if args.cold_path.parent != args.work_root:
        raise RuntimeError("synthetic cold path must be directly under the owned work root")
    if min(args.runtime_uid, args.runtime_gid, args.reader_gid) <= 0:
        raise RuntimeError("synthetic ownership identities must be positive")


def _validate(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root evidence setup must run as uid 0 through sudo -n")
    _validate_identity(args)


def _validate_render(args: argparse.Namespace) -> None:
    if os.geteuid() == 0:
        raise RuntimeError("synthetic evidence render must run as the unprivileged host user")
    _validate_identity(args)
    try:
        root_info = args.work_root.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("synthetic evidence render root is unavailable") from error
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise RuntimeError("synthetic evidence render root is not the exact owned work root")
    if args.evidence_root.exists() or args.evidence_root.is_symlink():
        raise RuntimeError("synthetic evidence render root is not absent")
    children = {path.name for path in args.work_root.iterdir()}
    allowed = {"pgdata", "receipts", "postgres.env"}
    if children - allowed:
        raise RuntimeError("synthetic evidence render root has unexpected children")


def _render_evidence(args: argparse.Namespace) -> None:
    args.evidence_root.mkdir(mode=0o700)
    mdadm_output = "\n".join(
        (
            "/dev/md0:",
            "Raid Level : raid1",
            "Raid Devices : 2",
            "Active Devices : 2",
            "Working Devices : 2",
            "Failed Devices : 0",
            "Spare Devices : 0",
            "State : clean",
            "",
            " 0 8 17 0 active sync /dev/sdb1",
            " 1 8 33 1 active sync /dev/sdc1",
        )
    )
    _write(
        args.evidence_root / "mdadm.json",
        _envelope(
            hostname=args.hostname,
            argv=["/usr/sbin/mdadm", "--detail", "/dev/md0"],
            subject={"array_device": "/dev/md0"},
            output=mdadm_output,
        ),
    )
    for device in ("/dev/sdb1", "/dev/sdc1"):
        _write(
            args.evidence_root / f"smart-{Path(device).name}.json",
            _envelope(
                hostname=args.hostname,
                argv=["/usr/sbin/smartctl", "-H", device],
                subject={"device": device},
                output="SMART overall-health self-assessment test result: PASSED",
            ),
        )
    backup = _envelope(
        hostname=args.hostname,
        argv=["/usr/local/sbin/nhms-backup-inventory", "--json"],
        subject={
            "pgdata": str(args.pgdata),
            "external_pg_tblspc_targets": ["/home/postgres/pgdata/tablespaces/nhms_cold"],
        },
        output="synthetic disposable backup inventory complete",
    )
    backup["covered_paths"] = [str(args.pgdata), "/home/postgres/pgdata/tablespaces/nhms_cold"]
    _write(args.evidence_root / "backup.json", backup)


def _prepare_pgdata(args: argparse.Namespace) -> None:
    args.pgdata.mkdir(mode=0o700, exist_ok=True)
    os.chown(args.pgdata, args.runtime_uid, args.runtime_gid)
    args.pgdata.chmod(0o700)


def _seal_evidence(args: argparse.Namespace) -> None:
    args.evidence_root.mkdir(mode=0o750, exist_ok=True)
    root_info = args.evidence_root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise RuntimeError("synthetic evidence root is unavailable for root sealing")
    os.chown(args.evidence_root, 0, args.reader_gid)
    args.evidence_root.chmod(0o750)
    for name in ("mdadm.json", "smart-sdb1.json", "smart-sdc1.json", "backup.json"):
        path = args.evidence_root / name
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("synthetic evidence file is unavailable for root sealing")
        os.chown(path, 0, args.reader_gid)
        path.chmod(0o640)


def _create_cold_path(args: argparse.Namespace) -> None:
    if args.cold_path.exists() or args.cold_path.is_symlink():
        raise RuntimeError("synthetic cold path is not absent")
    args.cold_path.mkdir(mode=0o700)
    os.chown(args.cold_path, args.runtime_uid, args.runtime_gid)
    args.cold_path.chmod(0o700)


def _cleanup(args: argparse.Namespace) -> None:
    root = args.work_root
    if not root.name.startswith(INTEGRATION_PREFIX) or root.parent == root:
        raise RuntimeError("root cleanup identity is unsafe")
    if not root.exists():
        return
    known = {"pgdata", "cold", "evidence", "receipts", "postgres.env"}
    children = {path.name for path in root.iterdir()}
    if children - known:
        raise RuntimeError("root cleanup refuses unknown owned-root child")
    for name in ("pgdata", "cold", "evidence", "receipts"):
        path = root / name
        if path.exists():
            if not path.is_dir() or path.is_symlink():
                raise RuntimeError("root cleanup child is not a safe directory")
            shutil.rmtree(path)
    env_path = root / "postgres.env"
    if env_path.exists():
        if not env_path.is_file() or env_path.is_symlink():
            raise RuntimeError("root cleanup environment child is unsafe")
        env_path.unlink()
    if any(root.iterdir()):
        raise RuntimeError("root cleanup could not empty owned work root")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action", choices=("prepare", "render", "seal", "create-cold-path", "cleanup"), required=True
    )
    parser.add_argument("--work-root", type=_absolute, required=True)
    parser.add_argument("--cold-path", type=_absolute, required=True)
    parser.add_argument("--pgdata", type=_absolute, required=True)
    parser.add_argument("--evidence-root", type=_absolute, required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--prior-container-name", required=True)
    parser.add_argument("--host-port", type=int, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--runtime-uid", type=int, required=True)
    parser.add_argument("--runtime-gid", type=int, required=True)
    parser.add_argument("--reader-gid", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "render":
            _validate_render(args)
            _render_evidence(args)
        else:
            _validate(args)
            if args.action == "prepare":
                _prepare_pgdata(args)
                _render_evidence(args)
                _seal_evidence(args)
            elif args.action == "seal":
                _seal_evidence(args)
            elif args.action == "create-cold-path":
                _create_cold_path(args)
            else:
                _cleanup(args)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f"root evidence setup refused: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
