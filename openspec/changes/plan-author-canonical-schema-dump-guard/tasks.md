# Tasks: plan-author-canonical-schema-dump-guard

Fixture level: standard · Repair intensity: standard · Issue #1268
(issue suggests p3/S, implementation-ready; level standard not
compact because the change MODIFIES a just-archived requirement and
carries one explicit adjudication — the container decision — that
review must be able to attack)

Review record: fixture review round 0 → REVISE (2 P2 + 3 P3);
repair iteration 1 folded all five. P2-1 (the container consumer
sweep missed the two SUPERVISOR-side consumers — the mirror
prefix+shape gate supervisor.py:350-364 and
`resolve_container_pg_restore_identity` :1055-1058/:1768 which
extracts argv[-1] verbatim, asserts `startswith` as mount
containment, then `docker exec sha256sum`s it; the spec's
"a guard there would be a constraint no verifier checks" was a
beyond-sweep generalization falsified by the reviewer's
`/var/lib/postgresql/../../etc/passwd` probe passing all four
gates — rationale re-scoped to verbatim-symmetry only, evidence
chain completed, the `..`-traversable prefix-containment defect
recorded as PRE-EXISTING and routed to a follow-up issue). P2-2
(the issue's fifth acceptance shape — relative path — had no test
and a false must-preserve claim that the absolute-branch message
satisfies the five-shape message contract; it names the label but
not the value/canonical rendering — a relative-path negative over
all three labels is added with the actual message posture asserted
and the difference recorded as deliberate). P3-1 (predicate anchor
:157 → :156; :157 is the raise). P3-2 (blast-radius claim inverted:
two call sites DO pass schema_dump_host —
tests/…capture.py:643-648 with the value built at capture-test
:634 and tests/…live_evidence.py:5079-5082 with the value built at
:5014, both `str(tmp_path / "schema-before.dump")` hence canonical
and guard-passing — rewritten as verified fact, which is exactly
what makes the capture-file zero-diff freeze satisfiable). P3-3
(the `..` failure chain for the host dump path aborts at the
SUPERVISOR's produced-artifact inspect supervisor.py:897-909, not
first at the verifier read; prearm.py:363 checks only is_absolute —
proposal and spec wording corrected to name the supervisor stage
first).

Fix round 1 record (post cross-review, verifier batch verdicts):
three CONFIRMED, all FIX_NOW as one folded text-only pass (zero
code-behavior, zero assertion, zero predicate change; both lenses
found C1 and C2 independently). (C1, P3 record-accuracy) the NEW
plan_author comment block cited SELF-file anchors in the master
frame (:181/:183/:196) which its own +23 inserted lines shifted to
:204/:206/:219 — the fix replaces bare self-file line numbers with
stable references (the pg-dump command block / the pg-restore-list
command block / the schema-dump-list capture argv) so later edits
cannot re-drift them; same fix in the two test docstrings that
echoed :183/:196. (C2, P2 contract-accuracy — the fixture's own
decisive hazard #1, and the same universal-claim class the #1265
loop already convicted once) the "ENTIRE/complete/only consumer
set" meta-claim omitted the FIFTH consumer chain: plan_author
embeds --schema-dump-container in the schema_dump_list CAPTURE
argv (the capture-argv builder in plan_author — named, not
numbered: the fix commit's own comment growth drifts self-frame
numbers, the round-1 C1 lesson one level up), capture.py:531/:533
executes
docker exec pg_restore --list from that capture-argv value and
records list_argv into the forensic bundle, and live_evidence:1522
compares the whole capture argv by EXACT equality (not
prefix/shape); the chain carries zero Path() normalization
(verifier-confirmed independently), so the RULING stands — the fix
completes the enumeration in all four artifacts (comment, test
docstring, spec delta residual sentence, runbook) and corrects the
runbook's literally-false "only prefix/shape gates" to symmetric
textual comparisons (prefix/shape + whole-argv exact equality).
(C3, P3 record-accuracy) the :5079-5081 span missed the kwarg line
:5082 — corrected here in tasks.md (both spans) and in the PR
body. Rider (verifier-recommended, adopted): a `//x` leading-
double-slash positive for schema_dump_host (argv[-1] and
association verbatim) makes the MODIFIED scenario's three-label
`//x` carve-out executable rather than transitive — suite 385→386.

Fix round 2 record (post Phase-7 final review): the final reviewer
found ONE P2 (contract/record accuracy, docs-only, non-blocking on
behavior) — proposal.md §What Changes item 2 still carried the
three-chain "full consumer set" enumeration that fix round 1 had
completed in the other four artifacts (the comment, the test
docstring, the spec delta, the runbook); the C2 fix simply skipped
the proposal. Fixed: the proposal now names BOTH plan_author
recording sites (pg_restore-list command argv + schema-dump-list
capture argv) and the (d) capture-argv chain (capture.py :531/:533
execute+record → live_evidence.py :1522 whole-argv exact
equality), with a parenthetical recording that the final reviewer
caught the omission. Plus one note folded: tasks.md's own Fix
round 1 record cited "(:305 in the shipped frame)" — drifted to
:315 by the fix commit's comment growth, the round-1 C1 class one
level up — replaced with a named reference; and the superseded
task-1 body's three-chain enumeration completed for consistency.
All edits fixture-text-only; code files untouched in this round.

Triage note: S — one tuple entry in an existing loop + a parametrize
domain extension + two small tests + one runbook clause + one
MODIFIED spec requirement; fully hermetic. The decisive hazards:
(1) ADJUDICATION SOUNDNESS — the container-not-guarded ruling rests
on every comparison over it being verbatim-symmetric; the swept
consumer set is live_evidence.py:744-749/:1892 plus the supervisor
mirror gates :350-364/:1055-1058 (fixture-review P2-1); if any
further consumer normalizes or reads host-side, the ruling
collapses (reviewers should attack exactly this); (2) MESSAGE/ORDER PRESERVATION — repo and root
must keep failing first with byte-identical messages (loop order),
and the label string must be `schema_dump_host` (the kwarg name, so
the message names something the operator can map to
`--schema-dump-host`); (3) SPEC CONSUMPTION — the archived residual
sentence names #1268 routing; after this change no sentence may still
claim `--schema-dump-host` is unguarded.

Line anchors (orchestrator-verified at master 2822e408):
plan_author.py — DEFAULT_SCHEMA_DUMP_HOST :38 (canonical),
DEFAULT_SCHEMA_DUMP_CONTAINER :39, params :94-95, shared loop :109
(`for label, value in (("repo", repo), ("root", root)):`), guard
comment block :112-155 (unvalidated list at :153 names
`schema_dump_host`/`schema_dump_container` — must be rewritten),
predicate :156 (:157 is the raise), absolute-path branch :110-111
(message `f"{label} must be an absolute path"` — names the label,
NOT the value or canonical rendering), pg_dump argv `--file` slot
:181, association
`{"schema_dump": schema_dump_host}` :183, pg_restore list argv
:196 (container path; associations for that command are empty),
run-plan invocation record :281-282, argparse :348-349.
live_evidence.py (FROZEN) — pg_dump argv template bound to the
association value :726-733 (symmetric), pg_restore list prefix gate
:744-749, captured-listing prefix gate :1892, ledger normalization
:437/:463/:510/:521, verbatim association comparison :1439.
supervisor.py (FROZEN) — mirror prefix+shape gate :350-364,
produced-artifact no-follow inspect :897-909 (the stage where a
`..` host dump path actually aborts),
`resolve_container_pg_restore_identity` :1055-1058 (argv[-1]
verbatim + `startswith` mount-containment) invoked :1768.
prearm.py (FROZEN) — :363 checks only `is_absolute` on association
paths (passes `..`).
tests/…live_evidence.py — `_NON_CANONICAL_PATHS` :6207-6224 (six
shapes, fixed literals), negatives :6227-6247 (label parametrize
`["root", "repo"]`, asserts label+value+canonical rendering+not
`_CAPTURE_OUTPUT_PATH_ERROR`), canonical positive :6250, defaults
pin :6268-6280 (loop over DEFAULT_ROOT/DEFAULT_REPO), boundary root
:6283-6302. Suite baseline 374; capture/supervisor 14/141.
docs/runbooks/tier-node27-timeseries-storage.md — constraint
paragraph :1041-1046 (names only `--root`/`--repo` today).
openspec/specs/hypertable-compression/spec.md — the #1265
requirement :461-524 (residual sentence :484-491 names #1268
routing); the pin-domain sentence :355 ("--schema-dump-* stay
deliberately unpinned") is about verifier VALUE-pinning — checked,
stays true, out of scope.
Blast radius (verified, fixture-review P3-2): exactly two call
sites pass a custom schema_dump_host —
tests/test_node27_timeseries_compression_capture.py:643-648 (value
built at :634) and
tests/test_node27_timeseries_compression_live_evidence.py:5079-5082
(value built at :5014), both `str(tmp_path / "schema-before.dump")`
hence canonical and guard-passing; this verified fact is what makes
the capture-test-file zero-diff freeze satisfiable. Production and
runbook paths use the canonical default.

Must preserve:
- `scripts/node27_timeseries_compression_live_evidence.py`,
  `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_supervisor.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `packages/common/safe_fs.py`, `schemas/**`,
  `tests/test_node27_timeseries_compression_capture.py`,
  `tests/test_node27_timeseries_compression_supervisor.py`: zero
  diff (14/141 baselines unchanged).
- Loop order and existing messages: repo, root keep failing in the
  same order with byte-identical messages; the pre-existing
  "must be an absolute path" branch applies to the new label
  unchanged — a relative host path refuses THERE, with a message
  that names the label but deliberately NOT the value or canonical
  rendering (fixture-review P2-2: a different, weaker message
  posture than the canonicality branch — covered by its own test,
  recorded as a deliberate divergence from the issue's literal
  five-shape message wording, never claimed equivalent).
- All #1265 tests green with bodies untouched EXCEPT the two
  designated extension points (negatives label list, defaults pin
  loop); the rewired trailing-slash test, `//x` boundary test,
  canonical-root positive, twelve-kind control: byte-identical.
- The predicate itself: byte-identical (domain extension only — if
  the implementer finds the predicate must change, that is a
  reported deviation, not a silent edit).

## Implementation tasks

- [ ] 1. Guard domain — plan_author.py :109: the tuple gains
  `("schema_dump_host", schema_dump_host)` as the THIRD entry
  (after repo, root — order is contract). Rewrite the comment
  block's unvalidated list (:150-155): `schema_dump_host` moves from
  "unvalidated, follow-up routed" to guarded; the entry records WHY
  all three conjuncts carry for it (interior `//` → the :1439
  verbatim-vs-normalized false refusal, reproduced in #1268; `..` →
  prearm passes (is_absolute only) but the SUPERVISOR's
  produced-artifact no-follow inspect (supervisor.py:897-909)
  refuses it the moment pg_dump exits, before any ledger ref exists —
  and the verifier's artifact read would refuse it identically);
  `schema_dump_container` stays listed as deliberately unvalidated
  with the adjudication rationale scoped to SYMMETRY ONLY (never in
  artifact_associations — :196's command has empty associations; its
  entire consumer set — verifier gates :744-749/:1892, supervisor
  mirror gates :350-364/:1055-1058, AND the capture-argv chain
  (plan_author's schema-dump-list capture argv → capture.py
  :531/:533 execute+record → live_evidence.py :1522 whole-argv
  exact equality; fix round 2 completed this enumeration here too) —
  compares verbatim with zero
  normalization, so the verbatim-vs-normalized false refusal cannot
  occur for it; do NOT claim "no verifier checks it" — the
  supervisor extracts and sha256sums it). `capture_repo` entry
  unchanged.
- [ ] 2. Negatives — tests :6227: label parametrize becomes
  `["root", "repo", "schema_dump_host"]` (6 shapes × 3 labels = 18
  ids); assertion body unchanged (label in message, value, canonical
  rendering, not `_CAPTURE_OUTPUT_PATH_ERROR`); docstring extended
  to say why the third label is the same disease one field over
  (verbatim into artifact_associations :183, compared verbatim at
  :1439 against the normalized ledger ref).
- [ ] 3. Positives and pins:
  (a) canonical custom host path: `build_run_plan(mutation_head_sha=
  HEAD, schema_dump_host=<canonical tmp-derived .dump path>)` authors,
  and the pg_dump command's `artifact_associations["schema_dump"]`
  records the value VERBATIM (equality assertion — the guard must
  not normalize, only refuse);
  (b) defaults pin :6277: the loop gains `DEFAULT_SCHEMA_DUMP_HOST`
  (same two assertions; docstring notes the runbook's authorized
  command passes no `--schema-dump-host` either);
  (c) container adjudication pin (new test): `schema_dump_container=
  "/var/lib/postgresql//evidence/schema-before.dump"` (interior `//`,
  prefix-compatible with the :744-749 gate) still AUTHORS, and the
  value lands verbatim as the pg_restore list argv's last element —
  docstring records the full adjudication scoped to the symmetry
  rationale (this is the executable form of the issue's "no third
  silent state" requirement; if a future change guards the container
  path, this test is the one that must consciously flip);
  (d) relative-path negative (fixture-review P2-2, the issue's fifth
  acceptance shape): parametrized over all three labels, a relative
  value (e.g. "relative/schema.dump") raises `PlanAuthorError` from
  the pre-existing absolute-path branch; asserts the label and the
  literal "must be an absolute path" wording and (docstring) records
  that this branch's message deliberately lacks the value/canonical
  rendering — pinning the branch so a future refactor moving the
  is_absolute check cannot silently drop the refusal.
- [ ] 4. Runbook :1041-1046: extend the paragraph — custom
  `--schema-dump-host` joins `--root`/`--repo` under the same
  canonical-absolute-path constraint; one clause records that
  `--schema-dump-container` is a container-internal path checked
  only by verbatim-symmetric prefix/shape gates (verifier and
  supervisor mirror alike) and deliberately not
  canonicality-guarded.
- [ ] 5. Spec delta (in this change): MODIFIED requirement "The plan
  author MUST reject non-canonical repo and root paths at authoring
  time" — guard domain now repo, root and `schema_dump_host`
  (labeled by kwarg name); rationale layers restated to cover the
  host-dump path; residual list shrinks to `capture_repo` and
  `--schema-dump-container` (with the symmetric-prefix adjudication
  rationale); the "#1268 routing" sentence is consumed (paid, not
  re-routed). Scenarios: non-canonical scenario WHEN gains the
  schema_dump_host label; defaults scenario names
  `DEFAULT_SCHEMA_DUMP_HOST`; a new scenario pins the container
  adjudication. `openspec validate
  plan-author-canonical-schema-dump-guard --strict --no-interactive`
  green.
- [ ] 6. Full verification (orchestrator Phase 2 reproduces): three
  suites green (live_evidence baseline 374 + new ids; 14/141
  frozen); red proof by reverting ONLY the tuple-entry hunk → ALL 7
  schema_dump_host negatives DID NOT RAISE (per-id list): the 6
  canonicality ids AND the relative id, because the is_absolute
  branch lives inside the same loop, so removing the tuple entry
  removes both refusals for that label — while the root/repo
  relative ids and everything else stay green (sharp attribution to
  the domain extension alone); host-path positive/adjudication/
  defaults tests stay green under the revert too; frozen-surface
  zero diff; `uv run ruff check .`; openspec strict.

## Evidence Floor

- Three suites green with counts (baseline 374/14/141; only
  live_evidence grows).
- Red proof: tuple-entry revert → exactly the 7 schema_dump_host
  negatives DID NOT RAISE (6 canonicality + 1 relative, per-id
  list), everything else green (root/repo relative ids included).
- Frozen surfaces zero diff (live_evidence.py included); ruff +
  openspec strict green.
- PR body records: the container adjudication and its COMPLETE
  consumer evidence chain (:196 empty associations, verifier
  :744-749/:1892 + supervisor :350-364/:1055-1058 symmetric gates),
  the pre-existing `..`-traversable prefix-containment defect routed
  to a follow-up issue, the relative-path message-posture
  divergence, the consumed spec residual, the checked-unchanged
  :355 pin-domain sentence, and 偏离记录 (explicit "no deviations"
  otherwise).
