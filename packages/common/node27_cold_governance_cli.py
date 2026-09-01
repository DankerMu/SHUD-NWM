"""Argument parsing and configuration assembly for cold-governance evidence."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def positive_seconds(raw: str, *, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer byte count") from error
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be positive")
    return value


def octal_mode(raw: str) -> int:
    try:
        value = int(raw, 8)
    except ValueError as error:
        raise argparse.ArgumentTypeError("evidence mode must be octal") from error
    if value < 0 or value > 0o777:
        raise argparse.ArgumentTypeError("evidence mode must be an octal permission value")
    return value


def absolute_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def smart_evidence(raw: str) -> tuple[str, Path]:
    device, separator, value = raw.partition("=")
    if not separator or not device.startswith("/dev/"):
        raise argparse.ArgumentTypeError("SMART evidence must be DEVICE=ABSOLUTE_PATH")
    return device, absolute_path(value)


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return absolute_path(value) if value else None


def _env_list(name: str, parse):
    return [parse(value) for value in os.getenv(name, "").split(",") if value]


def add_cold_governance_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cold-governance-evidence-hostname",
        default=os.getenv("NODE27_COLD_GOVERNANCE_EVIDENCE_HOSTNAME"),
    )
    parser.add_argument(
        "--cold-governance-array-device",
        default=os.getenv("NODE27_COLD_GOVERNANCE_ARRAY_DEVICE", "/dev/md0"),
    )
    parser.add_argument(
        "--cold-governance-evidence-max-age-seconds",
        type=lambda raw: positive_seconds(raw, label="cold-governance-evidence-max-age-seconds"),
        default=(
            positive_seconds(
                os.environ["NODE27_COLD_GOVERNANCE_EVIDENCE_MAX_AGE_SECONDS"],
                label="NODE27_COLD_GOVERNANCE_EVIDENCE_MAX_AGE_SECONDS",
            )
            if os.getenv("NODE27_COLD_GOVERNANCE_EVIDENCE_MAX_AGE_SECONDS")
            else None
        ),
    )
    parser.add_argument(
        "--cold-governance-evidence-owner-uid",
        type=int,
        default=int(os.getenv("NODE27_COLD_GOVERNANCE_EVIDENCE_OWNER_UID", "0")),
    )
    parser.add_argument(
        "--cold-governance-evidence-approved-mode",
        type=octal_mode,
        action="append",
        default=_env_list("NODE27_COLD_GOVERNANCE_EVIDENCE_APPROVED_MODE", octal_mode),
    )
    parser.add_argument(
        "--cold-governance-mdadm-evidence-path",
        type=absolute_path,
        default=_env_path("NODE27_COLD_GOVERNANCE_MDADM_EVIDENCE_PATH"),
    )
    parser.add_argument(
        "--cold-governance-smart-evidence",
        type=smart_evidence,
        action="append",
        default=_env_list("NODE27_COLD_GOVERNANCE_SMART_EVIDENCE", smart_evidence),
    )
    parser.add_argument(
        "--cold-governance-backup-evidence-path",
        type=absolute_path,
        default=_env_path("NODE27_COLD_GOVERNANCE_BACKUP_EVIDENCE_PATH"),
    )
    parser.add_argument(
        "--cold-governance-mdadm-bin",
        default=os.getenv("NODE27_COLD_GOVERNANCE_MDADM_BIN", "/usr/sbin/mdadm"),
    )
    parser.add_argument(
        "--cold-governance-smartctl-bin",
        default=os.getenv("NODE27_COLD_GOVERNANCE_SMARTCTL_BIN", "/usr/sbin/smartctl"),
    )
    parser.add_argument(
        "--cold-governance-backup-inventory-bin",
        default=os.getenv("NODE27_COLD_GOVERNANCE_BACKUP_INVENTORY_BIN", "/usr/local/sbin/nhms-backup-inventory"),
    )
    parser.add_argument(
        "--cold-governance-prior-receipt-path",
        type=absolute_path,
        default=_env_path("NODE27_COLD_GOVERNANCE_PRIOR_RECEIPT_PATH"),
    )
    parser.add_argument(
        "--cold-governance-prior-receipt-max-age-seconds",
        type=lambda raw: positive_seconds(raw, label="cold-governance-prior-receipt-max-age-seconds"),
        default=(
            positive_seconds(
                os.environ["NODE27_COLD_GOVERNANCE_PRIOR_RECEIPT_MAX_AGE_SECONDS"],
                label="NODE27_COLD_GOVERNANCE_PRIOR_RECEIPT_MAX_AGE_SECONDS",
            )
            if os.getenv("NODE27_COLD_GOVERNANCE_PRIOR_RECEIPT_MAX_AGE_SECONDS")
            else None
        ),
    )


def validate_cold_governance_arguments(args: argparse.Namespace) -> None:
    if args.cold_governance_evidence_owner_uid < 0:
        raise ValueError("cold governance evidence owner UID must be non-negative")
    smart_devices = [device for device, _path in args.cold_governance_smart_evidence]
    if len(set(smart_devices)) != len(smart_devices):
        raise ValueError("cold governance SMART evidence devices must be unique")
