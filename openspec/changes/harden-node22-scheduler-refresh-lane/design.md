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
| 1 | `probe_failed` | the systemd query could not be executed or returned an error; or `ActiveState != active` while `InactiveEnterTimestamp` is empty or unparseable, leaving the dwell arithmetic undefined | non-zero |
| 2 | `manifest_expired` | manifest age is **resolved** (D3b) and >= 168 h — the consumer is already fail-closed | non-zero |
| 3 | `timer_stopped` | `ActiveState != active` **and** `now - InactiveEnterTimestamp` exceeds the stopped-dwell (D3a) | non-zero |
| 4 | `timer_not_enabled` | `UnitFileState` is anything other than `enabled` (`disabled`, `linked`, `masked`, `static`, `not-found`) | non-zero |
| 5 | `timer_not_scheduled` | timer is `active` but `NextElapseUSecRealtime` is empty, or `NEXT` is further out than the next-dwell threshold | non-zero |
| 6 | `manifest_stale` | manifest age is **resolved** and >= the manifest-age threshold (and < 168 h). Both manifest comparisons are at-or-over, never strictly-greater, so an age exactly equal to a threshold is already a finding | non-zero |
| 7 | `manifest_unavailable` | no manifest age could be resolved from `latest.json` **or** from any receipt scanned in the bounded history fallback (D3b) | non-zero |
| 8 | `ok` | none of the above | 0 |

Four ordering choices carry weight:

- `probe_failed` is first so unreadable **systemd** evidence can never fall
  through to healthy. It also absorbs the one input on which the dwell
  arithmetic is undefined (`inactive` with no parseable
  `InactiveEnterTimestamp`): fail closed rather than guess. It deliberately no
  longer absorbs an unresolvable manifest — see the next bullet and D3b.
- `manifest_unavailable` sits **last before `ok`**, not first. An unresolvable
  manifest says nothing about the timer, and grading it at precedence 1 masked
  every timer verdict beneath it: one failed rehearsal receipt would hide a
  genuinely stopped timer behind a generic `probe_failed` for as long as that
  receipt stayed newest. The two evidence sources are independent, so they fail
  independently — systemd unreadable is still precedence 1, manifest
  unresolvable is its own non-zero verdict, and the timer signals grade normally
  in between. Rows 2 and 6 are skipped, not defaulted, when the age is
  unresolved: an absent number is never compared against a threshold.
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
(`NHMS_REFRESH_HEALTH_STOPPED_DWELL_HOURS`).

The bound on these was originally written as "strictly under 168", with the
prose claiming they "sit far enough below 168 h to leave operator margin". Those
are not the same statement, and only the prose one is useful: `167` passed the
literal check while leaving 0.6% of the consumer's budget as warning. Measured,
`STOPPED_DWELL_HOURS=167` with `MAX_MANIFEST_AGE_HOURS=167` grades the 2026-08-28
geometry — `enabled` + `inactive` for six days, manifest stale — as `ok`, exit 0,
for 166 hours. The margin is now enforced rather than described:

- Every threshold must leave at least **24 h** of margin below the consumer's
  168 h bound, so the accepted range is `1..144`. Twenty-four hours is one full
  refresh cadence: the alarm must fire with at least one whole scheduled refresh
  still able to land before the consumer fail-closes.
- `STOPPED_DWELL_HOURS` is additionally capped at **24 h**, one cadence. A timer
  idle longer than its own period has already missed a tick, whatever the
  operator's publisher window was for.

The shipped defaults (36 / 120 / 6) sit well inside both rules. An out-of-range
value is refused before any evidence is collected, so a bad threshold fails
closed rather than grading `ok`.

The manifest signal is computed and recorded independently of the systemd
signals, so the receipt still carries `manifest_age_hours` when systemd is
unreadable (and the systemd fields when the receipt is unreadable). Because the
two sources are graded independently as well as recorded independently, each one
has its own fail-closed verdict: unreadable systemd grades `probe_failed` (row
1), an unresolvable manifest grades `manifest_unavailable` (row 7), and neither
can mask the other's signal. No evidence failure of either kind reaches `ok`.

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

### D3b: the manifest age is resolved with a bounded history fallback

The manifest signal's source of record is the refresh runner's receipt — the
`registry` provider's `after_generated_at`, located by name and never by index.
The original design read exactly one file, the configured `latest.json`, and
treated any failure to extract that field as `probe_failed` at precedence 1.
That is wrong in a way the live lane reaches routinely: `latest.json` is
overwritten by **every** run, including a run that fails before it assembles a
provider list (the empty-`providers` terminal receipt of R17b is exactly this
shape). One such run leaves the probe unable to read a manifest age until the
next successful refresh — up to ~24 h on the daily cadence — and at precedence 1
that generic verdict masked `timer_stopped` and `timer_not_enabled` for the whole
window. The probe would have alarmed, but with the wrong fact, and the green
facade this change exists to close would have been replaced by a noisy one.

Resolution order, first success wins:

1. The configured `latest.json`.
2. The sibling `history/` directory of that file. The runner writes every
   receipt there as `refresh_<YYYYmmddTHHMMSSZ>_<uuid12>.json`
   (`scripts/scheduler_file_provider_refresh.py:611`), so the fixed-width UTC
   prefix makes a **lexical descending sort chronological** — no timestamp
   parsing is needed to order the candidates, and no `mtime` is trusted.
   Candidates are filtered to that filename shape, the directory listing is
   capped at 200 entries, and at most the 10 newest are opened. Each read is
   bounded and `O_NOFOLLOW` exactly as `latest.json` is.
3. If neither yields a parseable `registry.after_generated_at`, the age is
   unresolved and the verdict is `manifest_unavailable` (row 7).

The fallback deliberately does **not** gate on the receipt's `outcome`. A
receipt that carries a parseable `registry.after_generated_at` is reporting a
manifest that really was published at that instant, whatever the run's terminal
outcome was; a receipt that does not carry one is skipped whatever its outcome
says. One predicate, applied uniformly, rather than a second vocabulary to keep
in sync with the runner's. `dry_run` receipts are ordinary candidates for the
same reason: a dry run leaves `after_generated_at` equal to the pre-image, which
is the current manifest's real generation time.

The receipt records which source answered, as a closed three-shape field
`manifest_source`: `latest`, `history:<filename>`, or `unavailable`. That is the
difference between "the lane is fine and `latest.json` is merely a failed
rehearsal" and "nothing has published in a week", and it is the field an
operator reads first when the verdict is `manifest_unavailable`.

Scanning history costs at most 10 bounded reads of a directory the runner already
caps, on a probe that runs hourly. It buys the thing the fallback exists for: a
failed rehearsal no longer produces an alarm at all, because the previous
successful receipt still answers the question.

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
[--health-receipt-root PATH] [--now ISO8601] [--json]`. `--json` writes the
verdict document to stdout in addition to the receipt. Exit status is the
contract: 0 for `ok`, non-zero otherwise.

Every flag has an env default **except `--now`**, which is CLI-only and has no
environment seam at all. `--now` pins the clock, so an env default for it is a
false-green vector: a `NHMS_REFRESH_HEALTH_NOW` inherited into the probe unit's
environment — from a stray user-manager variable or a hand-edited drop-in —
would make the probe grade a pinned past instant and report `ok` with exit 0 on
the exact geometry this change exists to catch, silently and forever. Deleting
the seam is strictly better than unsetting it in the unit file: an
`UnsetEnvironment=` entry is a defence that has to be maintained in lockstep with
the flag list and that also interacts with drop-in `Environment=` lines, whereas
a flag that reads no environment cannot be poisoned by any environment. The
thresholds and paths keep their env defaults — they are the operator's
documented drop-in tuning surface.

Be precise about what that buys, because the tempting stronger claim is false.
The env surface splits into four kinds, and only the first two are hardened:

- **The clock** — removed outright. No env seam exists.
- **The thresholds** (`MAX_NEXT_DWELL_HOURS`, `MAX_MANIFEST_AGE_HOURS`,
  `STOPPED_DWELL_HOURS`) — range-checked at config time to `1..144`, with
  `STOPPED_DWELL_HOURS` additionally capped at 24 h (D3). They move *where* the
  alarm line sits, within a range that always leaves at least one full refresh
  cadence before the consumer's own fail-closed cliff, and an out-of-range value
  is refused before any evidence is collected rather than grading `ok`.
- **The target selectors** (`UNIT`, `REFRESH_RECEIPT`, `RECEIPT_ROOT`,
  `SYSTEMCTL`) — **unbounded, and a poisoned value can absolutely produce `ok`**:
  point `UNIT` at a healthy unrelated timer, or `SYSTEMCTL` at a script that
  prints a healthy fixture, and the probe faithfully grades what it was pointed
  at. These are not a defence and must not be described as one.
- **The output switch** (`JSON`) — selects whether the verdict document is also
  printed to stdout. It reaches neither the grading nor the target, and is listed
  only so the classification is exhaustive rather than silently incomplete.

The reason that is acceptable, where the clock seam was not, is the trust level.
The selectors say *what to inspect and with what tool*; they do not change how
collected signals are graded. Anyone able to set the probe unit's environment
already runs as the same UID that owns the probe script, its unit file, and the
timer itself — they can simply edit the probe. So the selectors sit at the same
trust level as the code, and hardening them buys nothing real. The clock was
different in kind: it changed the *grading* of honestly-collected evidence, and
it had a plausible accident path — an inherited or copy-pasted variable — that
required no attacker at all. The invariant this change actually holds is
therefore about grading, not about targeting: **no probe-specific environment
variable can make the probe misgrade the signals it did collect.**

One implicit input is a named exception, recorded rather than claimed away:
**`TZ`**. systemd renders most timestamp properties in the host's local zone, so
the probe must interpret a zone token, and it resolves a non-UTC token against
the running process's own zone. A `TZ` that differs from the emitting user
manager's would shift every parsed instant uniformly and could move a dwell
across a threshold — genuinely a misgrading of collected evidence. It is
accepted, not defended, for three reasons: the probe and the systemd that
emitted the token are the same host and the same user manager, so they share a
zone by construction; there is no reliable way to make systemd emit a uniform
zone, since `--timestamp=utc` was measured to convert only some properties (R8c);
and `TZ` is a stdlib-wide input, not a surface this probe invents. What the
change does owe is determinism, and R8c supplies it: the parse is pinned against
hardcoded strings in both a UTC and a non-UTC zone, so the behaviour is asserted
rather than incidental.

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
| R4b | probe, timer active, `NextElapseUSecRealtime` **present but unparseable** | `timer_not_scheduled`, exit non-zero — a distinct branch from R4's empty value, asserted separately |
| R5 | probe, timer active, `NEXT` beyond the next-dwell threshold | `timer_not_scheduled`, exit non-zero |
| R6 | probe, manifest age between the manifest-age threshold and 168 h | `manifest_stale`, exit non-zero |
| R7 | probe, manifest age >= 168 h | `manifest_expired`, exit non-zero (distinct from R6) |
| R7b | probe, timer stopped past dwell **and** manifest also stale | `manifest_expired` if >= 168 h, else `timer_stopped` — first-match-wins order is asserted, not left to the implementer |
| R8 | probe, `systemctl` missing or exiting non-zero | `probe_failed`, exit non-zero, never `ok` |
| R8b | probe, `ActiveState=inactive` with empty or unparseable `InactiveEnterTimestamp` | `probe_failed`, exit non-zero — dwell arithmetic undefined, fail closed |
| R8c | timestamp parser, a literal `systemctl show` string carrying a **non-local** zone abbreviation, asserted without relying on the test host's own zone | parsed per the zone token, not per `.astimezone()` of the runner; `UTC`/`GMT`/`Z` read as UTC, any other token read as the emitting host's local zone. `systemctl --timestamp=utc` is **not** adopted: measured on node-22 (systemd 255) it converts `InactiveEnterTimestamp` but leaves `NextElapseUSecRealtime` in local time, so it yields a mixed-zone surface rather than a uniform one |
| R9 | probe, `latest.json` missing / unreadable / schema-invalid / carrying no `registry` provider, **and** a newer-than-threshold history receipt resolves the age | the manifest age is taken from history; `manifest_source` is `history:<filename>`; the timer signals grade normally and a healthy lane grades `ok`, exit 0 |
| R9b | probe, `latest.json` unresolvable, crossed with **each** of the three timer verdicts | the timer verdict wins every time — `timer_stopped`, `timer_not_enabled`, and `timer_not_scheduled` (active with empty or unparseable `NextElapseUSecRealtime`) — never masked by a manifest-evidence verdict. All three arms are asserted: the spec says "SHALL NOT mask **any** timer verdict", and pinning two of three let the precedence-7 arm be hoisted above `timer_not_scheduled` with the suite still green |
| R9c | probe, `latest.json` unresolvable **and** no history candidate resolves an age | `manifest_unavailable`, exit non-zero, never `ok`; `manifest_source` is `unavailable` |
| R9d | probe, history fallback bounds | directory listing capped at 200 entries and at most 10 candidates opened; each read bounded and `O_NOFOLLOW`; only `refresh_<UTC>_<uuid>.json` names considered; candidates ordered by lexical descending filename, never by `mtime` |
| R10 | config thresholds outside their enforced range | rejected at config time, non-zero, before any evidence is collected. Enforced range is `1..144` for all three (>= 24 h of margin below the consumer's 168 h bound), with `STOPPED_DWELL_HOURS` additionally capped at 24 h. Asserted at the boundaries: 144 accepted, 145 refused; stopped-dwell 24 accepted, 25 refused; and the previously-accepted 167 refused |
| R10b | no in-range threshold combination grades a dead lane `ok` past one cadence | property test over the accepted threshold ranges: for the 2026-08-28 geometry (`enabled` + `inactive`, manifest stale), no accepted combination yields `ok` once idle exceeds 24 h. This is the executable form of the margin claim — the prose version held for the defaults only |
| R11 | probe source scanned for `start\|stop\|enable\|disable\|restart\|daemon-reload` | zero hits |
| R11c | the probe timer's own liveness is checkable, and no unit comment claims otherwise | `Persistent=` on the probe timer compensates a missed tick only when the timer transitions to active (boot, or an explicit `start`); it does nothing for a timer left `enabled` + `inactive`, which is the 2026-08-28 geometry. Any unit comment implying the probe catches itself up is removed. The steady-state check is documented for the probe timer exactly as it is for the refresh timer: `list-timers` `NEXT` is not `-`, and the probe receipt's `generated_at` is recent |
| R11b | probe env surface, enumerated | every environment variable the probe reads is classified into exactly one of the four kinds in D4 — removed (the clock), bounded-and-fail-closed (the three thresholds), target selector (`UNIT`, `REFRESH_RECEIPT`, `RECEIPT_ROOT`, `SYSTEMCTL`), or output switch (`JSON`). The asserted invariant is the grading one: **no probe-specific env variable can make the probe misgrade the signals it did collect** — the clock has no env seam, and every threshold is refused out of range before evidence is collected. Target selectors are explicitly *not* claimed to be a defence; they sit at the same UID and trust level as the probe script itself. `TZ` is the one named exception, accepted and recorded in D4 rather than defended, with R8c pinning the parse so the behaviour is deterministic. Each variable is exercised through its env path, not only through its flag |
| R12 | probe run against the fake `systemctl`, every invocation recorded | only `show` and `list-timers` subcommands ever invoked |
| R13 | probe source scanned for non-stdlib imports | zero hits (self-contained, D4) |
| R14 | probe receipt written | parent dir private, file mode 0600, bounded size, required fields present, no env values other than the integer thresholds and unit name. The field set is closed and includes `manifest_source`, whose value is exactly one of `latest`, `history:<filename>`, `unavailable` |
| R14b | probe receipt write fails (short write, or an error mid-write) | fails closed with a non-zero exit **and** the previous good receipt is left intact — never truncated, never destroyed; the verdict and any evidence errors are printed to the journal before the process exits, since D2 makes the journal the alert channel |
| R15 | installer `--install` / `--enable` / `--rollback` | the protected units are unchanged across the run, compared per unit **type**: for the two **timers** both `UnitFileState` and `is-active` byte-equal before and after; for the two timer-driven **oneshot services** only `UnitFileState`, since a oneshot's `is-active` legitimately flips on its own cadence (the compute scheduler every 5 minutes, the refresh service inside its 02:15-04:15Z window) and comparing it would abort on a unit nobody touched. Proven behaviourally on **both** installers by a divergent second read that must abort the run and back it out — a source grep is not evidence, because reverting the call sites while leaving the helper functions in place as dead code keeps every grep matching |
| R15b | the protected-state baseline is captured per invocation, never read across invocations | `scheduler.before` / `protected.before` are written at the start of **every** action (`--install`, `--enable`, `--rollback`), so the assertion means "this invocation changed nothing" — exactly what R15 claims — and no on-disk format is ever a contract between two versions of the installer. Evidence: a test seeding a stale, differently-shaped baseline must leave `--enable` exiting 0 with the timer still armed, and `--rollback` exiting 0 with its status line printed. Nothing restores *from* this file; `refresh.before` is the restore data and keeps its existence precondition |
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

**The watchdog is itself unwatched, and that is a real limit of D2, not a
detail.** D2 makes the alert of record a failed node-22 user unit plus a local
receipt. That channel reports a probe that *ran and found something*. It reports
nothing about a probe that never ran: a timer left `enabled` + `inactive` never
enters `failed`, `list-units --failed` stays clean, and the last receipt keeps
reading `verdict: "ok"` indefinitely. The originating incident began exactly
this way — a timer stopped during a manual-publisher window and not restarted —
so the same operator action that produced the 2026-08-28 outage, applied one
level up to the probe timer, silently returns the lane to the pre-change blind
spot while every surface still looks healthy. `Persistent=` does not cover this;
it only replays a missed tick when the timer transitions to active. The change
does not close this recursion in code — doing so needs a watcher that is not
itself a node-22 user timer, which is the off-host routing this change
explicitly does not attempt. What it owes instead is honesty and a check an
operator can run: the recursion is recorded here, and the probe timer gets the
same documented steady-state row the refresh timer has (R11c).

- **The alert does not leave node-22.** A `failed` user unit plus a 0600 local
  receipt is only an alert to whoever looks at that host, and nothing polls
  node-22's failed units today. This is accepted, not mitigated (D2): the change
  converts "invisible until the 168 h cliff" into "discoverable in hours on the
  host", and off-host routing is a separate alerting lane with its own auth and
  delivery surface. Recorded as a known limit and filed as a follow-up at merge.
- The probe cadence adds ticks on a production compute node. Bounded: hourly,
  stdlib-only, two read-only `systemctl` calls and one small JSON read, with a
  short `TimeoutStartSec` so a wedged probe becomes visible instead of hanging.
- Thresholds are judgement calls. All three are env-overridable and all three
  are range-checked to `1..144`, with the stopped-dwell additionally capped at
  24 h, so every accepted configuration leaves at least one full refresh cadence
  before the 168 h cliff. The 6 h default stopped-dwell trades detection latency
  for silence during legitimate #1104 windows — a real trade, bounded at 6 h
  against a 168 h budget.
- The probe duplicates a small bounded-read helper rather than importing
  `packages/common/safe_fs.py`. Deliberate (D4): a watchdog that only runs from
  inside the checkout it watches cannot produce pre-merge live evidence without
  deploying unreviewed code to the live tick. The duplicated surface is a few
  lines of `O_NOFOLLOW` + size-capped read, covered by its own tests.
