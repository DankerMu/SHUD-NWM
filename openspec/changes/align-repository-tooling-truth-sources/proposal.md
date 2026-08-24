## Why

Repository tooling currently consults facts from the wrong identity domain: local `uv` selects a newer interpreter than CI, the large-file guard reads configuration from the main checkout while inspecting a worktree, and the replay reason audit names mutable source line numbers. These mismatches have already caused one escaped Python-version incompatibility, one worktree hook deadlock, and a stale audit index.

## What Changes

- Track `.python-version` with `3.11`, keep `requires-python >=3.11`, document explicit multi-version runs, and explicitly align node-22's default `uv` interpreter with CI/node-27.
- Make the large-file guard resolve configuration from the Git worktree named by the tool-call `cwd`, with an actionable diagnostic naming the effective config path.
- Replace the replay reason index's mutable line-number citations with complete, test-enforced reason-to-function ownership.
- Preserve scheduler refusal classifications, CI dependency installation, merge-parent filtering, and production behavior.

## Capabilities

### New Capabilities

- `repository-tooling-truth-sources`: Repository-local tooling uses the current worktree, the CI-compatible default Python, and stable source identifiers as its facts.

### Modified Capabilities

None.

## Impact

Affected surfaces are `.python-version`, `instructions/agents/` and generated root instructions, `.claude/hooks/large-file-guard/`, `scripts/scheduler_state_index_copyback_replay.py`, and focused tests. The Python default changes local and node-22 `uv` behavior; node-27 is already on Python 3.11. No application API, database, display, Slurm scheduling, replay exit-code, or receipt contract changes.