#!/usr/bin/env python3
"""Independent GFS/IFS complete-cycle proof against the scheduler registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.common.node27_issue1895_publication import (
    prove_gfs_ifs_products,
    registry_expected_identities,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--gfs-identity", required=True, type=Path)
    parser.add_argument("--ifs-identity", required=True, type=Path)
    parser.add_argument("--gfs-rows", required=True, type=Path)
    parser.add_argument("--ifs-rows", required=True, type=Path)
    parser.add_argument("--valid-times", required=True, type=Path)
    parser.add_argument("--baseline-valid-times", required=True, type=Path)
    return parser


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _product(document: object) -> dict:
    if isinstance(document, dict) and "data" in document and isinstance(document["data"], dict):
        return document["data"]
    if isinstance(document, dict):
        return document
    raise Issue1895ReadinessError(
        "identity-only product is not an object",
        code="PUBLICATION_PRODUCT_INVALID",
        stage="publication",
    )


def _times(document: object) -> list:
    if isinstance(document, dict) and isinstance(document.get("data"), dict):
        return list(document["data"].get("valid_times") or [])
    if isinstance(document, list):
        return list(document)
    raise Issue1895ReadinessError(
        "valid-times payload is invalid",
        code="VALID_TIMES_EMPTY",
        stage="publication",
    )


def _rows(path: Path) -> list[dict]:
    payload = _load(path)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise Issue1895ReadinessError(
        "publication rows are not a JSON array",
        code="PUBLICATION_ROW_INVALID",
        stage="publication",
    )


def _registry_models(path: Path) -> list[dict]:
    payload = _load(path)
    if isinstance(payload, dict) and isinstance(payload.get("models"), list):
        return [item for item in payload["models"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise Issue1895ReadinessError(
        "registry models are missing",
        code="PUBLICATION_EXPECTED_EMPTY",
        stage="publication",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        expected = registry_expected_identities(_registry_models(args.registry))
        prove_gfs_ifs_products(
            gfs=_product(_load(args.gfs_identity)),
            ifs=_product(_load(args.ifs_identity)),
            gfs_rows=_rows(args.gfs_rows),
            ifs_rows=_rows(args.ifs_rows),
            current_valid_times=_times(_load(args.valid_times)),
            baseline_valid_times=_times(_load(args.baseline_valid_times)),
            expected_gfs=expected.get("gfs", frozenset()),
            expected_ifs=expected.get("IFS", frozenset()),
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("publication OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
