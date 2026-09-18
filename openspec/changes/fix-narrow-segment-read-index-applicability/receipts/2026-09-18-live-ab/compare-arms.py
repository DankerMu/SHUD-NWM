import json
from pathlib import Path
D = Path("/home/nwm/tmp/2451/live-ab")
FACT = ("_hyper_9_", "_hyper_3_", "compress_hyper_")

for probe in ("probe1987", "probe1987latest"):
    print("=" * 80); print(probe)
    per = {}
    for arm in ("base", "head"):
        js = json.loads((D / f"{probe}-{arm}.json").read_text())
        rows, dig, walked, idxnodes = [], {}, 0, 0
        for case, p in js["cases"].items():
            dig[case] = [s["digest"] for s in p.get("fact_statements") or []]
            for st in p.get("fact_statements") or []:
                for n in st["plan_nodes"]:
                    rel = n.get("relation", "")
                    if not rel.startswith(FACT):
                        continue
                    walked += 1
                    if "index_cond" not in n:
                        continue          # DecompressChunk / seq parent: judged via its child
                    idxnodes += 1
                    cond = n.get("index_cond") or ""
                    rem = int(n.get("rows_removed_by_filter") or 0)
                    act = int(n.get("actual_rows") or 0)
                    ratio = (rem / act) if act else (float("inf") if rem else 0.0)
                    seg = ("river_segment_key" in cond) or ("river_segment_id" in cond)
                    rows.append((case, rel, ratio, seg, n.get("shared_hit"), n.get("index_name", "")))
        per[arm] = (rows, dig, walked, idxnodes)
        bad = [r for r in rows if r[2] > 10 or not r[3]]
        print(f"  [{arm}] fact nodes={walked}, index-scan nodes judged={idxnodes}, violations={len(bad)}")
        for case, rel, ratio, seg, hits, idx in sorted(bad, key=lambda r: -r[2]):
            print(f"      VIOLATION {case} {rel} ratio={ratio:.1f} seg={seg} hits={hits} idx={idx}")
    # arm-to-arm deltas on the uncompressed narrow nodes
    bb = {(c, r): (ra, s, h) for c, r, ra, s, h, _ in per["base"][0] if r.startswith("_hyper_9_")}
    hh = {(c, r): (ra, s, h) for c, r, ra, s, h, _ in per["head"][0] if r.startswith("_hyper_9_")}
    print("  narrow nodes, base -> head:")
    for k in sorted(set(bb) | set(hh)):
        print(f"      {k[0]:24s} {k[1]:20s} base={bb.get(k)}  head={hh.get(k)}")
    same = all(per["base"][1][c] == per["head"][1].get(c) for c in per["base"][1])
    print("  digests identical across arms:", same)
