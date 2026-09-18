"""#1987 5.2 gate judge, 2026-09-18. Walks EVERY fact plan node and judges the four
SQL-level bounds per node. Uses the corrected key names (fact_statements / shared_hit):
the first two comparator versions in this family walked zero nodes and reported
"0 violations" — a vacuous pass. This one prints the node count it judged so a zero
walk is visible as a zero, not as a green."""
import json
import sys

D = json.load(open(sys.argv[1]))
FACT = ("_hyper_9_", "_hyper_3_", "compress_hyper_")
HIT_LIMIT, RATIO_LIMIT, P95_LIMIT = 5000, 10, 300.0

allg = True
for label, c in D["cases"].items():
    if c.get("error"):
        print(f"{label}: ERROR {c['error']}")
        allg = False
        continue
    for st in c["fact_statements"]:
        hits, p95 = st["warm_shared_hit"], st["p95_exec_ms"]
        walked = judged = 0
        bad = []
        for n in st["plan_nodes"]:
            rel = n.get("relation") or ""
            if not rel.startswith(FACT):
                continue
            walked += 1
            cond = n.get("index_cond")
            if cond is None:
                continue            # DecompressChunk / parent: judged through its child
            judged += 1
            rem = int(n.get("rows_removed_by_filter") or 0)
            act = int(n.get("actual_rows") or 0)
            ratio = (rem / act) if act else (float("inf") if rem else 0.0)
            # The legacy hypertable (_hyper_3_* / compress_hyper_7_*) carries no
            # river_segment_key — its segment identity is the text column
            # river_segment_id. Judging it by the narrow column name alone is what
            # turned this gate red on the first pass.
            seg = ("river_segment_key" in cond) or ("river_segment_id" in cond)
            if not seg:
                bad.append((rel, n.get("index"), "NO_SEGMENT_IDENTITY_IN_INDEX_COND", cond))
            if ratio > RATIO_LIMIT:
                bad.append((rel, n.get("index"), f"RATIO {ratio:.1f}", f"{rem} removed / {act} rows"))
        ok = not bad and hits <= HIT_LIMIT and p95 <= P95_LIMIT
        allg &= ok
        print(f"{'PASS' if ok else 'FAIL'} {label}: hit={hits} p95={p95:.3f}ms "
              f"fact_nodes={walked} index_nodes_judged={judged} rows={st['rows_returned']} "
              f"digest={st['digest']}")
        if judged == 0:
            print("      !! zero index nodes judged — treat as NOT a pass")
            allg = False
        for n in st["plan_nodes"]:
            rel = n.get("relation") or ""
            if rel.startswith(FACT) and n.get("index_cond"):
                rem = int(n.get("rows_removed_by_filter") or 0)
                act = int(n.get("actual_rows") or 0)
                print(f"      node {rel:28s} idx={(n.get('index') or '')[:46]:46s} "
                      f"rows={act} removed={rem} hit={n.get('shared_hit')}")
        for b in bad:
            print(f"      VIOLATION {b[0]} idx={b[1]} {b[2]} :: {b[3]}")
print("\nSQL GATE:", "GREEN" if allg else "RED")
