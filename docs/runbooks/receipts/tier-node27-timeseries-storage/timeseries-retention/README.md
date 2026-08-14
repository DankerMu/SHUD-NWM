# Retention live receipts (task §6.3)

This directory holds committed live receipts from `scripts/node27_timeseries_retention.py` on node-27's primary Postgres (`127.0.0.1:55432`).

## Receipt schema versions (dual-version reading)

Every receipt committed here so far declares `schema_version: "1.0"`. The
emitter moved to `1.1` in #1369, which added the required top-level
`archive_gate` object; the `1.0` files in this directory are **not**
back-filled and stay byte-unchanged as the historical record (the schema
file itself only validates `1.1`, so validate an old receipt against the
schema revision that produced it — `git log schemas/timeseries_retention_receipt.schema.json`).

Reading rule:

- **No `archive_gate` field / `schema_version: "1.0"`** — produced under the
  pre-#1369 hard gate, i.e. both archive receipts were loaded and judged.
  Everything the receipt says about candidates, deferrals and
  `salvage_backed_windows` is archive-backed.
- **`schema_version: "1.1"` with `archive_gate.mode = "enabled"`** — same
  fail-closed semantics, now stated explicitly.
- **`schema_version: "1.1"` with `archive_gate.mode = "disabled"`** — the
  archive gates were skipped under
  `docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11`
  (carried verbatim in `adr_reference`). Such a receipt records an
  irreversible deletion with no archive backstop: `salvage_backed_windows`
  is always `[]`, and boundary-partial chunks are candidates rather than
  deferrals (runbook §8.5).

## Receipts

### `refusal-completeness-missing-20260713T030936Z.json`

First live invocation of the retention runner on node-27, executed
under the `nwm` user against the `nhms` database in container
`nhms-db`. The env pointed the completeness receipt at a path that
does not exist because the upstream storage-inventory-audit runner
(task §2.3, issue #849) has not landed its first live completeness
receipt on node-27 yet — the audit systemd timer is not enabled.

Result: `outcome=refused`, `refusal_reason=COMPLETENESS_RECEIPT_MISSING`,
exit code 1, `mode=enforce`. Schema-valid per
`schemas/timeseries_retention_receipt.schema.json` (`oneOf` refused
branch). This satisfies §6.3 test row 1 ("Missing or stale completeness
receipt … Expected: refusal, non-zero exit, reason in the receipt").

### `dry-run-first-live-20260725T054745Z.json`

First gates-passing dry-run (2026-07-25, Step B of issue #1071). Both
gate receipts were live and fresh: the recurring audit's completeness
receipt (generated `2026-07-25T03:40Z`, age ~2h < 26h bound) and the
drill's first live PASS receipt
(`../archive-rebuild-drill/first-live-pass-20260725T053420Z.json`).

Configuration: `WINDOW_DAYS=21` (cutoff `2026-06-24T12Z` against
watermark reference_time `2026-07-15T12Z`), `PER_TICK_BOUND=5`,
`ENFORCE=0`. **Why 21 and not the spec's planned 14**: a 14-day probe
(cutoff `2026-07-01T12Z`) was correctly refused with
`COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW` — 39 runs-lane subjects
in cycles from `2026-06-20T12Z` through `2026-06-25` were still pending-archive (mover
frontier at 2026-06-20), so the wider window keeps the drop window
`[2026-06-04, 2026-06-18]` clear of the pending frontier. That refusal
is the gate working as specified, not a bypass.

Result: `outcome=dry-run`, rc=0. Four candidate chunks
(`_hyper_3_8`, `_hyper_3_1`, `_hyper_1_6`, `_hyper_1_3` — both detail
hypertables represented) and two boundary-partial chunks correctly held
in `deferred_remainder` (`_hyper_3_7`, `_hyper_1_5`). No chunk was
dropped (dry-run).

### `refusal-drop-contention-20260725T055600Z.json`

First enforce attempt (Step C, #1072), run while live autopipe ingest
(6 workers) was writing. Both gates passed and the runner entered the
DROP phase, but `drop_chunks('_hyper_3_8_chunk')` waited the full 300s
`_DROP_TIMEOUT_MS` on a tuple lock in TimescaleDB's `dimension_slice`
catalog (held by concurrent chunk-routing inserts) and was canceled by
the statement timeout. Outcome: `refused`,
`refusal_reason=RETENTION_DROP_FAILED:...statement timeout`, exit 1,
**zero chunks dropped** — live proof the DROP phase fails safe under
contention. Remediation: stop `nhms-node27-autopipe.timer`, let the
in-flight pass finish (~10 min), enforce in the quiet window, restart
the timer.

### `first-enforce-20260725T061740Z.json`

First live enforce (Step C, #1072; human go recorded on the issue).
Executed 2026-07-25T06:17Z in a quiet window (autopipe timer stopped,
zero active writers), single tick, `--enforce` CLI flag (env stays
`ENFORCE=0`). Outcome: `enforced`, rc=0. Dropped all 4 candidates from
the dry-run; both boundary-partial chunks stayed in
`deferred_remainder`; 87 db-export-provenance windows recorded in
`salvage_backed_windows`.

#### Pre-enforce checklist (precondition receipts)

- Completeness receipt: generated `2026-07-25T03:40Z` (age ~2.5h < 26h
  bound), from the recurring audit timer.
- Drill PASS receipt: `../archive-rebuild-drill/first-live-pass-20260725T053420Z.json`
  (age <1h < 30d bound), coverage spans the drop window.
- Dry-run receipt reviewed: `dry-run-first-live-20260725T054745Z.json`
  (4 candidates = the 4 dropped; PER_TICK_BOUND=5 not binding).
- Schema-only catalog snapshot:
  `~/nwm-presync-backup-20260725/nhms-schema-pre-enforce-20260725T055544Z.sql`
  on node-27 (outside the repo per sync discipline).

#### Before/after evidence (2026-07-25, UTC)

| Metric | Before (05:5xZ) | After (06:2xZ) | Δ |
| --- | --- | --- | --- |
| `hypertable_size('hydro.river_timeseries')` | 658,673,786,880 | 657,517,953,024 | −1,155,833,856 |
| `hypertable_size('met.forcing_station_timeseries')` | 95,055,085,568 | 77,113,745,408 | −17,941,340,160 |
| `pg_database_size('nhms')` | 754,661,249,839 | 735,564,075,823 | **−19,097,174,016 (~17.8 GiB)** |
| chunks (hydro / met) | 8 / 8 | 6 / 6 | −2 / −2 |
| `hydro.hydro_run` rows | 1,557 | 1,557 | 0 |
| `hydro.run_display_coverage` rows | 1,557 | 1,557 | 0 |
| `met.forcing_version` rows | 1,557 | 1,557 | 0 |
| `hydro.state_snapshot` rows | 0 | 0 | 0 |
| `ops.qc_result` rows | 1,610 | 1,610 | 0 |

Metadata row-count invariant (§8.3 test row 4): **unchanged** across
all five tables. The four dropped chunks are absent from
`timescaledb_information.chunks` post-enforce. Display API answered
200 immediately after; the autopipe timer was restarted in the same
minute.

The DB size delta (−19,097,174,016 B) equals the pre-enforce
`chunks_detailed_size` sum of the four candidates exactly
(535,822,336 + 620,011,520 + 538,116,096 + 17,403,224,064).

#### Known receipt limitation: `freed_bytes` under-reports compressed chunks

The receipt's per-chunk `freed_bytes` sum is 17,403,371,520 —
1,693,802,496 bytes (~1.7 GB) less than the true reclaim. For the three **compressed**
candidates the runner measured only the main chunk relation (57,344 /
57,344 / 32,768 bytes); the actual bytes live in the compressed
sibling relation that `drop_chunks` also removes. The uncompressed
candidate (`_hyper_1_3`, 17,403,224,064) is accurate. Tracked as a
follow-up issue (receipt-accuracy only; the drop itself and the H4
"measured before drop" ordering are correct).

**Resolved by #1125.** The runner now measures each chunk with
`SELECT total_bytes FROM chunks_detailed_size(<hypertable>::regclass)
WHERE chunk_schema = ... AND chunk_name = ...`, which includes the
compressed sibling relation, so future receipts report
compression-inclusive `freed_bytes`. H4 ordering, the per-chunk
isolated connection, and the best-effort "failure → 0 → continue"
semantics are unchanged. The 2026-07-25 numbers above are **not**
rewritten — they remain the immutable record of what that run
measured.

#### Reversibility footnote

`drop_chunks` is not per-chunk reversible. The tested recovery oracle
for the dropped window is Step B's drill PASS receipt
(`../archive-rebuild-drill/first-live-pass-20260725T053420Z.json`):
runs-lane reingest proven on a fresh staging DB at exact count parity,
db-export salvage objects sha256+row-count verified. The salvage
`COPY FROM` and raw-source replay paths remain never-executed against
production (see #1072's reversibility warning).

## Steady-state gate behavior

- `nhms-node27-timeseries-retention.timer` (OnCalendar 05:15 UTC
  daily) was **disabled** through the runs recorded above — enabling it was
  a separate operator decision, deferred until the operator had observed
  manual runs; the enforce path also needs env `ENFORCE=1`, which stayed
  `0`. That decision was taken on 2026-08-14 (issue #1369): the timer is
  enabled at the same 05:15 UTC cadence with the archive gate `disabled`,
  after a manual dry-run and a bounded manual enforce. The bullets below
  describe the `enabled`-gate steady state and no longer apply once the
  gate is `disabled` (there are no gate receipts to re-evaluate).
- Each tick re-evaluates both gates: the completeness receipt must be
  <26h old (recurring audit timer refreshes daily) and the drill PASS
  receipt <30d old with coverage spanning the tick's drop window.
- A drill re-run is required when the PASS receipt ages past 30 days,
  when archive tooling or formats change, or when the drop window
  advances beyond the receipt's declared coverage.
- Under live ingest, a DROP-phase statement timeout on
  `dimension_slice` contention is possible (see the refusal receipt
  above); the runner fails safe and the tick can be retried in a quiet
  window.

## Reproduction

Env file at `/home/nwm/NWM/infra/env/node27-timeseries-retention.env`
(mode 0600). Invocation from node-27, worktree at
`/home/nwm/NWM-tier`:

```bash
set -a && . /home/nwm/NWM/infra/env/node27-timeseries-retention.env && set +a
RECEIPT="/home/nwm/node27-timeseries-retention-logs/retention-dryrun-$(date -u +%Y%m%dT%H%M%SZ).json"
cd /home/nwm/NWM-tier
NODE27_TIMESERIES_RETENTION_ENFORCE=0 \
  /home/nwm/.local/bin/uv run --frozen python scripts/node27_timeseries_retention.py \
    --dry-run --receipt-path "$RECEIPT"
# rc=1, refusal_reason=COMPLETENESS_RECEIPT_MISSING
```

Two things this block is not being cute about:

- **The `--receipt-path` is mandatory.** The deployed env file leaves
  `NODE27_TIMESERIES_RETENTION_RECEIPT_PATH` unset so the wrapper can write a
  per-tick timestamped receipt (a fixed path would be overwritten by every
  daily tick). A direct `python` invocation does not get that substitution, so
  without an explicit path it aborts with `RETENTION_CONFIG_INVALID`, exit 2,
  and no receipt. Use a timestamped filename so a manual run never clobbers a
  timer tick's receipt.
- **The `NODE27_TIMESERIES_RETENTION_ENFORCE=0` prefix is mandatory too.** The
  `--dry-run` flag does NOT override the env — dry-run vs enforce is decided
  solely by `--enforce` / the env variable. With `ENFORCE=1` resident in the
  deployed env file (the steady state since the timer was enabled), an
  unprefixed run enforces and irreversibly drops up to
  `NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND` chunks. The inline assignment is
  placed after the `source`, so it wins.

Runner invocation used `--dry-run` CLI flag; refused receipts always
carry `mode=enforce` per schema `oneOf` pin (documented in runbook
§8.5 and design.md #855 fixture block; behavior-lock test at
`tests/test_node27_timeseries_retention.py::test_dry_run_evaluates_gates_before_dryrun_branch`).
