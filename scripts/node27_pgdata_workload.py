#!/usr/bin/env python3
"""PGDATA-owned explicit-cycle SQL/API workload CLI.

``measure`` always captures the shipping named query once, then discards one
warmup and accepts 20 serial SQL and API samples. Isolated receipts never
imply live acceptance, so the ``live`` evidence kind is opt-in and admitted
only by proof: a readonly ``nhms_display_ro`` session plus a reviewed SHA
bound to the HEAD of the checkout this script executes from.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packages.common.node27_pgdata_workload import capture_workload_query, measure_workload
from packages.common.node27_pgdata_workload_io import (
    bind_reviewed_sha,
    close_readonly_connection,
    format_refusal,
    open_readonly_connection,
    prove_readonly_session,
    publish_measurement_output,
    read_private_dsn_file,
    require_runtime_anchored,
    resolve_repository_head,
    validate_id,
    validate_origin,
    validate_sha,
    validate_source,
)
from packages.common.node27_pgdata_workload_query import parse_issue_time
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError

EVIDENCE_KINDS = ("isolated", "live")
DEFAULT_EVIDENCE_KIND = "isolated"
# The checkout that owns this executing script, never the process cwd: a live
# receipt must bind its reviewed SHA to the code that actually ran.
REPO_ROOT = Path(__file__).resolve().parents[1]
# This script is a thin entrypoint; the work is done by the modules below. Anchoring
# the script alone would admit the published-bytes layout (script under
# ~/.local/state/issue1987-tools/<commit>/ run with PYTHONPATH at a working checkout),
# where REPO_ROOT's HEAD describes none of the code that produced the samples.
ANCHORED_RUNTIME_MODULES = (
    "packages.common.node27_pgdata_workload",
    "packages.common.node27_pgdata_workload_io",
    "packages.common.node27_pgdata_workload_query",
    "packages.common.node27_pgdata_workload_plan",
    "packages.common.node27_pgdata_workload_measure",
    "packages.common.node27_pgdata_workload_http",
    "packages.common.node27_pgdata_workload_types",
)


def _default_head_resolver() -> str:
    return resolve_repository_head(REPO_ROOT)


class _ArgumentParser(argparse.ArgumentParser):
    """Keep command-line errors static and free of supplied input text."""

    def error(self, _message: str) -> None:
        self.exit(2, "QUERY_USAGE: invalid arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__, add_help=True, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)
    measure_cmd = sub.add_parser("measure", add_help=False, allow_abbrev=False)
    measure_cmd.add_argument("--reader-dsn-file", required=True)
    measure_cmd.add_argument("--api-origin", required=True)
    measure_cmd.add_argument("--basin-version-id", required=True)
    measure_cmd.add_argument("--river-network-version-id", required=True)
    measure_cmd.add_argument("--segment-id", required=True)
    measure_cmd.add_argument("--issue-time", required=True)
    measure_cmd.add_argument("--run-id", required=True)
    measure_cmd.add_argument("--model-id", required=True)
    measure_cmd.add_argument("--source", required=True, choices=("GFS", "IFS"))
    measure_cmd.add_argument("--reviewed-sha", required=True)
    measure_cmd.add_argument("--evidence-kind", choices=EVIDENCE_KINDS, default=DEFAULT_EVIDENCE_KIND)
    measure_cmd.add_argument("--output", required=True)
    return parser


def _report(error: Exception) -> int:
    print(format_refusal(error), file=sys.stderr)
    return 1


def measure(
    args: argparse.Namespace,
    *,
    connect: Any | None = None,
    opener: Any | None = None,
    connection: Any | None = None,
    sql_probe: Any | None = None,
    api_probe: Any | None = None,
    head_resolver: Any | None = None,
) -> dict[str, Any]:
    evidence_kind = str(args.evidence_kind)
    origin = validate_origin(args.api_origin)
    basin = validate_id(args.basin_version_id, code="INPUT_BASIN_INVALID")
    network = validate_id(args.river_network_version_id, code="INPUT_NETWORK_INVALID")
    segment = validate_id(args.segment_id, code="INPUT_SEGMENT_INVALID")
    run_id = validate_id(args.run_id, code="INPUT_RUN_INVALID")
    model_id = validate_id(args.model_id, code="INPUT_MODEL_INVALID")
    source = validate_source(args.source)
    reviewed_sha = validate_sha(args.reviewed_sha, label="reviewed_sha")
    issue_time = parse_issue_time(args.issue_time)
    output = Path(args.output)
    # A live receipt is admitted only by proof: the reviewed SHA is bound here,
    # before any measurement work or publication, and the readonly session proof
    # below completes the admission. The isolated path resolves no HEAD at all.
    if evidence_kind == "live":
        # Anchor first: resolving a HEAD is only meaningful once the checkout that
        # supplies the working code is known to be the one being asked.
        require_runtime_anchored(REPO_ROOT, ANCHORED_RUNTIME_MODULES)
        bind_reviewed_sha(
            reviewed_sha,
            head_resolver=head_resolver if head_resolver is not None else _default_head_resolver,
        )
    dsn = read_private_dsn_file(Path(args.reader_dsn_file))
    captured = capture_workload_query(
        basin_version_id=basin,
        segment_id=segment,
        river_network_version_id=network,
        issue_time=issue_time,
        run_id=run_id,
        model_id=model_id,
        source=source,
    )
    owned = None
    live = connection
    try:
        if live is None:
            owned = open_readonly_connection(dsn, connect=connect)
            live = owned
        prove_readonly_session(live)
        document = measure_workload(
            connection=live,
            origin=origin,
            captured=captured,
            evidence_kind=evidence_kind,
            reviewed_sha=reviewed_sha,
            opener=opener,
            sql_probe=sql_probe,
            api_probe=api_probe,
        )
        publish_measurement_output(output, document)
        return document
    finally:
        if owned is not None:
            close_readonly_connection(owned)
        elif connection is not None:
            close_readonly_connection(connection)


def main(argv: Sequence[str] | None = None, **injected: Any) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command != "measure":
        print("QUERY_USAGE: invalid arguments", file=sys.stderr)
        return 2
    try:
        document = measure(args, **injected)
    except PgdataWorkloadError as error:
        return _report(error)
    except Exception as error:
        return _report(error)
    print(json.dumps({"ok": True, "status": document["status"], "evidence_kind": document["evidence_kind"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
