"""Evaluate the #1987 5.2 EXPLAIN gate against the probe bundle."""

import json
import sys

d = json.load(open(sys.argv[1]))
print(f"checkout={d['checkout']} warm_rounds={d['warm_rounds']}\n")

for label, c in d["cases"].items():
    if c.get("error"):
        print(f"### {label}: ERROR {c['error']}")
        continue
    print(f"### {label}")
    print(f"    run={c['run_id']} run_key={c['run_key']} cycle={c['cycle_time']} store={c['store']}")
    for s in c["fact_statements"]:
        nodes = s["plan_nodes"]
        removed = sum(n.get("rows_removed_by_filter", 0) or 0 for n in nodes)
        returned = s["rows_returned"] or 1
        seg_cond = [
            n for n in nodes
            if "river_segment_key" in (n.get("index_cond") or "")
        ]
        seg_filter = [
            n for n in nodes
            if "river_segment_key" in (n.get("filter") or "")
        ]
        runkey_cond = [
            n for n in nodes
            if "run_key" in (n.get("index_cond") or "") or "run_key" in (n.get("filter") or "")
        ]
        decompress = [n for n in nodes if "Decompress" in (n.get("node") or "")]
        seqscan = [
            n for n in nodes
            if n.get("node") == "Seq Scan"
            and (n.get("relation") or "").startswith("_hyper")
        ]
        narrow_chunks = sorted({
            n.get("relation") for n in nodes
            if (n.get("relation") or "").startswith("_hyper_9_")
        })
        legacy_chunks = sorted({
            n.get("relation") for n in nodes
            if (n.get("relation") or "").startswith("_hyper_3_")
        })
        print(f"    shared_hit={s['warm_shared_hit']}  (bound 5000)  "
              f"p95={s['p95_exec_ms']:.3f} ms  (bound 300)")
        print(f"    rows_returned={s['rows_returned']}  rows_removed_by_filter={removed}  "
              f"ratio={removed/returned:.3f}  (bound 10)")
        print(f"    river_segment_key in Index Cond: {len(seg_cond)} node(s) "
              f"{[n.get('relation') or n.get('index') for n in seg_cond][:6]}")
        print(f"    river_segment_key in Filter only: {len(seg_filter)} node(s)")
        print(f"    run_key referenced in cond/filter: {len(runkey_cond)} node(s)")
        print(f"    Decompress nodes: {len(decompress)} {[n.get('relation') for n in decompress]}")
        print(f"    Seq Scan on chunk: {len(seqscan)} {[n.get('relation') for n in seqscan]}")
        print(f"    narrow chunks touched: {narrow_chunks}")
        print(f"    legacy chunks touched: {legacy_chunks}")
        print(f"    exec_ms samples: {[round(x['exec_ms'],3) for x in s['samples']]}")
        # show the nodes that actually read a fact chunk
        for n in nodes:
            rel = n.get("relation") or ""
            if rel.startswith("_hyper") or rel.startswith("compress_hyper"):
                print(f"      - {n.get('node')} on {rel} idx={n.get('index')}")
                if n.get("index_cond"):
                    print(f"          Index Cond: {n['index_cond'][:220]}")
                if n.get("filter"):
                    print(f"          Filter: {n['filter'][:220]}")
                if n.get("rows_removed_by_filter") is not None:
                    print(f"          Rows Removed by Filter: {n['rows_removed_by_filter']}"
                          f"  actual_rows={n.get('actual_rows')}")
    print()
