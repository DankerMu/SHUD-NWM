import json
from pathlib import Path
D = Path("/home/nwm/tmp/2451/live-ab")
print(f"{'case':26s} {'stmt':5s} {'arm':5s} {'hits':>9s} {'p95_ms':>10s} {'rows':>6s}  digest")
for probe in ("probe1987", "probe1987latest"):
    js = {a: json.loads((D / f"{probe}-{a}.json").read_text()) for a in ("base", "head")}
    for case in js["base"]["cases"]:
        fb = js["base"]["cases"][case].get("fact_statements") or []
        fh = js["head"]["cases"][case].get("fact_statements") or []
        for i, (b, h) in enumerate(zip(fb, fh)):
            for arm, s in (("base", b), ("head", h)):
                print(f"{case:26s} {i:<5d} {arm:5s} {s['warm_shared_hit']:9d} {s['p95_exec_ms']:10.3f} "
                      f"{s['rows_returned']:6d}  {s['digest']}")
            d = h['warm_shared_hit'] - b['warm_shared_hit']
            pct = (h['p95_exec_ms'] - b['p95_exec_ms']) / b['p95_exec_ms'] * 100 if b['p95_exec_ms'] else 0
            flag = "" if (d == 0 and abs(pct) < 15) else "   <-- DELTA"
            print(f"{'':26s} {'':5s} {'diff':5s} {d:9d} {pct:9.1f}% {'':6s}  "
                  f"{'same' if b['digest']==h['digest'] else 'DIGEST DIFF'}{flag}")
