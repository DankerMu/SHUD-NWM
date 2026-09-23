## Context

Measured on master `f3fd8034c`:

- `.claude/skills/subagent-workflow/scripts/` holds `fix_gate.py` and `run_unit_tests.py` only.
  - `fix_gate.py` keeps per-PR fix-pass state in the gitignored `.review-gate.json`. It has no issue history, no outcome vocabulary and no cross-PR escalation.
  - `grep -rn "review-gate-issues|review_gate|loop_log|review-loop-log|evidence_check" .claude/skills/subagent-workflow/` returns nothing.
- `.review-gate-issues.json` has only the `issues` key (PR #2469 repaired it). Its last commit is `f5057b03e` (2026-09-18) and its highest issue key is 2472. PRs #2581, #2588, #2592, #2598 and #2606 were merged after that and left no record.
- `docs/review-loop-log.jsonl` has 690 lines. Its last row is PR #2496 (2026-09-18).
- `tests/test_loop_log_audit_attribution.py` loads `.agents/skills/subagent-workflow/scripts/loop_log_audit.py` by path and module-level-skips, because that path has not existed since `002ba4b59`.
- The #2036 version of the audit is recoverable. `git show 002ba4b59^:.agents/skills/subagent-workflow/scripts/loop_log_audit.py` gives 421 lines, stdlib-only, with no sibling imports.

## Goals / Non-Goals

- **Goals**
  - The #2036 audit lives at a tracked path, its suite runs in CI with zero skips, and ADR 0003 gets the recomputed ratio.
  - The round-ceiling memory has a tracked writer that cannot emit a bare top-level key or an out-of-vocabulary outcome, plus a tracked escalation read, and both have a documented caller.
  - The three specs name no deleted suite and state the measured importer count.
  - #2044 gets a written ruling.
- **Non-Goals**
  - Re-tracking `.claude/skills/**` or `.agents/skills/**`; the `002ba4b59` governance decision stands.
  - Editing existing `docs/review-loop-log.jsonl` lines, or appending synthesized ones. The log is frozen, and no current writer exists.
  - Back-filling round counts for F/G1/G2/H (#2581 #2588 #2592 #2598). Their gate state was never persisted, so any number would be fabricated.
  - A verdict-table hook. Its only enforcement point would be `fix_gate.py record-round`, which is untracked and upstream-owned. A repo-local checker over the gitignored `.workplans/` cannot be verified by CI.
  - Changing `ci.yml` for #2044.

## Decisions

### D1 (#2477): restore the #2036 audit as tracked governance tooling

1. Copy `002ba4b59^:.agents/skills/subagent-workflow/scripts/loop_log_audit.py` to `scripts/governance/loop_log_audit.py`. Keep the semantics byte-for-byte (`rotation_sample`, `Attribution(core, rotated, final_review, skipped)`, exit codes 0/2). Allowed edits are mechanical only:
   - ruff-clean;
   - docstring and stdout wording that no longer claims the skill runs it, or names the retired `evidence_check --loop-log-entry` (`:61`, `:123`, `:142`, `:219`, `:330`), so that the ADR does not inherit a false claim.
2. **11 of the suite's 32 cases** exercise the historical writer-side entry checker, `evidence_check.check_loop_log_entry`, through the `check_entry` helper and `evidence.main`:
   - pending entry accepted;
   - legacy pseudo-lens ×2;
   - off-vocabulary `rotation_intent`;
   - `phase7_catches` lens required;
   - flat-string `round_lenses`;
   - nested lists accepted;
   - malformed shapes ×3;
   - CLI exit 2.

   **Port.** Port `check_catch` and the body of `check_loop_log_entry` (from `002ba4b59^:.agents/skills/subagent-workflow/scripts/evidence_check.py:89-200`), minus file IO, as a pure function `loop_log_entry_errors(entry) -> list[str]` in the tracked module.
   - Its local constants come along: `FIXTURE_LEVELS`, `OUTCOMES`, `ENTRY_REQUIRED_KEYS`, `DATE_RE`.
   - Keep the message text byte-identical for the pinned substrings.
   - Add a `--check-entry FILE` flag to the module's CLI: it reads one JSON line and exits 2 on findings, which keeps the CLI case.
   - The module still does not depend on `review_gate` (the historical checker used it only in `main` / `load_state`).
3. **Real-log guard.** The guard over the committed log tolerates exactly the rows the audit itself excludes:
   - the flat or non-list rows (`non-list=45`);
   - the empty-core row (`empty=1`, PR 1788, `[[], []]`).

   The guard is scoped to the audit's own population, the multi-round merged rows that `rotation_sample` reads. It asserts that the tolerated counts equal `rotation_sample`'s exclusion counts, and that every other row in that population passes the shape rule. Eight shape-noncompliant rows lie outside the population (rounds=1 PRs 1239, 1241, 1262, 1303, 1360, 1374, 1376, and descoped 1371); the audit never reads them, so the guard does not either.
4. The suite imports `scripts.governance.loop_log_audit` as a normal module. The `pytest.skip(allow_module_level=True)` fallback is removed.
   - **Routing.** A path-exact selector rule for `scripts/governance/loop_log_audit.py`, and one for `docs/review-loop-log.jsonl`, both target `tests/test_loop_log_audit_attribution.py`.
   - The log's rule also rides `SELECTOR_META_GUARD_TEST`, following the `.review-gate-issues.json` precedent at `scripts/select_ci_tests.py:961-971`.
   - The log also needs an exact `ci.yml` `backend` literal, because `docs/**` is excluded (precedent: `docs/runbooks/tier-node27-timeseries-storage.md`, and the `.review-gate-issues.json` literal).
5. `--log` defaults to the repo's `docs/review-loop-log.jsonl`, so the documented bare invocation works. That is a CLI-ergonomics edit only.
6. Run the restored audit once against the frozen log. Append an ADR 0003 Revisit with:
   - the DECIDABLE state and the recomputed core/rotated/final-review counts;
   - that the sample is frozen at 690 lines because the append step left the workflow on 2026-09-22;
   - that the installed workflow no longer rotates lenses. Its re-review seat is the fixed `correctness+test-evidence`, so the keep/cut question has no live subject.

   The keep/cut decision itself stays a maintainer call. The ADR records the facts and marks the question closed-by-workflow-change.

### D2 (#2261): tracked `scripts/review_gate.py`, memory-only

The file lives at the path the user named. It is a small stdlib CLI whose scope is `.review-gate-issues.json` and nothing else. Round counting stays in `fix_gate.py`.

- `load_history(root)`:
  - A missing file yields `{"issues": {}}`.
  - Any top-level key other than `issues` goes through the structure guard:
    - if `issues` lacks that key, or holds an identical record, fold the bare key in and warn on stderr;
    - otherwise exit 1 and print both records (for example `1660`'s `gateEntries` 1 vs 0).
  - Every entry must carry `ceilingPrs: list[int]`, `gateEntries: int` and `closed: list`, and every `closed[].outcome` must be in `OUTCOMES`; a violation exits 1.
- `OUTCOMES = ("merged", "superseded-by-split", "abandoned", "descoped")` is the single authority. `tests/test_review_gate_issue_memory.py` imports it rather than copying it.
- `record --issue N [--issue M ...] --pr P --rounds R --outcome {OUTCOMES} [--ceiling]`:
  - `--outcome` is required and choice-restricted, so no default can escape the vocabulary.
  - Per issue it appends `{"pr", "outcome", "rounds"}` to `closed`.
  - It **never touches `gateEntries`**. Historically that field was incremented only on a gate-lock retro registration (`record-retro`), never on close, and 292 of 323 committed entries have `gateEntries != len(closed)`. New entries start at 0.
  - `--ceiling` appends P to `ceilingPrs` if absent, which dedupes. It now means that `fix_gate.py` locked on that PR (after 2 not-clean fix passes). The 14 historical `ceilingPrs` entries meant the old round ceiling; both are "the loop hit its hard stop", which is all `check` reads.
  - It is idempotent per (issue, pr): a second identical `record` leaves the whole entry byte-identical, and a differing one replaces that pair's closed entry. This makes a re-run after a merge conflict safe.
  - It writes with the historical serializer (`json.dumps(indent=2, ensure_ascii=False) + "\n"`) so the diffs stay minimal.
- `check --issue N [--pr P]`:
  - exits 2 and prints the prior ceiling PRs when the issue already has a ceiling PR (other than P, when `--pr` is given);
  - otherwise exits 0.
  - It runs next to `fix_gate.py open --pr N`, when the PR exists but has no review round yet, so `--pr` is available. Before a PR exists it runs without `--pr`.
  - This is the cross-PR escalation that `fix_gate.py` does not do.
- **Caller.** `instructions/agents/shared.md`, in the PR-conventions section, gets one bullet:
  - run `check` next to `fix_gate.py open`;
  - run `record` on **every** close of the issue's PR, with the matching `--outcome`: `merged` in the post-merge archive commit (the same commit that already carries the openspec archive, so no extra CI push); `superseded-by-split` / `abandoned` / `descoped` committed when `fix_gate.py close` runs. Add `--ceiling` whenever fix_gate locked, so a successor PR's `check` sees it (review round 1).
  - `record` validates the mutated memory before writing, and rejects non-positive `--issue` / `--pr`, so the writer never persists what the loader refuses (review round 1).

  Then regenerate `CLAUDE.md` and `AGENTS.md` exactly as the byte-exact oracle `tests/test_node22_entrypoint_invariant.py::test_generated_roots_byte_exact` composes them:
  - `AGENTS.md` = header + `shared.md` + `"\n"` + `codex.md`;
  - `CLAUDE.md` = header + `shared.md` + `claude.md` (empty).

  `tests/test_python_environment_truth.py` also pins phrases in all three files.
- **First real use.** Record J1 in this PR: issues 2316, 2317, 2323, 2390 and 2498, PR 2606, rounds 2, outcome merged, no ceiling. That is the only post-freeze batch whose gate facts were persisted (fix_gate rounds recorded, PR comment "Agent Review"). J2 records itself in its archive PR.
- **Routing.** `tests/test_review_gate_cli.py` joins `REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS`, so a `.review-gate-issues.json` diff runs the CLI round-trip too. The exact pin at `tests/test_select_ci_tests.py:3174` is updated. `scripts/review_gate.py` routes to both memory suites. For script paths the new pins are membership pins: they already carry the unknown-backend fallback. For data paths they are exact.
- **#2261 disposition.** The PR closes #2261 with these accepted deviations:
  - AC1 and AC3 land in the tracked CLI instead of the retired untracked writer.
  - AC4 (the verdict hook) is not landed, because its only enforcement point is upstream `fix_gate.py record-round`.
  - AC5 (`phase-flow.md:527` wording) is untracked and upstream-owned. Its repo-side equivalent is the instruction bullet.
- **Rejected alternatives.**
  - Resurrecting the 496-line old `review_gate.py`: it duplicates `fix_gate.py`'s round gate and brings back retired retro machinery.
  - Deleting the memory: that silently drops cross-PR escalation, which is the only thing the file is for.

### D3 (#2533): restate the stale requirements from measured truth

The truth sources are `select_tests` probes and tracked-module AST derivation, never the old spec prose. Every requirement below is restated in full as a MODIFIED block (the validator refuses to drop scenarios), with only the named suites and counts changed.

| Spec : requirement | Stale name | Replacement source |
|---|---|---|
| ci-contract-baseline "Entropy hard gate MUST be green on master…" | `test_entropy_audit_script.py` | hard-gate node in `tests/test_entropy_audit_report_contract.py` |
| ci-contract-baseline "Shell wrapper changes MUST gate their guard suites" | `test_scheduler_file_provider_refresh.py` | probe of `scripts/scheduler_file_provider_refresh_once.sh` → `tests/test_scheduler_refresh_deployment_contract.py` |
| ci-contract-baseline "Calibration declaration changes MUST execute…" | `test_publish_scheduler_file_registry.py` | probe → `tests/test_publish_registry_calibration_overrides.py` |
| ci-contract-baseline "display and scheduler unit files…" | `test_scheduler_file_provider_refresh.py` | probes of the `.service` / `.timer` units → `tests/test_scheduler_refresh_deployment_contract.py` |
| ci-contract-baseline refresh-env-template and copyback-mutex (J1 text) | historical mentions | reworded without naming the deleted files |
| orchestrator-structural-burndown "Basins registry-import tests remain complete…" | `test_publish_scheduler_file_registry.py`, "eight" | AST-derived importer set of `tests/basins_registry_import_helpers.py` (issue measured 11 collectible) |
| orchestrator-structural-burndown "Entropy audit enforcement and its corpus split…" | `test_entropy_audit_script.py` | the 15 `tests/test_entropy_audit_*.py` partitions (`ENTROPY_AUDIT_TESTS`) |
| real-integration-test-matrix "Barrier concurrency harnesses…" | `test_scheduler_file_provider_refresh.py` | the partitions that now hold the thread-lock serialization and receipt-retention tests |

The implementer measures the replacement sets without editing specs, and the orchestrator writes the blocks. Closure is `git grep -F` of the five deleted paths over `openspec/specs/` returning nothing after archive; before archive every hit lies inside a requirement restated by a MODIFIED block.

### D4 (#2044): ruling — make it recorded and alertable (plan only; not implemented here)

**Measured facts** (master push runs created after 2026-09-09; the cap has been 60 min since `bae9cae1b` on 2026-09-04):
- 121 runs reached a terminal state: 112 success, 8 failure, 1 cancelled.
- Success durations: median 43.2 min, p95 51.1 min (85% of the cap), max 57.5 min (96%).
- One wall-kill at 60.4 min, run `35759146799` on sha `6a13a557e`, 2026-09-22.
- No channel noticed it. Later docs-only pushes skip the job and show the run as green.

**Why not "accept the risk".** The kill already recurred at the raised cap, and the success median has not come down since the raise (42.8 min in the #2044 window, 43.2 min now).

**Ruling.** The unverified-master state must be **recorded and alert**. The plan:

- **A new workflow triggered by `workflow_run`** (workflow `CI`, `types: [completed]`, `branches: [master]`), with `permissions: actions: read`.
  - Triggering on completion means it sees the terminal conclusion of the run it watches, not an `in_progress` job on the same push, and it fires even if no later push arrives.
  - It reads that run's `Unit Tests (full)` job. When the conclusion is `cancelled` (with the `exceeded the maximum execution time` annotation distinguishing a wall-kill) or `failure`, it fails with an annotation naming the SHA and the run.
  - `skipped` (no backend change) passes.
  - This covers the 8 red runs as well as the cancel.
- **Margin rule.** Once that workflow lands, it also computes the rolling p95 of the last 20 successful full runs. It annotates a warning when p95 exceeds 80% of `timeout-minutes`, which is 48 min at the 60 cap. From then on the next backend merge must ship a split, a speed-up or a raise.
  - The rule takes effect when the workflow lands, not retroactively. It does not bind J2 or other in-flight PRs.
  - Today's 51.1 min p95 means the rule will fire on its first run. That is intended: it is the signal the issue asked for.

Implementation is follow-up work on #2044, and the issue stays open as its tracker. This PR only states the ruling.

## Risks / Trade-offs

- The CLI only helps if it is run. That relies on the instruction bullet, which is how every other repo-local procedure is enforced, and on the J1 record proving the path works.
- The restored audit is a 421-line tool over a frozen log. It is kept because ADR 0003 explicitly waits on it, and because the suite pins the #2036 fixes. It becomes live again if a loop-log writer returns.
- The K batch rebases on the selector edits.
