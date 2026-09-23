# node-27 receipt — #2529 root-cause discrimination for the 2026-09-19 parse 57014 burst

- Capture: node-27 (`host=ghdc user=nwm`), read-only, by the batch-G1 orchestrator on
  2026-09-23 (`captured_at 2026-09-23T08:58:01Z` for the root-cause file; the backlog probe
  was run at 09:08Z per the design record). Committed verbatim beside this file:
  [`rootcause-raw.txt`](rootcause-raw.txt) (md5 `743e9172a79931ceb04a986e1f55e456`) and
  [`backlog-probe.txt`](backlog-probe.txt) (md5 `a0d1a0758ecd50034d9f5532a2a42081`).
- Sources the capture reads (per its own section headers): `awk`/`grep` over
  `/home/nwm/autopipe-logs/autopipe.log` (§1, §2), `journalctl --user` for the retention and
  compression units (§3, §3b/4b, §4), `systemctl --user show` timer properties (§5), a key
  grep over the live `infra/env/node27-ingest.env` plus the parser default (§6), `git log -1`
  of the deployed checkout (§7). The backlog probe is one read-only aggregate over
  `hydro.hydro_run` (failed `OUTPUT_PARSE_*` rows).
- **Limit:** the handed raw files carry the section headers and outputs, not the literal
  command lines; nothing below is reconstructed beyond what the outputs show.
- **Zero writes**: no unit started/stopped, no env edited, no DB write.
- Deployed checkout at capture (§7): `8371024a Merge pull request #2554 from DankerMu/fix/issue-2550-china-fit-view`.

## 1. The two rc=1 ticks (autopipe.log, §1/§2)

Verbatim from §1 (line numbers are `autopipe.log` line numbers):

```
4693205:[2026-09-19T05:55:40Z] autopipe: start
4694787:[2026-09-19T06:30:56Z] autopipe: done rc=1 elapsed_sec=2116
4694788:[2026-09-19T06:30:56Z] autopipe: start
4695679:[2026-09-19T06:53:36Z] autopipe: done rc=1 elapsed_sec=1360
4695680:[2026-09-19T06:53:36Z] autopipe: start
4696490:[2026-09-19T07:02:41Z] autopipe: done rc=0 elapsed_sec=545
```

- **tick #16** `05:55:40Z → 06:30:56Z`, **tick #17** `06:30:56Z → 06:53:36Z`, tick #18 heals
  (`rc=0`). Every other tick in the 05:00–08:00Z capture is `rc=0`.
- §2 error tally inside `05:55:40Z..06:53:36Z`:
  `22 OUTPUT_PARSE_DB_ERROR: Output parser database operation failed: canceling statement due to statement timeout`
  and `11 "outcome": "failed"` — 11 failed runs, each error string printed twice
  (`runs.details[]` and `runs.failed_runs[]`, #2529 body).
- Per-tick split (from the 2026-09-20 receipt
  `docs/runbooks/receipts/2026-09-20-node27-publish-tick-and-basemap-origin/README.md` §3,
  same log): tick #16 `processed=38 failed=7`, tick #17 `processed=7 failed=4` (the 7 re-queued).

## 2. Retention and compression units on 2026-09-19 (journal, UTC; §3, §3b/4b, §4)

| unit | Starting | end line | outcome |
|---|---|---|---|
| `nhms-node27-timeseries-retention` | `06:36:08` | `06:41:43` `Failed with result 'exit-code'` | `{"mode": "enforce", "outcome": "refused", "refusal_reason": "RETENTION_DROP_FAILED:hydro._hyper_9_150_chunk: lock-contention(55P03): canceling statement due to lock timeout\n"}`; per-chunk `_hyper_9_150_chunk` `elapsed_ms: 334925.215`, `outcome: failed` |
| `nhms-node27-timeseries-retention` | `08:54:21` | `08:54:21` `Finished` | `{"mode": "enforce", "outcome": "enforced"}` |
| `nhms-node27-timeseries-compression` | `00:57:32` | `02:02:32` `Failed with result 'exit-code'` | — |
| `nhms-node27-timeseries-compression` | `02:10:00` | `02:33:35` `Finished` | — |
| `nhms-node27-timeseries-compression` | `04:25:32` | `05:03:57` `Finished` | — |

(Start/end pairing is by order within each unit's journal: each `Starting` is followed by the
next `Finished`/`Failed` of the same unit.)

§5 timer properties at capture, in capture order: `NextElapseUSecRealtime=Thu 2026-09-24 14:36:00 CST` /
`LastTriggerUSec=Wed 2026-09-23 14:36:00 CST` (14:36 CST = 06:36 UTC, the retention calendar) and
`NextElapseUSecRealtime=Thu 2026-09-24 12:25:00 CST` / `LastTriggerUSec=Wed 2026-09-23 12:25:32 CST`
(12:25 CST = 04:25 UTC, the compression calendar). The unit names are not printed in §5; the
mapping is by calendar.

## 3. Live parse budget and worker count (§6)

```
OUTPUT_PARSER_DB_STATEMENT_TIMEOUT_MS: <absent in node27-ingest.env>
AUTOPIPE_RUN_WORKERS: AUTOPIPE_RUN_WORKERS=6
36:DEFAULT_DB_STATEMENT_TIMEOUT_MS = 60_000
```

The key is absent, so the parser runs at its code default of **60 000 ms**
(`workers/output_parser/parser.py:36`). `AUTOPIPE_RUN_WORKERS=6` is the value at capture
(2026-09-23); the file's value on 2026-09-19 is not in the capture.

## 4. Verdict per hypothesis

- **H1 — retention/compression overlap: holds for tick #17 only, EXCLUDED as the sole cause.**
  Retention ran `06:36:08–06:41:43Z`, inside tick #17 (`06:30:56–06:53:36Z`), and was itself
  `refused` with 55P03 lock contention on `_hyper_9_150_chunk` after waiting 334.9 s. Tick #16
  (`05:55:40–06:30:56Z`, 7 of the 11 failures) overlapped neither retention (first start
  06:36:08Z) nor compression (last run finished 05:03:57Z).
- **H2 — 60 s parse budget: precondition holds, OPEN.** `OUTPUT_PARSER_DB_STATEMENT_TIMEOUT_MS`
  is absent from `node27-ingest.env`, so the replace-chain runs under the 60 000 ms default —
  the tightest of the tick's three budgets (#2529 body: forcing handoff 600 s, stats guard 120 s).
- **H3 — ingest self-contention: precondition holds, OPEN.** `AUTOPIPE_RUN_WORKERS=6` at capture;
  six workers write the same hypertable concurrently. Not discriminated against same-scale
  successful ticks (their worker count is not in the capture).
- **Confounder:** the manual read-only probe at ~06:36Z recorded in the #2355 comment, which
  that comment names as the AccessShareLock holder behind retention's 55P03 refusal; it sits in
  tick #17's window as well.

## 5. Oracle-blocked: AC1 "replace-chain measured duration"

#2529 AC1 asks for the failed runs' replace-chain statement durations. That cannot be reproduced
post hoc: the 11 runs have since re-parsed (tick #18 onward) and PostgreSQL retains neither the
cancelled statements' timings nor the lock history. **Capture method for the next occurrence**
(`docs/runbooks/production-ops/parse-failure-residency-alert.md` §13.3): the residency observer's
report names the resident runs and their first-failing time; while such a tick is still running,
take the read-only `pg_stat_activity` snapshot given there (`state`, `wait_event_type`,
`wait_event`, `now() - query_start`, query head) together with the retention/compression journal
for the same window.

## 6. Backlog probe (read-only, `backlog-probe.txt`)

```
error_code|count|min|max|min|max
OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED|2|2026-08-28 08:42:56.635348+00|2026-08-28 08:42:56.636219+00|2026-08-28 08:29:17.20341+00|2026-08-28 08:32:35.113427+00
(1 row)
touched_1d|touched_7d|total
0|0|2
(1 row)
```

Two failed `OUTPUT_PARSE_*` rows exist, both `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`, last
touched 2026-08-28, none touched in 7 days. They lie outside the residency observer's 6 h
retry-liveness bound and do not alert (design D5, §Stated consequences); clearing them is the
operator action in the §13 runbook, not an alert.
