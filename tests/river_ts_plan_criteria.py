"""#2451 pass criteria for one fact-reading plan node group, as pure functions.

No database and no psycopg2 here on purpose. ``design.md`` ("How the selection
is made") states three pass criteria per fact-reading node, and criterion 3
exists precisely because criteria 1 and 2 are satisfiable by a BAD plan:
PostgreSQL lists every qual on an indexed column in ``Index Cond`` including
non-boundary quals, and rows discarded in the index layer are never counted in
``Rows Removed by Filter``, which is read off the heap layer. A criterion that
cannot fail is worse than no criterion, so the three are expressed as functions
of an ``EXPLAIN (FORMAT JSON)`` plan dict and proven to BITE offline in
``tests/test_river_ts_plan_criteria.py`` — no PostgreSQL, no TimescaleDB, no
skip.

Criterion 1 is judged PER BRANCH. On a narrow node the segment identity is
``river_segment_key``; on a legacy node it is ``river_segment_id``, because
``render_river_ts_sql(..., "legacy")`` keeps the text aid conjuncts and the
legacy compression segmentby is text-based
(``db/migrations/000047_*.sql``: ``run_id, river_network_version_id,
river_segment_id``). Requiring ``river_segment_key`` on a legacy node would be a
permanent false red — verified against
``openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/explain-1987.json``
case ``shj_nj/legacy``, where ``compress_hyper_7_104_chunk`` binds
``river_segment_id`` in its ``Index Cond`` while ``_hyper_3_62_chunk`` carries
``river_segment_key`` only as a ``Filter``. ``river_segment_key`` is ALSO
accepted on a legacy node: a plan that legitimately binds the key there is a
good plan, and rejecting it would be the same false red with the sign flipped.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

#: TimescaleDB names a compressed chunk's backing relation
#: ``compress_hyper_<hypertable_id>_<chunk_id>_chunk``. A ``DecompressChunk``
#: node carries the USER chunk's ``Relation Name`` and the compressed relation
#: appears on its child scan, which is where the ``Index Cond`` lives.
COMPRESSED_RELATION_PREFIX = "compress_hyper_"

#: Criterion 1's accepted identity columns, per branch. See the module docstring
#: for why legacy accepts two and narrow accepts one.
BRANCH_IDENTITY_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "narrow": ("river_segment_key",),
    "legacy": ("river_segment_id", "river_segment_key"),
}

#: Criterion 3's RELATIVE bound: a node may read at most this multiple of the
#: SAME cell's post-``ANALYZE`` baseline. The throwaway fixture measured 27
#: shared hits for the good (primary-key) plan against 1581 for the bad
#: (discovery index) one — a factor of 58 — so 8 leaves a 7x margin while still
#: rejecting the "segment key late in the Index Cond" shape criterion 3 exists to
#: catch. The multiple ALONE is not the verdict: see
#: :data:`SHARED_HIT_ABSOLUTE_FLOOR`.
DEFAULT_SHARED_HIT_MULTIPLE = 8

#: Criterion 3's ABSOLUTE floor, in 8 kB buffers. A node reading fewer than 256
#: buffers (2 MB) cannot represent a full-network segment scan at any geometry
#: this bench seeds, so below that the count is dominated by index descent.
SHARED_HIT_ABSOLUTE_FLOOR = 256


def walk_plan(node: Mapping[str, Any], depth: int = 0) -> Iterator[tuple[int, Mapping[str, Any]]]:
    """Every node of an EXPLAIN plan subtree, depth-first, with its depth."""
    yield depth, node
    for child in node.get("Plans") or []:
        if isinstance(child, Mapping):
            yield from walk_plan(child, depth + 1)


def access_predicate(node: Mapping[str, Any]) -> str:
    """Everything the ACCESS METHOD evaluated, as opposed to a heap-level filter.

    A bitmap plan splits the answer over two nodes: ``Index Cond`` sits on the
    child ``Bitmap Index Scan`` (which carries no ``Relation Name``) while
    ``Filter`` and the buffer counters sit on the parent ``Bitmap Heap Scan``.
    Reading only the parent's own keys would lose the index predicate and report
    every bitmap plan as a demotion. ``Recheck Cond`` is included for the same
    reason D11's ``_node_predicate_raw`` includes it.
    """
    parts = [str(node.get(key)) for key in ("Index Cond", "Recheck Cond") if node.get(key)]
    for child in node.get("Plans") or []:
        if isinstance(child, Mapping) and child.get("Node Type") == "Bitmap Index Scan" and child.get("Index Cond"):
            parts.append(str(child["Index Cond"]))
    return " ".join(parts)


def index_names(node: Mapping[str, Any]) -> list[str]:
    names = [str(node["Index Name"])] if node.get("Index Name") else []
    for child in node.get("Plans") or []:
        if isinstance(child, Mapping) and child.get("Node Type") == "Bitmap Index Scan" and child.get("Index Name"):
            names.append(str(child["Index Name"]))
    return names


def _node_record(
    node: Mapping[str, Any],
    *,
    depth: int,
    role: str,
    identity_columns: Sequence[str],
    filter_ratio_limit: int,
) -> dict[str, Any]:
    predicate = access_predicate(node)
    filter_text = str(node.get("Filter") or "")
    loops = int(node.get("Actual Loops") or 1) or 1
    actual_rows = int(node.get("Actual Rows") or 0)
    rows_removed = int(node.get("Rows Removed by Filter") or 0)
    returned_total = actual_rows * loops
    removed_total = rows_removed * loops
    bound = [column for column in identity_columns if column in predicate]
    return {
        "depth": depth,
        "role": role,
        "node_type": node.get("Node Type"),
        "relation": str(node.get("Relation Name") or ""),
        "index_names": index_names(node),
        "index_cond": node.get("Index Cond"),
        "recheck_cond": node.get("Recheck Cond"),
        "access_predicate": predicate,
        "filter": node.get("Filter"),
        "rows_removed_by_filter": rows_removed,
        "actual_rows": actual_rows,
        "actual_loops": loops,
        "returned_total": returned_total,
        "removed_total": removed_total,
        # D11's own comparison (node27_pgdata_workload_plan.py:514), spelled as a
        # product so a zero-row node is judged the way the gate judges it rather
        # than dividing by zero.
        "breaks_filter_ratio": removed_total > returned_total * filter_ratio_limit,
        "filter_ratio": removed_total / max(returned_total, 1),
        "shared_hit_blocks": int(node.get("Shared Hit Blocks") or 0),
        "shared_read_blocks": int(node.get("Shared Read Blocks") or 0),
        # Estimates, not measurements: `Plan Rows == 1` on a no-statistics node
        # means the index paths tied on cost and the winner was decided by path
        # order rather than by selectivity (proposal.md, "Deviations").
        "plan_rows": node.get("Plan Rows"),
        "startup_cost": node.get("Startup Cost"),
        "total_cost": node.get("Total Cost"),
        "identity_columns_in_access_predicate": bound,
        "identity_in_access_predicate": bool(bound),
        "identity_in_filter": any(column in filter_text for column in identity_columns),
    }


def extract_cell_nodes(
    plan: Mapping[str, Any],
    *,
    chunk_relation: str,
    branch: str,
    filter_ratio_limit: int,
) -> list[dict[str, Any]]:
    """Every node that reads ``chunk_relation``, plus its compressed children.

    Attribution is by relation name against the chunk resolved from
    ``timescaledb_information.chunks``, never by index name: the other branch of
    the same ``UNION ALL`` reads the other hypertable and must not be mixed in.

    ``compress_hyper_*`` relations are mapped to the measured chunk STRUCTURALLY
    — as descendants of the matched node — rather than through a catalog lookup,
    so the mapping does not depend on a TimescaleDB catalog shape. Without it the
    compressed condition (tasks.md 1.4) would extract nothing and pass for
    nothing, because the compressed access shows up under a relation name the
    chunk allowlist does not contain.

    A ``DecompressChunk`` parent INHERITS criterion 1 from its compressed
    children: in the 2026-09-17 receipt the parent ``_hyper_3_62_chunk`` carries
    every key predicate in its ``Filter`` while the child
    ``compress_hyper_7_104_chunk`` is the node that actually pruned.
    """
    if branch not in BRANCH_IDENTITY_COLUMNS:
        raise ValueError(f"unknown branch {branch!r} (expected one of {sorted(BRANCH_IDENTITY_COLUMNS)})")
    identity_columns = BRANCH_IDENTITY_COLUMNS[branch]
    records: list[dict[str, Any]] = []
    for depth, node in walk_plan(plan):
        if str(node.get("Relation Name") or "") != chunk_relation:
            continue
        records.append(
            _node_record(
                node,
                depth=depth,
                role="chunk",
                identity_columns=identity_columns,
                filter_ratio_limit=filter_ratio_limit,
            )
        )
        for child_depth, child in walk_plan(node, depth):
            if child is node:
                continue
            if not str(child.get("Relation Name") or "").startswith(COMPRESSED_RELATION_PREFIX):
                continue
            records.append(
                _node_record(
                    child,
                    depth=child_depth,
                    role="compressed_child",
                    identity_columns=identity_columns,
                    filter_ratio_limit=filter_ratio_limit,
                )
            )
    inherited = any(
        record["identity_in_access_predicate"] for record in records if record["role"] == "compressed_child"
    )
    for record in records:
        record["criterion_1_pass"] = record["identity_in_access_predicate"] or (
            record["role"] == "chunk" and inherited
        )
        record["criterion_1_inherited"] = (
            record["role"] == "chunk" and inherited and not record["identity_in_access_predicate"]
        )
    return records


def evaluate_cell(
    plan: Mapping[str, Any],
    *,
    chunk_relation: str,
    branch: str,
    filter_ratio_limit: int,
    shared_hit_baseline: int | None = None,
    shared_hit_multiple: int = DEFAULT_SHARED_HIT_MULTIPLE,
    shared_hit_absolute_floor: int = SHARED_HIT_ABSOLUTE_FLOOR,
) -> dict[str, Any]:
    """Judge one cell's fact-reading nodes against design.md's three criteria.

    ``shared_hit_baseline`` is the SAME cell's post-``ANALYZE`` measurement. It
    is ``None`` when no trustworthy baseline exists, in which case criterion 3
    is recorded as ``None`` (not evaluated) rather than silently passing — a
    baseline taken from a plan that itself failed criteria 1 or 2 would be
    inflated and would make criterion 3 unfalsifiable.

    Criterion 3 fails a node only when BOTH bounds are exceeded: the multiple of
    the baseline AND ``shared_hit_absolute_floor``. The 2026-09-18 run measured
    why (matrix.json, base variant): ``latest/absent/legacy/uncompressed`` read
    51 shared hits against a baseline of 8 (limit 64) and PASSED, while
    ``latest/absent/narrow/uncompressed`` read 50 against a baseline of 6
    (limit 48) and FAILED — the same healthy plan shape, the segment key bound in
    the ``Index Cond``, ratio 0.0 in both, and the verdict flipping only on which
    post-``ANALYZE`` integer the baseline happened to land on. The genuine defect
    cells in the same run sat at 5977 and 12841 shared hits against baselines of
    3 and 4. The multiple alone therefore could not tell "the planner picked a
    different but healthy index" from "the node reads the whole network"; the
    floor is what separates them. BOTH verdicts are recorded per node and per
    cell — ``criterion_3_multiple_exceeded`` and ``shared_hit_multiple_observed``
    beside the floored ``criterion_3_pass`` — so an archived run stays re-readable
    under either rule without being re-measured.
    """
    nodes = extract_cell_nodes(
        plan,
        chunk_relation=chunk_relation,
        branch=branch,
        filter_ratio_limit=filter_ratio_limit,
    )
    failures: list[str] = []
    if not nodes:
        failures.append(
            f"EMPTY EXTRACT: no plan node read {chunk_relation}; every criterion below would pass for nothing"
        )
    shared_hit_limit = None if shared_hit_baseline is None else shared_hit_baseline * shared_hit_multiple

    criterion_1: bool | None = all(node["criterion_1_pass"] for node in nodes) if nodes else False
    criterion_2: bool | None = all(not node["breaks_filter_ratio"] for node in nodes) if nodes else False
    for node in nodes:
        if shared_hit_limit is None:
            node["shared_hit_multiple_observed"] = None
            node["criterion_3_multiple_exceeded"] = None
            node["criterion_3_pass"] = None
            continue
        hits = node["shared_hit_blocks"]
        # `max(baseline, 1)`: a post-ANALYZE plan may legitimately measure 0
        # shared hits (an all-read cold cell, or a compressed child that pruned
        # to nothing), and the OBSERVED multiple must stay a readable number
        # rather than a ZeroDivisionError inside the criterion it describes.
        node["shared_hit_multiple_observed"] = hits / max(int(shared_hit_baseline or 0), 1)
        node["criterion_3_multiple_exceeded"] = hits > shared_hit_limit
        node["criterion_3_pass"] = not (hits > shared_hit_limit and hits > shared_hit_absolute_floor)
    if shared_hit_limit is None:
        criterion_3: bool | None = None
        criterion_3_multiple_exceeded: bool | None = None
    else:
        criterion_3 = all(node["criterion_3_pass"] for node in nodes) if nodes else False
        criterion_3_multiple_exceeded = any(node["criterion_3_multiple_exceeded"] for node in nodes) if nodes else True

    for node in nodes:
        label = f"{node['node_type']}({node['relation']}) index={node['index_names'] or None}"
        if not node["criterion_1_pass"]:
            failures.append(
                f"criterion 1 [{label}]: no {'/'.join(BRANCH_IDENTITY_COLUMNS[branch])} in the access predicate "
                f"{node['access_predicate']!r}; filter={node['filter']!r}"
            )
        if node["breaks_filter_ratio"]:
            failures.append(
                f"criterion 2 [{label}]: removed {node['removed_total']} / returned {node['returned_total']} "
                f"= {node['filter_ratio']:.1f} exceeds filter_ratio_limit {filter_ratio_limit}"
            )
        if node.get("criterion_3_pass") is False:
            failures.append(
                f"criterion 3 [{label}]: {node['shared_hit_blocks']} shared hits exceed BOTH "
                f"{shared_hit_multiple} x the post-ANALYZE baseline {shared_hit_baseline} = {shared_hit_limit} "
                f"AND the absolute floor {shared_hit_absolute_floor}"
            )

    return {
        "chunk_relation": chunk_relation,
        "branch": branch,
        "filter_ratio_limit": filter_ratio_limit,
        "shared_hit_baseline": shared_hit_baseline,
        "shared_hit_multiple": shared_hit_multiple,
        "shared_hit_limit": shared_hit_limit,
        "shared_hit_absolute_floor": shared_hit_absolute_floor,
        "node_count": len(nodes),
        "nodes": nodes,
        "node_types": [node["node_type"] for node in nodes],
        "index_names": [name for node in nodes for name in node["index_names"]],
        "worst_filter_ratio": max((node["filter_ratio"] for node in nodes), default=0.0),
        "max_shared_hit_blocks": max((node["shared_hit_blocks"] for node in nodes), default=0),
        "criterion_1_segment_identity_bound": criterion_1,
        "criterion_2_filter_ratio": criterion_2,
        # The FLOORED verdict, and beside it the raw multiple comparison the
        # 2026-09-18 run was judged by, so that run stays re-readable under the
        # new rule without being re-measured.
        "criterion_3_shared_hits": criterion_3,
        "criterion_3_multiple_exceeded": criterion_3_multiple_exceeded,
        "shared_hit_multiple_observed": max(
            (
                node["shared_hit_multiple_observed"]
                for node in nodes
                if node["shared_hit_multiple_observed"] is not None
            ),
            default=None,
        ),
        # DATA, never an assertion: which cells reproduce #2451 is what the
        # per-cell table reports to tasks.md 2.1, and it must stay readable after
        # a candidate makes them all green.
        "defect_reproduced": bool(nodes) and (not criterion_1 or not criterion_2),
        "passed": not failures and criterion_1 is not False and criterion_2 is not False and criterion_3 is not False,
        "failures": failures,
    }


def cell_row(cell: Mapping[str, Any]) -> str:
    """One line of the per-cell result table tasks.md 2.1 consumes.

    ``multiple_exceeded`` is printed beside the floored verdict rather than
    replaced by it: a cell that trips the multiple but not the floor is the
    adjacency the 2026-09-18 run mis-judged, and it must stay VISIBLE in the
    table rather than becoming an invisible pass.
    """
    criteria = "".join(
        {True: "P", False: "F", None: "-"}[cell[key]]
        for key in (
            "criterion_1_segment_identity_bound",
            "criterion_2_filter_ratio",
            "criterion_3_shared_hits",
        )
    )
    label = cell.get("cell_key", cell["chunk_relation"])
    variant = cell.get("variant")
    if variant:
        label = f"{variant}/{label}"
    observed = cell.get("shared_hit_multiple_observed")
    return (
        f"{label}: criteria={criteria} "
        f"nodes={cell['node_count']} types={cell['node_types']} index={cell['index_names']} "
        f"ratio={cell['worst_filter_ratio']:.1f} hits={cell['max_shared_hit_blocks']} "
        f"(baseline={cell['shared_hit_baseline']} limit={cell['shared_hit_limit']} "
        f"floor={cell.get('shared_hit_absolute_floor')} "
        f"observed_multiple={'n/a' if observed is None else format(observed, '.1f')} "
        f"multiple_exceeded={cell.get('criterion_3_multiple_exceeded')}) "
        f"defect_reproduced={cell['defect_reproduced']}"
    )
