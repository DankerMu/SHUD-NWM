#!/usr/bin/env python3
"""Bounded backfill of published pipeline-job provenance.

Invokes the SAME publisher used by copyback for explicit run IDs, then the
SAME importer used by the node-27 autopipeline catch-up. Never starts,
retries, or cancels Slurm work, and never re-runs scientific ingest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.orchestrator.chain_slurm_client import HttpSlurmGatewayClient  # noqa: E402
from services.orchestrator.chain_types import OrchestratorError  # noqa: E402
from services.orchestrator.journal_root_authority import journal_root_refusal_line  # noqa: E402
from services.orchestrator.pipeline_job_provenance import (  # noqa: E402
    PipelineJobProvenanceError,
    import_runs_pipeline_job_provenance,
    publish_runs_pipeline_job_provenance,
)

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish and/or import pipeline job provenance for explicit run IDs."
    )
    parser.add_argument(
        "--run-id",
        action="append",
        dest="run_ids",
        required=True,
        help="Forecast run identity to backfill. Repeat for multiple runs.",
    )
    parser.add_argument(
        "--journal-root",
        default=os.environ.get("NHMS_SCHEDULER_JOURNAL_ROOT"),
        help="DB-free journal root. Defaults to NHMS_SCHEDULER_JOURNAL_ROOT.",
    )
    parser.add_argument(
        "--object-store-root",
        default=os.environ.get("OBJECT_STORE_ROOT") or os.environ.get("NHMS_OBJECT_STORE_ROOT"),
        help="Object-store filesystem root. Defaults to OBJECT_STORE_ROOT.",
    )
    parser.add_argument(
        "--object-store-prefix",
        default=os.environ.get("OBJECT_STORE_PREFIX", ""),
        help="Object-store URI prefix. Defaults to OBJECT_STORE_PREFIX.",
    )
    parser.add_argument(
        "--published-artifact-root",
        default=os.environ.get("NHMS_PUBLISHED_ARTIFACT_ROOT"),
        help="Published artifact root for verified logs.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL for node-27 projection. Defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--slurm-gateway-url",
        default=os.environ.get("SLURM_GATEWAY_URL"),
        help="Optional gateway origin for verified parent-array log reads.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--publish-only",
        action="store_true",
        help="Run the node-22 publisher only; it never connects to node-27 PostgreSQL.",
    )
    mode.add_argument(
        "--import-only",
        action="store_true",
        help="Run the node-27 importer only against already-published sidecars.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.object_store_root:
        parser.error("OBJECT_STORE_ROOT or --object-store-root is required.")
    run_ids = list(args.run_ids)
    summary: dict[str, Any] = {"run_ids": run_ids}
    failed = False
    do_publish = args.publish_only
    do_import = args.import_only
    if do_publish:
        if not args.journal_root:
            parser.error("NHMS_SCHEDULER_JOURNAL_ROOT or --journal-root is required to publish.")
        slurm_client = None
        if args.slurm_gateway_url:
            slurm_client = HttpSlurmGatewayClient(args.slurm_gateway_url)
        try:
            publication = publish_runs_pipeline_job_provenance(
                run_ids=run_ids,
                journal_root=args.journal_root,
                object_store_root=args.object_store_root,
                object_store_prefix=args.object_store_prefix or "",
                published_artifact_root=args.published_artifact_root,
                slurm_client=slurm_client,
            )
        except PipelineJobProvenanceError as error:
            publication = {"status": "failed", "reason": error.code, "runs": []}
        except OrchestratorError as error:
            print(journal_root_refusal_line(error), file=sys.stderr)
            publication = {"status": "failed", "reason": error.error_code, "runs": []}
        summary["publication"] = publication
        failed = failed or publication.get("status") != "published"
    if do_import:
        if not args.database_url:
            parser.error("DATABASE_URL or --database-url is required to import.")
        try:
            projection = import_runs_pipeline_job_provenance(
                database_url=args.database_url,
                object_store_root=args.object_store_root,
                run_ids=run_ids,
                object_store_prefix=args.object_store_prefix or "",
            )
        except PipelineJobProvenanceError as error:
            projection = {"status": "failed", "reason": error.code, "runs": []}
        except OrchestratorError as error:
            print(journal_root_refusal_line(error), file=sys.stderr)
            projection = {"status": "failed", "reason": error.error_code, "runs": []}
        summary["projection"] = projection
        failed = failed or projection.get("status") == "failed"
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return EXIT_FAILURES if failed else EXIT_OK




if __name__ == "__main__":
    raise SystemExit(main())
