# Tasks — node-27 coverage freshness alert (#2080)

Fixture level: expanded · Repair intensity: high

## Change surface

- `scripts/node27_coverage_freshness_alert.py` (new)
- `tests/test_node27_coverage_freshness_alert.py` (new)
- `infra/systemd/nhms-node27-coverage-freshness-alert.service` (new)
- `infra/systemd/nhms-node27-coverage-freshness-alert.timer` (new)
- `docs/runbooks/current-production-ops.md` (new section 11)
- `openspec/specs/display-coverage-freshness/spec.md` (via this change's delta)
- `openspec/project-profile.md` (registers the new alerting-lane risk axis, domain pack and verification-matrix row this fixture consumes)

## Must preserve

- `services/tiles/mvt.py` — read only (`national_discharge_cycles`, `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`); no edit of any kind.
- `packages/common/display_coverage.py` refresh/refusal semantics — unchanged.
- `scripts/node27_autopipeline.py:2196-2209` and `scripts/node27_autopipe_cron.sh:225-231` — unchanged, still non-fatal (design D7).
- `scripts/node27_frontier_stall_alert.py`, its state schema and its units — untouched; its tests stay green.
- `infra/systemd/nhms-node27-unit-failure-alert@.service` — consumed by one new `OnFailure=` line, not edited.

## Seams under test

- `main(argv, *, now=..., observe=..., env=...)` — the public boundary; every scenario runs through it with an injected observation provider, so unit tests need no database and no sendmail.
- `default_observe(config)` is the thin real adapter (SQLAlchemy engine + `national_discharge_cycles`) and is covered by the node-27 live receipt, not by unit tests.

## Implementation tasks

- [x] T1 `default_observe(config)`: run the design D0 ready-frontier statement, then call `national_discharge_cycles(session, source=<key>)` once per non-`__null_source__` key and take `default_cycle`. Engine built with `connect_args={"connect_timeout": 10}`; `SET statement_timeout = 30000` issued on the session. Nothing on the covered side is re-derived.
- [x] T2 Criterion: per-source gap in days, the lookback window filter, the `__null_source__` exclusion (D1), and the constant-derived threshold with the `NHMS_COVERAGE_GAP_DAYS` override (D2).
- [x] T3 Exit contract 0/1/2/3 exactly as design D6, including **zero source keys → exit 3**, with DSN redaction on every operator-visible string.
- [x] T4 Report shaped for the mail body (D3): ≤ `MAX_REPORT_LINES = 24` lines, per-source table with breaching sources first and an explicit `… N more sources omitted` truncation line, `VERDICT:` block printed **last**.
- [x] T5 `infra/systemd/nhms-node27-coverage-freshness-alert.service`: `Type=oneshot`, `EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env`, `Environment=PYTHONPATH=/home/nwm/NWM`, `ExecStart=` the venv interpreter on the script, `OnFailure=nhms-node27-unit-failure-alert@%n.service`, `TimeoutStartSec=300`, and **no** `StandardOutput=append:` (the journal is the mail body).
- [x] T6 `infra/systemd/nhms-node27-coverage-freshness-alert.timer`: `OnCalendar=*-*-* 06:00:00`, `Persistent=true`, `WantedBy=timers.target`.
- [x] T7 `tests/test_node27_coverage_freshness_alert.py` covering every scenario in "Required evidence" below, plus the red proof (new test file run once against pre-change source, failing output pasted).
- [x] T8 `docs/runbooks/current-production-ops.md`: insert the new lane as `## 11.` immediately after
  `## 10. 前沿停摆告警（frontier stall alert）` and renumber the existing `## 11. 相关文档` to `## 12.`
  (fix any in-document cross-reference to it). Content: what the mail means, the two handling branches (coverage refresh stalled vs a river network not producing / newly activated), the exit-3 "nothing observable" branch, the division of labour with `frontier-stalled`, and the `NHMS_COVERAGE_GAP_DAYS` knob with its validity range.
- [x] T9 PR body carries the design D7 adjudication verbatim — both coverage-refresh call sites stay non-fatal, with the rationale and the accepted detection-latency consequence (issue acceptance criterion 7 requires it in the PR body specifically, not only in the runbook).

## Risk packs considered (core)

- Public API / CLI / script entry: **selected** — new operator entrypoint with a meaningful exit contract.
- Config / project setup: **selected** — new systemd units, reused env file, new threshold knob.
- File IO / path safety / overwrite: **not selected** — the lane writes no file at all (journal only) and reads no path.
- Schema / columns / units / field names: **selected** — the gap is a day-valued float over two `timestamptz` instants and `default_cycle` arrives as a formatted string that must be parsed back.
- Auth / permissions / secrets: **selected** — reads a DSN with a password from a 0600 env file; redaction required.
- Concurrency / shared state / ordering: **selected** — deliberately stateless; `national_discharge_cycles`' two-statement read is argued in D0.
- Resource limits / large input / discovery: **selected** — one aggregate statement plus one catalog call per source, and a report that must fit a 30-line mail tail.
- Legacy compatibility / examples: **not selected** — nothing existing changes shape; no example file consumes this lane; the legacy source-less discovery path is an explicit non-goal.
- Error handling / rollback / partial outputs: **selected** — the fail-closed exit contract is the core of D6.
- Release / packaging / dependency compatibility: **not selected** — no new dependency; `sqlalchemy`, `psycopg2` and `services.tiles.mvt` are already installed and in use on node-27.
- Documentation / migration notes: **selected** — a new alert lane an operator must be able to act on.

## Domain packs (all eight from `openspec/project-profile.md`, plus the new lane pack)

| Domain pack | Disposition | Reason / evidence |
|---|---|---|
| Geospatial / CRS / basin geometry | **not selected** | No geometry, CRS or basin shape is read; the lane reads only `cycle_time`, `source_id` and the catalog's `default_cycle`. |
| Hydro-met time series / forcing windows | **not selected** | No forcing series, no forecast horizon and no valid-time arithmetic of its own — the stride/window logic stays inside `national_discharge_cycles` (D0). |
| SHUD numerical runtime / conservation / NaN | **not selected** | No solver, no numerical output; the only float is a day-valued gap, whose non-finite inputs are rejected as config errors (evidence 12). |
| PostGIS / TimescaleDB domain behavior | **selected** | The covered side runs `national_discharge_cycles`' two READ COMMITTED statements; D0 records why that race is acceptable at daily cadence, and the live receipt (evidence L2) runs it against the real hypertable-bearing node-27 database rather than a mock. |
| Slurm production lifecycle / mock-vs-real parity | **not selected** | Nothing in this lane reaches node-22, sbatch or the Slurm gateway. |
| External hydro-met providers / snapshot reproducibility | **not selected** | Provider identity is read only as an opaque lower-cased `source_id` key; no provider snapshot semantics are interpreted. |
| Run manifest / QC provenance | **not selected** | No manifest, receipt or QC artifact is produced or consumed; the lane is stateless and its only evidence is the journal. |
| Published NHMS artifacts / display identity | **selected** | The covered frontier *is* the published `default_cycle` identity. Discharged by construction (D0: the lane calls the catalog owner instead of re-deriving it) and asserted by evidence 20 plus the live receipt L2. |
| Operator alerting lanes / observer-observed predicate parity | **selected** | The governing invariant. Discharged by D0 and asserted by evidence 18/20; the fail-closed zero-source rule (evidence 10b) closes the constructive-silence class. |

## Required evidence

Unit tests (`tests/test_node27_coverage_freshness_alert.py`), each through `main()` with an
injected `observe` and `now`. `D0` means "the injected now, truncated to the cycle".

- [x] 1. `gfs` ready=D0, covered=D0 → exit 0, report shows `gap=0.0`.
- [x] 2. `gfs` ready=D0, covered=D0-5d, threshold 4.0 → exit 1; `VERDICT:` names `gfs` and `5.0`.
- [x] 3. `gfs` ready=D0, covered=D0-3d → exit 0 (below threshold).
- [x] 4. `gfs` ready=D0, covered=D0-4d → exit 0 (exact equality does not trip; strictly greater does).
- [x] 5. Full ingest stall: ready=covered=D0-2d, in window → exit 0.
- [x] 6. Ingest stall beyond the window: ready=covered=D0-20d → `not-evaluated`, exit 0.
- [x] 7. In-window source with `covered=None` → exit 1; report states it has no covered cycle.
- [x] 8. Two sources, `gfs` gap 6d, `ifs` gap 0 → exit 1, only `gfs` in `VERDICT:`, `ifs` still in the table.
- [x] 9. `__null_source__` with a huge gap, in window → `not-evaluated`, exit 0.
- [x] 10a. Source keys exist but every one is outside the window → exit 0, all `not-evaluated`.
- [x] 10b. Observation returns **no source key at all** → exit 3 (fail-closed, design D6), report names the condition.
- [x] 11. `NHMS_COVERAGE_GAP_DAYS=2.5` → a 3-day gap trips (override applied).
- [x] 12. `NHMS_COVERAGE_GAP_DAYS` ∈ {`abc`, `0`, `-1`, `inf`, `nan`, `str(NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS)`} → exit 2 and the injected observation provider is never called. The window-valued case is built from the constant, never the literal `12`, so raising the window cannot silently flip it from refusal to acceptance.
- [x] 13. Missing `DATABASE_URL` → exit 2, observation provider never called.
- [x] 14. Observation raises `RuntimeError("could not connect to postgresql://u:s3cr3t@h/db")` → exit 3 and `s3cr3t` appears nowhere in captured stdout/stderr.
- [x] 15. 25 sources, several breaching → total printed lines ≤ `MAX_REPORT_LINES`, every breaching source present, an explicit omission line present, and `VERDICT:` is the last block.
- [x] 16. `default_cycle` arrives as an ISO string (the shape `national_discharge_cycles` returns) → parsed tz-aware and the gap reported in days (a 5-day gap reports `5.0`, not `120`).
- [x] 17. Threshold default equals `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS / 3` and is strictly less than the constant (guards a future window shrink).
- [x] 18. Ready-frontier statement contract: contains `status IN ('succeeded', 'parsed', 'published')`, `mi.active_flag`, `mi.river_network_version_id IS NOT NULL`, `h.cycle_time IS NOT NULL`, `lower(h.source_id)`, and does **not** reference `run_display_coverage`.
- [x] 19. Rerun determinism: two `main()` calls with the same injected observation give the same exit code and report and create no files under a temp `HOME`.
- [x] 20. Covered-side parity: `default_observe` obtains the covered frontier by calling `services.tiles.mvt.national_discharge_cycles` (asserted with a monkeypatched spy) and uses the returned `default_cycle` unmodified; no coverage predicate is re-derived in this module. This is the one test that reaches into `default_observe`, so it fakes the engine/session for the ready-frontier statement — it is a call-shape assertion, not a database test, and does not contradict "`default_observe` is covered by the live receipt" in Seams under test.

Commands (local):

- [x] L-a `uv run pytest -q tests/test_node27_coverage_freshness_alert.py`
- [x] L-b `uv run pytest -q tests/test_node27_frontier_stall_alert.py` (unchanged sibling lane stays green)
- [x] L-c `uv run ruff check .`
- [x] L-d `openspec validate node27-coverage-freshness-alert --strict --no-interactive`

node-27 live receipt (orchestrator, on the pushed head; `export TMPDIR=/home/nwm/tmp`):

- [x] L1 `cd /home/nwm/NWM && git status --porcelain && git pull --ff-only`.
- [x] L2 Read-only run against the production DSN → exit 0 plus the real per-source table, captured verbatim.
- [x] L3 Scratch database on node-27 seeded to reproduce the closed loop. The seed must satisfy the **whole** listing contract, because the covered side is `national_discharge_cycles` (D0) and `segment_count > 0` alone never reaches exit 0:
  - `core.model_instance`: exactly one row with `active_flag = true` and a non-NULL `river_network_version_id` (one active network keeps `covered == active` trivially satisfiable).
  - `hydro.hydro_run`: rows with `status = 'parsed'`, non-NULL `cycle_time` on whole hours, `source_id = 'gfs'`, `basin_version_id` matching that model instance.
  - `hydro.run_display_coverage` for the *healed* cycles: `segment_count > 0`; `min_lead_time_hours`/`max_lead_time_hours` non-NULL with `lead_count = max - min + 1 > 0`; `river_sample_count = segment_count * lead_count`; `river_valid_time_start`/`river_valid_time_end` non-NULL with `end - start = (lead_count - 1) * 3600` s; `river_valid_time_start` on the same whole-hour phase as `cycle_time` (so `(start - cycle) % 3600 == 0`); and the clamped window must contain at least one instant on the 3 h stride from `cycle_time`.
  - Wall-clock band: the seeded `cycle_time`s must be anchored on `now()`, not on a fixed historical date.
    The newest one has to sit inside `now() - NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS` or the source is
    reported `not-evaluated` and the run exits 0 for the wrong reason; the covered fallback has to be more
    than the threshold behind it. Seed cycles spanning roughly 5-10 days back from `now()`.
  - Gap state: the newest cycles carry **no** coverage row (or a zeroed one) so `default_cycle` falls back by more than the threshold → run → **exit 1**.
  - Healed state: insert conforming coverage rows for the newest cycle → run → **exit 0**.
- [x] L4 Real `OnFailure=` wiring: install both units, `systemd-analyze --user verify` them, point one invocation at the scratch DSN with the design D5 drop-in (`EnvironmentFile=` reset, then the scratch file), `systemctl --user start`, confirm the unit entered `failed` and the mail was delivered (`node27-unit-failure-alert: SENT` in the handler's journal); then heal the scratch coverage rows and `systemctl --user start` **again with the drop-in still in place**, confirming the unit now succeeds — this is the clear→recover half proven inside systemd, not just in a bare shell. Finally remove the drop-in, `daemon-reload`, confirm `systemctl --user show -p EnvironmentFiles` is back to the production env file, and re-run → exit 0.
- [x] L5 Enable the timer and capture `systemctl --user list-timers` showing the next elapse. **Done post-merge** (`master` had to carry the script first): the installed unit ran on the production DSN → exit 0, `gfs`/`ifs` both `gap=0.0d`; `systemctl --user enable --now …timer` created the `timers.target.wants` symlink; next elapse `Sat 2026-09-19 06:00:00 CST`. Scratch database `nhms_issue2080`, the receipt worktree and the scratch env file were then removed; only the two production units remain.

## Verification matrix rows consumed

| Surface | Command | Expected evidence |
|---|---|---|
| Python script + shared-helper import | `uv run pytest -q tests/test_node27_coverage_freshness_alert.py`; `uv run ruff check .` | Evidence 1-20 pass, zero lint findings |
| Unchanged sibling alert lane | `uv run pytest -q tests/test_node27_frontier_stall_alert.py` | Green, proving the frontier lane is untouched |
| OpenSpec contract | `openspec validate node27-coverage-freshness-alert --strict --no-interactive` | Strict-valid change |
| node-27 alerting lane (script + systemd unit) | L1-L5 above | Exit contract on the real DB, delivered mail through the real `OnFailure=` wiring, timer armed — all on the exact pushed head |

## Non-goals

- Changing the lookback window, its anchor, or any tile/catalog SQL.
- Changing coverage refresh semantics, the #1446 refusal guard, or either non-fatal call site (D7).
- Observing the legacy source-less discovery path.
- Dedup/resend state for this lane.
- Alerting on full ingest stalls — owned by `frontier-stalled`.

## Review focus

1. Is the covered frontier really taken from `national_discharge_cycles` (parity by construction, D0), with nothing re-derived on that side?
2. Does the ready-frontier statement use exactly that function's run-set minus the coverage join?
3. Is the wall-clock window filter the minimal exception, and does a real coverage stall stay detectable?
4. Does every failure path raise alerting tendency (D6) — including zero observable sources — with the DSN redacted on all of them?
5. Does the verdict survive `journalctl -n 30` after systemd's own framing lines?
