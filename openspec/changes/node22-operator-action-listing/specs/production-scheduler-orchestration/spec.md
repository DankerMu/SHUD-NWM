## ADDED Requirements

### Requirement: Operator-action decisions SHALL be enumerable from db-free pass evidence

The scheduler CLI SHALL provide a read-only `list-operator-actions` subcommand that scans the most recent N terminal scheduler pass evidence files under the evidence root (`--evidence-root`, defaulting to `NHMS_SCHEDULER_EVIDENCE_ROOT`), excluding `.pre_execution.json` files and ordering by modification time. It SHALL identify blocked candidates by decision literal — `permanent_failure`, `cancelled_manual_retry_required`, `blocked_strict_warm_start_init_state_mismatch`, `blocked_journal_predecessor_identity_quarantine`, `blocked_operator_reentry_restart_stage_refused` — not by the `manual_retry_required` flag, so that bounded-summarized passes remain enumerable. That set SHALL equal the set of decisions the db-free scheduler writes a **literal** `manual_retry_required: true` on, because a decision missing from it is answered with `exit 0` ("nothing waits") while the runbook still prescribes an operator action for it. The writers that instead set the flag from an expression SHALL be enumerated and dispositioned rather than ignored, so that a new one cannot enter unnoticed. The two that exist (`scheduler_state_failure.py:514` `retry_downstream` and `:1954` `retry_failed`, both `failure["permanent"]`) are outside the listed set because neither can be built with the flag true: `:498` returns `None` for a permanent failure before the first dict is built, and for the second the guard is at the call site — `scheduler_state_decision.py:385` returns the permanent `blocked` decision before the `retry_failed` return point at `:412`. The same `_failure_retry()` evidence reaching the missing-forcing channel (`scheduler_state_decision.py:373`) is likewise safe: every return point there goes through `_artifact_blocker_evidence` (`scheduler_state_failure.py:909`), which writes its own `decision` (`:927`) and its own literal `manual_retry_required: false` (`:947`) and inherits neither. It SHALL also list each model named in a not-selected `source_cycles` entry whose `selection_reason` is `journal_predecessor_identity_quarantine_breaker_engaged`, because a breaker-released cycle never reaches candidate construction. Each listed action SHALL carry `candidate_id`, `source_id`, `cycle_time`, `model_id`, `decision`, `reason`, `attempt`, `retry_limit`, `occurrences`, `recorded_init_state_id` (null when absent), and first/last seen pass. When one action appears in several scanned passes it SHALL be listed once, and apart from `first_seen_pass` and the seen count **every** value field of that entry SHALL be the value carried by `last_seen_pass` — the operator feeds `recorded_init_state_id` from this receipt into `confirm-operator-reentry`, which refuses a stale token — except that a `candidate_id` absent from the newest pass (the breaker-released leg carries none) SHALL fall back to a known one. The command SHALL exit `1` when at least one action is listed, `0` when none, and `2` when the evidence root is missing or unreadable or `--passes` is not an integer `>= 1`; an individual unreadable pass file — including one deleted between the directory scan and its own `stat`, as the evidence retention timer may do at any time — SHALL be reported under `unreadable_passes` and SHALL NOT abort the scan. A scanned pass SHALL be decidable only when it is readable and its terminal status belongs to a closed allowlist of statuses known to be written only after candidate construction ran (a post-construction status missing from the allowlist errs toward undecidable), and it is not a size-fallback product (`resource_limit_blocked` carrying `limit.pre_limit_status`), because that product empties `source_cycles` and so cannot show breaker-released cycles — its summarized blocked candidates SHALL still be listed; every other pass (for example `lock_contended`, `preflight_blocked`, or an unknown status) SHALL be reported under `non_evaluating_passes` with its status. A transparent pass is one whose status belongs to a closed set — `lock_contended` and `preflight_blocked` — known either to have evaluated nothing or to have written its full candidate lists and `source_cycles`; a transparent pass never hides anything. When no action is listed and at least one scanned pass dropped its candidate lists under the evidence byte budget, or a pass file vanished between the directory scan and its `stat` (its modification time was never read, so it cannot be placed in the scan's time order at all and vetoes `exit 0` wherever it sat), or no scanned pass is evaluating and scope-complete, or any pass that is neither evaluating-and-scope-complete, nor transparent, nor merely scope-narrowed is newer than the newest evaluating and scope-complete pass (it may have evaluated candidates it cannot show — a size-fallback product, an unreadable pass file, a `lease_lost` or exception-path `resource_limit_blocked` pass that emptied its lists, an unknown status, or a pass whose scope keys are missing — and so may hide a breaker release that engaged after that pass), the command SHALL report those passes and exit `3` (undecidable) instead of `0`. The three triggers are anchored on **evaluating and scope-complete**, not on "decidable": a scope-narrowed pass is decidable yet answers only for its own scope (see the narrowed-scope requirement below), so it neither backs `exit 0` nor forces `exit 3`. The bounded candidate summary SHALL retain `retry_policy` `attempt`, `retry_limit`, `occurrences`, and `manual_retry_required`, including false and zero values, and SHALL retain `journal_predecessor_identity.recorded_init_state_id`. That last key is not decoration: the summary drops `state_evidence` wholesale, so without retaining it a newest pass that fell back to the bounded summary would report `recorded_init_state_id: null` for a candidate whose token the scheduler did record — and `confirm-operator-reentry`'s breaker arm requires that token, while its `recorded_init_state_id_mismatch` refusal returns only `occurrences` and `quarantine_rerun_count` and no other operator surface prints the live token. Scanning with `--passes 1` does not recover it: the token is exactly what the newest pass dropped. Because the reader takes every value field from `last_seen_pass`, retention at the writer — not a fallback to an older pass — is what makes the token correct rather than stale. The `recovery_runbook` slug returned by the display API's manual-action 409 SHALL name an existing file under `docs/runbooks/`.

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


### Requirement: A narrowed-scope pass SHALL NOT be read as evidence that nothing is pending

`list-operator-actions` SHALL decide `exit 0` ("nothing needs an operator") only from passes that actually looked everywhere. A pass may be narrowed in four ways that the pass file records: backfill disabled, operator filters that select a subset of models, basins, or an expression, a `sources` list naming a subset of the production source set, and a backfill discovery window collapsed to zero width. A narrowed pass that lists no action has not established that no action exists — it has established that none exists *inside its own scope* — and the breaker-released entries that make up one of the listed decisions are produced only on the backfill leg.

**The set of narrowing dimensions SHALL be closed against the writer, not maintained by hand.** Every top-level key that a pass in an **evaluating** status publishes SHALL carry one of three dispositions — judged by the scope test, documented as a known boundary, or recorded as not a scope dimension with its reason — and a published key that has none SHALL fail the build. Wherever a disposition is given at subkey level, the closure SHALL descend to that key's subkeys and apply the same rule, so that a knob added one level down fails the build too.

**The authority for that key set SHALL be obtained by running the writer, never by reading it.** The closure test SHALL execute a real pass through the existing db-free doubles and take the published mapping's own keys; it SHALL NOT parse writer source, and it SHALL NOT compare against a key list typed into the test. The check runs both ways: a published key with no disposition fails, and a dispositioned key absent from the published mapping fails, so that a stale entry cannot survive the writer dropping its key.

Scoping the authority to evaluating passes is exact rather than convenient: the scope test runs only on evaluating passes, and every early-exit branch of the writer rewrites `status` out of the evaluating set before returning, so a key that only an early-exit branch publishes can never reach the scope test. Keys that no db-free double can produce SHALL be marked conditional individually, with the reason that path cannot run, rather than being dropped from the dispositions.

Three separate rounds of review found a different unread narrowing dimension in this one function, and a fourth found that the disposition table itself had been built by reading two writer files and so covered a third of the keys a live pass actually publishes. Enumerating by hand is what allowed every one of them — including the attempt to fix it — which is why the enumeration is derived from a run and the dispositions are the thing reviewed.

A scanned pass SHALL therefore be treated as **scope-complete** if and only if all of the following hold, read from that pass's own file:

- its `backfill.enabled` is exactly `True`; and
- its `operator_filters` selects nothing away — `basin_ids` empty, `model_ids` empty, and `expression` null; and
- its top-level `sources` list **covers** the whole production source set (`("gfs", "IFS")`, `scheduler.py`'s `DEFAULT_PRODUCTION_SOURCES`, which `cli.py` repeats as the `resolved_sources` fallback); and
- its `cycle_window.lookback_hours` is greater than zero; and
- its `runtime_config.allowed_cycle_hours_utc` **covers** the default cycle-hour set (`scheduler.py`'s `DEFAULT_ALLOWED_CYCLE_HOURS_UTC`); and
- its `counts.selected_model_count` is greater than zero.

`sources` SHALL NOT be judged by emptiness: `cli.py` resolves it to the full set when the operator passes no `--source`, so it is never empty, and `scheduler_evidence.py:268` writes it unconditionally as `list(config.sources)`. A pass run with `--source gfs` therefore never looked at IFS and is scope-narrowed exactly as a basin-narrowed pass is. The test is **coverage, not equality**: a pass naming a superset did look at every production source, and the spelling of the names needs no normalising here because `ProductionSchedulerConfig.__post_init__` already routes every `--source` value through `normalize_source_id`, which upper-cases into a closed table and raises on an unknown name — a case variant cannot reach the evidence file. Case-folding at the reader would be worse than useless: the db-free adapter's manifest paths (`raw/{source_id}/…`) are case-sensitive, so a reader that treated `GFS` as `gfs` would hand a key to a false `exit 0` that the config layer currently locks shut.

`cycle_window.lookback_hours` is the **time** dimension, and only its degenerate value is judged. A zero-width discovery window (`--lookback-hours 0` without `--cycle-time`, which the CLI accepts because it validates no lower bound — `max_cycles_per_source < 1` raises, `lookback_hours` does not, and the config layer's `max(int(...), 0)` clamps negatives to zero rather than rejecting them) leaves the backfill leg blind to every cycle older than the window edge, and the breaker-released cycles that make up `blocked_journal_predecessor_identity_quarantine` sit by construction on exactly that oldest side. Such a pass SHALL be `scope_narrowed`. It is read from the top-level `cycle_window` block rather than from `backfill.lookback_hours`: both carry the same `config.lookback_hours`, but `cycle_window` is written unconditionally by `base_evidence` while the `backfill` copy exists only on the `enabled: True` leg, so judging the latter would couple this presence check to the `backfill.enabled` verdict and drift the moment anyone gives the other leg that key.

`runtime_config.allowed_cycle_hours_utc` is the **cycle-hour** dimension, and it is the one narrowing knob the evidence publishes exactly once — every other knob appears two or three times over (`sources` also at `runtime_config.sources`, `lookback_hours` also at `runtime_config.lookback_hours` and, on one leg, at `backfill.lookback_hours`, the operator filters also at `filters` and inside `model_discovery`). Cycle discovery drops every cycle whose hour is outside this set, so an operator who narrows it below what production runs leaves whole cycle times unevaluated while the pass still reports no action. It SHALL be judged as **coverage against the code-side default**, `DEFAULT_ALLOWED_CYCLE_HOURS_UTC`, imported from the same module the reader imports `DEFAULT_PRODUCTION_SOURCES` from and never restated as a literal in the reader. The default is itself a narrow subset of the twenty-four possible hours, and that is deliberate: the question this rule answers is whether the operator narrowed *below the production baseline*, not whether the baseline covers the day. Judging against the full day would classify every production pass as narrowed and make `exit 0` unreachable. It is read from the top-level `runtime_config` block, which `base_evidence` writes unconditionally and no later branch overwrites, rather than from the nested runtime mirror.

`counts.selected_model_count` is not a narrowing dimension but an **emptiness** one, and it is judged for a different reason than the others. A pass whose model registry resolved to no runnable models discovers cycles, writes evidence, and terminates in an evaluating status having evaluated nothing at all; under the rule above it would carry empty operator filters, the full source set, a positive window and the default cycle hours, and so would be scope-complete and would clear the hidden-pass flag. Such a pass SHALL instead be reported under `non_evaluating_passes` with its own reason, distinct from both `scope_narrowed` and `scope_unknown`, and SHALL **arm** the hidden-pass flag. The distinction that decides this is the one the rest of this requirement already uses: a field that is present and narrowing is the operator's own instruction and leaves the flag alone, whereas a pass that made no observations is exactly the case arming exists for. **This verdict SHALL take precedence over `scope_narrowed`** when a pass is both narrowed and empty. An operator who narrows to a model set that resolves to nothing has not merely limited what was looked at — nothing was looked at — and the "leave the flag as you found it" rule exists for a pass that did observe its own smaller scope. Leaving the flag alone there would let a pass that observed nothing inherit a `cleared` flag from an older pass and endorse `exit 0`. The writer-side cause — that `backfill.enabled` records the configured intent rather than the leg discovery actually took when the model set is empty — is a separate defect on the writer and is out of scope here; this requirement governs only what the reader may conclude from `exit 0`.

**A non-degenerate window is a documented boundary, not a defect.** For `lookback_hours > 0` and for any `cycle_lag_hours`, `exit 0` asserts only that nothing is pending *within that pass's own window* `[start_time_utc, end_time_utc]`. Production runs `lookback=96h` with `cycle_lag=16h`, so the most recent 16 hours of cycles are outside every window. There is no in-repo authority for a "complete" window against which a narrower one could be judged — the production setting and the code default disagree — so no threshold is invented here; the boundary is stated instead. (The production value is 96 and the code default is 24.)

**The inactive-model boundary.** `exit 0` SHALL NOT be read as asserting anything about models the registry manifest marks inactive. The registry's own `model_count` counts every model row the manifest declares, while `active_model_count` counts what `list_models(active=True, …)` returned, and that filter runs before model discovery ever sees a row — so the difference between the two is structurally absent from the `exclusions` array, which can only explain drops that happen after discovery receives the row. This difference SHALL be documented rather than judged: requiring the two counts to be equal would amount to forbidding the manifest from ever retiring a model, and there is no in-repo authority for how many models a manifest ought to declare, so no threshold is invented here for the same reason none is invented for the discovery window. Measured at the live node-22 evidence root on 2026-09-16, the two counts are equal on every retained pass, so the boundary is presently empty in production while remaining structurally real.

**The single-slot boundary.** The backfill leg evaluates only the **oldest** incomplete cycle per source per pass (`scheduler_discovery.py`), recording newer gaps as `backfill_deferred_waiting_for_prior_cycle`; those newer cycles are evaluated only after the prior one yields usable state. This is narrower than it sounds, and the mitigation SHALL be documented with it: an unresolved action keeps its own cycle a gap, so that cycle is re-evaluated and re-listed on every subsequent pass — what is deferred is an action on a *newer* cycle, which cannot even be created until the older one clears. `blocked_journal_predecessor_identity_quarantine` is exempt: breaker release runs before the single-slot split and releases every consecutive breaker-engaged cycle from the oldest, so that decision's visibility is complete. This boundary SHALL NOT be attributed to `max_cycles_per_source`, which is inert for any pass whose `backfill.enabled` is `True`.

`operator_filters` is a mapping that a normal production pass always writes, carrying those keys at their empty defaults. **Scope-completeness SHALL be decided from the filter *values*, never from the presence or size of the `operator_filters` mapping itself**: a pass that carries the mapping with every filter empty is scope-complete. Measured at the live node-22 evidence root on 2026-09-16, every retained pass carries `operator_filters` as a four-key mapping whose `basin_ids` and `model_ids` are empty and whose `expression` is null, with `backfill.enabled` true — so a rule keyed on mapping-emptiness would classify every production pass as narrowed and could never reach `exit 0`. The same measurement found, on every retained pass without exception: `sources` at the full production set; `cycle_window` carrying all five of its keys (`lookback_hours` 96, `cycle_lag_hours` 16, `max_cycles_per_source` 1); `runtime_config.allowed_cycle_hours_utc` exactly equal to the code-side default; and `counts.selected_model_count` at 76. No rule in this requirement can therefore turn a production pass undecidable or narrowed. (The retained-pass count is deliberately not quoted here: it drifts with the retention timer, and four measurements taken for this change across two days returned four different totals.)

A scope-complete pass behaves as today. A **scope-narrowed** pass SHALL still have its own actions listed, SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`, and SHALL leave the hidden-pass flag exactly as it found it — it neither clears it, because it did not look everywhere, nor arms it, because the narrowing was the operator's own instruction and hides nothing unexpectedly.

**The scope test SHALL be applied only to a pass that is otherwise evaluating.** A pass whose status already makes it non-evaluating — a transparent `lock_contended` or `preflight_blocked` pass, a size-fallback product, an unreadable file — keeps the classification its status gives it and is never reclassified as scope-narrowed. This ordering is not a convenience: a pass written before candidate construction structurally carries no `backfill` key at all, because the scheduler writes that key only once candidates exist, so a scope test applied ahead of the status test would declare every such pass undecidable and contradict the rule above that lets a transparent pass sit newer than a decidable one without forcing `exit 3`.

Within that restriction, when a pass that is otherwise evaluating is missing any field the scope test reads — the `backfill` key, the `operator_filters` key, `backfill.enabled`, any of `operator_filters.basin_ids`, `operator_filters.model_ids`, `operator_filters.expression`, the top-level `sources` list (absent, or not a list of strings), the top-level `cycle_window` block and its `lookback_hours` (absent, or not an integer), the top-level `runtime_config` block and its `allowed_cycle_hours_utc` (absent, or not a sequence of integers), or the top-level `counts` block and its `selected_model_count` (absent, or not an integer) — its scope cannot be determined and it SHALL NOT be assumed scope-complete. The dividing line is presence, not value: a field that is present and narrowing is the operator's own instruction (`scope_narrowed`), while a field that is absent is unreadable (`scope_unknown`). Every one of those fields is written unconditionally by a normal pass — `scheduler_evidence.py` emits the four-key `operator_filters` mapping as a dict literal, emits `sources` as `list(config.sources)`, and emits `cycle_window` and `runtime_config` each carrying all of their keys, `scheduler_runtime.py` emits `counts` as a dict literal on the main branch, and both legs of the `if/else` in `scheduler_runtime.py` write `backfill` carrying `enabled` — so an absent field is not a narrowing the pass chose to record but a shape the writer cannot produce. The presence check on each block is not optional bookkeeping: judging a block's value while assuming its absence means "complete" would reintroduce, one field over, exactly the missing-field-read-as-complete defect this paragraph exists to forbid — which is how the window dimension came to be missed in the first place. Such a pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL **arm** the hidden-pass flag, exactly as any other pass that may have evaluated candidates it cannot show. Arming is positional: a scope-complete decidable pass newer than it clears the flag again, so a missing-key pass older than such a pass does not by itself force `exit 3`, while one newer than every scope-complete pass does. A missing-key pass SHALL NOT be treated as a global veto in the way a dropped candidate list is.

A scope-narrowed pass is reported under `non_evaluating_passes` and therefore does not count toward the window's evaluating-pass total, while still being a pass that was read and understood. "Decidable" throughout this capability carries the single definition given in the requirement above — readable, terminal status in the closed allowlist, not a size-fallback product — and "evaluating" means a pass that counts toward the window having looked at anything. Scope-narrowed and `scope_unknown` are the **two** kinds that are decidable but not evaluating, which is why a window containing nothing but those cannot reach `exit 0`. They differ only in what they do to the hidden-pass flag: a narrowed pass leaves it as it found it, an unknown-scope pass arms it.

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

#### Scenario: A zero-width backfill window is scope-narrowed, not scope-complete
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and evaluating, a size-fallback pass is newer than it, and the newest pass carries `backfill.enabled` true, empty operator filters, the full `sources` set, and `cycle_window.lookback_hours` of `0`
- **THEN** that newest pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed` and SHALL leave the armed hidden-pass flag armed
- **AND** the command SHALL exit `3`, never `0`

#### Scenario: A pass missing the cycle-window block has unknown scope
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a full `operator_filters` mapping, `backfill.enabled` true, the full `sources` set, and no `cycle_window` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `3`
- **AND** the same SHALL hold when `cycle_window` is present but carries no `lookback_hours`

#### Scenario: A sources list naming a superset is scope-complete
- **WHEN** a scanned pass lists no action and carries `backfill.enabled` true, empty operator filters, a positive `cycle_window.lookback_hours`, and a `sources` list naming every production source plus one further source
- **THEN** that pass SHALL be treated as scope-complete, because it did look at every production source
- **AND** when no newer pass hides anything, the command SHALL exit `0`

#### Scenario: A cycle-hour set narrower than the default is scope-narrowed
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and evaluating, a size-fallback pass is newer than it, and the newest pass carries `backfill.enabled` true, empty operator filters, the full `sources` set, a positive `cycle_window.lookback_hours`, a positive `counts.selected_model_count`, and a `runtime_config.allowed_cycle_hours_utc` naming only one of the default cycle hours
- **THEN** that newest pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed` and SHALL leave the armed hidden-pass flag armed
- **AND** the command SHALL exit `3`, never `0`
- **AND** a pass whose `allowed_cycle_hours_utc` names every default cycle hour plus a further hour SHALL be treated as scope-complete on this dimension

#### Scenario: A pass missing the runtime-config block has unknown scope
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a full `operator_filters` mapping, `backfill.enabled` true, the full `sources` set, a positive `cycle_window.lookback_hours`, and no `runtime_config` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL arm the hidden-pass flag
- **AND** the command SHALL exit `3`
- **AND** the same SHALL hold when `runtime_config` is present but carries no `allowed_cycle_hours_utc`, and when that value is present but is not a sequence of integers

#### Scenario: A pass that selected no models arms the flag rather than clearing it
- **WHEN** a scanned pass lists no action, carries an evaluating status, `backfill.enabled` true, empty operator filters, the full `sources` set, a positive `cycle_window.lookback_hours`, the default `runtime_config.allowed_cycle_hours_utc`, and `counts.selected_model_count` of `0`
- **THEN** that pass SHALL NOT be treated as scope-complete and SHALL NOT clear the hidden-pass flag
- **AND** it SHALL be reported under `non_evaluating_passes` with a reason distinct from both `scope_narrowed` and `scope_unknown`
- **AND** it SHALL arm the hidden-pass flag, so that when it is the newest scanned pass the command SHALL exit `3`
- **AND** when a scope-complete evaluating pass is newer than it, the flag SHALL be cleared again and the command SHALL exit `0`
- **AND** a pass that is both narrowed and empty — `counts.selected_model_count` of `0` together with `operator_filters.model_ids` naming one model — SHALL carry the empty-pass reason rather than `scope_narrowed`, and SHALL arm rather than leave the flag

#### Scenario: A pass missing the counts block has unknown scope
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a full `operator_filters` mapping, `backfill.enabled` true, the full `sources` set, a positive `cycle_window.lookback_hours`, the default `runtime_config.allowed_cycle_hours_utc`, and no `counts` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL arm the hidden-pass flag
- **AND** the same SHALL hold when `counts` is present but carries no `selected_model_count`, and when that value is present but is not an integer

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
