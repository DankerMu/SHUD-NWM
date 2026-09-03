# Tasks

Fixture level: expanded · repair intensity: high · issues: #1691 #1764 #1812 #1662.
Line cites against `origin/master` `f9a1345f`; symbol names are authoritative.

## 0. Evidence Floor

Oracles: local (macOS) pytest/ruff/openspec; vault `npm run test:subagent-workflow`;
CI on every pushed head. No node-27/node-22 receipt: no production data path,
DB, display, or scheduler behaviour changes (verifier output bytes unchanged).

- [x] Red proof (#1691): the 15 non-reference shapes and the wrapper-around-existing-ref case qualify on pre-change source (test run output pasted in the implementer report), fail closed after.
- [x] `uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py` green.
- [x] `uv run ruff check .` clean.
- [x] `openspec validate fail-closed-invocation-shapes-and-loop-log-catch-schema --strict --no-interactive` passes.
- [x] `openspec validate node22-db-free-scheduler-state --strict --no-interactive` passes (#1812).
- [x] Vault: `npm run test:subagent-workflow` green (#1764).
- [x] `uv run .claude/skills/subagent-workflow/scripts/loop_log_audit.py --log docs/review-loop-log.jsonl` (post-sync, post-backfill) reports the skipped count under the fixture's compliant-catch definition (`round` non-negative integer, `0` = fixture-review round; `lens` non-empty string) with every remaining skipped catch declared unattributable in ADR 0003 by line + index (task 2.5's sanctioned alternative: 9 of 24 recoverable, `skipped=15`, all 15 listed); the resulting `core=109 rotated=301 skipped=15` recorded in ADR 0003. (Deviation from the original `skipped=0` wording recorded in the PR 偏离记录.)
- [ ] CI green on the final head.

## 1. #1691 — fail-closed `*_invocation` input shapes (D1)

- [x] 1.1 New helper `_require_artifact_reference_shape(value, label) -> dict` in `scripts/node27_timeseries_compression_live_evidence.py` whose checks and messages are exactly the syntactic prefix of `_artifact_ref_from_raw` (`:501-511`): `"{label} must be an object"`, exact keys `{path, sha256, bytes}`, `"{label}.path must be absolute"`, `"{label}.sha256 must be lowercase sha256"`, `"{label}.bytes must be a non-negative integer"` (bool rejected). `_artifact_ref_from_raw` delegates that prefix to it (messages byte-identical; existing tests green). `_artifact_bytes` and `_streaming_artifact_ref` are NOT changed (intentional — their message sets differ and the streaming helper has no pre-read sha/bytes check; changing them is out of scope). Diff to those two functions must be empty.
- [x] 1.2 Call the helper on `recovery.invocation` (after `:3475`), `migration.first_invocation` / `migration.second_invocation` (after `:3548-3558`), `receipts.dry_run_invocation` / `receipts.enforce_invocation` (after `:3614-3618`) with those exact labels; any other shape raises `EvidenceError`. The re-derivation from `execution.ledger` stays as is.
- [x] 1.3 Replace `test_non_reference_invocation_shapes_escape_closure_and_still_qualify` with `test_non_reference_invocation_shapes_fail_closed_at_the_input_shape_gate`: 5 slots × {extra scalar key, bare string, `null`} = 15 cases → `EvidenceError` whose message contains the slot label; `_UNCHECKED_INVOCATION_SHAPES` comment block rewritten; no "escape is expected" wording survives.
- [x] 1.4 Replace `test_invocation_shape_wrapping_a_reference_is_still_closure_checked` with two pins: `test_invocation_wrapper_around_a_missing_reference_fails_closed` (through `verify_bundle`: `EvidenceError` whose message names the slot, not the closure node) and `test_invocation_wrapper_around_an_existing_reference_fails_closed_at_the_input_shape_gate` (wrapper around the slot's own existing authored ref → `EvidenceError` naming the slot; the previously-escaping case), both parametrized over the 5 slots.
- [x] 1.5 `main()` pin `test_main_rejects_an_invocation_wrapper_around_an_existing_reference`: bundle with one slot wrapping its own existing authored ref written to disk, `evidence.main([...]) == 1`, failure terminal document produced (pattern at `tests/…:3507`).
- [x] 1.6 Positive pins unchanged and green: missing-path / symlink / identity-disagrees (`BoundedEvidenceError`), retained-in-manifest, bundle-author-shape-once.
- [x] 1.7 Byte-identity pin: terminal document for the canonical fixture bundle (and the bundle-author shape) equals the pre-change output except `generated_at` — implementer captures the pre-change output in the red-proof run and asserts structural equality in the report with the diff command shown (a committed golden is not required).
- [x] 1.8 Five schema `description`s (`schemas/timeseries_compression_live_evidence.schema.json:90,128,129,157,158`) and the runbook sentence (`docs/runbooks/tier-node27-timeseries-storage.md`, "A value of any other shape is not itself a closure node…") replaced with: any other shape is rejected by the verifier's input-shape check and the run fails closed (no "before closure" claim — see D1). The schema-description scenario test (`tests/…:7265-7268`) extended to assert each description also states the fail-closed clause.

## 2. #1764 — loop-log catch schema (D2)

- [x] 2.1 Vault `scripts/evidence_check.py::check_loop_log_entry`: merged lines — every `catches[i]` must be a compliant catch (D2 definition: mapping; `round` non-negative integer, bool rejected, `0` allowed; `lens` non-empty string); one finding per violation naming `catches[i]` and the missing/invalid key. Tests: compliant entry (incl. a `round: 0` catch) → exit 0, no finding; each of missing `round`, missing `lens`, string `round`, negative `round`, bool `round`, empty `lens`, non-mapping catch (one bad catch per test entry) → exit 2 with exactly one `[loop-log]` finding naming `catches[<i>]` and the key.
- [x] 2.2 Vault `scripts/loop_log_audit.py`: `rotation_attribution` skips only non-compliant catches (same definition) and returns the skipped count; a missing `lens` is no longer counted as rotated. `main` scans the catches of **all** entries (not only `multiround`), prints `NOTE non-compliant catches skipped: <n> in <k> entry(ies) (pr …)` when `n > 0`, and appends `skipped=<n>` to the rotation attribution line. Tests: skipped surfaced with count and PR; `skipped=0` when compliant; an entry without a `round_lenses` key but with non-compliant catches is still reported; a `lens`-less `round: 2` catch is skipped, not rotated. Existing `core=8 rotated=8` assertion stays green.
- [x] 2.3 Vault sibling `skills/orche-omp-workflow/scripts/loop_log_audit.py`: same fix if it carries the same `rotation_attribution`; otherwise CHANGELOG records why not.
- [x] 2.4 Vault: `skill.json` + `SKILL.md` frontmatter → 0.31.1, CHANGELOG `## [0.31.1] - 2026-09-02`, `npm run test:subagent-workflow` green. Commit locally in the vault; **do not push** (report the commit SHA).
- [x] 2.5 Backfill `docs/review-loop-log.jsonl` lines 440/442/443/445/461 (PR #1730/#1738/#1746/#1751/#1802): each non-compliant catch gains `round` + `lens` traced from the PR's review evidence comments (`gh pr view <n> --comments`), keeping `class`/`severity`/`what`; fixture-review findings are `round: 0`, `lens: "fixture-review"`; line 461 (no `round_lenses` key today) gains `round_lenses` from its evidence. Anything not recoverable is left unchanged and declared unattributable (line + catch index) in the ADR. Report per catch: source comment and the lens/round it yielded.
- [x] 2.6 ADR 0003 correction section (dated 2026-09-02): measured 1600 catches / 1576 with a string `lens` / 24 without, across 5 entries (17 `phase`-only + 7 `lens`-less with `round` present in PR #1802, whose line has no `round_lenses` key); the two failure modes and their opposite biases (missing `round` → skipped/undercount; missing `lens` at `round >= 2` → counted rotated/overcount); supersedes the PR #1759 revisit's 17/1331/4-entry figure; post-backfill audit line with `skipped=0`; keep direction unchanged or changed, stated.
- [x] 2.7 Orchestrator: re-sync `.claude/skills/subagent-workflow/scripts/` and `.agents/skills/subagent-workflow/scripts/` from the vault before Phase 8; PR body states the two landing places.

## 3. #1812 — stale `reconciliation_decision` delta (D3)

- [x] 3.1 `openspec/changes/node22-db-free-scheduler-state/specs/file-orchestration-journal/spec.md:198-201`: enum → the eight members of `ACCEPTED_RECONCILIATION_DECISIONS` (`services/orchestrator/accepted_submit_identity.py:149-166`).
- [x] 3.2 `:249-252`: retry-permission clause → each absence decision (`absence_retry_permitted`, `operator_verified_absence`) is produced only by its own dedicated typed boundary (aligned with live `job-retry-mechanism/spec.md:2457-2466`). `:352` gains the second absence member in its illustration; `:393` unchanged (automatic discovery path only).
- [x] 3.3 Sibling deltas grep (four files) recorded: zero hits.
- [x] 3.4 `openspec validate node22-db-free-scheduler-state --strict --no-interactive` passes.
- [x] 3.5 Archive simulation in a scratchpad copy: produced `openspec/specs/file-orchestration-journal/spec.md` contains neither the six-value enum nor the single-producer clause; real change not archived; receipt in `.workplans/`.

## 4. #1662 — receipt only (D4)

- [x] 4.1 On this branch's base: hard-gate test green; CLI `hard_gate_status=pass`, `failing_count=0`; spec md5 unchanged vs `origin/master`; recorded in the PR body and the closing comment, which also names the "mirrored" wording question as unaddressed. No edit.

## Risk packs

See `design.md` "Risk packs" (selected: Public API/CLI, Schema/field names,
Error handling, Documentation, Legacy compatibility, Release (light); the rest
not selected with reasons).

## Non-goals

See `proposal.md`.
