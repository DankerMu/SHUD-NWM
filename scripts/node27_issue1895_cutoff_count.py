#!/usr/bin/env python3
"""Observe the fresh admitted G3 population without parity or census publication."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_residency import compute_cutoff
from packages.common.compressed_chunk_cold_runtime_catalog import (
    ColdRuntimeError,
    collect_residency_group,
    snapshot_group,
)
from packages.common.display_watermark import DisplayWatermarkError, fetch_display_watermark
from packages.common.node27_issue1895_census_bind import load_original_census
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts.node27_cold_residency_census import (
    MAX_MEMBERS_PER_GROUP,
    CensusError,
    CensusObserver,
    close_observer_connection,
    lag_seconds_from_env,
    open_readonly_connection,
    per_table_catalog_limit,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--original-sha256", required=True)
    parser.add_argument("--reviewed-sha", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    connect: Callable[[str], Any] | None = None,
    watermark_fetcher: Callable[..., Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    owned = None
    try:
        original, count = load_original_census(
            args.original,
            expected_original_sha256=args.original_sha256,
            reviewed_sha=args.reviewed_sha,
        )
        values = dict(os.environ) if env is None else env
        dsn = values.get("DATABASE_URL")
        if not dsn:
            raise CensusError("readonly DSN is missing", error_class="config", stage="config")
        lag = lag_seconds_from_env(values)
        opener = connect if connect is not None else open_readonly_connection
        fetch = watermark_fetcher if watermark_fetcher is not None else fetch_display_watermark
        watermark = fetch(dsn, connect=connect)
        cutoff = compute_cutoff(watermark, lag)
        if cutoff.isoformat().replace("+00:00", "Z") != original.get("cutoff"):
            raise CensusError("eligibility cutoff drifted", error_class="cutoff", stage="count")
        owned = opener(dsn)
        observer = CensusObserver(owned)
        if not observer.session_read_only():
            raise CensusError("session is not readonly", error_class="session", stage="count")
        inventories = observer.inventories()
        if inventories.digest != original["inventory"]["digest"]:
            raise CensusError("original inventory drifted", error_class="inventory", stage="count")
        candidates = observer.candidates(
            inventories=inventories,
            cutoff=cutoff,
            per_table_limit=per_table_catalog_limit(count),
        )
        for _rank, _end, _schema, _name, _oid, candidate in candidates:
            group = collect_residency_group(observer.binder(), candidate)
            snapshot = snapshot_group(group)
            if (
                group.blocker
                or not group.is_compressed
                or group.compressed_oid is None
                or not group.members
                or len(group.members) > MAX_MEMBERS_PER_GROUP
                or snapshot["residency"] != "all_source"
            ):
                raise CensusError("admitted group is incomplete or drifted", error_class="group", stage="count")
        observed = len(candidates)
    except (Issue1895ReadinessError, CensusError, ColdRuntimeError, DisplayWatermarkError) as error:
        print(getattr(error, "code", "CUTOFF_COUNT_REFUSED"), file=sys.stderr)
        return 1
    except Exception:  # Residual driver/OS errors must never echo connection details.
        print("CUTOFF_COUNT_REFUSED", file=sys.stderr)
        return 1
    finally:
        if owned is not None:
            close_observer_connection(owned)
    print(observed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
