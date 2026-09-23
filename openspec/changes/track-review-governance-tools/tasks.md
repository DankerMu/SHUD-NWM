## 1. Loop-log audit (#2477, D1)

- [x] 1.1 Restore `scripts/governance/loop_log_audit.py` from `002ba4b59^`, with semantics unchanged. It must be ruff-clean, and docstring and stdout must no longer claim the skill runs it or name `evidence_check`.
- [x] 1.2 Port `check_catch` + `check_loop_log_entry` as the pure `loop_log_entry_errors(entry)`, with pinned messages byte-identical, plus a `--check-entry FILE` CLI flag that exits 2 on findings.
- [x] 1.3 Retarget `tests/test_loop_log_audit_attribution.py`:
  - plain import, with the module-level skip removed;
  - the 11 evidence cases now use `loop_log_entry_errors` or `--check-entry`, with every assertion preserved;
  - a new real-log guard, scoped to the `rotation_sample` population, that tolerates exactly the audit's exclusions (non-list=45, empty=1) and asserts the counts equal `rotation_sample`'s.
- [x] 1.4 Run the audit over `docs/review-loop-log.jsonl` and capture its stdout and exit code for the ADR.

## 2. Review-gate memory CLI (#2261, D2)

- [x] 2.1 `scripts/review_gate.py`, containing:
  - `OUTCOMES`;
  - `load_history` with the fold/conflict structure guard and entry validation;
  - `record` (required choice-restricted `--outcome`, repeatable `--issue`, deduped `--ceiling`, never touches `gateEntries`, idempotent per issue and PR with the whole entry byte-identical on re-run);
  - `check` (`--pr` optional; exit 2 on a prior ceiling on another PR).
- [x] 2.2 `tests/test_review_gate_cli.py` covers:
  - fold of an unambiguous bare key;
  - conflict exit using the real 1660 case (`gateEntries` 1 vs 0);
  - out-of-vocabulary outcome rejection by argparse;
  - `record` idempotency;
  - `check` exit 0 and exit 2, with and without `--pr`;
  - `gateEntries` unchanged by `record`;
  - a missing file;
  - byte-stable round-trip of the committed file (load and save leaves the bytes identical).
- [x] 2.3 `tests/test_review_gate_issue_memory.py` imports `OUTCOMES` from `scripts.review_gate`, and its docstring is updated.
- [x] 2.4 Record J1 via the CLI: `--issue 2316 --issue 2317 --issue 2323 --issue 2390 --issue 2498 --pr 2606 --rounds 2 --outcome merged`.
- [x] 2.5 Add the `instructions/agents/shared.md` bullet (`check` next to `fix_gate.py open`; `record` in the archive commit). Regenerate `CLAUDE.md` and `AGENTS.md` per `test_generated_roots_byte_exact` (`AGENTS.md` = header + shared + `\n` + codex).

## 3. Routing

- [x] 3.1 Selector path-exact rules:
  - `scripts/governance/loop_log_audit.py` and `docs/review-loop-log.jsonl` → `tests/test_loop_log_audit_attribution.py`;
  - `scripts/review_gate.py` → `tests/test_review_gate_cli.py` and `tests/test_review_gate_issue_memory.py`;
  - `tests/test_review_gate_cli.py` added to `REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS` (and the `:3174` exact pin updated);
  - the log rule also rides `SELECTOR_META_GUARD_TEST`.

  Pins are membership pins for script paths (fallback included) and exact pins for data paths.
- [x] 3.2 Add the `ci.yml` `backend` exact literal `docs/review-loop-log.jsonl` with a comment. Satisfy any meta-suite pin of the filter literal set.

## 4. Specs (#2533, D3)

- [x] 4.1 The implementer reports the measured replacement sets (read-only).
- [x] 4.2 Orchestrator writes the MODIFIED blocks. `git grep -F` of the five deleted paths over `openspec/specs/` must return nothing.

## 5. Evidence Floor

- [x] 5.1 `uv run pytest -q -rs tests/test_loop_log_audit_attribution.py` must show 0 skipped. Also run `tests/test_review_gate_cli.py`, `tests/test_review_gate_issue_memory.py`, `tests/test_select_ci_tests.py`, `tests/test_node22_entrypoint_invariant.py` and `tests/test_python_environment_truth.py`.
- [x] 5.2 node-27 oracle at the final head.
- [x] 5.3 `uv run ruff check .`; `openspec validate track-review-governance-tools --strict --no-interactive`.
- [x] 5.4 ADR 0003 Revisit appended with the audit output from 1.4.
- [x] 5.5 `git ls-files .agents/skills .claude/skills` is still empty.
- [ ] 5.6 CI green on the final push.

## Deviations (recorded)

- The user named `docs/review-loop-log.json`; the file is `.jsonl`. It is read-only input here: frozen, with no current writer.
- `scripts/review_gate.py` is a new memory-only tool, not an edit of an existing file. No tracked `review_gate.py` existed, and the old one is retired upstream.
- #2261 is closed with accepted deviations. AC1/AC3 land in the tracked CLI. AC4 (verdict hook) and AC5 (`phase-flow.md` wording) are upstream-owned and untracked. The repo-side instruction bullet stands in for AC5.
- #2044 gets a ruling only. The issue stays open as the tracker for the D4 plan.
- F/G1/G2/H are not back-filled into the memory.
