"""Shared M24 live-closure receipt infrastructure.

This package hosts the canonical M24 receipt schema, its validator, and the
read-only baseline emitter. Every M24 section (`baseline|gateway|warm_start|
concurrency|multibasin|daemon`) writes `artifacts/m24/<run_id>/<section>.json`
through :mod:`services.m24_live.receipt`.
"""

from services.m24_live.receipt import (
    CONTRACT_ID,
    EXECUTION_MODE_VALUES,
    RECEIPT_SECTIONS,
    SCHEMA_VERSION,
    STATUS_VALUES,
    WARM_START_QUALITY_VALUES,
    ReceiptValidationError,
    receipt_path,
    validate_receipt,
    write_receipt,
)

__all__ = [
    "CONTRACT_ID",
    "EXECUTION_MODE_VALUES",
    "RECEIPT_SECTIONS",
    "SCHEMA_VERSION",
    "STATUS_VALUES",
    "WARM_START_QUALITY_VALUES",
    "ReceiptValidationError",
    "receipt_path",
    "validate_receipt",
    "write_receipt",
]
