## Context

The node-22 compute lane is permanently DB-free. Its scheduler reads three file
providers (registry, readiness, state) plus a worker registry mirror, all
renewed by a daily oneshot driven by
`nhms-scheduler-file-provider-refresh.timer` (`OnCalendar=*-*-* 02:15:00 UTC`,
`RandomizedDelaySec=30m`, `Persistent=false`). Consumers reject a manifest older
than `DEFAULT_MAX_MANIFEST_AGE_HOURS = 168`
(`services/orchestrator/scheduler_file_providers.py:61`).

Live read-only state captured 2026-09-12T15:34Z on node-22:

```
nhms-scheduler-file-provider-refresh.timer  UnitFileState=enabled ActiveState=active SubState=waiting
  InactiveEnterTimestamp = Fri 2026-08-28 00:11:18 CST   (the silent stop)
  ActiveEnterTimestamp   = Tue 2026-09-08 13:47:49 CST   (operator recovery, #2146)
  LastTriggerUSec        = Sat 2026-09-12 10:35:58 CST   (= 02:35:58 UTC, 02:15+jitter)
  NextElapseUSecRealtime = Sun 2026-09-13 10:23:33 CST
nhms-compute-scheduler.timer                UnitFileState=enabled ActiveState=active
compute.scheduler-dbfree.env:79-80          NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc
                                            NHMS_REQUIRE_FORECAST_WARM_START=true
```

Two consequences for scope: **#2041's one-time recovery is already complete** —
its live acceptance is satisfied by the read-only capture above, and re-running
`--enable` on a healthy timer is an unnecessary production mutation, so this
change does not do it. And **#2075 resolves on its Branch (2)**: the live env
already sets both keys; only the tracked template is behind, so no live env edit
and no scheduler restart are required.

## Goals / Non-Goals

Goals:

- Make `enabled` + `inactive` observable in hours, not at the 168-hour cliff.
- Make the operator's `--dry-run` rehearsal usable under the production shape
  (direct-grid authority + worker mirror).
- Make the tracked env template a faithful, self-checking source.

Non-Goals: listed in `proposal.md`. The load-bearing one is **no self-heal** —
see the decision below.

## Decisions

### D1: Detection only, no automated self-heal

`#2146` leaves alert-vs-self-heal open. Alert-only is chosen.

An auto-`--enable` would have to interlock with the #1104 manual-publisher
window, whose documented procedure is `stop` the timer, run the CLI, `start` it
again (`docs/runbooks/current-production-ops.md:1050-1060`). A self-healer that
cannot distinguish "operator deliberately stopped it 4 minutes ago" from "someone
forgot the `start` 6 days ago" will race a live canonical replace. The dwell
timer that would make it safe is itself the hard part, and the 168-hour budget
means detection buys days of operator margin. Alert-only is the simplest thing
that closes the actual gap; self-heal stays available as a later, separately
argued change.

The detection side has the mirror-image version of the same problem and does
need an answer: during a legitimate #1104 window the timer sits at exactly the
`enabled` + `inactive` geometry the probe alarms on. That is solved by a dwell,
not by distinguishing intent — see D3a.

### D2: The alert of record is a failed node-22 user unit plus a local receipt — and that is a node-22-local signal

node-22 has `/usr/sbin/sendmail`, but no lane in this repo has ever proven it
delivers from that host; node-27's alert lane deliberately routes around its own
local MTA through an authenticated-SMTP shim (`scripts/node27_frontier_smtp_sendmail.py`,
runbook §10.6). Standing up an unproven mail channel inside this change would
ship a watchdog whose alerting arm is itself untested — the exact failure mode
being fixed. So an unhealthy verdict exits non-zero, which puts
`nhms-node22-refresh-timer-health.service` into `failed` (visible in
`systemctl --user list-units --failed` and in the unit's own status), and writes
a receipt to a private node-22 receipt root.

State the limit plainly rather than dressing it up: **this signal is node-22-local.**
The receipt is written 0600 under `/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts`,
a sibling of the mode-0700 private workspace roots the runbook already
establishes for this lane (`docs/runbooks/current-production-ops.md:695-696`,
`:735-738`). The probe's own root is created 0700 by the probe/installer — the
runbook's existing `install -d` covers `workspace/provider-refresh`, not this
new sibling — and node-27, running as a different user, does not read it. Nothing polls
node-22's failed units today. So what this change buys is: the failure becomes
*discoverable in hours by anyone who looks at the host*, instead of *invisible
until the 168-hour cliff takes compute and journal retention down with it*. That
is a real and sufficient improvement over the status quo, and it is not the same
thing as paging someone.

Routing this verdict off-host — a proven node-22 mail channel, or a shared-root
health artifact with an explicit owner/group/mode/ACL decision and a node-27
consumer — is deliberately **not** in this change: it is a second alerting lane
with its own auth and delivery surface, and bolting an unproven one onto this PR
would repeat the mistake. It is recorded as a known limit and filed as a
follow-up issue at merge.

### D3: Four independent signals, two independent thresholds

The defect this lane keeps hitting is one signal standing in for another
(`is-enabled` read as "alive"). The probe therefore records
`unit_file_state`, `active_state`, `next_elapse`, and `manifest_age_hours` as
four separate fields and grades them separately:

Verdicts are evaluated in the order below and the **first match wins**. `ok` is
a pure `else` — it is reached only by falling through every failing condition,
never by matching a positive predicate of its own. That is what makes the
grading total: any signal combination that is not caught above is healthy by
exhaustion, and no combination is unclassified.

| order | verdict | condition | exit |
|---|---|---|---|
| 1 | `probe_failed` | the systemd query could not be executed or returned an error; or the refresh receipt is missing / unreadable / schema-invalid; or `ActiveState != active` while `InactiveEnterTimestamp` is empty or unparseable, leaving the dwell arithmetic undefined | non-zero |
| 2 | `manifest_expired` | manifest age >= 168 h — the consumer is already fail-closed | non-zero |
| 3 | `timer_stopped` | `ActiveState != active` **and** `now - InactiveEnterTimestamp` exceeds the stopped-dwell (D3a) | non-zero |
| 4 | `timer_not_enabled` | `UnitFileState` is anything other than `enabled` (`disabled`, `linked`, `masked`, `static`, `not-found`) | non-zero |
| 5 | `timer_not_scheduled` | timer is `active` but `NextElapseUSecRealtime` is empty, or `NEXT` is further out than the next-dwell threshold | non-zero |
| 6 | `manifest_stale` | manifest age >= the manifest-age threshold (and < 168 h). Both manifest comparisons are at-or-over, never strictly-greater, so an age exactly equal to a threshold is already a finding | non-zero |
| 7 | `ok` | none of the above | 0 |

Three ordering choices carry weight:

- `probe_failed` is first so missing evidence can never fall through to healthy.
  It also absorbs the one input on which the dwell arithmetic is undefined
  (`inactive` with no parseable `InactiveEnterTimestamp`): fail closed rather
  than guess.
- `manifest_expired` outranks every timer verdict because it is the more severe
  fact — the consumer is already blocked — regardless of why the timer is idle.
- `timer_stopped` does **not** carry an `enabled` predicate, and
  `timer_not_enabled` is a separate verdict rather than a sibling condition of
  it. A `disabled` timer is just as dead as a stopped one, and the refresh
  installer reaches exactly that state on purpose
  (`scripts/install_node22_scheduler_file_provider_refresh.sh:147` `--install`
  ends `disable --now`; `:104` `rollback_files` does the same), while the #1104
  precondition at `docs/runbooks/current-production-ops.md:1051-1053` explicitly
  accepts `disabled`. Splitting the two keeps each verdict's message true: a
  `disabled` timer that is also idle is reported as *stopped* (the operationally
  urgent fact — no tick is coming), while a `disabled` timer that happens to be
  running right now is reported as *not enabled* (it will not survive a reload
  or a reboot). Folding `enabled` into the stopped predicate instead would leave
  the whole `disabled` family to be caught by a single lower-priority verdict,
  and any future reordering of that verdict would silently re-open the
  green-facade gap this change exists to close.

Defaults, all overridable by env: next-dwell 36 h
(`NHMS_REFRESH_HEALTH_MAX_NEXT_DWELL_HOURS`), manifest age 120 h
(`NHMS_REFRESH_HEALTH_MAX_MANIFEST_AGE_HOURS`), stopped-dwell 6 h
(`NHMS_REFRESH_HEALTH_STOPPED_DWELL_HOURS`). The two freshness thresholds sit far
enough below 168 h to leave operator margin, and are asserted at config time to
be strictly under 168.

The manifest signal is computed and recorded independently of the systemd
signals, so the receipt still carries `manifest_age_hours` when systemd is
unreadable (and the systemd fields when the receipt is unreadable). That is a
statement about the **receipt's** completeness, not about the verdict: an
unreadable evidence source always grades `probe_failed` per row 1.

### D3a: `timer_stopped` needs a dwell, or routine operations become the alarm

The documented #1104 manual-publisher procedure is: `stop` the refresh timer, run
the CLI, `start` it again (`docs/runbooks/current-production-ops.md:1056-1060`).
For the whole of that window the timer's geometry is precisely `enabled` +
`inactive` — the state the probe exists to catch. The refresh oneshot's own
`TimeoutStartSec=7200` means such a window can legitimately run for two hours,
and the probe ticks hourly, so overlap is expected, not hypothetical. A probe
that fires on every manual publish teaches operators to ignore it, which inverts
the change's value.

The fix is a dwell, not intent detection: `systemctl show` already returns
`InactiveEnterTimestamp` (the live capture above carries it), so the probe
requires `now - InactiveEnterTimestamp > stopped-dwell` before grading
`timer_stopped`. Default 6 h — three times the oneshot's own 2 h ceiling, so a
maximal legitimate window plus slack stays quiet, while the 08-28 episode (six
days stopped) trips on the very first tick past the dwell. Inside the dwell the
lane is graded on its remaining signals; it is not silently `ok` if the manifest
is also stale.

### D4: The probe is structurally incapable of mutation, and runs from anywhere

The probe shells out only to `systemctl --user show` and
`systemctl --user list-timers` (both read-only) via an injectable binary path
(`NHMS_REFRESH_HEALTH_SYSTEMCTL`, mirroring the installer's
`NHMS_SCHEDULER_REFRESH_SYSTEMCTL` seam, which is also what makes the probe
testable against a fake). A test greps the probe source for
`start|stop|enable|disable|restart|daemon-reload` and fails on a hit. This is
the invariant that keeps a watchdog on a production compute node from becoming
an actor.

It is also **self-contained**: standard library only, no import of
`packages.common` or any other repo package. That is not stylistic. Pre-merge
live evidence has to come from a copy staged outside `/scratch/frd_muziyao/NWM`
— checking the feature branch out in that tree would deploy unreviewed code into
the live 02:15Z tick — and a staged bare file cannot import from a checkout that
is not on `sys.path`. A probe that only runs from inside the very checkout it is
meant to watch is the wrong shape for a watchdog anyway. It therefore carries
its own bounded, no-follow, size-capped receipt read rather than importing
`packages/common/safe_fs.py:406`.

CLI surface, fixed here so it is not invented during implementation:
`node22_refresh_timer_health.py [--unit NAME] [--refresh-receipt PATH]
[--health-receipt-root PATH] [--now ISO8601] [--json]`. Every flag has an env
default; `--now` exists so tests can pin the clock; `--json` writes the verdict
document to stdout in addition to the receipt. Exit status is the contract:
0 for `ok`, non-zero otherwise.

### D5: Dry-run mirror count comes from the prospective set, never a constant

In `refresh_scheduler_file_providers`, `worker_registry_result` is initialized to
`None` (`scripts/scheduler_file_provider_refresh.py:721`) and only assigned
inside the `not dry_run` publisher branch (`:859-860`). Under dry-run the mirror
evidence is built from that `None` (`:985-987`), so `_provider_evidence`'s
`entry_count` coalescing chain (`:1738-1742`) bottoms out at `0`. The dry-run
normalization loop (`:1000-1005`) restores `after_sha256`,
`after_schema_version`, `after_generated_at`, and `after_payload_checksum` — but
not `entry_count`. `_validate_receipt` then requires
registry`.entry_count` == mirror`.entry_count` (`:1860-1862`), raises
`receipt_provider_invalid`, and the caller folds it to `primary_receipt_failed`.

The fix is at the evidence-assembly point: under dry-run the mirror's
`entry_count` is taken from the same prospective registry evidence the canonical
provider already carries (`registry_publish_evidence`'s `model_count` /
`selected_model_count`), never from the before-image and never from a literal.
That keeps the invariant the validator checks — the mirror is a byte-copy of the
registry, so their counts are equal by construction — true in dry-run for the
same reason it is true after a real publish.

`not dry_run` paths are untouched, including the `postcommit` check that the
mirror's `after_sha256` equals the registry's.

One thing the fix must not be allowed to erode: once the counts agree by
construction, the receipt validator's
registry`.entry_count` == mirror`.entry_count` check is vacuous **in dry-run**.
The actual dry-run defense against a divergent mirror is the earlier preimage
guard at `scripts/scheduler_file_provider_refresh.py:743-744`
(`dry_run and worker_registry_preimage.sha256 != registry_preimage.sha256` ->
`provider_invalid`), covered by
`tests/test_scheduler_file_provider_refresh.py:1582`. That guard is what keeps
#1926's "inconsistent preimage still fails closed" criterion true, and it stays.

### D6: The env template gets an authoritative key list, not a self-confirming grep

Adding the two missing keys (`infra/env/README.md:283-285` is the mandate;
`docs/runbooks/current-production-ops.md:168-172` repeats it) fixes today's
drift. A guard test that re-derives the required keys from the same prose that
was already ambiguous would pass trivially and prevent nothing — the README
states the rest of the required set as "the union of two layers" (`:174-186`),
prose that never says "must set", and that vagueness is exactly what produced
#2075.

So the authority becomes explicit: a checked-in, machine-readable required-key
block in `infra/env/README.md` listing each mandatory key for the node-22
DB-free scheduler lane, with a pinned value where the docs pin one. The guard
test asserts `compute.scheduler-dbfree.env.example` satisfies every entry —
key present, and **value equal** where pinned, so
`NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast` could never pass. #2075's acceptance
("one grep can prove it", and README/runbook/template mutually consistent) is
then a CI-run check rather than a reading exercise.

## Invariant Matrix

Governing invariant: **nothing this change adds or modifies mutates a systemd
unit, a provider byte, or the `nhms-compute-scheduler.*` units** — the probe and
its installer observe and report only, and the refresh fix touches dry-run
evidence assembly alone.

That rule is the enforceable one and is stated alone on purpose. The change's
*purpose* — that a stopped refresh lane becomes observable well inside the
168-hour bound — is not a single checkable predicate; it is made concrete by the
D3 verdict table and its thresholds, and is proven row by row below rather than
asserted as an invariant.

Source-of-truth identity/contract: the published provider manifest's
`generated_at` (as recorded in the refresh receipt's
`providers[].after_generated_at`), the refresh timer's systemd
`(UnitFileState, ActiveState, InactiveEnterTimestamp, NextElapseUSecRealtime)`
tuple, and the receipt invariant
registry`.entry_count` == `registry_worker_mirror.entry_count`.

Surfaces:

- Producers: `scripts/scheduler_file_provider_refresh.py`
  (`refresh_scheduler_file_providers` dry-run evidence assembly, `_provider_evidence`);
  new `scripts/node22_refresh_timer_health.py` (produces the probe receipt)
- Validators/preflight: `scripts/scheduler_file_provider_refresh.py`
  `_validate_receipt`, `validate_current_receipt`, and the dry-run preimage guard
  at `:743-744`; the probe's own verdict grading and threshold config assertion
- Storage/cache/query: refresh receipt root
  (`.../provider-refresh/receipts/latest.json`, read-only to the probe); new
  probe receipt root (`.../workspace/refresh-timer-health/receipts`, the only
  path this change writes on node-22)
- Public routes/entrypoints: `scripts/scheduler_file_provider_refresh_once.sh --dry-run`;
  `scripts/node22_refresh_timer_health.py`;
  `scripts/install_node22_refresh_timer_health.sh`
- Frontend/downstream consumers: `services/orchestrator/scheduler_file_providers.py`
  (the 168 h bound), `scheduler_runtime.py`, `retention_frontier.py`,
  `scheduler_journal_retention.py` — read-only reference, none changed
- Failure paths/rollback/stale state: probe non-zero exit and resulting `failed`
  unit; installer `--rollback`; refresh dry-run failure reasons after the
  precommit gate
- Evidence/audit/readiness: probe receipt, refresh receipt history + latest,
  `infra/env/compute.scheduler-dbfree.env.example` and the README required-key
  block, `docs/runbooks/current-production-ops.md` §3.1.2

Regression rows:

| # | surface + input | expected |
|---|---|---|
| R1 | probe, timer enabled/active, `NEXT` present and within next-dwell, manifest fresh | `ok`, exit 0 |
| R2 | probe, `UnitFileState=enabled` + `ActiveState=inactive`, inactive longer than stopped-dwell (08-28 geometry) | `timer_stopped`, exit non-zero |
| R2b | probe, `UnitFileState=disabled` (installer `--install` / `--rollback` terminal state), any `ActiveState` within dwell | `timer_not_enabled`, exit non-zero — never `ok` |
| R3 | probe, enabled + inactive but inactive **less** than stopped-dwell (live #1104 window) | not `timer_stopped`; graded on remaining signals; `ok` only if manifest is also fresh |
| R4 | probe, timer active, `NextElapseUSecRealtime` empty | `timer_not_scheduled`, exit non-zero |
| R5 | probe, timer active, `NEXT` beyond the next-dwell threshold | `timer_not_scheduled`, exit non-zero |
| R6 | probe, manifest age between the manifest-age threshold and 168 h | `manifest_stale`, exit non-zero |
| R7 | probe, manifest age >= 168 h | `manifest_expired`, exit non-zero (distinct from R6) |
| R7b | probe, timer stopped past dwell **and** manifest also stale | `manifest_expired` if >= 168 h, else `timer_stopped` — first-match-wins order is asserted, not left to the implementer |
| R8 | probe, `systemctl` missing or exiting non-zero | `probe_failed`, exit non-zero, never `ok` |
| R8b | probe, `ActiveState=inactive` with empty or unparseable `InactiveEnterTimestamp` | `probe_failed`, exit non-zero — dwell arithmetic undefined, fail closed |
| R9 | probe, refresh receipt missing / unreadable / schema-invalid | `probe_failed`, exit non-zero, never `ok` |
| R10 | config with either freshness threshold >= 168 | rejected at config time, non-zero |
| R11 | probe source scanned for `start\|stop\|enable\|disable\|restart\|daemon-reload` | zero hits |
| R12 | probe run against the fake `systemctl`, every invocation recorded | only `show` and `list-timers` subcommands ever invoked |
| R13 | probe source scanned for non-stdlib imports | zero hits (self-contained, D4) |
| R14 | probe receipt written | parent dir private, file mode 0600, bounded size, required fields present, no env values other than the integer thresholds and unit name |
| R15 | installer `--install` / `--enable` / `--rollback` | `nhms-compute-scheduler.timer` **and** `.service` enabled/active states byte-equal before and after; refresh timer/service states likewise unchanged |
| R16 | dry-run, direct-grid + worker mirror, N models | `outcome=dry_run`, `reason=dry_run_complete`, `phase=complete`; registry and mirror `entry_count` both N |
| R17 | dry-run boundaries: single model and N models | counts agree at 1 and at N; no literal 76 anywhere in the assertion |
| R17b | dry-run over an **empty** model set, both paths | fails closed with `provider_invalid` — direct-grid at `:896-898`, non-direct-grid at `:961-962` (empty readiness). The runner catches the `RefreshError` and persists a terminal `outcome=failed` / `reason=provider_invalid` receipt with an empty `providers` list, which is correct: what is unreachable is a **successful zero-count `dry_run` receipt**, and neither guard may be relaxed to make one reachable |
| R18 | dry-run of any kind | registry, mirror, readiness, state bytes and preimages identical before and after |
| R19 | successful dry-run | receipt in history **and** latest; emergency reservation removed; no 0-byte reserved slot |
| R20 | dry-run with divergent canonical/mirror preimage | still fails closed at `:743-744` with `provider_invalid` (unchanged) |
| R21 | dry-run failing after the precommit gate | real reason preserved; explicitly not `primary_receipt_failed` |
| R22 | non-dry-run refresh (unchanged sibling) | publish order, CAS, rollback, postcommit mirror-sha check, receipt semantics unchanged |
| R23 | env template (unchanged sibling consumer) | satisfies every entry in the README required-key block, values included |

## Boundary Surfaces

- Shared helper roots: `_provider_evidence` is shared by all four providers in
  both dry-run and real paths — the fix must not change its behaviour for
  non-mirror providers or for real publishes.
- Public entrypoints: `scheduler_file_provider_refresh_once.sh --dry-run`,
  the new probe CLI, the new probe installer.
- Read surfaces: `systemctl --user show|list-timers`, refresh `latest.json`.
- Write/delete/overwrite surfaces: the probe writes only its own receipt; it
  writes nothing under the provider store.
- Staging/publish/rollback surfaces: untouched for `not dry_run`.
- Producer/consumer evidence boundaries: probe receipt (new) vs refresh receipt
  (existing, read-only to the probe).
- Stale-state/idempotency boundaries: a probe tick is pure read + one receipt
  write; repeated ticks are idempotent apart from the receipt timestamp.
- Unchanged downstream consumers: the 168 h consumer chain
  (`scheduler_file_providers.py` -> `scheduler_runtime.py` ->
  `retention_frontier.py` -> `scheduler_journal_retention.py`), untouched.

## Risks / Trade-offs

- **The alert does not leave node-22.** A `failed` user unit plus a 0600 local
  receipt is only an alert to whoever looks at that host, and nothing polls
  node-22's failed units today. This is accepted, not mitigated (D2): the change
  converts "invisible until the 168 h cliff" into "discoverable in hours on the
  host", and off-host routing is a separate alerting lane with its own auth and
  delivery surface. Recorded as a known limit and filed as a follow-up at merge.
- The probe cadence adds ticks on a production compute node. Bounded: hourly,
  stdlib-only, two read-only `systemctl` calls and one small JSON read, with a
  short `TimeoutStartSec` so a wedged probe becomes visible instead of hanging.
- Thresholds are judgement calls. All three are env-overridable; the two
  freshness thresholds are asserted to stay strictly under 168 h. The 6 h
  stopped-dwell trades detection latency for silence during legitimate #1104
  windows — a real trade, bounded at 6 h against a 168 h budget.
- The probe duplicates a small bounded-read helper rather than importing
  `packages/common/safe_fs.py`. Deliberate (D4): a watchdog that only runs from
  inside the checkout it watches cannot produce pre-merge live evidence without
  deploying unreviewed code to the live tick. The duplicated surface is a few
  lines of `O_NOFOLLOW` + size-capped read, covered by its own tests.
