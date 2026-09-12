## Risk Triage

```text
Issue type: bugfix
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issues; no `Suggested fixture level` field)
Why:
- Production systemd units on the live node-22 compute lane (new probe units).
- Production config template whose current content bricks a rebuilt node (#2075).
- Evidence-chain Python: receipt self-validation and dry-run provider evidence (#1926).
- Four issues in one PR at user direction; #1926's stated PR Boundary was single-issue.
Selected risk packs:
- Public API / CLI / script entry
- Config / project setup
- File IO / path safety / overwrite
- Concurrency / shared state / ordering
- Resource limits / large input / discovery
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Release / packaging / dependency compatibility
- Documentation / migration notes
- Run manifest / QC provenance
OpenSpec change: harden-node22-scheduler-refresh-lane (generated)
Evidence floor:
- uv run pytest -q tests/test_scheduler_file_provider_refresh.py tests/test_node22_refresh_timer_health.py tests/test_env_templates.py
- uv run ruff check .
- openspec validate harden-node22-scheduler-refresh-lane --strict --no-interactive
- node-22 read-only probe receipt on the live lane (#1831 exception: exact interpreter, no uv)
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | selected | New probe CLI + installer; `--dry-run`'s exit contract changes from failing to succeeding. |
| Config / project setup | selected | `compute.scheduler-dbfree.env.example` is the only tracked source for a production env; new probe env knobs and thresholds. |
| File IO / path safety / overwrite | selected | Probe reads `latest.json` no-follow/size-capped and writes its own 0600 receipt; refresh dry-run must prove zero provider writes. |
| Schema / columns / units / field names | not selected | The refresh receipt schema is unchanged — the fix corrects a *value* (`entry_count`) the existing `_validate_receipt` already constrains, adding no field. The probe receipt is node-22-local ops evidence with no cross-service consumer, so it gets required-field assertions (R14) rather than a tracked `schemas/` artifact and the CI `check-jsonschema` loop. |
| Auth / permissions / secrets | not selected | No credential, token, or secret is read or written. The probe receipt stays on the private node-22 root at 0600 with no cross-user consumer (design D2), so no ACL/ownership surface is created; the receipt must echo no env values beyond the integer thresholds and the unit name (R14). |
| Concurrency / shared state / ordering | selected | Probe ticks share the host with the refresh oneshot and the #1104 manual-publisher stop window; the stopped-dwell (D3a) is the interlock. |
| Resource limits / large input / discovery | selected | Probe must bound its receipt read and its own runtime (`TimeoutStartSec`) so a wedged watchdog becomes visible. |
| Legacy compatibility / examples | selected | `.example` template is the node-rebuild compatibility surface; non-dry-run refresh is the legacy path that must not shift. |
| Error handling / rollback / partial outputs | selected | Probe fails closed on unreadable systemd or receipt; installer needs `--rollback`; dry-run must preserve real failure reasons. |
| Release / packaging / dependency compatibility | selected | Probe must be stdlib-only (D4) and run under both node-22's current 3.12.7 `.venv` and the repo's 3.11 pin. |
| Documentation / migration notes | selected | Runbook must record `enabled`+`inactive` as a failure state and pair the #1104 stop/start window. |
| Geospatial / CRS / basin geometry | not selected | No geometry, CRS, or raster path is touched. |
| Hydro-met time series / forcing windows | not selected | No forcing window, provider snapshot, or time-series read changes. |
| SHUD numerical runtime | not selected | No solver, restart, or output-cadence surface. |
| PostGIS / TimescaleDB domain behavior | not selected | node-22 is permanently DB-free; this change adds no DB access anywhere. |
| Slurm production lifecycle | not selected | No sbatch, gateway, or job-lifecycle surface; the probe submits nothing. |
| External hydro-met providers | not selected | No GFS/ERA5/IFS/CDS discovery path. |
| Run manifest / QC provenance | selected | The refresh receipt is the run-provenance artifact whose self-validation is being fixed. |
| Published NHMS artifacts / display identity | not selected | No published artifact, DB row, or display identity changes. |

## Tasks

### 1. Refresh-lane health probe (#2146, #2041 anti-regression)

- [x] 1.1 Add `scripts/node22_refresh_timer_health.py`: **standard library only**, no
      import of `packages.common` or any repo package (design D4 — it must run from a
      staged copy outside the checkout). It carries its own bounded, size-capped,
      `O_NOFOLLOW` receipt read rather than importing
      `packages/common/safe_fs.py:406`.
      CLI surface: `[--unit NAME] [--refresh-receipt PATH] [--health-receipt-root PATH]
      [--now ISO8601] [--json]`. Every flag has an env default **except `--now`**,
      which is CLI-only with no environment seam at all (design D4): an env default for
      the clock is a false-green vector, since a pinned past instant grades `ok`/exit 0
      on the exact stopped geometry this change exists to catch. `--json` prints the
      verdict document to stdout in addition to writing the receipt. Reads `systemctl --user show <timer> -p UnitFileState,ActiveState,SubState,
      InactiveEnterTimestamp,NextElapseUSecRealtime,LastTriggerUSec` and
      `systemctl --user list-timers <timer>` through an injectable binary
      (`NHMS_REFRESH_HEALTH_SYSTEMCTL`, default `/usr/bin/systemctl`).
- [x] 1.2 Grade the four independent signals into exactly one verdict from
      `probe_failed | manifest_expired | timer_stopped | timer_not_enabled |
      timer_not_scheduled | manifest_stale | manifest_unavailable | ok`, evaluated in
      that order, first match wins, with `ok` implemented as a pure `else` and never as
      a positive predicate (design D3 table). `timer_stopped` must not carry an
      `enabled` predicate, and `UnitFileState != enabled` must always reach a non-zero
      verdict. `probe_failed` covers unreadable **systemd** evidence and the undefined
      dwell only; an unresolvable manifest age grades `manifest_unavailable` at
      precedence 7, so it can never mask a timer verdict, and the two manifest-age
      comparisons are **skipped** rather than defaulted when no age resolved.
      Thresholds from env: `NHMS_REFRESH_HEALTH_MAX_NEXT_DWELL_HOURS` (36),
      `NHMS_REFRESH_HEALTH_MAX_MANIFEST_AGE_HOURS` (120),
      `NHMS_REFRESH_HEALTH_STOPPED_DWELL_HOURS` (6). Reject at config time any freshness
      threshold >= 168, and bound the stopped-dwell at config time too — all three
      tunables are bounded, not two.
- [x] 1.3 Apply the `timer_stopped` dwell (design D3a): grade `timer_stopped` only when
      `now - InactiveEnterTimestamp` exceeds the stopped-dwell, so a live #1104
      manual-publisher window does not alarm. Inside the dwell, continue grading the
      remaining signals — never short-circuit to `ok`. When `ActiveState != active`
      but `InactiveEnterTimestamp` is empty or unparseable the dwell arithmetic is
      undefined: grade `probe_failed`, never `ok`.
- [x] 1.4 Write a bounded JSON receipt (`schema_version`, `generated_at`, `verdict`, the
      raw signals — `unit_file_state`, `active_state`, `sub_state`,
      `inactive_enter_timestamp`, `next_elapse`, `last_trigger`, `manifest_age_hours`,
      `manifest_source` — the three thresholds, and the inspected unit name) to the probe
      receipt root, file mode 0600 under a private directory. Echo no env values other
      than the integer thresholds and the unit name. `manifest_source` is a closed set:
      `latest`, `history:<filename>`, or `unavailable` (design D3b).
      The write must be durable and non-destructive: a write loop that tolerates short
      writes, `fsync`, and write-temp-then-`replace`, so a failed write neither silently
      truncates the receipt nor destroys the previous good one. Print the verdict and any
      evidence errors to the journal **before** returning on the receipt-write failure
      path — D2 makes the journal the alert channel, so a receipt failure must not also
      swallow the verdict.
- [x] 1.5 Exit 0 only on `ok`; non-zero on every other verdict, including unreadable
      systemd and an unresolvable manifest. `ok` is never a fallback.
- [x] 1.8 Resolve the manifest age with the bounded history fallback (design D3b):
      the configured `latest.json` first, then the sibling `history/` directory of that
      file, whose entries the runner names `refresh_<YYYYmmddTHHMMSSZ>_<uuid12>.json`
      (`scripts/scheduler_file_provider_refresh.py:1628`) — the fixed-width UTC prefix
      makes a lexical **descending** sort chronological, so no timestamp is parsed and no
      `mtime` is trusted. Filter to that filename shape, cap the directory listing at 200
      entries, open at most the 10 newest, and bound each read `O_NOFOLLOW` exactly as
      `latest.json` is read. First candidate yielding a parseable
      `registry.after_generated_at` wins; do **not** gate on the receipt's `outcome`
      (a `dry_run` receipt is an ordinary candidate — its `after_generated_at` is the
      current manifest's real generation time). Record the answering source in
      `manifest_source`. Exhausting both sources means the age is unresolved.
- [x] 1.6 Add `infra/systemd/nhms-node22-refresh-timer-health.{service,timer}`:
      user-scope oneshot, hourly, `Persistent=true`, bounded `TimeoutStartSec`,
      `UnsetEnvironment=` the same DB selector set the refresh service uses, and no
      `PrivateTmp` (same node-22 namespace constraint documented in the refresh unit).
- [x] 1.7 Add `scripts/install_node22_refresh_timer_health.sh --install|--enable|--rollback`,
      modeled on the existing refresh installer's state-capture/rollback shape but
      minimal. It must capture and assert that **both** `nhms-compute-scheduler.timer`
      and `nhms-compute-scheduler.service`, and both refresh units, have identical
      enabled/active states before and after its own run.

### 2. Dry-run worker-mirror entry_count (#1926)

- [x] 2.1 Red-proof first: add a test that, against pre-change source, reproduces
      `primary_receipt_failed` for direct-grid + worker mirror + `dry_run=True`.
- [x] 2.2 Fix the dry-run mirror evidence so `entry_count` comes from the prospective
      registry model count (design D5), never the before-image, never a literal.
      A 0-count dry-run receipt is **unreachable by design on both paths** —
      `scripts/scheduler_file_provider_refresh.py:896-898` (direct-grid, empty previous
      snapshot) and `:961-962` (empty readiness entries) each fail closed before any
      receipt exists. Neither guard may be relaxed to produce a 0-count boundary case;
      the empty boundary is tested as fail-closed (R17b), not as a successful dry-run.
      #1926's AC8 asks for an empty-set success case; that part of the AC is
      unsatisfiable without weakening a fail-closed guard and is reported upstream.
- [x] 2.3 Leave every `not dry_run` path byte-identical in behaviour, including the
      postcommit mirror-sha equality check, and leave the dry-run preimage guard at
      `scripts/scheduler_file_provider_refresh.py:743-744` in place — once the counts
      agree by construction it is the only thing still failing a divergent mirror closed.

### 3. Env template alignment (#2075)

- [x] 3.1 Add `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc` and
      `NHMS_REQUIRE_FORECAST_WARM_START=true` to
      `infra/env/compute.scheduler-dbfree.env.example`, matching the live values captured
      2026-09-12 (`compute.scheduler-dbfree.env:79-80`), placed with the other
      orchestrator-chain keys and commented with why a DB-free node must stop at
      `forecast_state_save_qc`.
- [x] 3.2 Add a machine-readable required-key block to `infra/env/README.md` (near the
      existing mandate at `:283-285`) enumerating every mandatory key for the node-22
      DB-free scheduler lane, with a pinned value where the docs pin one.
- [x] 3.3 Add `tests/test_env_templates.py` asserting `compute.scheduler-dbfree.env.example`
      satisfies every entry in that block — key present **and value equal** where pinned,
      so `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast` cannot pass.
- [ ] 3.4 Post the #2075 gating conclusion on issue #2069: #2072 is a **behaviour-equivalent
      move**, not a first activation, because the live node-22 env already sets
      `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`; cite the 2026-09-12
      read-only capture recorded in this change's design.md and the PR evidence bundle.
      (Orchestrator task, not implementer.)

### 4. Documentation (#2041, #2146)

- [x] 4.1 `docs/runbooks/current-production-ops.md`: record `is-enabled=enabled` +
      `is-active=inactive` (`Trigger: n/a` / `NEXT=-`) as a **failure state**; split the
      steady-state check into independent `is-enabled` / `is-active` / `list-timers NEXT`
      / manifest-age columns; make the #1104 `stop`/`start` pair explicit with the
      missing-`start` terminal state named.
- [x] 4.2 Record the 2026-08-28 -> 2026-09-08 episode as a worked counter-example
      (enabled facade, 168 h crossing, `db_free_registry_blocked`,
      `frontier_block_missing`, operator recovery). State explicitly that
      `Persistent=false` is why a stopped timer never catches up the missed calendar
      events — enabled stays green while nothing fires — and that the one-time recovery
      is complete and out of scope here.
- [x] 4.3 Document the probe: each verdict's meaning, the three thresholds, where the
      receipt lands, that it never mutates units, that a `failed` probe unit is the
      alert of record, and that this alert is **node-22-local** (no mail/paging) as a
      stated known limit.

## Evidence Mapping

| Matrix row | Proof |
|---|---|
| R1 `ok` | unit test, fake systemctl + fresh manifest -> exit 0 |
| R2 `timer_stopped` past dwell | unit test, `enabled`+`inactive`, `InactiveEnterTimestamp` 6 days back, pinned `--now` -> non-zero |
| R2b `timer_not_enabled` | unit test, `UnitFileState=disabled` -> non-zero; a second case with `disabled`+`active` -> still non-zero |
| R3 no alarm inside dwell | unit test, `enabled`+`inactive` 30 min back, fresh manifest -> not `timer_stopped`; second case with stale manifest -> `manifest_stale`, still non-zero |
| R4 `timer_not_scheduled`, empty NEXT | unit test, active timer, empty `NextElapseUSecRealtime` -> non-zero |
| R4b `timer_not_scheduled`, unparseable NEXT | unit test, active timer, `NextElapseUSecRealtime` present but unparseable -> non-zero; a separate case from R4 so the branch is covered, not inferred |
| R5 `timer_not_scheduled`, distant NEXT | unit test, `NEXT` beyond 36 h -> non-zero |
| R6 `manifest_stale` | unit test, injected clock, age between 120 h and 168 h |
| R7 `manifest_expired` | unit test, injected clock, age >= 168 h, verdict distinct from R6 |
| R7b precedence | unit test, stopped past dwell + manifest >= 168 h -> `manifest_expired`; stopped past dwell + manifest stale but < 168 h -> `timer_stopped` |
| R8 systemd unreadable | unit tests: fake systemctl absent; fake systemctl exits non-zero -> `probe_failed`, never `ok` |
| R8b undefined dwell | unit test, `inactive` with empty `InactiveEnterTimestamp` -> `probe_failed` |
| R8c timestamp zone handling | unit test over hardcoded `systemctl show` strings — a `UTC` token and a non-local abbreviation — asserted without depending on the test host's own zone |
| R9 latest unresolvable, history answers | unit tests over the four unresolvable shapes (missing file, malformed JSON, schema-invalid payload, empty `providers`) each paired with a usable history receipt -> age from history, `manifest_source=history:<file>`, healthy lane grades `ok` exit 0 |
| R9b history fallback does not mask the timer | unit test: `latest.json` unresolvable **and** `enabled`+`inactive` past the dwell -> `timer_stopped`, not an evidence verdict. This is the A1 regression and must fail if the manifest arm is moved back above the timer arms |
| R9c `manifest_unavailable` | unit tests: no history directory; history present but every candidate unresolvable -> `manifest_unavailable`, non-zero, `manifest_source=unavailable`, never `ok` |
| R9d fallback bounds | unit tests: a history directory of >200 entries is not fully listed; at most 10 candidates are opened (spy on the read); off-shape filenames are skipped; a symlinked candidate is refused; ordering is by descending filename with `mtime` deliberately set to contradict it |
| R10 threshold bound | unit test, freshness threshold 168 and 200 both rejected at config time; stopped-dwell outside its bounds likewise rejected |
| R11 no mutation verbs | source-scan test over the probe file, zero hits |
| R11b env surface enumerated | implementer reports the table (every env var the probe reads x removed / unit-tunable / bounded / exercised through the env path); a test asserts the clock has no env seam, and a test drives the production shape — env defaults set, **no CLI flags** — asserting the resolved unit, receipt path and clock |
| R12 only read subcommands | fake systemctl records every invocation; test asserts the set is a subset of `{show, list-timers}` |
| R13 stdlib only | source-scan test of the probe's import statements against `sys.stdlib_module_names` |
| R14 receipt shape and mode | unit test: parent dir mode, file mode 0600, required fields present (incl. `manifest_source` from its closed set), size bounded, no env values beyond thresholds and unit name |
| R14b durable receipt write | unit tests: `os.write` monkeypatched to a short write -> fails closed, non-zero; a write failure leaves the previous receipt byte-identical; the verdict is on stdout/journal before the failure exit |
| R15 installer leaves units untouched | installer's own before/after assertion, per unit type (both fields for the two timers, `UnitFileState` only for the two timer-driven oneshots); a regression test drives the installer fake to return a **divergent** second read and asserts the assertion actually bites and aborts; a second case flips only a oneshot's `is-active` and asserts it does **not** fire; plus the node-22 live receipt recording all four states before and after |
| R16 dry-run counts agree, N models | pytest over a multi-model fixture; assertion derives N from the fixture, no literal 76 |
| R17 dry-run boundaries | pytest: 1 model and N models, counts derived from the fixture |
| R17b empty set stays fail-closed | pytest: empty model set fails closed with `provider_invalid` on both the direct-grid (`:896-898`) and non-direct-grid (`:961-962`) paths, yielding a terminal `outcome=failed` receipt with no providers — never a successful zero-count `dry_run` receipt |
| R18 dry-run mutates nothing | pytest asserts registry/mirror/readiness/state bytes and preimages identical before and after |
| R19 dry-run receipt persisted twice | pytest asserts history and latest both written, emergency reservation cleaned, no 0-byte reserved slot |
| R20 divergent preimage still fails closed | existing `tests/test_scheduler_file_provider_refresh.py:1582` still green, asserted post-fix |
| R21 post-gate failure keeps reason | existing pytest for that path plus an explicit assertion the reason is not `primary_receipt_failed` |
| R22 non-dry-run unchanged | full `tests/test_scheduler_file_provider_refresh.py` green |
| R23 env template complete | `tests/test_env_templates.py` against the README required-key block, values included |
| 3.11 / 3.12 compatibility | R13 (stdlib-only) plus a probe run under node-22's 3.12.7 interpreter in the live receipt; repo suite runs under the 3.11 pin |

## Verification

Local (non-oracle for the node-22 items):

```bash
uv run pytest -q tests/test_scheduler_file_provider_refresh.py
uv run pytest -q tests/test_node22_refresh_timer_health.py tests/test_env_templates.py
uv run ruff check .
openspec validate harden-node22-scheduler-refresh-lane --strict --no-interactive
```

node-22 live receipt, read-only, under the #1831 exception口径 — exact interpreter
`/scratch/frd_muziyao/NWM/.venv/bin/python`, no `uv`, no `.venv` rebuild, no Slurm
submit, no DB, no Basins write, and **no checkout of this branch into
`/scratch/frd_muziyao/NWM`** (that tree is the live execution root for both the
scheduler and the refresh oneshot):

```bash
# baseline — all four unit states, captured before and again after; none may change
systemctl --user is-enabled nhms-compute-scheduler.timer
systemctl --user is-active  nhms-compute-scheduler.timer
systemctl --user is-enabled nhms-compute-scheduler.service || true
systemctl --user is-active  nhms-compute-scheduler.service || true
systemctl --user show nhms-scheduler-file-provider-refresh.timer \
  -p UnitFileState,ActiveState,SubState,InactiveEnterTimestamp,NextElapseUSecRealtime,LastTriggerUSec
# #2041 AC3: the oneshot must be inactive between ticks, never a lingering service
systemctl --user is-enabled nhms-scheduler-file-provider-refresh.service || true
systemctl --user is-active  nhms-scheduler-file-provider-refresh.service || true

# #2075 AC1: record the live EnvironmentFile binding and the key's presence/value
systemctl --user cat nhms-compute-scheduler.service | grep -n 'EnvironmentFile'
grep -n '^NHMS_ORCHESTRATOR_TERMINAL_STAGE=\|^NHMS_REQUIRE_FORECAST_WARM_START=' \
  /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env

# probe, staged OUTSIDE the live checkout, run read-only against the live lane
/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/tmp/<staged>/node22_refresh_timer_health.py \
  --health-receipt-root /scratch/frd_muziyao/tmp/<staged>/receipts --json
# --health-receipt-root is explicit: the env default points at the production
# workspace root, and this pre-merge run must write nothing there.
```

Probe **arming** (`--install` then `--enable`, plus a
`systemctl --user list-timers nhms-node22-refresh-timer-health.timer` receipt) is a
**post-merge** step on node-22 master, recorded as a follow-up receipt on the PR.
Pre-merge, #2146's detection criterion is evidenced by the fixture tests plus the
staged read-only run against the live lane; the armed lane is evidenced after merge.

## Non-Goals (explicit)

- No `--enable` / `start` / `stop` on any refresh or scheduler unit: the refresh timer
  is already `enabled` + `active` (verified 2026-09-12T15:34Z) and #2041's one-time
  recovery landed 2026-09-08. Only the **new probe** units are armed, and only post-merge.
- No self-heal path (design D1).
- No off-host alert routing (design D2) — stated known limit, filed as a follow-up.
- No change to the 168 h bound or any fail-closed consumer semantics.
- No live env edit and no scheduler restart for #2075: the live env already carries both
  keys; only the tracked template was behind.
