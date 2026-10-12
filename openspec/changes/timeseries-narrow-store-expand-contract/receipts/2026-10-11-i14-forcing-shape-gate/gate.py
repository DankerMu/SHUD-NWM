"""#1993 task 8.2, fixture I14-1993-task82.md D5: judge for ``probe.py``'s JSON.

Usage:  python gate.py <probe-output.json>

Per reader statement it reports the number of Seq Scan nodes on
``met.forcing_station_timeseries`` or one of its chunks (must be zero), the
measured wall time and the shared hit/read buffers.

Exit codes:
  0  every expected statement was captured, planned and measured; no fact-table
     Seq Scan; the QHH fallback is within an order of magnitude of its baseline
  1  at least one fact-table Seq Scan (criterion (a)), or the QHH fallback
     regressed by an order of magnitude against its legacy baseline (criterion (b))
  2  the receipt is incomplete: the probe refused or found no identity, a
     statement is missing, was not recorded, or one of its EXPLAINs failed.
     An incomplete receipt is NOT a pass.

A Seq Scan on a ``compress_hyper_*`` relation whose parent node is a fact chunk
(TimescaleDB's DecompressChunk) reads every compressed batch of that chunk. It is
counted as a fact-table Seq Scan. Seq Scans on any other relation are printed
as information and are not judged.

The eight keys are restated here on purpose rather than read from the JSON: a
probe that silently captured seven statements must not be able to shrink the
set it is judged against.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

EXPECTED_STATEMENTS: tuple[str, ...] = (
    "forecast_store.latest_product_station_source",
    "forecast_store.station_forcing_membership",
    "forecast_store.station_series_rows",
    "forecast_store.forcing_readiness_overall",
    "forecast_store.forcing_readiness_variable_rows",
    "display_coverage.station_sample_rows",
    "best_available.forcing_inputs",
    "qhh_production_bootstrap.dynamic_forcing_count",
)

#: Criterion (b). The legacy leg of the QHH fallback measured on node-27 on
#: 2026-09-20 (receipts/2026-09-20-i13-forcing-dual-store-readpath/README.md §3).
QHH_FALLBACK_KEY = "forecast_store.latest_product_station_source"
QHH_FALLBACK_LEGACY_BASELINE_MS = 1612.573
ORDER_OF_MAGNITUDE = 10.0

EXIT_PASS, EXIT_VIOLATION, EXIT_INCOMPLETE = 0, 1, 2

_SEQ_SCAN_NODES = frozenset({"Seq Scan", "Parallel Seq Scan"})
_COMPRESSED_PREFIX = "compress_hyper_"


def seq_scans(plan_nodes: list[Mapping[str, Any]], fact_relations: frozenset[str]) -> tuple[list[str], list[str]]:
    """``(on the fact table or its chunks, on anything else)``, one line per node."""
    fact: list[str] = []
    other: list[str] = []
    for node in plan_nodes:
        if node.get("node") not in _SEQ_SCAN_NODES:
            continue
        relation = str(node.get("relation") or "")
        parent = str(node.get("parent_relation") or "")
        detail = (
            f"rows={node.get('actual_rows', node.get('plan_rows'))} "
            f"removed_by_filter={node.get('rows_removed_by_filter', 0)} filter={node.get('filter')}"
        )
        if relation in fact_relations:
            fact.append(f"{relation} [{detail}]")
        elif relation.startswith(_COMPRESSED_PREFIX) and parent in fact_relations:
            fact.append(f"{relation} (compressed batches of {parent}) [{detail}]")
        else:
            other.append(relation or "<no relation>")
    return fact, other


def _fact_nodes(plan_nodes: list[Mapping[str, Any]], fact_relations: frozenset[str]) -> int:
    """Plan nodes that touch the fact table at all — the non-vacuity count."""
    return sum(
        1
        for node in plan_nodes
        if str(node.get("relation") or "") in fact_relations
        or (
            str(node.get("relation") or "").startswith(_COMPRESSED_PREFIX)
            and str(node.get("parent_relation") or "") in fact_relations
        )
    )


def judge(report: Mapping[str, Any]) -> tuple[int, list[str]]:
    """Exit code and the lines to print."""
    lines: list[str] = []
    if report.get("error"):
        return EXIT_INCOMPLETE, [f"INCOMPLETE probe error: {report['error']}"]
    fact = report.get("fact") or {}
    fact_relations = frozenset(fact.get("relations") or ())
    if not fact_relations:
        return EXIT_INCOMPLETE, ["INCOMPLETE the probe recorded no fact relations; nothing can be judged"]
    identity = report.get("identity") or {}
    lines.append(
        f"identity run={identity.get('run_id')} forcing_version={identity.get('forcing_version_id')} "
        f"basin={identity.get('basin_id')} cycle_time={identity.get('cycle_time')} "
        f"station={identity.get('station_id')}"
    )
    server = report.get("server") or {}
    lines.append(
        f"fact {fact.get('hypertable')}: {fact.get('chunks')} chunk(s), {fact.get('compressed_chunks')} compressed; "
        f"server {server.get('server_version')} / timescaledb {server.get('timescaledb')}; "
        f"read_only={server.get('transaction_read_only')}"
    )

    statements = report.get("statements") or {}
    violations = incomplete = 0
    for key in EXPECTED_STATEMENTS:
        entry = statements.get(key)
        if entry is None:
            incomplete += 1
            lines.append(f"INCOMPLETE {key}: missing from the probe output")
            continue
        if entry.get("capture_error"):
            incomplete += 1
            lines.append(f"INCOMPLETE {key}: {entry['capture_error']} (driver error: {entry.get('driver_error')})")
            continue
        plan_only, analyze = entry.get("plan_only") or {}, entry.get("analyze") or {}
        problems = []
        for leg, result in (("plan-only", plan_only), ("analyze", analyze)):
            if result.get("error"):
                problems.append(f"{leg} EXPLAIN failed: {result['error']}")
            elif result.get("skipped"):
                problems.append(f"{leg} EXPLAIN skipped: {result['skipped']}")
            elif "plan_nodes" not in result:
                problems.append(f"{leg} EXPLAIN absent")
        fact_seq: list[str] = []
        other_seq: list[str] = []
        touched = 0
        for result in (plan_only, analyze):
            nodes = result.get("plan_nodes") or []
            leg_fact, leg_other = seq_scans(nodes, fact_relations)
            fact_seq.extend(leg_fact)
            other_seq.extend(leg_other)
            touched += _fact_nodes(nodes, fact_relations)
        if not problems and touched == 0:
            problems.append("no plan node touches the fact table: a zero walk is not a pass")

        notes: list[str] = []
        regression = False
        execution_ms = analyze.get("execution_ms")
        if key == QHH_FALLBACK_KEY and isinstance(execution_ms, int | float):
            ratio = execution_ms / QHH_FALLBACK_LEGACY_BASELINE_MS
            regression = ratio >= ORDER_OF_MAGNITUDE
            notes.append(
                f"vs legacy baseline {QHH_FALLBACK_LEGACY_BASELINE_MS} ms (2026-09-20, another cycle): x{ratio:.2f}"
            )
        if entry.get("analyze_statement") == "derived_select":
            notes.append("analyze leg ran the derived SELECT, not the product INSERT")

        if fact_seq or regression:
            verdict = "FAIL"
            violations += 1
        elif problems:
            verdict = "INCOMPLETE"
            incomplete += 1
        else:
            verdict = "PASS"
        lines.append(
            f"{verdict} {key}: fact_seq_scans={len(fact_seq)} fact_nodes={touched} "
            f"exec_ms={execution_ms} planning_ms={analyze.get('planning_ms')} "
            f"shared_hit={analyze.get('shared_hit')} shared_read={analyze.get('shared_read')}"
        )
        for relation in sorted(set(fact_seq)):
            lines.append(f"      VIOLATION Seq Scan on {relation}")
        if regression:
            lines.append("      VIOLATION order-of-magnitude regression against the legacy baseline")
        for problem in problems:
            lines.append(f"      PROBLEM {problem}")
        if other_seq:
            lines.append(f"      info: Seq Scan on non-fact relation(s): {', '.join(sorted(set(other_seq)))}")
        for note in notes:
            lines.append(f"      note: {note}")

    for skipped in report.get("not_probed") or ():
        lines.append(f"NOT PROBED {skipped.get('key')}: {skipped.get('reason')}")

    if violations:
        code = EXIT_VIOLATION
    elif incomplete:
        code = EXIT_INCOMPLETE
    else:
        code = EXIT_PASS
    lines.append(
        f"\nSHAPE GATE: {('GREEN', 'RED', 'INCOMPLETE')[code]} "
        f"({len(EXPECTED_STATEMENTS)} expected, {violations} violating, {incomplete} incomplete)"
    )
    return code, lines


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: gate.py <probe-output.json>", file=sys.stderr)
        return EXIT_INCOMPLETE
    with open(arguments[0], encoding="utf-8") as handle:
        report = json.load(handle)
    code, lines = judge(report)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
