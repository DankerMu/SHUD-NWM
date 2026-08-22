# Design — lineage-scoped cycle completion

## Risk triage

**Fixture level: full.** This is a production-blocking scheduler-core change on
the completeness predicate that gates every execution decision.

| Axis | Level | Why |
|---|---|---|
| Blast radius | high | `cycle_completion_status` gates discovery, backfill selection, and cohort membership for every model and source. |
| Reversibility | medium | Pure read-side predicate change, no migration, no data rewrite; revert is a code revert. But a wrong verdict silently skips real work. |
| Silent-failure potential | **high** | A too-permissive filter marks genuinely incomplete cycles complete and the gap disappears from evidence — no crash, no alert. |
| Concurrency | low | Per-pass, single-threaded discovery. |
| External contract | low | No API, no schema, no migration. |

**Selected risk packs**: `correctness-boundary` (the `<` vs `<=` at `t*`,
empty-scope disambiguation), `silent-degradation` (never let scoping hide a real
gap for an in-scope model), `state-machine-invariants` (quarantine and breaker
interactions).

**Not selected**: `schema-migration` (no DB change), `concurrency` (single
threaded read path), `api-contract` (no external surface).

## Must-preserve behavior

1. **A model with no lineage is scored exactly as today.** No clone entry for
   `(model_id, source_id)` ⇒ the model is in completion scope for every cycle in
   the window, `exists_any_generation` unchanged, `packaged_ic_bootstrap` /
   cold-start path unchanged.
2. **A cycle at or after `t*` is scored exactly as today** for the recalibrated
   model: it is in scope and must genuinely complete.
3. **D8.3 / D8.7 generation quarantine on admission is untouched.**
4. **The §8.7 stale-identity breaker must not re-engage** on cycles that now
   score complete by lineage scoping.
5. **`tests/test_scheduler_backfill.py:1087`** (`no models ⇒ gap`) keeps its
   verdict for the unconfigured case.

## Recorded limitation — pre-cutover debt on a retired predecessor

Scoping `M1'` out of `C < t*` means a genuine unresolved gap that the
predecessor `M` carried into the cutover becomes permanently unschedulable and
no longer appears as a gap: `M` is retired and absent from the active model
set, and `M1'` is scoped out. This is structurally the same "retired
predecessor can never close its own gap" that D1 cites against the delegation
design — reached here by omission rather than by a visibly stuck cycle.

It is accepted, because no scheduler action can close such a gap either way, and
a permanently stuck cycle starves the forward lane while an annotation does not.
The `lineage_scoped_out_pre_cutover` evidence record (§4) is what keeps it
visible. Not applicable to the live incident: the pre-rollout pass scored
`gaps 0 / complete 29`.

A second, smaller residual: on the db-free plane the earliest clone row is the
earliest *present in the index*. If an older clone row were ever pruned, the
resolved boundary shifts later — the silent-hide direction. Accepted: it
requires both a re-activation and index pruning, and retention demonstrably
does not prune state entries (2026-08-22 run deleted only evidence JSONs;
audited models retain 97 usable entries each).

## Seams under test

- `_models_in_completion_scope` (`services/orchestrator/scheduler_discovery.py:158`)
  — the single choke point where the filter lands; all three verdict tiers
  consume its output.
- The lineage resolver — new, pure, per `(model_id, source_id)`. No ancestry
  walk, hence no recursion and no visited-set guard (D4).
- Cohort/candidate construction — the symmetric suppression site.
- `_breaker_engaged_gap_identities` (`:364-417`) — interaction seam, assertion
  only, no change intended.
- Backfill predecessor emission at the `t*` boundary
  (`tests/test_scheduler_backfill_predecessor.py` surface) — assertion only.

## Decisions

### D1 — Scope, do not delegate

Rationale in `proposal.md` ("Why scope, not delegate").

**Both halves are load-bearing; neither may be cut.** An earlier draft of this
design claimed that omitting cohort suppression would leave only retry noise.
That is wrong, and the correction matters for implementation priority:

- **Suppression-only** leaves the deadlock — completeness is still per-`model_id`,
  so pre-`t*` cycles keep scoring `gap`.
- **Scoping-only also leaves a deadlock**, by a different route — and this one
  is observed in production, not inferred.
  `generation_scoped_history_signal`'s `entries_for_model` filter carries **no
  `valid_time` bound** (`packages/common/state_manager.py:1418-1432`, deliberate
  per its docstring), so `M1'`'s clone row makes `exists_any_generation` `True`
  at *every* cycle, including `C < t*`. A pre-`t*` submission is therefore
  blocked as a history-bearing model missing its exact predecessor, and the
  backfill predecessor-emission path
  (`services/orchestrator/scheduler_backfill_predecessor.py`,
  `_extract_pending_predecessors`) responds by synthesizing and prepending a
  candidate at an **even earlier** cycle — the backward-recursion dead chain.

  **Field proof** (node-22 pass `scheduler_2026082207_dc7374b257be`): the 336 h
  window ending 2026-08-21T12Z makes `2026080712` the earliest in-window cycle,
  and `backfill.audit` reports `gap_count 29 / complete_count 0 /
  selected_count 1` per source. Yet `blocked_candidates` contains the four
  `M1'` entries at **`2026080700`** — a cycle *outside* the window, reachable
  only by predecessor prepending. The emitted block reason is
  `state_snapshot_index_exact_checkpoint_missing`; an earlier draft of this
  design read that token's `retryable: True` flag
  (`packages/common/state_manager.py:1198-1212`) as proof the failure mode was
  benign. The retry flag is real, but it does not stop the prepend, and the
  prepend is what recurses.

Task 3.2 (predecessor-emission guard) is therefore a primary correctness
obligation, not a tidiness item.

Note also from the same pass: at `2026080700` the **only** blocked models are
the four `M1'` entries — no legacy basin is blocked or pending there. That is
the evidence that scoping `M1'` out actually unpins the frontier rather than
exposing a second obstruction underneath it.

### D2 — Both `clone_gate_kind` values confer lineage

`'state_compatibility'` (eight-surface, ADR 0005 recalibration) and
`'hydrologic_core'` (ten-surface, fix-forward) both assert that the state
transferred and the new identity continues the old trajectory. Both therefore
establish that the predecessor's pre-`t*` work belongs to this basin. A clone
row with an absent/unrecognised `clone_gate_kind` (legacy rows, per
`openspec/specs/fingerprint-gated-state-clone/spec.md`) still confers lineage
when `cloned_from_model_id` is present — the gate kind refines *why* the clone
was admitted, not *whether* it happened.

One qualification on "present": the field must name a *different* model. A row
whose `cloned_from_model_id` equals its own `model_id` names no predecessor and
so evidences no existence-start; it is disqualified on every plane. The
disqualification is per row, not per model — a legitimate clone row alongside it
still sets `t*`, even if the self-referential row is earlier. Rejecting the row
is not the silent-hide direction that D4 forbids: D4 arbitrates genuine
ambiguity about where an identity began, and a row naming itself carries no such
evidence to weigh.

### D3 — `t*` is the clone row's `valid_time`, resolved per `(model_id, source_id)`

Clone rows are written per source, and GFS/IFS can in principle cut over at
different instants, so the resolver signature and the filter both take
`source_id`. The receipt's `cutover_time` is corroboration only, never the
scheduling input — the scheduler must not depend on receipt files.

Boundary is **strict**: a cycle is scoped out iff `cycle_time < t*`.
`cycle_time == t*` is `M1'`'s own first cycle and warm-starts from the clone row
— that is exactly `#1698`'s design. Note the clone row's own `cycle_id`
(`2026082112` in the live case) is `< t*`; that cycle was run by the predecessor
and is scoped out, which is correct.

When more than one clone row exists for a pair, take the **earliest** by
`(valid_time, created_at)` — see D4 for why existence-start, not the newest
row, is the boundary. Note this deliberately differs from
`get_latest_clone_row_for_model_source`'s `DESC` ordering
(`packages/common/state_manager.py:798-841`), which serves the publisher's
mirror-the-just-committed-row job, not an existence question. The DB-plane
resolver needs its own earliest-row read; it MUST NOT reuse the publisher's
reader unchanged.

### D4 — The boundary is the model's **own** cutover; no chain walk

A basin recalibrated twice yields `M → M'` at `t1` and `M' → M''` at `t2`, with
`t1 < t2` and only `M''` in the active model set.

An earlier draft scoped on the **earliest** `t*` in the ancestry chain. That is
wrong and reintroduces this very bug on the second recalibration: cycles in
`[t1, t2)` were run by `M'`, `M''` has no history there, and scoping on `t1`
leaves `M''` in scope for them — an unclosable gap, identical in shape to the
incident this change fixes.

**The boundary is the model's own `t*`**: the instant that `model_id` came into
existence for that source. It is resolved from the clone rows written under the
model's own `(model_id, source_id)`, with **no walk up the ancestry**. `M''` is
scoped out of `C < t2`, which is exactly right.

Dropping the walk removes the recursion and the visited-set guard from the
critical path entirely — there is nothing to recurse into, because a chain
ancestor's `t*` never participates in the scope decision. Ancestry beyond the
immediate predecessor is not needed for any decision this change makes, so the
resolver SHALL NOT walk it.

**Within the model's own rows the boundary is the EARLIEST clone row**, not the
newest (correcting D3's ordering for this specific use, see D3). Existence
starts at the first clone; a later clone row under the same `model_id` (the
re-activation / backdated-`t*` case that
`get_latest_clone_row_for_model_source`'s docstring anticipates,
`packages/common/state_manager.py:798-841`) must not retroactively scope out
cycles this identity actually ran. Taking the earliest errs toward keeping a
model in scope, which is the safe direction: it can leave a stuck gap (loud,
observable) but can never hide completed work (silent).

### D5 — Empty scope is disambiguated

Two distinct empty-scope causes must not share a verdict:

- **empty because unconfigured** (no models passed in, or all filtered by source
  scope) ⇒ `gap`, unchanged. This is a misconfiguration guard and
  `tests/test_scheduler_backfill.py:1087` pins it.
- **empty because every model was lineage-scoped out** ⇒ not a gap. Reachable
  when every current model is a recalibration (all-basin recalibration, or a
  single-basin deployment). Without this branch the starvation returns in
  exactly the shape this change is fixing.

The implementation must therefore distinguish "scope shrank to empty by the
lineage filter" from "scope was empty before the lineage filter".

## Evidence mapping

| Requirement | Oracle |
|---|---|
| Pre-`t*` cycle scores complete for a lineage-bearing model | unit: new cases in `tests/test_scheduler_backfill.py` |
| `C == t*` still requires genuine completion | unit |
| No lineage ⇒ unchanged scoring and unchanged cold-start branch | unit (both `scheduler_backfill` and `scheduler_generation` surfaces) |
| Empty-scope disambiguation (both causes) | unit, one test each |
| §8.7 breaker does not re-engage on lineage-scoped cycles | unit |
| No pre-`t*` predecessor prepend at the `t*` boundary | unit in `tests/test_scheduler_backfill_predecessor.py` |
| Second recalibration is scoped by its own `t*`, not the chain's earliest | unit on the resolver |
| A backdated re-activation does not retroactively scope out run cycles | unit on the resolver |
| Whole-system: gaps collapse, forward lane resumes | **node-22 live receipt** (production oracle) |
| Real-DB parity of the DB-plane resolver | **node-27 real-DB pytest** |

## Live receipt shape (node-22, post-deploy, next pass)

1. `gaps` collapses to only genuinely-incomplete post-`t*` cycles;
2. `gfs_2026080700` is no longer the pinned backfill frontier;
3. `2026082200` is selected as the earliest genuine gap;
4. `M1'` (`basins_jialingjiang`, `Huai-MAIN`) decides `warm_continue` from the
   `t*` clone row;
5. the forward lane advances;
6. `2026080700` / `2026080712` per-model detail shows no residual blocked model
   other than genuine post-`t*` work — this discriminates "the fix worked" from
   "the fix worked and something else is also broken".

Items 3–4 double as `#1698`'s outstanding warm-cutover verification.
