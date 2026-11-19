## MODIFIED Requirements

### Requirement: A narrowed-scope pass SHALL NOT be read as evidence that nothing is pending

`list-operator-actions` SHALL decide `exit 0` ("nothing needs an operator") only from passes that actually looked everywhere. A pass may be narrowed in four ways that the pass file records: backfill disabled, operator filters that select a subset of models, basins, or an expression, a `sources` list naming a subset of the production source set, and a backfill discovery window collapsed to zero width. A narrowed pass that lists no action has not established that no action exists — it has established that none exists *inside its own scope* — and the breaker-released entries that make up one of the listed decisions are produced only on the backfill leg.

**The set of narrowing dimensions SHALL be closed against the writer, not maintained by hand.** Every top-level key that a pass in an **evaluating** status publishes SHALL carry one of three dispositions — judged by the scope test, documented as a known boundary, or recorded as not a scope dimension with its reason — and a published key that has none SHALL fail the build. Wherever a disposition is given at subkey level, the closure SHALL descend to that key's subkeys and apply the same rule, so that a knob added one level down fails the build too.

**The authority for that key set SHALL be obtained by running the writer, never by reading it.** The closure test SHALL execute a real pass through the existing db-free doubles and take the published mapping's own keys; it SHALL NOT parse writer source, and it SHALL NOT compare against a key list typed into the test. The check runs both ways: a published key with no disposition fails, and a dispositioned key absent from the published mapping fails, so that a stale entry cannot survive the writer dropping its key.

Scoping the authority to evaluating passes is exact rather than convenient: the scope test runs only on evaluating passes, and every early-exit branch of the writer rewrites `status` out of the evaluating set before returning, so a key that only an early-exit branch publishes can never reach the scope test.

**The doubles' reachable surface is itself a copy, and SHALL be closed too.** Running the writer only moves the enumeration from a hand-typed list to whichever branches the fixture happens to walk; a key that an evaluating pass can publish down a branch no fixture leg opens is as invisible as one nobody typed. The closure fixture SHALL therefore drive every configuration switch that changes which keys an evaluating pass publishes — at minimum a leg with Slurm execution enabled alongside the planning-only leg, since `slurm_preflight` is published on the main evaluating path and is absent from a planning-only run. There is no conditional-row escape hatch: where a double falls short of a live pass the fixture SHALL be opened, never the disposition table annotated. This is not a hypothetical courtesy to future keys — the first version of this closure was green while `slurm_preflight` had no disposition at all, in both directions, because neither fixture leg reached it.

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

`runtime_config.allowed_cycle_hours_utc` is the **cycle-hour** dimension, and it is one of the two narrowing knobs the evidence publishes exactly once — the other being `backfill.enabled`, which has no second copy either. The remaining knobs appear two or three times over (`sources` also at `runtime_config.sources`, `lookback_hours` also at `runtime_config.lookback_hours` and, on one leg, at `backfill.lookback_hours`, the operator filters also at `filters` and inside `model_discovery`). Single publication matters here only as a caution against judging a mirror instead of the source; it is not what makes the knob worth judging. Cycle discovery drops every cycle whose hour is outside this set, so an operator who narrows it below what production runs leaves whole cycle times unevaluated while the pass still reports no action. It SHALL be judged as **coverage against the code-side default**, `DEFAULT_ALLOWED_CYCLE_HOURS_UTC`, imported from the same module the reader imports `DEFAULT_PRODUCTION_SOURCES` from and never restated as a literal in the reader. The default is itself a narrow subset of the twenty-four possible hours, and that is deliberate: the question this rule answers is whether the operator narrowed *below the production baseline*, not whether the baseline covers the day. Judging against the full day would classify every production pass as narrowed and make `exit 0` unreachable. It is read from the top-level `runtime_config` block, which `base_evidence` writes unconditionally and no later branch overwrites, rather than from the nested runtime mirror.

`counts.selected_model_count` is not a narrowing dimension but an **emptiness** one, and it is judged for a different reason than the others. A pass whose model registry resolved to no runnable models discovers cycles, writes evidence, and terminates in an evaluating status having evaluated nothing at all; under the rule above it would carry empty operator filters, the full source set, a positive window and the default cycle hours, and so would be scope-complete and would clear the hidden-pass flag. Such a pass SHALL instead be reported under `non_evaluating_passes` with its own reason, distinct from both `scope_narrowed` and `scope_unknown`, and SHALL **arm** the hidden-pass flag. The distinction that decides this is the one the rest of this requirement already uses: a field that is present and narrowing is the operator's own instruction and leaves the flag alone, whereas a pass that made no observations is exactly the case arming exists for. **This verdict SHALL take precedence over `scope_narrowed`** when a pass is both narrowed and empty. An operator who narrows to a model set that resolves to nothing has not merely limited what was looked at — nothing was looked at — and the "leave the flag as you found it" rule exists for a pass that did observe its own smaller scope. Leaving the flag alone there would let a pass that observed nothing inherit a `cleared` flag from an older pass and endorse `exit 0`. The writer-side cause — that `backfill.enabled` records the configured intent rather than the leg discovery actually took when the model set is empty — is a separate defect on the writer and is out of scope here; this requirement governs only what the reader may conclude from `exit 0`.

**A non-degenerate window is a documented boundary, not a defect.** For `lookback_hours > 0` and for any `cycle_lag_hours`, `exit 0` asserts only that nothing is pending *within that pass's own window* `[start_time_utc, end_time_utc]`. A cycle newer than the configured cycle lag is outside every window until the lag has elapsed. There is no in-repo authority for a "complete" window against which a narrower one could be judged — the production setting and the code default disagree — so no threshold is invented here; the boundary is stated instead. The production lookback and cycle lag are operator configuration; this requirement SHALL NOT restate their values, and the listing's help text SHALL name the window keys without quoting a production value for them.

**The inactive-model boundary.** `exit 0` SHALL NOT be read as asserting anything about models the registry manifest marks inactive. The registry's own `model_count` counts every model row the manifest declares, while `active_model_count` counts what `list_models(active=True, …)` returned, and that filter runs before model discovery ever sees a row — so the difference between the two is structurally absent from the `exclusions` array, which can only explain drops that happen after discovery receives the row. This difference SHALL be documented rather than judged: requiring the two counts to be equal would amount to forbidding the manifest from ever retiring a model, and there is no in-repo authority for how many models a manifest ought to declare, so no threshold is invented here for the same reason none is invented for the discovery window. Measured at the live node-22 evidence root on 2026-09-16, the two counts are equal on every retained pass, so the boundary is presently empty in production while remaining structurally real.

**The single-slot boundary.** The backfill leg evaluates only the **oldest** incomplete cycle per source per pass (`scheduler_discovery.py`), recording newer gaps as `backfill_deferred_waiting_for_prior_cycle`; those newer cycles are evaluated only after the prior one yields usable state. This is narrower than it sounds, and the mitigation SHALL be documented with it: an unresolved action keeps its own cycle a gap, so that cycle is re-evaluated and re-listed on every subsequent pass **as long as that cycle stays inside the later passes' discovery range** — what is deferred is an action on a *newer* cycle, which cannot even be created until the older one clears. `blocked_journal_predecessor_identity_quarantine` is exempt **from this boundary**: breaker release runs before the single-slot split and releases every consecutive breaker-engaged cycle from the oldest, so that decision's visibility is complete with respect to slot contention. This boundary SHALL NOT be attributed to `max_cycles_per_source`, which is inert for any pass whose `backfill.enabled` is `True`.

**The discovery-retraction boundary.** That mitigation depends on re-discovery, and re-discovery is not guaranteed across a configuration change. `allowed_cycle_hours_utc` filters cycles in `scheduler_discovery.py` **before** the single-slot gap selection runs, and candidates are built only from the cycles a pass discovered, so a cycle whose hour leaves the allowed set is not merely deferred — it stops being discovered, stops producing candidates, and its already-listed unresolved action stops appearing in `blocked_candidates` and `source_cycles` alike. The same holds for a `lookback_hours` reduction that moves a cycle outside the window. `exit 0` SHALL therefore NOT be read as asserting anything about actions on cycles that an earlier, wider configuration could see and the current one cannot. This is documented rather than judged, for the reason the other boundaries are: the reader sees only the current pass's own configuration and has no in-repo authority for what an earlier one was. The knob is environment-only (no CLI flag), so reaching this state takes a deliberate widen-then-retract of the service environment, not an ordinary run. **Unlike the single-slot boundary, this one grants `blocked_journal_predecessor_identity_quarantine` no exemption**: breaker release is computed inside `_select_backfill_source_cycles` from the *already filtered* `discoveries`, so a cycle the hour filter removed is invisible to the breaker release exactly as it is to everything else. The exemption above protects that decision from losing its slot, not from losing its cycle.

`operator_filters` is a mapping that a normal production pass always writes, carrying those keys at their empty defaults. **Scope-completeness SHALL be decided from the filter *values*, never from the presence or size of the `operator_filters` mapping itself**: a pass that carries the mapping with every filter empty is scope-complete. Measured at the live node-22 evidence root on 2026-09-16, every retained pass carries `operator_filters` as a four-key mapping whose `basin_ids` and `model_ids` are empty and whose `expression` is null, with `backfill.enabled` true — so a rule keyed on mapping-emptiness would classify every production pass as narrowed and could never reach `exit 0`. The same measurement found, on every retained pass without exception: `sources` at the full production set; `cycle_window` carrying all five of its keys (`lookback_hours` 96, `cycle_lag_hours` 16, `max_cycles_per_source` 1); `runtime_config.allowed_cycle_hours_utc` exactly equal to the code-side default; and `counts.selected_model_count` at 76. No rule in this requirement can therefore turn a production pass undecidable or narrowed. (The retained-pass count is deliberately not quoted here: it drifts with the retention timer, and four measurements taken for this change across two days returned four different totals.)

A scope-complete pass behaves as today. A **scope-narrowed** pass SHALL still have its own actions listed, SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`, and SHALL leave the hidden-pass flag exactly as it found it — it neither clears it, because it did not look everywhere, nor arms it, because the narrowing was the operator's own instruction and hides nothing unexpectedly.

**The scope test SHALL be applied only to a pass that is otherwise evaluating.** A pass whose status already makes it non-evaluating — a transparent `lock_contended` or `preflight_blocked` pass, a size-fallback product, an unreadable file — keeps the classification its status gives it and is never reclassified as scope-narrowed. This ordering is not a convenience: a pass written before candidate construction structurally carries no `backfill` key at all, because the scheduler writes that key only once candidates exist, so a scope test applied ahead of the status test would declare every such pass undecidable and contradict the rule above that lets a transparent pass sit newer than a decidable one without forcing `exit 3`.

Within that restriction, when a pass that is otherwise evaluating is missing any field the scope test reads — the `backfill` key, the `operator_filters` key, `backfill.enabled`, any of `operator_filters.basin_ids`, `operator_filters.model_ids`, `operator_filters.expression`, the top-level `sources` list (absent, or not a list of strings), the top-level `cycle_window` block and its `lookback_hours` (absent, or not an integer), the top-level `runtime_config` block and its `allowed_cycle_hours_utc` (absent, or not a sequence of integers), or the top-level `counts` block and its `selected_model_count` (absent, or not an integer) — its scope cannot be determined and it SHALL NOT be assumed scope-complete. The dividing line is presence, not value: a field that is present and narrowing is the operator's own instruction (`scope_narrowed`), while a field that is absent is unreadable (`scope_unknown`). Every one of those fields is written unconditionally by a normal pass — `scheduler_evidence.py` emits the four-key `operator_filters` mapping as a dict literal, emits `sources` as `list(config.sources)`, and emits `cycle_window` and `runtime_config` each carrying all of their keys, `scheduler_runtime.py` emits `counts` as a dict literal on the main branch, and both legs of the `if/else` in `scheduler_runtime.py` write `backfill` carrying `enabled` — so an absent field is not a narrowing the pass chose to record but a shape the writer cannot produce. The presence check on each block is not optional bookkeeping: judging a block's value while assuming its absence means "complete" would reintroduce, one field over, exactly the missing-field-read-as-complete defect this paragraph exists to forbid — which is how the window dimension came to be missed in the first place. Such a pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL **arm** the hidden-pass flag, exactly as any other pass that may have evaluated candidates it cannot show. Arming is positional: a scope-complete decidable pass newer than it clears the flag again, so a missing-key pass older than such a pass does not by itself force `exit 3`, while one newer than every scope-complete pass does. A missing-key pass SHALL NOT be treated as a global veto in the way a dropped candidate list is.

A scope-narrowed pass is reported under `non_evaluating_passes` and therefore does not count toward the window's evaluating-pass total, while still being a pass that was read and understood. "Decidable" throughout this capability carries the single definition given in the requirement above — readable, terminal status in the closed allowlist, not a size-fallback product — and "evaluating" means a pass that counts toward the window having looked at anything. Scope-narrowed, `scope_unknown` and `no_models_evaluated` are the **three** kinds that are decidable but not evaluating, which is why a window containing nothing but those cannot reach `exit 0`. They differ only in what they do to the hidden-pass flag: a narrowed pass leaves it as it found it, while an unknown-scope pass and a pass that evaluated no models both arm it.

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

#### Scenario: A key published only when Slurm execution is enabled still carries a disposition
- **WHEN** the closure test runs its evaluating passes through the db-free doubles
- **THEN** it SHALL run at least one leg with Slurm execution enabled in addition to the planning-only leg
- **AND** the union of the keys those legs publish SHALL be the authority both directions of the closure are checked against
- **AND** `slurm_preflight`, which only the Slurm-enabled leg publishes, SHALL carry a disposition like every other published key

#### Scenario: The help text states the window boundary without a production value
- **WHEN** `LIST_OPERATOR_ACTIONS_HELP` is read
- **THEN** it SHALL name `cycle_window.start_time_utc`, `cycle_window.end_time_utc`, `lookback_hours` and `cycle_lag_hours`
- **AND** it SHALL NOT quote a production value for `lookback_hours` or `cycle_lag_hours`
