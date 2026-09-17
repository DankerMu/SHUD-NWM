import json, sys
d = json.load(open(sys.argv[1]))
for label in ("shj_nj/latest", "small_tailanhe/latest"):
    c = d["cases"][label]
    print("###", label, "total_hit=", c["total_shared_hit"])
    for i, s in enumerate(c["fact_statements"]):
        print(f"  stmt{i}: hit={s['warm_shared_hit']} p95={s['p95_exec_ms']:.3f} rows={s['rows_returned']} digest={s['digest']}")
        print(f"     head: {s['sql_head'][:120]}")
        for n in s["plan_nodes"]:
            rel = n.get("relation") or ""
            if not (rel.startswith("_hyper_9_") or rel.startswith("compress_hyper_10")):
                continue
            rem = n.get("rows_removed_by_filter") or 0
            act = n.get("actual_rows") or 0
            ratio = (rem / act) if act else 0.0
            idx = n.get("index") or ""
            seg = "river_segment_key" in (n.get("index_cond") or "")
            print("       %-24s idx=%-44s seg_in_cond=%-5s removed=%-8s actual=%-6s ratio=%8.1f hit=%s"
                  % (rel, idx[-44:], seg, rem, act, ratio, n.get("shared_hit")))
