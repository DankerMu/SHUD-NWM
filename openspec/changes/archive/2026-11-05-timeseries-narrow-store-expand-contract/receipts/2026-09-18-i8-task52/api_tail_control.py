"""#1987 5.2 control: is the forecast-series tail the QUERY or the SERVER?

/health touches no fact table and issues no forecast query. If its tail matches
forecast-series', the tail is the node's request-service capacity, not this change's
read path -- which is what the regression criterion turns on.
"""
import json
import statistics
import time
import urllib.request

N = 30
out = {}
for name, url in (("health", "http://127.0.0.1:8080/health"),
                  ("runtime_config", "http://127.0.0.1:8080/api/v1/runtime/config")):
    for _ in range(2):
        urllib.request.urlopen(url, timeout=30).read()
    ms = []
    for _ in range(N):
        t0 = time.perf_counter()
        urllib.request.urlopen(url, timeout=30).read()
        ms.append((time.perf_counter() - t0) * 1000)
    ms.sort()
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    out[name] = {"n": N, "min_ms": round(ms[0], 1), "median_ms": round(statistics.median(ms), 1),
                 "p95_ms": round(p95, 1), "max_ms": round(ms[-1], 1),
                 "samples_ms": [round(v, 1) for v in ms]}
    print(f"{name:16s} min={ms[0]:.1f} med={statistics.median(ms):.1f} p95={p95:.1f} max={ms[-1]:.1f} ms")
json.dump(out, open("/home/nwm/tmp/1987/api-tail-control-0918.json", "w"), indent=2)
