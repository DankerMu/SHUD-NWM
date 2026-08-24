## 1. Fixture and invariant setup

- [x] 1.1 Record expanded/high risk triage, all core/domain risk-pack decisions, must-preserve behavior, non-goals, seams, and the invariant matrix.
- [x] 1.2 Reproduce the unpinned Python 3.14 default, worktree wrong-config block, and stale replay line citations before implementation.

## 2. Default Python truth source (#1571)

- [x] 2.1 Track `.python-version` with `3.11` without changing `requires-python >=3.11` or CI's pip dependency resolution.
- [x] 2.2 Update `instructions/agents/shared.md` and regenerate `CLAUDE.md`/`AGENTS.md` to document default 3.11, explicit `uv run --python <ver>`, and the operator-approved maintenance-window gate for node-22's active 3.12.7 -> 3.11 cutover.
- [x] 2.3 Verify default Python 3.11, the expected 3.13-only `Path.rglob` TypeError, explicit Python 3.14 selection, and the full Python regression suite.

## 3. Worktree-local guard truth source (#1634)

- [x] 3.1 Resolve the active Git top level from hook input `cwd`, use it for both config and Git reads, and name the effective config path in block diagnostics.
- [x] 3.2 Add linked-worktree regressions where `CLAUDE_PROJECT_DIR` points at main and tool-call `cwd` is nested: prove exclusion and block diagnostics both use `{worktree-top-level}/.large-file-guard.json`; preserve all existing plain/merge cases.

## 4. Stable replay audit ownership (#1619)

- [x] 4.1 Replace mutable line-number citations with complete reason-to-one-or-more-function ownership metadata, preserving all old indexed raise-point owners and both copyback lock reasons, without changing `_PRE_COMMIT_INDEX_REASONS`.
- [x] 4.2 Add regressions asserting exact key-set equality, the seven known multi-owner rows, and that every named owner function contains its reason literal.
- [x] 4.3 Run `uv run pytest -q tests/test_scheduler_state_index_copyback_replay.py` and confirm replay classification behavior remains unchanged.

## 5. Evidence Floor

- [x] 5.1 `uv sync --all-extras --dev && uv run python -V` -> Python 3.11.x; `uv run python -c "import sys; assert sys.version_info[:2] == (3, 11)"` -> exit 0.
- [x] 5.2 `uv run python -c "from pathlib import Path; list(Path('.').rglob('*', recurse_symlinks=True))"` -> expected `TypeError`; `uv run --python 3.14 python -V` -> Python 3.14.x.
- [x] 5.3 `bash .claude/hooks/large-file-guard/test-large-file-guard.sh` -> all plain, merge, diagnostic, and linked-worktree nested-`cwd` cases pass, including exact top-level config-path assertions.
- [x] 5.4 `uv run pytest -q tests/test_scheduler_state_index_copyback_replay.py` -> pass; exact allowlist and stable owner assertions execute.
- [x] 5.5 `uv run pytest -q` and `uv run ruff check .` -> pass on Python 3.11; `openspec validate align-repository-tooling-truth-sources --strict --no-interactive` -> valid.
- [x] 5.6 On node-22, verify the exact implementation commit in a disposable clean worktree -> Python 3.11.15, version assertion passes, zero Slurm commands, and worktree cleanup succeeds; explicitly defer the active shared `.venv` cutover from 3.12.7 to the next operator-approved service maintenance window because live processes map it (`evidence/node22-python-pin-receipt.md`).

## 6. Explicit exclusions

- [x] 6.1 No CI pip-to-uv migration, vermin integration, support-range narrowing, scheduler/Slurm behavior, replay allowlist/exit-code/receipt change, DB/display receipt, or unrelated source cleanup.