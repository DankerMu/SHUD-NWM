## MODIFIED Requirements

### Requirement: Operator-action decisions SHALL be enumerable from db-free pass evidence

The scheduler CLI SHALL provide a read-only `list-operator-actions` subcommand that scans the most recent N terminal scheduler pass evidence files under the evidence root (`--evidence-root`, defaulting to `NHMS_SCHEDULER_EVIDENCE_ROOT`), excluding `.pre_execution.json` files and ordering by modification time. It SHALL identify blocked candidates by decision literal — `permanent_failure`, `cancelled_manual_retry_required`, `blocked_strict_warm_start_init_state_mismatch`, `blocked_journal_predecessor_identity_quarantine`, `blocked_operator_reentry_restart_stage_refused` — not by the `manual_retry_required` flag, so that bounded-summarized passes remain enumerable. That set SHALL equal the set of decisions the db-free scheduler writes a **literal** `manual_retry_required: true` on, because a decision missing from it is answered with `exit 0` ("nothing waits") while the runbook still prescribes an operator action for it. The writers that instead set the flag from an expression SHALL be enumerated and dispositioned rather than ignored, so that a new one cannot enter unnoticed. The two that exist (`scheduler_state_failure.py:514` `retry_downstream` and `:1954` `retry_failed`, both `failure["permanent"]`) are outside the listed set because neither can be built with the flag true: `:498` returns `None` for a permanent failure before the first dict is built, and for the second the guard is at the call site — `scheduler_state_decision.py:385` returns the permanent `blocked` decision before the `retry_failed` return point at `:412`. The same `_failure_retry()` evidence reaching the missing-forcing channel (`scheduler_state_decision.py:373`) is likewise safe: every return point there goes through `_artifact_blocker_evidence` (`scheduler_state_failure.py:909`), which writes its own `decision` (`:927`) and its own literal `manual_retry_required: false` (`:947`) and inherits neither. It SHALL also list each model named in a not-selected `source_cycles` entry whose `selection_reason` is `journal_predecessor_identity_quarantine_breaker_engaged`, because a breaker-released cycle never reaches candidate construction. Each listed action SHALL carry `candidate_id`, `source_id`, `cycle_time`, `model_id`, `decision`, `reason`, `attempt`, `retry_limit`, `occurrences`, `recorded_init_state_id` (null when absent), and first/last seen pass. When one action appears in several scanned passes it SHALL be listed once, and apart from `first_seen_pass` and the seen count **every** value field of that entry SHALL be the value carried by `last_seen_pass` — the operator feeds `recorded_init_state_id` from this receipt into `confirm-operator-reentry`, which refuses a stale token — except that a `candidate_id` absent from the newest pass (the breaker-released leg carries none) SHALL fall back to a known one. The command SHALL exit `1` when at least one action is listed, `0` when none, and `2` when the evidence root is missing or unreadable or `--passes` is not an integer `>= 1`; an individual unreadable pass file — including one deleted between the directory scan and its own `stat`, as the evidence retention timer may do at any time — SHALL be reported under `unreadable_passes` and SHALL NOT abort the scan. A scanned pass SHALL be decidable only when it is readable and its terminal status belongs to a closed allowlist of statuses known to be written only after candidate construction ran (a post-construction status missing from the allowlist errs toward undecidable), and it is not a size-fallback product (`resource_limit_blocked` carrying `limit.pre_limit_status`), because that product keeps at most a capped projection of breaker-released `source_cycles` and drops every other source cycle, so it cannot show that nothing else was released — its summarized blocked candidates SHALL still be listed, and when its `limit.source_cycles.status` is `summarized` each model of each projected breaker-released entry SHALL be listed too, the pass being reported non-evaluating with reason `size_fallback_source_cycles_summarized`; a size-fallback product whose marker is absent or `dropped` SHALL be reported with reason `size_fallback_source_cycles_absent`. A pass written by the non-blocking summary tier (true status, `evidence_compaction.mode: non_blocking_summary`, no `limit`) is not a size-fallback product and SHALL be read exactly as the same pass written in full; every other pass (for example `lock_contended`, `preflight_blocked`, or an unknown status) SHALL be reported under `non_evaluating_passes` with its status. A transparent pass is one whose status belongs to a closed set — `lock_contended` and `preflight_blocked` — known either to have evaluated nothing or to have written its full candidate lists and `source_cycles`; a transparent pass never hides anything. When no action is listed and at least one scanned pass dropped its candidate lists under the evidence byte budget, or a pass file vanished between the directory scan and its `stat` (its modification time was never read, so it cannot be placed in the scan's time order at all and vetoes `exit 0` wherever it sat), or no scanned pass is evaluating and scope-complete, or any pass that is neither evaluating-and-scope-complete, nor transparent, nor merely scope-narrowed is newer than the newest evaluating and scope-complete pass (it may have evaluated candidates it cannot show — a size-fallback product, an unreadable pass file, a `lease_lost` or exception-path `resource_limit_blocked` pass that emptied its lists, an unknown status, or a pass whose scope keys are missing — and so may hide a breaker release that engaged after that pass), or a pre-execution reservation is orphaned (the terminal pass evidence file named from its own pass id is absent, its `reserved_at` is newer than the payload `started_at` of the newest evaluating and scope-complete pass, and its modification time is older than twice its recorded lease ttl or it records no lease — the pass's lease heartbeat refreshes a live reservation's modification time, so a fresh one is in flight and changes nothing), the command SHALL report those passes (and such reservations under `orphan_reservations`) and exit `3` (undecidable) instead of `0`. Reservation files remain excluded from the pass scan itself. The three triggers are anchored on **evaluating and scope-complete**, not on "decidable": a scope-narrowed pass is decidable yet answers only for its own scope (see the narrowed-scope requirement below), so it neither backs `exit 0` nor forces `exit 3`. The bounded candidate summary SHALL retain `retry_policy` `attempt`, `retry_limit`, `occurrences`, and `manual_retry_required`, including false and zero values, and SHALL retain `journal_predecessor_identity.recorded_init_state_id`. That last key is not decoration: the summary drops `state_evidence` wholesale, so without retaining it a newest pass that fell back to the bounded summary would report `recorded_init_state_id: null` for a candidate whose token the scheduler did record — and `confirm-operator-reentry`'s breaker arm requires that token, while its `recorded_init_state_id_mismatch` refusal returns only `occurrences` and `quarantine_rerun_count` and no other operator surface prints the live token. Scanning with `--passes 1` does not recover it: the token is exactly what the newest pass dropped. Because the reader takes every value field from `last_seen_pass`, retention at the writer — not a fallback to an older pass — is what makes the token correct rather than stale. The `recovery_runbook` slug returned by the display API's manual-action 409 SHALL name an existing file under `docs/runbooks/`.

#### Scenario: Summarized pass still lists a budget-exhausted candidate
- **WHEN** the latest pass evidence was bounded-summarized and contains a blocked candidate whose decision is `blocked_strict_warm_start_init_state_mismatch`
- **THEN** `list-operator-actions` SHALL list it with `attempt` and `retry_limit` taken from the retained bounded keys
- **AND** the command SHALL exit `1`

#### Scenario: Breaker-released cycle is listed from source-cycle evidence
- **WHEN** a pass released a breaker-engaged cycle from the backfill slot so no candidate entry exists for it
- **THEN** `list-operator-actions` SHALL list each model of that not-selected entry with decision `blocked_journal_predecessor_identity_quarantine`

#### Scenario: Dropped candidate lists are undecidable
- **WHEN** no action is found and a scanned pass marked its candidate lists as dropped
- **THEN** the command SHALL exit `3` and name that pass

#### Scenario: A window without an evaluating pass is undecidable
- **WHEN** every scanned pass is unreadable or non-evaluating (for example lock-contended or preflight-blocked) while an older evaluated pass outside the window holds a blocked candidate
- **THEN** the command SHALL list those passes under `unreadable_passes` or `non_evaluating_passes` and exit `3`, never `0`

#### Scenario: A window of size-fallback passes is undecidable
- **WHEN** every scanned pass is a size-fallback product whose breaker-released source-cycle projection was dropped (marker `dropped` or absent) and which carries no other listed decision
- **THEN** the command SHALL report those passes under `non_evaluating_passes` and exit `3`, never `0`
- **AND WHEN** such a pass instead carries a `summarized` projection holding a breaker-released cycle
- **THEN** each of its models SHALL be listed and the command SHALL exit `1`
- **AND WHEN** such a pass still carries a summarized blocked candidate of a listed decision
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`

#### Scenario: A hidden pass newer than every evaluating and scope-complete pass is undecidable
- **WHEN** no action is listed, an older scanned pass is evaluating and scope-complete, and a newer scanned pass is a size-fallback product, unreadable, `lease_lost`, or of an unknown status — whether or not a still newer transparent pass follows it
- **THEN** the command SHALL exit `3`, never `0`
- **AND WHEN** instead only transparent passes are newer than the newest evaluating and scope-complete pass
- **THEN** the command SHALL exit `0`

#### Scenario: A pass file deleted mid-scan is reported and vetoes exit 0
- **WHEN** no action is listed, every scanned pass is evaluating and scope-complete, and one pass file is deleted between the directory scan and its `stat`
- **THEN** the command SHALL still scan and report the remaining passes, SHALL name the deleted file under `unreadable_passes`, and SHALL exit `3`
- **AND** it SHALL NOT exit `2`, which names the evidence root and would send the operator after a root that is in fact correct

#### Scenario: A candidate seen in several passes reports the newest pass's values
- **WHEN** the same candidate is listed by three scanned passes carrying different `recorded_init_state_id` values
- **THEN** it SHALL be listed once with `first_seen_pass` naming the oldest of them, `last_seen_pass` naming the newest, and `recorded_init_state_id` (and every other value field) taken from that newest pass

#### Scenario: The re-entry token survives bounded summarization
- **WHEN** the same breaker-quarantined candidate appears in an older full pass and again in a newer pass that fell back to the bounded candidate summary
- **THEN** the merged entry SHALL name the newer pass as `last_seen_pass` and SHALL carry that newer pass's `recorded_init_state_id` rather than `null`
- **AND** the token SHALL come from the newer pass's retained summary key, never from a fallback to the older pass, so the operator is given a current token and not a stale one

#### Scenario: The listed decision set covers the refused re-entry sink
- **WHEN** a scanned pass is evaluating and scope-complete and the only blocked candidate of a listed decision it carries is one whose decision is `blocked_operator_reentry_restart_stage_refused`
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`, never `0`

#### Scenario: No operator actions
- **WHEN** the scanned passes contain only blocked candidates of other decisions, at least one scanned pass is evaluating and scope-complete, no pass dropped its candidate lists, no pass file vanished mid-scan, and only transparent or scope-narrowed passes are newer than the newest evaluating and scope-complete pass
- **THEN** the command SHALL print an empty `operator_actions` list and exit `0`

#### Scenario: Full and summarized passes list the same actions

- **WHEN** the same pass evidence is written once in full and once through the non-blocking summary tier
- **THEN** `list-operator-actions` SHALL return identical `operator_actions`, `non_evaluating_passes` and exit code for both

#### Scenario: Summarized source cycles list a breaker release

- **WHEN** the newest pass is a size-fallback product whose `limit.source_cycles.status` is `summarized` and whose projection holds a breaker-released cycle
- **THEN** each of its models SHALL be listed with decision `blocked_journal_predecessor_identity_quarantine`, the pass SHALL be reported with reason `size_fallback_source_cycles_summarized`, and the command SHALL exit `1`

#### Scenario: A crashed submit pass is not answered by an older pass

- **WHEN** an older evaluating and scope-complete pass lists no action and a newer reservation has no final artifact and a stale lease
- **THEN** the command SHALL list that reservation under `orphan_reservations` and exit `3`

#### Scenario: An in-flight pass does not add noise

- **WHEN** the newer reservation's heartbeat is fresh
- **THEN** the command SHALL behave exactly as without that reservation


## ADDED Requirements

### Requirement: Scheduler pass evidence SHALL record the executed backfill leg

Scheduler pass evidence SHALL record, as `backfill.mode`, the discovery leg `discover_cycles` actually executed (`backfill` or `legacy`) alongside the configured `backfill.enabled`, taken from discovery itself rather than recomputed. `list-operator-actions` SHALL keep reporting a pass whose registry selected no models as `no_models_evaluated`, and SHALL report a pass whose evidence claims `backfill.enabled` true, `backfill.mode` `legacy` and a non-zero `counts.selected_model_count` as `scope_unknown`.

#### Scenario: A zero-model backfill pass records the legacy leg

- **WHEN** backfill is enabled and the model registry selects no models
- **THEN** the pass evidence SHALL carry `backfill.enabled: true` and `backfill.mode: legacy`, and `list-operator-actions` SHALL report it `no_models_evaluated` and never exit `0` on its account

#### Scenario: A normal backfill pass records the backfill leg

- **WHEN** backfill is enabled and models are selected
- **THEN** the pass evidence SHALL carry `backfill.mode: backfill` and the listing SHALL behave as before

### Requirement: The evaluating pass-status allowlist SHALL be bound to its writers

The closed allowlist of evaluating pass statuses SHALL be reconciled by a test that reads the scheduler writer sources as text, resolves every statically written pass status (including the size-fallback status and the literal fallback arguments of the evidence-status helper), fails on any unresolved write site that is not explicitly enumerated with its reason, and declares the passthrough of an execution-evidence status as a dynamic source it cannot cover.

#### Scenario: A new writer status is caught

- **WHEN** a writer gains a new pass-status literal not reconciled with the allowlists
- **THEN** the closure test SHALL fail
