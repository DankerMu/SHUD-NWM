## MODIFIED Requirements

### Requirement: Comment-based absence proof requires proven comment accounting capability

The restart-reconciliation comment capability probe SHALL classify one querier instance as comment-storing, explicitly comment-less, or unknown. A present `AccountingStoreFlags` line containing `job_comment` is comment-storing; a present line explicitly lacking that token, including `(null)`, is explicitly comment-less; probe execution failure or an absent `AccountingStoreFlags` line is unknown. The probe SHALL log execution failure differently from an explicit missing flag. The accepted-submit contract-version check SHALL still run before capability classification.

For a comment-storing cluster, exact-comment owner/global queries, coverage proof, confirmed absence, and retry-permission behavior SHALL remain unchanged. The global-visibility gate SHALL continue to apply to every exact-comment query scope it guarded before this change, and the existing raise-priority order after the contract-version check SHALL remain unchanged.

For an unknown cluster, and for legacy or non-forecast queries on an explicitly comment-less cluster, the querier SHALL raise its transient query-unavailable error with reason class `comment_accounting_unproven` before issuing any `sacct` command. Restart reconcile SHALL emit `action=query_unavailable`, keep the row reserved and unbound, and never record an absence conclusion.

Only an explicitly comment-less cluster and a current accepted-submit forecast cohort with a strict UTC `submission_attempt_started_at` plus non-empty expected Slurm user and account MAY enter the conservative fallback. The fallback SHALL issue one name-scoped accounting query for `nhms_forecast`, exact user/account, and the interval from the immutable attempt anchor through one frozen query-end instant. It SHALL render `--starttime` and `--endtime` in host-local wall-clock strings using the same rule as the existing comment query, request `JobID,JobName,State,ExitCode,Comment,User,Account,Submit,SubmitLine` (`SubmitLine` last, parsed as the rejoined remainder of the `|`-split row because a command line may itself contain `|`), and use the existing shared byte, logical-row, and whole-query timeout budget. The parser SHALL interpret a timezone-less Slurm `Submit` value in the same host-local timezone before converting it to UTC, reject a missing or unparsable Submit value on an otherwise eligible forecast/owner row as transient query-unavailable evidence, reject a submit instant outside the closed attempt window, validate exact owner/account and forecast-family job name before candidate classification, normalize accepted forecast array and step ids to a bare numeric master id, apply the submit-line key rule below, and retain at most two distinct counted masters because only zero, unique, and ambiguous classifications are required. Forcing, batch/extern, and unrelated job names SHALL be ineligible and SHALL NOT become identity-mismatch candidates or malformed-Submit evidence.

An eligible row's submit-line key SHALL be the single distinct value of the whitespace-delimited `--comment=<value>` tokens in its `SubmitLine`; a missing or empty `SubmitLine`, no such token, or two or more distinct values yield no key. A master's key SHALL be known only when every eligible row of that master yields the same key; otherwise the master's key is unknown. Match basis `submitline_exact` SHALL apply only when every eligible master in the window has a known key; the parser SHALL read every row of the bounded result set and SHALL decide the basis over all eligible masters (it SHALL NOT stop early), so an unknown-key master anywhere in the output forces `name_window_count`; the two-master cap bounds only the retained masters. Under `submitline_exact`, a master whose key differs from the reservation's `nhms_idem:<idempotency_key>` comment is provably another submission and SHALL be excluded before the two-master cap: it SHALL NOT count toward ambiguity or identity-mismatch evidence. Exclusion applies only to eligible rows whose Submit parsed, because the key is read after Submit: a missing or unparsable Submit on an owned forecast-family row remains malformed-Submit transient denial regardless of that row's submit-line key; exactly one remaining master is a unique candidate, and two or more remaining masters are a genuine duplicate submission that SHALL stay `ambiguous_fallback_match`. When any eligible master's key is unknown, no master SHALL be excluded and the pre-existing count-only classification SHALL apply to all eligible masters with match basis `name_window_count`, so a known-foreign master plus an unknown-key master remains `ambiguous_fallback_match`. The submit-line key SHALL NOT be consulted on comment-storing or unknown-capability clusters.

Exactly one fallback master MAY bind only after all remaining durable/runtime/ownership identity gates pass and durable claimant exclusivity is proven atomically. A bare Slurm master id already bound to any other active current accepted-submit master SHALL NOT bind. Every newly bound attempt SHALL persist immutable `slurm_binding_source`: `gateway_submit` for an ordinary successful submit commit, `slurm_exact_comment` for exact-comment recovery, or `slurm_name_window_unique` for name-window fallback recovery. Only accounting evidence MAY populate `slurm_accounting_submitted_at`; a successful name-window fallback bind SHALL require and persist its parsed strict-UTC sacct `Submit`. These attempt-scoped binding fields SHALL survive defer and terminal projection transitions and SHALL clear only when reclaim starts a new attempt. Existing `submitted_at` SHALL retain its gateway acceptance/commit-time semantics and SHALL NOT be used as canonical Slurm accounting `Submit` evidence.

A settled sibling master with the same numeric id SHALL be treated as recycled only when it has provenance-compatible strict-UTC `slurm_accounting_submitted_at` and that canonical instant differs from the new candidate. Equal canonical Submit, missing/malformed canonical Submit, `gateway_submit`, or exact-comment history without canonical accounting Submit SHALL block fail-closed. A later transition that reasserts `matched_bound` SHALL restore the legal current `reconciliation_source` from immutable `slurm_binding_source` and SHALL NOT replace fallback provenance with a factory-default exact-comment source.

Direct master projections MAY identify bounded source/cycle candidates, but canonical cycle authority SHALL decide occupancy so a stale, damaged, missing, or valid-but-wrong-kind projection cannot fabricate or hide an owner. Source/cycle discovery SHALL derive from the safe master filename before payload-kind checks; a decoded payload MAY assist no-lineage compatibility but SHALL NOT suppress canonical replay. Inventory cleanup SHALL restore a missing bounded flat locator from canonical authority before pruning a terminal anchor; first-migration backfill SHALL preserve a handoff anchor until that restore completes. Under concurrent cleanup, migration, and bind attempts, a fallback SHALL therefore observe either the canonical anchor or the bounded locator, never vacancy caused only by a derived-projection crash. For a candidate with match basis `name_window_count`, a candidate whose submit instant falls inside another current reserved-unbound forecast attempt's window for the same expected Slurm user/account SHALL have more than one durable claimant and SHALL NOT bind for any claimant; every claimant stays fail-closed regardless of reconcile iteration order or concurrent source/cycle writers. For a candidate with match basis `submitline_exact`, the durable claimants SHALL be only the current reserved-unbound forecast attempts whose own idempotency comment equals the candidate's submit-line key; an overlapping-window attempt with a different idempotency comment SHALL NOT be a claimant for that candidate. The typed commit SHALL receive the proven submit-line key and SHALL refuse, with zero journal bytes written, a commit whose key differs from the committing reservation's own idempotency comment. Active-owner and same-accounting-incarnation occupancy gates apply unchanged to both bases. Only a candidate with one current reserved claimant, no other durable owner of the same accounting incarnation, and all remaining identity gates passing MAY bind. The two reserved comment gates SHALL treat an empty accounting comment as not stored on this fallback path; a present comment different from the reservation's idempotency comment remains fatal at both gates. A successful fallback bind of either match basis SHALL atomically persist `reconciliation_source=slurm_name_window_unique`, `reconciliation_decision=matched_bound`, the matched bare Slurm id, `slurm_binding_source=slurm_name_window_unique`, and canonical `slurm_accounting_submitted_at`. `slurm_name_window_unique` SHALL be accepted only for `matched_bound`; every other current durable accounting decision remains `slurm_exact_comment` sourced without erasing immutable binding provenance.

Every unsuccessful fallback SHALL remain fail-closed and SHALL preserve the durable #1564 held authority tuple byte-for-byte: `status=reserved`, no `slurm_job_id`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven`. If that tuple is not yet present on the first pass, the only permitted durable write is its attempt-scoped establishment; no unsuccessful fallback may write a bind, status demotion, retry permission, identity-mismatch transition, or identity-blocked streak. Pass evidence SHALL distinguish the cases as follows:

- zero eligible masters: `action=fallback_no_match`, `match_count=0`;
- two or more distinct eligible masters: `action=ambiguous_fallback_match`, `match_count=2` (the bounded “at least two” value);
- one eligible master that fails a remaining identity gate: `action=identity_mismatch_blocked`, `match_count=1`;
- process/timeout/byte/row failure: `action=query_unavailable` with the existing bounded-query reason class;
- missing or unparsable Submit evidence: `action=query_unavailable`, `reconciliation_reason_class=fallback_submit_unparsable` in pass evidence only.

Every fallback outcome that classified at least one remaining master SHALL additionally carry the pass-evidence-only key `fallback_match_basis` (`submitline_exact` or `name_window_count`) on its `restart_reconcile.reserved_unbound.outcomes[]` entry, including a successful bind; the key SHALL NOT be persisted on the durable row.

The comment-unproven and unsuccessful-fallback outcome family deliberately SHALL NOT converge automatically: it SHALL NOT increment `identity_blocked_streak`, SHALL NOT enter the identity-mismatch release ladder, and SHALL NOT create any automatic absence/release exit. On a comment-less cluster, only a uniquely proven live job binds; row-scoped confirmed-dead disposal remains the documented guarded operator action.

#### Scenario: an explicitly comment-less cluster binds one unique owned candidate

- **WHEN** `AccountingStoreFlags=(null)`, a current forecast reservation has an immutable attempt anchor and exact expected user/account, and the bounded name-window query yields one in-window master with an empty comment that passes every remaining identity gate
- **THEN** the reservation binds that master exactly once with source `slurm_name_window_unique`, decision `matched_bound`, and the matched bare Slurm id

#### Scenario: ambiguity never binds or changes disposal authority

- **WHEN** the fallback query yields at least two distinct owned in-window forecast masters
- **THEN** pass evidence reports `ambiguous_fallback_match` and `match_count=2`, the row stays reserved and unbound, and the durable comment-unproven held tuple remains byte-for-byte valid for guarded operator demotion

#### Scenario: overlapping reserved attempts cannot claim one master

- **WHEN** one otherwise eligible master has a submit instant inside the attempt windows of two current reserved-unbound forecast masters with the same expected Slurm user/account
- **THEN** neither claimant binds the master, both rows stay reserved and unbound under the durable held tuple with streak zero, and the result is independent of row iteration or source/cycle lock order

#### Scenario: an already-bound master cannot be claimed again

- **WHEN** an otherwise eligible fallback master id is already bound to another active current accepted-submit master in any source or cycle
- **THEN** the reserved claimant cannot bind that id and stays under the durable held tuple, even when its own accounting query contains no second master

#### Scenario: terminal history distinguishes accounting incarnations only with canonical provenance

- **WHEN** a settled sibling carries the same numeric Slurm id and a provenance-compatible canonical `slurm_accounting_submitted_at`
- **THEN** canonical cycle authority blocks the fallback when that instant equals the candidate Submit and permits a recycled id only when the two canonical instants differ

#### Scenario: gateway time or missing accounting Submit cannot prove recycle

- **WHEN** a settled same-id sibling was bound by an ordinary gateway submit or exact-comment recovery without canonical `slurm_accounting_submitted_at`, even if its legacy `submitted_at` differs from the candidate Submit
- **THEN** the candidate stays held fail-closed; gateway acceptance time, microsecond formatting, absent provenance, and mixed provenance SHALL NOT be interpreted as numeric-id recycling

#### Scenario: terminal projection preserves original fallback bind provenance

- **WHEN** a name-window fallback-bound row passes through incomplete coverage, defer, and later complete terminal `matched_bound` projection
- **THEN** its immutable `slurm_binding_source` and canonical `slurm_accounting_submitted_at` survive, and the current matched-bound source is restored to `slurm_name_window_unique`; an exact-comment/gateway sibling remains exact-comment sourced

#### Scenario: derived projection failure cannot erase an accounting incarnation

- **WHEN** a terminal canonical master owns the candidate's exact `(Slurm id, canonical accounting Submit)` incarnation but its derived flat projection is stale, damaged, missing, or replaced by a valid legacy/candidate payload during steady-state cleanup, first migration, crash-resume, or a concurrent bind
- **THEN** safe filename identity still triggers canonical cycle replay, cleanup/migration preserves an anchor-to-flat locator handoff under journal-global serialization, canonical authority blocks the fallback, and no payload kind or ordering may bind the same incarnation twice

#### Scenario: concurrent sibling-source submissions bind by exact submit-line key

- **WHEN** an explicitly comment-less cluster holds the gfs and IFS reservations of one pass whose attempt windows both contain the two accepted forecast arrays, each array's `SubmitLine` carries `--comment=nhms_idem:<that source's key>`, and every remaining identity gate passes
- **THEN** each reservation excludes the other source's array, binds its own array exactly once with `reconciliation_source=slurm_name_window_unique`, `reconciliation_decision=matched_bound`, and pass evidence `fallback_match_basis=submitline_exact`, independent of reconcile iteration order

#### Scenario: a wide window with many foreign masters still binds the exact key

- **WHEN** the attempt window contains many other owned `nhms_forecast` masters whose submit-line keys all differ from the reservation comment and exactly one master whose key equals it
- **THEN** the foreign masters are excluded before the two-master cap and the equal master binds with basis `submitline_exact`

#### Scenario: two masters carrying the same key stay ambiguous

- **WHEN** two distinct in-window masters both carry a submit-line key equal to the reservation comment
- **THEN** pass evidence reports `ambiguous_fallback_match`, `match_count=2`, `fallback_match_basis=submitline_exact`, nothing binds, and the durable held tuple is preserved byte-for-byte

#### Scenario: an unknown submit-line key keeps count-only semantics

- **WHEN** at least one eligible in-window master has a missing, empty, key-less, internally inconsistent, or multi-valued `SubmitLine`, including a window holding one known-foreign-key master and one unknown-key master
- **THEN** no master is excluded, classification, durable claimant exclusivity, and the durable held tuple behave exactly as before this change, and pass evidence only gains `fallback_match_basis=name_window_count`, so two eligible masters stay `ambiguous_fallback_match` and an overlapping reserved sibling still blocks the bind

#### Scenario: the typed commit refuses a key that is not the reservation's own

- **WHEN** a `submitline_exact` commit is attempted with a submit-line key different from the committing reservation's idempotency comment
- **THEN** the commit is refused and zero journal bytes are written

#### Scenario: an exclusive claimant still binds

- **WHEN** one in-window master has exactly one current reserved claimant, no other current accepted-submit master owns its id, and every remaining identity gate passes
- **THEN** that claimant alone binds the master with source `slurm_name_window_unique`

#### Scenario: no fallback match is not an absence proof

- **WHEN** the explicitly comment-less fallback yields zero eligible masters, including a result set containing only forcing, batch/extern, or unrelated job names
- **THEN** pass evidence reports `fallback_no_match` and `match_count=0`, the row stays reserved and unbound, and no retry permission, identity-mismatch transition, or reservation-lost transition is written

#### Scenario: present-but-different comment remains fatal at both gates

- **WHEN** the unique name-window candidate carries a non-empty comment that differs from the reservation comment
- **THEN** both the reserved identity check and final bind guard refuse it, pass evidence reports `identity_mismatch_blocked` with `match_count=1`, and no bind/status/retry/streak write occurs beyond establishing the held tuple if needed

#### Scenario: fallback runtime identity failure stays held

- **WHEN** an explicitly comment-less query yields one exclusive candidate but the reservation's genuine runtime identity is missing or present-but-different
- **THEN** the candidate does not bind, pass evidence reports `identity_mismatch_blocked` with `match_count=1`, the durable held tuple remains valid, and repeated passes keep `identity_blocked_streak=0` without automatic release

#### Scenario: incomplete ownership cannot enter fallback

- **WHEN** the current reservation lacks expected user or account, or the candidate lacks or disagrees with either value
- **THEN** no name-window candidate binds and the row remains reserved and unbound under the durable held tuple

#### Scenario: malformed Submit evidence is transient denial

- **WHEN** an otherwise eligible name-window row has missing, unparsable, or out-of-window Submit evidence
- **THEN** it cannot bind or prove absence; unparsable evidence reports pass-only reason `fallback_submit_unparsable`, and the durable held tuple remains unchanged

#### Scenario: the attempt window is closed at both endpoints

- **WHEN** an otherwise eligible candidate's host-local Submit instant converts exactly to the immutable attempt anchor or frozen query-end
- **THEN** the instant is in-window, while an instant before the anchor or after the query-end is ineligible and cannot bind

#### Scenario: bounded query failure is transient denial

- **WHEN** fallback accounting exceeds the existing byte, logical-row, or whole-query timeout bound, or the subprocess fails
- **THEN** pass evidence reports `query_unavailable` with the applicable existing bounded-query reason, and no bind or absence transition occurs

#### Scenario: unknown capability remains query-free

- **WHEN** `scontrol` fails or its output omits `AccountingStoreFlags`
- **THEN** pass evidence reports `query_unavailable` / `comment_accounting_unproven` before any `sacct`, and name-window fallback is not attempted

#### Scenario: a comment-storing cluster is unchanged

- **WHEN** the probe proves `AccountingStoreFlags` includes `job_comment`
- **THEN** owner/global exact-comment queries page `sacct` exactly as before, owned matches bind with source `slurm_exact_comment`, and a coverage-complete confirmed absence past grace may still permit retry

#### Scenario: the probe runs once per querier instance

- **WHEN** one querier instance serves multiple queries in a session
- **THEN** capability classification executes at most once and its verdict is reused; rebuilding the querier on the next pass re-probes transient unknown state
