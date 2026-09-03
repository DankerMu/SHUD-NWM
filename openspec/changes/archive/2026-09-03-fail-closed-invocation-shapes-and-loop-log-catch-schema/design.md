# Design

Fixture level: expanded · repair intensity: high · project profile: NHMS
(`openspec/project-profile.md`). Line cites against `origin/master` `f9a1345f`;
symbol names are authoritative when lines drift.

## D1 (#1691) — input-shape gate at the slot sites, not a bundle schema

`verify_bundle` already pins the five slots by key presence:
`_require_exact_keys(recovery_bundle, {"preflight","receipt","invocation"}, "recovery")`
(`scripts/node27_timeseries_compression_live_evidence.py:3475`), the `migration`
key set (`:3548-3558`), the `receipts` key set (`:3614-3618`). Directly after
each of those three sites, validate the slot **values** with a new helper
`_require_artifact_reference_shape(value, label) -> dict` whose checks and
messages are exactly the syntactic prefix of `_artifact_ref_from_raw`
(`:501-511`): `_require_mapping` (`"{label} must be an object"`),
`_require_exact_keys(ref, {"path","sha256","bytes"}, label)`, then
`"{label}.path must be absolute"`, `"{label}.sha256 must be lowercase sha256"`,
`"{label}.bytes must be a non-negative integer"` (bool excluded). The mapping
check comes first because a bare `_require_exact_keys` on a string does
`set("...")` and yields a misleading key-diff error. `_artifact_ref_from_raw`
delegates its syntactic prefix to the new helper (byte-identical messages, no
behaviour change), so the five slots and the raw-bytes dereferencer share one
definition. `_artifact_bytes` (`:428-447`, folds sha/bytes/`max_bytes` into one
`"reference metadata is invalid"` condition) and `_streaming_artifact_ref`
(`:525-541`, no pre-read sha/bytes check) are **intentionally left untouched**:
routing them through the helper would change their error strings and, for the
streaming helper, add new pre-read rejections — out of #1691's scope. No test or
spec pins those strings (grep `reference metadata is invalid`: source line + a
comment referring to `evidence_io.py`), but the diff still does not touch them.
Labels: `recovery.invocation`, `migration.first_invocation`,
`migration.second_invocation`, `receipts.dry_run_invocation`,
`receipts.enforce_invocation`. Errors raise `EvidenceError`
(`= TerminalStateError`, `:377`), the verifier's fail-closed type.

Why here and not a bundle JSON Schema: the issue's own trade-off — a bundle
schema duplicates `verify_bundle`'s structural checks and drifts.

**Ordering facts the tests depend on.**
- In `verify_bundle` the slot sites run before closure resolution
  (`resolve_artifact_closure` at `:4095` when `artifact_manifest` is not passed).
  So through `verify_bundle` directly, a malformed slot fails with `EvidenceError`
  at the shape gate; a well-formed three-key ref to a missing path passes the gate
  and still fails at closure with `BoundedEvidenceError` (unchanged).
- In `main()` (`:4172-4174`) `resolve_artifact_closure` runs **before**
  `verify_bundle`. A wrapper around a *missing* nested ref fails there
  (`BoundedEvidenceError` → re-raised as `EvidenceError`); a wrapper around an
  *existing* nested ref passes closure and is rejected by the new shape gate in
  `verify_bundle`. Both are fail-closed; the shape gate is the only thing that
  catches the second case, which is the escape closure structurally cannot see.
- Positive path: the gate only reads the authored value; the terminal slot is
  still re-derived from `execution.ledger` (`:3568-3569`, `:3623-3624`), so the
  terminal document is byte-identical except `generated_at`.

**Tests (all in `tests/test_node27_timeseries_compression_live_evidence.py`).**
- `test_non_reference_invocation_shapes_escape_closure_and_still_qualify` (`:7383`)
  is replaced by `test_non_reference_invocation_shapes_fail_closed_at_the_input_shape_gate`:
  parametrized over the 5 slots × {extra scalar key, bare string, `null`} = 15
  cases, each raising `EvidenceError` whose message contains the slot label; the
  old `_UNCHECKED_INVOCATION_SHAPES` comment block is rewritten (no "escape is
  expected" wording survives).
- `test_invocation_shape_wrapping_a_reference_is_still_closure_checked` (`:7417`)
  is replaced by two pins: `test_invocation_wrapper_around_a_missing_reference_fails_closed`
  (through `verify_bundle`: `EvidenceError` whose message names the slot, not
  the closure node) and
  `test_invocation_wrapper_around_an_existing_reference_fails_closed_at_the_input_shape_gate`
  (wrapper around the slot's own existing authored ref → `EvidenceError` naming
  the slot — the case that escaped before this change).
- `main()` pin (the CLI boundary the Public API and Error-handling packs cite):
  `test_main_rejects_an_invocation_wrapper_around_an_existing_reference` — the
  wrapper-around-existing-ref bundle written to disk, `evidence.main([...])`
  returns `1`, and the failure terminal document is produced (pattern at
  `tests/…:3507`). Under `main()`'s ordering this is the only shape the new gate
  catches that closure does not, so it is the one to exercise end to end.
- Description-scenario test (`tests/…:7265-7268`, five slots each naming
  `execution.ledger`) extended to assert each description also states that any
  shape other than a `{path, sha256, bytes}` reference fails closed.
- `test_wellformed_invocation_reference_at_a_missing_path_fails_closed` (`:7307`),
  `..._naming_a_symlink_fails_closed`, `..._whose_identity_disagrees_fails_closed`,
  `test_wellformed_authored_invocations_are_retained_in_the_terminal_manifest`
  (`:7437`), `test_bundle_author_shape_carries_the_ledger_reference_once_in_the_manifest`:
  unchanged and green.
- Byte-identity pin: canonical fixture bundle verified before/after the change
  differs only in `generated_at` — implementer records the pre-change terminal
  JSON (minus `generated_at`) in the red-proof run and asserts equality in a new
  test against a committed expectation or by structural comparison with the
  pre-change output captured in the report.
- Red proof: the 15-case and wrapper-around-existing-ref tests must FAIL on
  pre-change source (they qualify) and PASS after.

**Wording.** Five schema `description`s
(`schemas/timeseries_compression_live_evidence.schema.json:90,128,129,157,158`)
and the runbook paragraph (`docs/runbooks/tier-node27-timeseries-storage.md`
§"Referenced JSON contracts", the sentence "A value of any other shape is not
itself a closure node, though any well-formed reference nested inside it still
is, collected in its own right") change to: any other shape — a mapping with
extra or missing keys, a wrapper around a reference, a string, `null` — is
rejected by the verifier's input-shape check and the run fails closed; it never
qualifies. Do **not** write "before closure": through `main()` closure
resolution runs first, so a wrapper around a missing reference fails at the
closure node before the gate is reached — still fail-closed, but the error
names the closure node, not the slot. The spec delta (this change's
`specs/hypertable-compression/spec.md`) carries the same qualified statement as
a MODIFIED requirement, replacing the scenario that currently asserts the escape.

## D2 (#1764) — enforce the catch schema upstream, repair the log here

Two repos, stated up front.

**Compliant catch — one definition, used by both scripts and by the backfill:**
a mapping with `round` present as a non-negative integer (`0` is the established
fixture-review round: 454 catches in the log carry `round: 0`, 437 of them with
`lens: "fixture-review"`) and `lens` a non-empty string. Bool is not an integer.

| Where | What |
| --- | --- |
| `/Users/danker/Desktop/AI-vault/my-agents` (git, `DankerMu/my-agents`) — `skills/subagent-workflow/` | `scripts/evidence_check.py::check_loop_log_entry`: for a merged line, every element of `catches` must be a compliant catch per the definition above; each violation is one finding naming `catches[i]` and the missing/invalid key. `scripts/loop_log_audit.py::rotation_attribution`: skip only non-compliant catches and return the skipped count alongside `(core, rotated)` — today a missing `round` defaults to 1 and is skipped, while a missing `lens` with `round >= 2` is silently counted as **rotated** (`:71-74`); both become "non-compliant, skipped, counted". `main` scans the catches of **every** entry (not only the `multiround` subset — PR #1802's line has no `round_lenses` key and would otherwise never be scanned), prints `NOTE non-compliant catches skipped: <n> in <k> entry(ies) (pr …)` whenever `n > 0`, and the rotation line carries the same whole-log `skipped=<n>` (population: every entry, as on the NOTE; `core`/`rotated` remain the multi-round subset). Tests in `tests/test_evidence_check.py` (compliant → 0; missing `round`, missing `lens`, string `round`, negative `round`, bool `round`, empty `lens`, non-mapping catch → 2; `round: 0` → 0) and `tests/test_loop_log_audit.py` (skipped count and PR surfaced; zero when compliant; an entry without `round_lenses` but with non-compliant catches is reported; existing `core=8 rotated=8` stays green). Bump `skill.json` + `SKILL.md` frontmatter version (PATCH → 0.31.1), CHANGELOG entry, `npm run test:subagent-workflow` green. `skills/orche-omp-workflow/scripts/loop_log_audit.py` is a diverging sibling copy: apply the same `rotation_attribution` fix there if it carries the same function, else record why not in the CHANGELOG. |
| this repo | `docs/review-loop-log.jsonl` lines 440/442/443/445/461: rewrite each non-compliant catch to `{"round","lens","class","severity"}` (keep `what`) using the PR's evidence comments (`gh pr view <n> --comments`, review-round bundles name reviewer/lens and round; fixture-review findings are `round: 0`, `lens: "fixture-review"`); line 461 (no `round_lenses` key) gains `round_lenses` from its evidence (outcome: PR #1802's record carries no lens token, so the line stays unchanged and its 7 catches are declared unattributable). Any catch whose round or lens cannot be recovered from the PR record is left as-is and declared unattributable in the ADR with its line number and catch index — never invented. `docs/adr/0003-review-lens-rotation-keep.md`: a dated correction section that (a) restates the measured figures (1600 catches / 1576 with a string `lens` / 24 without, across 5 entries: 17 `phase`-only in PR #1730/#1738/#1746/#1751 and 7 `lens`-less with `round` present in PR #1802), (b) distinguishes the two failure modes and their opposite biases (missing `round` → skipped, undercounts; missing `lens` at `round >= 2` → counted rotated, overcounts), (c) supersedes the PR #1759 revisit's "17/1331, four entries" figure, (d) records the post-backfill audit line (`core=? rotated=? skipped=<n>`, `n = 0` only when every catch proved recoverable) and whether the keep direction changed. |

Local copies (`.claude/skills/subagent-workflow/scripts/`, `.agents/skills/subagent-workflow/scripts/`)
are gitignored; the orchestrator re-syncs them from the vault after integration
so this PR's own loop-log line is checked by the fixed scripts. The vault commit
is made locally and **not pushed** (outside this PR's pre-authorisation).

## D3 (#1812) — refresh the pending delta to the merged decision face

`openspec/changes/node22-db-free-scheduler-state/specs/file-orchestration-journal/spec.md:198-201`:
enum becomes the eight members of `ACCEPTED_RECONCILIATION_DECISIONS`
(`services/orchestrator/accepted_submit_identity.py:149-166`), listed in the same
order. `:249-252`: "`absence_retry_permitted` MUST be produced only by the
authoritative typed retry-permission boundary" → each absence decision
(`absence_retry_permitted`, `operator_verified_absence`) MUST be produced only by
its own dedicated typed boundary, mirroring live
`openspec/specs/job-retry-mechanism/spec.md:2457-2466` (the authoring-boundary
sentence is at `:2464-2466`). Line `:393` ("an owner-scoped zero result alone
cannot become `absence_retry_permitted`") governs the automatic discovery path
only and stays unchanged. Line `:352` (generic upsert writing
`absence_retry_permitted` cannot make typed reclaim submit) gains the second
absence member so the illustration stays symmetric with the generalized
retry-permission clause (fixture-review recommendation; one clause). The other
four deltas of that change were grepped
(`matched_bound|absence_retry_permitted|identity_mismatch|reconciliation_decision|reconciliation_source|six-value`):
zero hits, so no sibling copy there; the change's own `design.md` (`:1323-1327`,
`:1537`) carried the six-value enum and the single-source wording and was
updated in step. Round-1 review caught two more stale faces in the same
requirement block, fixed together: `reconciliation_source` was pinned to the
single value `slurm_exact_comment` although live `pipeline-job-persistence`
and the journal code persist `slurm_name_window_unique` on the fallback bind,
and the generic-API clause's "mismatch" shorthand admitted
`identity_mismatch_released` once the enum carried two mismatch members
(`_GENERIC_VERSIONED_RECONCILIATION_DECISIONS` excludes it); the clause now
names the four generic decisions and pins the release exit to its typed
transition. Verification: `openspec validate
node22-db-free-scheduler-state --strict --no-interactive`; archive simulation in
a scratchpad copy of the repo (`openspec archive ... -y`), grep the produced
`openspec/specs/file-orchestration-journal/spec.md` for the six-value enum,
the single-producer clause, the single-value `reconciliation_source=` form and
the generic-API "mismatch" shorthand (all four must be absent), and for the
four named generic decisions plus the typed release-transition sentence (both
present). The real change is **not**
archived.

## D4 (#1662) — verified resolved, no edit

Receipt on this base (`f9a1345f`): the hard-gate test passes; CLI
`hard_gate_status=pass`, `failing_count=0`, `gate_eligible=0`; the spec file's
md5 is unchanged vs `origin/master`; `audit_repo_entropy.py` untouched. The
existing repo-wide test is the regression pin. The closing comment carries this
receipt **and names the "mirrored" wording question as unaddressed** (the
issue's last comment reserves it for a human; it does not die silently with the
issue).

## Invariant Matrix (#1691)

Governing invariant: at every `_require_exact_keys` site owned by
`verify_bundle` (the input-shape gate's domain), each required key is either
dereferenced by a validating helper or shape-checked by the same helper before
the run can qualify — no such key is presence-only. Evidence-only keys read by
`_validate_checkpoint_artifacts` / `_validate_phase` are outside this invariant.
Source-of-truth identity/contract: the artifact reference shape
`{path, sha256, bytes}` (`schemas/…schema.json` `$defs/artifact_ref`;
`packages/common/evidence_io.artifact_references`).
Surfaces:
- Producers: `scripts/node27_timeseries_compression_bundle_author.py:237-260`
  (writes the ledger ref into all five slots — unchanged, stays valid).
- Validators/preflight: `verify_bundle` slot sites `:3475`, `:3548-3558`,
  `:3614-3618` (changed: shape gate via `_require_artifact_reference_shape`);
  `_artifact_ref_from_raw` delegates its syntactic prefix to that helper
  (messages byte-identical); `_artifact_bytes` and `_streaming_artifact_ref`
  intentionally untouched (see D1).
- Storage/cache/query: none — verifier has no store.
- Public routes/entrypoints: `main()` (`:4148`) — unchanged ordering; the gate
  lives in `verify_bundle` which `main()` always calls.
- Frontend/downstream consumers: terminal document consumers (`schemas/…schema.json`
  validation at `:4208`) — terminal bytes unchanged except `generated_at`.
- Failure paths/rollback/stale state: `EvidenceError` propagates to the existing
  failure terminal-state writer; no partial publish (CAS publish is after verify).
- Evidence/audit/readiness: schema descriptions ×5, runbook §"Referenced JSON
  contracts", this change's spec delta.
Regression rows:
- slot = exactly `{path, sha256, bytes}` naming an existing regular file → qualifies; terminal identical except `generated_at`.
- slot = three-key ref, missing path / symlink / hash-or-size mismatch → `BoundedEvidenceError` at closure (unchanged).
- slot ∈ {four-key mapping with scalar extra, bare string, `null`} ×5 slots → `EvidenceError` naming the slot; never qualifies.
- slot = wrapper mapping around an existing valid ref → `EvidenceError` naming the slot (new pin; previously qualified).
- slot = wrapper mapping around a missing ref → fail closed (`EvidenceError` naming the slot via `verify_bundle`; `BoundedEvidenceError`→`EvidenceError` naming the closure node via `main()`).
- `main()` + wrapper around an existing ref → exit `1`, failure terminal document written (the only escape closure cannot see).
- bundle-author shape (all five slots = ledger ref) → qualifies; manifest carries the ledger ref once (unchanged sibling).
- other required keys dereferenced by `_json_artifact` / `_load_receipt` / `_artifact_bytes` / `_streaming_artifact_ref` → unchanged behaviour and error strings (only `_artifact_ref_from_raw` delegates, message-identical).

## Boundary-surface checklist (high)

- Shared helper roots: `packages/common/evidence_io.py` — read only, not changed.
- Public entrypoints: `main()` — not changed.
- Read surfaces: slot values now read for shape only; no new file reads.
- Write/delete/overwrite surfaces: none touched.
- Producer/consumer evidence boundaries: bundle author (producer) unchanged and
  still accepted; terminal schema (consumer) unchanged.
- Stale-state/idempotency: none.
- Unchanged downstream consumers: node-27 runbook operators using the committed
  bundle author — unaffected (author output is well-formed).

## Risk packs

- Public API / CLI / script entry: selected — `main()` of the verifier is an
  operator CLI; the new rejection is a new stable error at that boundary.
- Schema / columns / units / field names: selected — schema descriptions and the
  `reconciliation_decision` enum text are the contract surface.
- File IO / path safety / overwrite: not selected — no new reads/writes; closure
  semantics untouched.
- Error handling / rollback / partial outputs: selected — fail-closed is the
  whole point; the failure terminal path must stay stable.
- Documentation / migration notes: selected — runbook, schema descriptions, ADR
  0003 correction, PR body split statement.
- Legacy compatibility / examples: selected — bundle-author shape and the
  hand-assembled legacy shape both keep qualifying.
- Config / project setup: not selected — none touched.
- Auth / permissions / secrets: not selected — `_reject_secrets` untouched.
- Concurrency / shared state / ordering: not selected — single-process verifier.
- Resource limits / large input / discovery: not selected — no new traversal.
- Release / packaging / dependency compatibility: selected (light) — upstream
  skill version bump is a release step in another repo.
- Domain packs (geospatial, hydro-met series, SHUD numerics, PostGIS/Timescale
  semantics, Slurm lifecycle, providers, run manifest/QC, published artifacts):
  not selected — no production data path changes; the verifier's compressed
  output contract is byte-unchanged.

## Review focus

1. The shape gate sits before any closure use and uses the single shared helper.
2. The flipped tests really fail on pre-change source (red proof in the report).
3. Terminal byte-identity except `generated_at` on both live bundle shapes.
4. Backfilled catches trace to PR evidence; nothing invented; ADR numbers match
   a fresh measurement.
5. #1812 delta text equals the code/live decision set; archive simulation greps
   are clean; the real change is not archived.
