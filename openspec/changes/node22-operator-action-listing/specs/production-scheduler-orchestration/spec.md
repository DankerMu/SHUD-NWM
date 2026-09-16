## ADDED Requirements

### Requirement: Operator-action decisions SHALL be enumerable from db-free pass evidence

The scheduler CLI SHALL provide a read-only `list-operator-actions` subcommand that scans the most recent N terminal scheduler pass evidence files under the evidence root (`--evidence-root`, defaulting to `NHMS_SCHEDULER_EVIDENCE_ROOT`), excluding `.pre_execution.json` files and ordering by modification time. It SHALL identify blocked candidates by decision literal — `permanent_failure`, `cancelled_manual_retry_required`, `blocked_strict_warm_start_init_state_mismatch`, `blocked_journal_predecessor_identity_quarantine`, `blocked_operator_reentry_restart_stage_refused` — not by the `manual_retry_required` flag, so that bounded-summarized passes remain enumerable. That set SHALL equal the set of decisions the db-free scheduler writes a **literal** `manual_retry_required: true` on, because a decision missing from it is answered with `exit 0` ("nothing waits") while the runbook still prescribes an operator action for it. The writers that instead set the flag from an expression SHALL be enumerated and dispositioned rather than ignored, so that a new one cannot enter unnoticed. The two that exist (`scheduler_state_failure.py:514` `retry_downstream` and `:1954` `retry_failed`, both `failure["permanent"]`) are outside the listed set because neither can be built with the flag true: `:498` returns `None` for a permanent failure before the first dict is built, and for the second the guard is at the call site — `scheduler_state_decision.py:385` returns the permanent `blocked` decision before the `retry_failed` return point at `:412`. The same `_failure_retry()` evidence reaching the missing-forcing channel (`scheduler_state_decision.py:373`) is likewise safe: every return point there goes through `_artifact_blocker_evidence` (`scheduler_state_failure.py:909`), which writes its own `decision` (`:927`) and its own literal `manual_retry_required: false` (`:947`) and inherits neither. It SHALL also list each model named in a not-selected `source_cycles` entry whose `selection_reason` is `journal_predecessor_identity_quarantine_breaker_engaged`, because a breaker-released cycle never reaches candidate construction. Each listed action SHALL carry `candidate_id`, `source_id`, `cycle_time`, `model_id`, `decision`, `reason`, `attempt`, `retry_limit`, `occurrences` (null when absent), and first/last seen pass. When one action appears in several scanned passes it SHALL be listed once, and apart from `first_seen_pass` and the seen count **every** value field of that entry SHALL be the value carried by `last_seen_pass` — the operator feeds `recorded_init_state_id` from this receipt into `confirm-operator-reentry`, which refuses a stale token — except that a `candidate_id` absent from the newest pass (the breaker-released leg carries none) SHALL fall back to a known one. The command SHALL exit `1` when at least one action is listed, `0` when none, and `2` when the evidence root is missing or unreadable or `--passes` is not an integer `>= 1`; an individual unreadable pass file — including one deleted between the directory scan and its own `stat`, as the evidence retention timer may do at any time — SHALL be reported under `unreadable_passes` and SHALL NOT abort the scan. A scanned pass SHALL be decidable only when it is readable and its terminal status belongs to a closed allowlist of statuses known to be written only after candidate construction ran (a post-construction status missing from the allowlist errs toward undecidable), and it is not a size-fallback product (`resource_limit_blocked` carrying `limit.pre_limit_status`), because that product empties `source_cycles` and so cannot show breaker-released cycles — its summarized blocked candidates SHALL still be listed; every other pass (for example `lock_contended`, `preflight_blocked`, or an unknown status) SHALL be reported under `non_evaluating_passes` with its status. A transparent pass is one whose status belongs to a closed set — `lock_contended` and `preflight_blocked` — known either to have evaluated nothing or to have written its full candidate lists and `source_cycles`; a transparent pass never hides anything. When no action is listed and at least one scanned pass dropped its candidate lists under the evidence byte budget, or a pass file vanished between the directory scan and its `stat` (its modification time was never read, so it cannot be placed in the scan's time order at all and vetoes `exit 0` wherever it sat), or no scanned pass is evaluating and scope-complete, or any pass that is neither evaluating-and-scope-complete, nor transparent, nor merely scope-narrowed is newer than the newest evaluating and scope-complete pass (it may have evaluated candidates it cannot show — a size-fallback product, an unreadable pass file, a `lease_lost` or exception-path `resource_limit_blocked` pass that emptied its lists, an unknown status, or a pass whose scope keys are missing — and so may hide a breaker release that engaged after that pass), the command SHALL report those passes and exit `3` (undecidable) instead of `0`. The three triggers are anchored on **evaluating and scope-complete**, not on "decidable": a scope-narrowed pass is decidable yet answers only for its own scope (see the narrowed-scope requirement below), so it neither backs `exit 0` nor forces `exit 3`. The bounded candidate summary SHALL retain `retry_policy` `attempt`, `retry_limit`, `occurrences`, and `manual_retry_required`, including false and zero values. The `recovery_runbook` slug returned by the display API's manual-action 409 SHALL name an existing file under `docs/runbooks/`.

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
- **WHEN** every scanned pass is a size-fallback product whose original payload had a breaker-released not-selected source cycle and no other listed decision
- **THEN** the command SHALL report those passes under `non_evaluating_passes` and exit `3`, never `0`
- **AND WHEN** such a pass still carries a summarized blocked candidate of a listed decision
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`

#### Scenario: A hidden pass newer than every decidable pass is undecidable
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

#### Scenario: The listed decision set covers the refused re-entry sink
- **WHEN** a scanned pass is evaluating and scope-complete and the only blocked candidate of a listed decision it carries is one whose decision is `blocked_operator_reentry_restart_stage_refused`
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`, never `0`

#### Scenario: No operator actions
- **WHEN** the scanned passes contain only blocked candidates of other decisions, at least one scanned pass is evaluating and scope-complete, no pass dropped its candidate lists, no pass file vanished mid-scan, and only transparent or scope-narrowed passes are newer than the newest evaluating and scope-complete pass
- **THEN** the command SHALL print an empty `operator_actions` list and exit `0`


### Requirement: A narrowed-scope pass SHALL NOT be read as evidence that nothing is pending

`list-operator-actions` SHALL decide `exit 0` ("nothing needs an operator") only from passes that actually looked everywhere. A pass may be narrowed in three ways that the pass file records: backfill disabled, operator filters that select a subset of models, basins, or an expression, and a `sources` list naming a subset of the production source set. A narrowed pass that lists no action has not established that no action exists — it has established that none exists *inside its own scope* — and the breaker-released entries that make up one of the listed decisions are produced only on the backfill leg.

A scanned pass SHALL therefore be treated as **scope-complete** if and only if all of the following hold, read from that pass's own file:

- its `backfill.enabled` is exactly `True`; and
- its `operator_filters` selects nothing away — `basin_ids` empty, `model_ids` empty, and `expression` null; and
- its top-level `sources` list is the whole production source set (`("gfs", "IFS")`, `scheduler.py`'s `DEFAULT_PRODUCTION_SOURCES`, which `cli.py` repeats as the `resolved_sources` fallback), compared as a set.

`sources` SHALL NOT be judged by emptiness: `cli.py` resolves it to the full set when the operator passes no `--source`, so it is never empty, and `scheduler_evidence.py:268` writes it unconditionally as `list(config.sources)`. A pass run with `--source gfs` therefore never looked at IFS and is scope-narrowed exactly as a basin-narrowed pass is.

`operator_filters` is a mapping that a normal production pass always writes, carrying those keys at their empty defaults. **Scope-completeness SHALL be decided from the filter *values*, never from the presence or size of the `operator_filters` mapping itself**: a pass that carries the mapping with every filter empty is scope-complete. Measured at the live node-22 evidence root on 2026-09-16, all 169 retained passes carry `operator_filters` as a four-key mapping whose `basin_ids` and `model_ids` are empty and whose `expression` is null, with `backfill.enabled` true — so a rule keyed on mapping-emptiness would classify every production pass as narrowed and could never reach `exit 0`.

A scope-complete pass behaves as today. A **scope-narrowed** pass SHALL still have its own actions listed, SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`, and SHALL leave the hidden-pass flag exactly as it found it — it neither clears it, because it did not look everywhere, nor arms it, because the narrowing was the operator's own instruction and hides nothing unexpectedly.

**The scope test SHALL be applied only to a pass that is otherwise evaluating.** A pass whose status already makes it non-evaluating — a transparent `lock_contended` or `preflight_blocked` pass, a size-fallback product, an unreadable file — keeps the classification its status gives it and is never reclassified as scope-narrowed. This ordering is not a convenience: a pass written before candidate construction structurally carries no `backfill` key at all, because the scheduler writes that key only once candidates exist, so a scope test applied ahead of the status test would declare every such pass undecidable and contradict the rule above that lets a transparent pass sit newer than a decidable one without forcing `exit 3`.

Within that restriction, when a pass that is otherwise evaluating is missing any field the scope test reads — the `backfill` key, the `operator_filters` key, `backfill.enabled`, any of `operator_filters.basin_ids`, `operator_filters.model_ids`, `operator_filters.expression`, or the top-level `sources` list (absent, or not a list of strings) — its scope cannot be determined and it SHALL NOT be assumed scope-complete. The dividing line is presence, not value: a field that is present and narrowing is the operator's own instruction (`scope_narrowed`), while a field that is absent is unreadable (`scope_unknown`). Every one of those six fields is written unconditionally by a normal pass — `scheduler_evidence.py:248-253` emits the four-key `operator_filters` mapping as a dict literal, `scheduler_evidence.py:268` emits `sources` as `list(config.sources)`, and both legs of the `if/else` at `scheduler_runtime.py:1394-1402` write `backfill` carrying `enabled` — so an absent field is not a narrowing the pass chose to record but a shape the writer cannot produce. Such a pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL **arm** the hidden-pass flag, exactly as any other pass that may have evaluated candidates it cannot show. Arming is positional: a scope-complete decidable pass newer than it clears the flag again, so a missing-key pass older than such a pass does not by itself force `exit 3`, while one newer than every scope-complete pass does. A missing-key pass SHALL NOT be treated as a global veto in the way a dropped candidate list is.

A scope-narrowed pass is reported under `non_evaluating_passes` and therefore does not count toward the window's evaluating-pass total, while still being a pass that was read and understood. "Decidable" in this requirement means a pass the command could read and classify; "evaluating" means a pass that counts toward the window having looked at anything. A scope-narrowed pass is the one kind that is decidable but not evaluating, which is why a window containing nothing else cannot reach `exit 0`.

#### Scenario: A window of only narrowed passes cannot produce exit 0
- **WHEN** every scanned pass lists no action, none of them is scope-complete, and the newest carries `operator_filters.model_ids` naming one model
- **THEN** each narrowed pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`
- **AND** the command SHALL exit `3`, never `0`

#### Scenario: A window of only backfill-disabled passes cannot produce exit 0
- **WHEN** every scanned pass lists no action, none of them is scope-complete, and the newest carries `backfill.enabled` false
- **THEN** each narrowed pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`
- **AND** the command SHALL exit `3`, never `0`

#### Scenario: A narrowed pass does not re-arm a flag an earlier scope-complete pass cleared
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the only newer pass is scope-narrowed
- **THEN** the narrowed pass SHALL leave the hidden-pass flag as it found it — neither clearing nor arming it
- **AND** the command SHALL exit `0`

#### Scenario: A narrowed pass does not clear a flag a hidden pass armed
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and decidable, a size-fallback pass is newer than it, and a scope-narrowed pass is newer still
- **THEN** the hidden-pass flag armed by the size-fallback pass SHALL survive the narrowed pass
- **AND** the command SHALL exit `3`

#### Scenario: A source-narrowed pass does not clear a flag a hidden pass armed
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and evaluating, a size-fallback pass is newer than it, and the newest pass carries a `sources` list naming only `gfs`
- **THEN** that newest pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed` and SHALL leave the armed hidden-pass flag armed
- **AND** the command SHALL exit `3`

#### Scenario: A transparent pass is never reclassified as narrowed
- **WHEN** a scanned pass carries status `lock_contended` and, as such a pass structurally does, no `backfill` key
- **THEN** it SHALL keep its transparent classification and SHALL NOT be reported with reason `scope_narrowed`
- **AND** when it is newer than a scope-complete decidable pass that listed no action, the command SHALL exit `0`

#### Scenario: An unnarrowed pass carrying the empty filter mapping is scope-complete
- **WHEN** a scanned pass carries `backfill.enabled` true and an `operator_filters` mapping whose `basin_ids` and `model_ids` are empty and whose `expression` is null
- **THEN** that pass SHALL be treated as scope-complete
- **AND** when it lists no action and no newer pass hides anything, the command SHALL exit `0`

#### Scenario: A narrowed pass that does list a blocked action still reports it
- **WHEN** a scope-narrowed pass carries a blocked candidate of a listed decision
- **THEN** that candidate SHALL appear in `operator_actions`
- **AND** the command SHALL exit `1`

#### Scenario: An evaluating pass missing the backfill key arms the hidden-pass flag
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status and an `operator_filters` mapping but no `backfill` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `3`

#### Scenario: A partially written scope block is unreadable, not scope-complete
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a `backfill` mapping with `enabled` true, and an `operator_filters` mapping that is empty
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `3`, never `0`
- **AND** the same SHALL hold when the newest pass carries a full `operator_filters` mapping and a `backfill` mapping with no `enabled` key

#### Scenario: A missing-key pass older than a scope-complete pass does not force exit 3
- **WHEN** no action is listed, the oldest scanned pass carries an evaluating status and a `backfill` mapping but no `operator_filters` key, and a scope-complete decidable pass is newer than it
- **THEN** that older pass SHALL still be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `0`, because the newer scope-complete pass looked everywhere after it
