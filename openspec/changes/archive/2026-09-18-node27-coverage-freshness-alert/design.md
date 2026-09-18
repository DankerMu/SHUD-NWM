# Design: node-27 coverage freshness alert (#2080)

## D0 — Parity by construction: the covered frontier IS the catalog's `default_cycle`

The first draft of this design re-derived the "covered" predicate in SQL as
`rdc.segment_count > 0` plus the active-network set rule. The fixture review showed that
predicate is **weaker than what actually lights the layer**: `national_discharge_cycles`
(`services/tiles/mvt.py:2115`) drops a cycle again when `_national_cycle_valid_times`
returns empty, which happens whenever any row fails `_national_coverage_window`
(`services/tiles/mvt.py:2447`) — NULL `river_valid_time_start/end` or
`min/max_lead_time_hours`, `river_sample_count != segment_count * lead_count`, a
rectangle whose span is not `(lead_count - 1) * 3600` s — or whenever the hourly grid is
off the 3 h phase from the cycle, or the clamped cross-network intersection holds no
stride instant. A populated-but-inconsistent coverage row would therefore have made the
alerter report `gap = 0` while the layer was already dark. Ironically the drift risk the
issue names (#1983's narrow-store river leg) touches exactly those columns, not
`segment_count`.

**Therefore the alerter does not re-derive the predicate at all.** It calls the display
module's own owner of the answer:

```python
covered = national_discharge_cycles(session, source=<lowercased source key>)["default_cycle"]
```

`default_cycle` is literally the field `/api/v1/layers` publishes and the field that goes
`null` when the national layer goes dark (`apps/api/routes/hydro_display.py:1150-1156`).
Parity is by construction: every predicate, the `covered == active` network set rule, the
window-consistency checks, the 3 h stride and the `now() - NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`
bound are all applied by the same code path the API uses, so no drift is possible — the
observer cannot disagree with the observed surface because it *is* the observed surface.

Cost accepted for that parity:

- The lane imports `services.tiles.mvt` and needs a SQLAlchemy `Session`. This is a
  display-side import in a node-27 ops script; it touches neither `apps/api` nor
  `apps/frontend`, so the ADR 0001 display carve-out the frontier lane observes is
  respected.
- `national_discharge_cycles` reads coverage rows and the active-network set in two
  statements under READ COMMITTED (`_national_discharge_coverage_rows` docstring). The
  race that matters for a served request is irrelevant for a once-a-day observer: a
  mis-snapshotted tick can only produce one spurious mail or one delayed one, and the
  next tick re-observes.
- `now()` inside `national_discharge_cycles` is not injectable. Unit tests therefore
  inject at a higher seam (`observe`), and the real adapter is covered by the node-27
  live receipt.

### The other half: the ready frontier

The ready frontier is what the display *would* offer if coverage were perfect, so it must
use the same run-set with the coverage join removed. There is no existing helper for it,
so it is the lane's one re-derived statement — deliberately small, and asserted in tests
against the predicate list below:

```sql
SELECT COALESCE(lower(h.source_id), '__null_source__') AS source_key,
       max(h.cycle_time) AS ready_frontier
FROM hydro.hydro_run h
JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
WHERE h.status IN ('succeeded', 'parsed', 'published')
  AND h.cycle_time IS NOT NULL
  AND mi.river_network_version_id IS NOT NULL
  AND mi.active_flag
GROUP BY 1
```

`lower(h.source_id)`: production stores `gfs` and `IFS`, and the display path matches
`lower(h.source_id)`, so the alerter's source keys must be lower-cased or it would split a
source the display treats as one.

**Governing invariant:** the covered frontier the alerter compares against is the value
`national_discharge_cycles` itself returns for that source, and the ready frontier uses
that function's run-set minus its coverage join. A `gap = 0` verdict therefore means the
national catalog's newest listed cycle is the newest cycle ingest has produced.

## D1 — Criterion: relational gap, not wall-clock lag

For each source key the lane computes `gap = ready_frontier - covered_frontier` and alerts
when `gap > threshold`, or when the source has no `default_cycle` at all (the layer is
already dark for it).

- **Full ingest stall** freezes both frontiers together, `gap` stays where it was, and this
  lane stays silent — owned by `frontier-stalled` (`scripts/node27_frontier_stall_alert.py`).
- **Coverage stall** advances only the ready frontier, so `gap` grows monotonically and
  trips days before the covered frontier ages out of the lookback window.
- The criterion is **outcome-based, never rc-based**: `scripts/node27_autopipeline.py:2196-2209`
  records a legitimate rc=0 `no_coverage_row` path, and a scan returning zero rows for
  upstream reasons leaves every rc channel silent. Only the frontier gap sees it.

### What else this criterion legitimately catches (recorded, not a bug)

Because the covered frontier is the catalog's own answer, the gap also grows when the
cause is **not** the refresh script: a single active river network stops producing runs for
recent cycles, or a network is newly activated with no display-ready runs
(`_national_discharge_coverage_rows` closes every cycle in that case). Those are true
positives — the national layer *will* go dark on the same schedule — and a coverage
refresh cannot close them. The lane is therefore honestly "the national catalog's frontier
is falling behind ingest", with coverage stall as its main cause; the runbook branches the
handling. This is the division of labour with `frontier-stalled`, which watches the global
per-source ingest frontier and cannot see a per-network hole at all.

### Window filter (the one wall-clock rule)

A source is **evaluated** only when its ready frontier is inside the display window:
`ready_frontier >= now() - NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`. A source whose newest
display-ready cycle already predates the window contributes nothing to the national layer
regardless of coverage, so its darkness is an ingest/retention question; without this rule a
retired or long-stalled source would alert forever from day one. The wall clock is legitimate
here precisely because the observed thing — the lookback window — is itself anchored on
`now()` (`services/tiles/mvt.py:2176`).

`__null_source__` (runs with `source_id IS NULL`) is always reported as **not evaluated**:
`national_discharge_cycles` takes a `str` source and matches `lower(h.source_id) = :source`,
so such runs cannot be listed by the per-source catalog at all. The legacy source-less
discovery path is out of this issue's scope (non-goal below). Measured on node-27
(2026-09-18, read-only, `nhms_display_ro`): sources `gfs` and `ifs`, both ready frontier
`2026-09-17 00:00Z`, both covered frontiers identical, gap `0.000` d, 38 active networks, no
`__null_source__` cohort — so neither the window rule nor the null-source rule introduces a
day-one alert.

## D2 — Threshold expressed in the display constant

`DEFAULT_GAP_DAYS = NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS / GAP_THRESHOLD_DIVISOR` with
`GAP_THRESHOLD_DIVISOR = 3` → 4.0 days at the current window of 12. No day count is
hard-coded, so shrinking the window automatically shrinks the threshold.

`NHMS_COVERAGE_GAP_DAYS` overrides it. A value that is not a finite number, `<= 0`, or
`>= NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS` is a **config error** (exit 2), never silently
clamped: a threshold at or beyond the window can only fire once the layer is already dark.

## D3 — Mail channel: the existing `OnFailure=` handler, no new state machine

The unit carries `OnFailure=nhms-node27-unit-failure-alert@%n.service`, whose template
documents that "Adding the next consumer stays one `OnFailure=` line". The alert *is* the
non-zero exit; the handler mails the last 30 journal lines. Consequences, accepted:

- **No dedup** — the handler is deliberately dumb, so a standing gap mails once per tick.
  Silence comes from clearing the condition, not from suppressing the alert. This bounds
  the cadence (D4).
- The verdict must land in the **journal**, so the unit must not redirect stdout to a file
  the way `nhms-node27-frontier-alert.service` does with `StandardOutput=append:`.
- **Journal budget**: `journalctl -n 30` keeps the *tail*, and systemd itself contributes
  roughly four framing lines (`Starting…`, `Main process exited…`, `Failed with result…`,
  `nhms-…: Failed…`). The script therefore prints **at most 24 lines**
  (`MAX_REPORT_LINES = 24`), the per-source table first (breaching sources sorted first,
  truncated with an explicit `… N more sources omitted` line), and the `VERDICT:` block
  **last**, so the operator-critical lines are the ones that survive the tail window.
- The rejected alternative — extending `scripts/node27_frontier_stall_alert.py` — keeps its
  round-1 rationale: its state machine is per-source directional high-water progress, pinned
  by D1 of `openspec/changes/archive/2026-08-14-node27-frontier-stall-alert/design.md` as
  "never compared against wall-clock lag". A relational two-frontier gap is a second,
  mutually exclusive criterion in the same state file, and the schema change would force a
  `state-corrupt` rebuild of a live alerting lane.

## D4 — Cadence: daily

`OnCalendar=*-*-* 06:00:00`, `Persistent=true`. With a 4-day threshold against a 12-day
window there are ~8 days of remaining margin after the first trip; a daily tick costs at
most one of them and caps the dedup-less mail volume at one message per day. A 30-minute
cadence (the frontier lane's) would emit 48 identical mails a day for a days-scale condition.

## D5 — No wrapper shell; drop-in-overridable env

`ExecStart` is the venv interpreter directly, with `Environment=PYTHONPATH=/home/nwm/NWM`;
there is no `*_once.sh`. The frontier wrapper exists for a thin-shell mutex contract,
env-file safety checks and a `--once` hand-off; here the env-file 0600/symlink contract is a
property of `infra/env/node27-frontier-alert.env` that the frontier lane already enforces
every 30 minutes on the same file, and this lane has no in-process mail path to guard.
Recorded because reviewers will compare the two lanes.

Because the unit's DSN comes from `EnvironmentFile=`, the live receipt points one invocation
at a scratch database with a systemd drop-in that **resets the list first**:

```ini
[Service]
EnvironmentFile=
EnvironmentFile=/home/nwm/tmp/issue2080/scratch.env
```

An empty `EnvironmentFile=` clears the previously assigned list; `Environment=` would not
win against a later-read `EnvironmentFile=`, which is why the reset form is pinned here.

## D6 — Fail-closed

Every internal failure raises alerting tendency, mirroring the frontier lane's D2 direction:

| failure | behaviour |
|---|---|
| config invalid (missing `DATABASE_URL`, bad threshold) | exit 2, structured stderr line, unit fails, handler mails |
| DB unreachable / statement timeout / permission denied / display-module error | exit 3, DSN-redacted error, unit fails, handler mails |
| gap exceeded, or no `default_cycle`, on ≥1 evaluated source | exit 1, per-source table + `VERDICT:` block |
| the ready-frontier statement returns **zero source keys** | exit 3 — the observer cannot see the surface it watches |
| source keys exist but none is in the lookback window | exit 0, each reported `not-evaluated` |
| all evaluated sources within threshold | exit 0, table still printed |

**Zero observable sources is fail-closed, not fail-open.** An empty ready-frontier result is not
"healthy, nothing to do": the statement joins `core.model_instance`, so a mass `active_flag`
flip or a `river_network_version_id` drift empties it while `hydro.hydro_run` keeps advancing.
`scripts/node27_frontier_stall_alert.py`'s `OBSERVATION_QUERY` does not join
`core.model_instance` at all, so it still sees progress and stays silent — and
`national_discharge_cycles` already returns `default_cycle = null`, i.e. the layer is dark.
That is exactly the constructive silence this issue exists to remove, so zero source keys exits
3. Zero *evaluated* sources with at least one key is a different state (every source aged out of
the window) and stays exit 0, owned by `frontier-stalled`.

Bounds: `connect_timeout=10` s on the engine and `SET statement_timeout = 30000` on the
session, matching the frontier lane. `TimeoutStartSec=300` on the unit: for `Type=oneshot`
the whole execution is the start phase, and a monitoring lane that hangs must become a
visible failed unit rather than sitting in `activating` forever — the exact 2026-08-12
geometry the frontier lane's `TimeoutStartSec=900` exists for. 300 s is two orders of margin
over connect 10 s + a bounded query per source. The DSN is redacted from every
operator-visible string so a password never reaches the journal or the mail body. The role is
`nhms_display_ro`, verified to hold SELECT on `hydro.hydro_run`, `hydro.run_display_coverage`
and `core.model_instance`.

## D7 — Adjudication of the issue's open question (non-fatal call sites)

**Decision: both call sites stay non-fatal and gain no new persisted failure signal.**

- `scripts/node27_autopipeline.py:2196-2209` and `scripts/node27_autopipe_cron.sh:225-231`
  are unchanged.
- Rationale: the catalog's own frontier *is* the persisted outcome signal, and it is strictly
  stronger than any rc record — the issue's `no_coverage_row` case is an rc=0 stall no
  rc-derived signal can see, and after D0 the lane also sees inconsistent-row and
  missing-network stalls no rc could ever describe. Making the refresh fatal would turn a
  successful ingest into a failed one to buy a weaker detector; a second persisted rc lane
  would add a state artifact with its own lifecycle and drift risk for no extra coverage.
- Consequence accepted: an rc≠0 refresh failure stays visible only in
  `coverage_refresh=refresh_failed_rc<N>` and the cron log **until** it moves the gap past
  the threshold. Detection latency for that class is bounded by the threshold (4 days), not
  immediate. This adjudication is restated in the PR body, as the issue requires.

## Invariant Matrix

- **Governing invariant:** the lane's covered frontier is the value
  `services/tiles/mvt.py:national_discharge_cycles` returns as `default_cycle` for that
  source — the same call the display catalog serves — and its ready frontier uses that
  function's run-set predicates minus the coverage join. A reported `gap = 0` therefore means
  the national catalog's newest listed cycle equals the newest cycle ingest has produced.
- **Source-of-truth identity/contract:** `(lower(source_id), cycle_time)`; covered side owned
  entirely by `national_discharge_cycles`, ready side by the D0 statement.
- **Producers:** `packages/common/display_coverage.py`, `scripts/node27_refresh_coverage.py`,
  `scripts/node27_autopipeline.py` — read only, all unchanged.
- **Validators/preflight:** the new script's config validation (`DATABASE_URL`, threshold vs
  lookback), executed before any observation.
- **Storage/cache/query:** one re-derived ready-frontier statement plus one
  `national_discharge_cycles` call per source; no writes, no cache, no state file.
- **Public routes/entrypoints:** the new script's `main(argv)`; units
  `nhms-node27-coverage-freshness-alert.{service,timer}`.
- **Frontend/downstream consumers:** none new — `nhms-node27-unit-failure-alert@.service`
  consumes the non-zero exit; `services/tiles/mvt.py` and `apps/api/routes/hydro_display.py`
  are read, never modified.
- **Failure paths/rollback/stale state:** exits 1/2/3 above; stateless, so there is no
  stale-state or corrupt-state class.
- **Evidence/audit/readiness:** journal lines (the mail body),
  `tests/test_node27_coverage_freshness_alert.py`, the node-27 live receipt.

### Regression rows

- Ready frontier equals covered frontier, in window → exit 0, `gap=0.0`.
- Covered frontier lags by more than the threshold → exit 1, that source named in `VERDICT:`.
- rc=0 `no_coverage_row`-shaped stall (coverage rows exist but stop advancing) → exit 1; the
  criterion never reads an rc.
- Populated-but-inconsistent coverage rows (the D0 class: bad window columns, wrong sample
  count, off-phase grid) → the catalog drops those cycles, so the covered frontier falls back
  and the lane alerts. This is the regression the re-derived predicate would have missed.
- Both frontiers frozen together (full ingest stall) → exit 0; `frontier-stalled` owns it.
- One active network stops producing, or a network is newly activated with no display-ready
  runs → covered frontier falls back, lane alerts (recorded true positive, D1), and the
  runbook routes the operator to the network, not to the refresh script.
- Source in-window with `default_cycle = null` → exit 1, reported as having no covered cycle.
- Source whose ready frontier predates the lookback window → `not-evaluated`, exit 0.
- `__null_source__` key present → `not-evaluated`, exit 0, never alerts on its own.
- `NHMS_COVERAGE_GAP_DAYS >= NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`, `<= 0`, or non-finite →
  exit 2, no observation attempted.
- Observation raises → exit 3 with the DSN redacted; no password anywhere in the output.
- Report with 25 sources → ≤ `MAX_REPORT_LINES` lines, breaching sources kept, `VERDICT:`
  block last.
- Unchanged sibling consumer: `nhms-node27-frontier-alert.{service,timer}` and its state file
  are untouched; the frontier lane's tests stay green.

## Boundary-surface checklist

- **Shared helper roots:** none written. The lane *reads* `services/tiles/mvt.py`
  (`national_discharge_cycles`, `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`) and modifies nothing
  there; any change to that module is out of scope.
- **Public entrypoints:** one new script entrypoint; no existing CLI changes.
- **Read surfaces:** `hydro.hydro_run`, `hydro.run_display_coverage`, `core.model_instance` —
  read-only through `nhms_display_ro`.
- **Write/delete/overwrite surfaces:** none — no state file, no receipt, no own log file.
- **Staging/publish/rollback surfaces:** none.
- **Producer/consumer evidence boundaries:** the journal tail is the mail body; the verdict
  must survive `journalctl -n 30` (D3 budget).
- **Stale-state/idempotency boundaries:** stateless by construction — every tick is a fresh
  observation, so re-running is idempotent and there is no baseline to corrupt.
- **Unchanged downstream consumers:** `services/tiles/mvt.py`,
  `packages/common/display_coverage.py`, both coverage-refresh call sites, the frontier lane.

## Non-goals

- Changing `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`, its anchor, or any tile/catalog SQL.
- Changing coverage refresh semantics, the #1446 refusal guard, or either non-fatal call site (D7).
- Observing the legacy source-less discovery path (`source=None`); the issue scopes this lane
  per source, and production currently has no `source_id IS NULL` display-ready runs.
- Dedup/resend/escalation state for this lane (D3).
- Any change to the frontier stall alerter's criterion or state schema.
