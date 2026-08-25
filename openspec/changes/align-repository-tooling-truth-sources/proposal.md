## Why

Repository tooling currently consults facts from the wrong identity domain: local `uv` selects a newer interpreter than CI; after pinning, node-22's tracked automatic/operator entrypoints can still update the deferred shared environment; the production-topology audit does not consume the governed whole-document status marker used to separate historical bring-up evidence from current instructions; the large-file guard reads configuration from the main checkout while inspecting a worktree; and the replay reason audit names mutable source line numbers. These mismatches have already caused one escaped Python-version incompatibility, one interrupted active-environment replacement, one historical runbook to be classified as current topology, one worktree hook deadlock, and a stale audit index.

## What Changes

- Track `.python-version` with `3.11`, keep `requires-python >=3.11`, document explicit multi-version runs, and align node-22's tracked default with CI/node-27 while the active shared `.venv` stays on 3.12.7 until an operator-approved maintenance window performs the cutover. Before that window, active node-22 automatic and operator entrypoints use a checked-in wrapper or the exact active `.venv` interpreter instead of environment-updating `uv`; e2e/grib validation follows the current node-27 oracle and uses a fail-closed Python 3.11 guard plus `uv run --no-sync` against its already-active environment rather than the deferred node-22 checkout. Historical bring-up material uses the governed whole-document authority marker, and production-topology audit code consumes that marker without allowing current authority documents or incomplete markers to hide drift.
- Make the large-file guard resolve configuration from the Git worktree named by the tool-call `cwd`, with an actionable diagnostic naming the effective config path.
- Replace the replay reason index's mutable line-number citations with complete, test-enforced reason-to-function ownership.
- Preserve scheduler refusal classifications, CI dependency installation, merge-parent filtering, and production behavior.

## Capabilities

### New Capabilities

- `repository-tooling-truth-sources`: Repository-local tooling uses the current worktree, the CI-compatible default Python, and stable source identifiers as its facts.

### Modified Capabilities

None.

## Impact

Affected surfaces are `.python-version`, `instructions/agents/` and generated root instructions, node-22 systemd/operator entrypoints and their current/historical runbooks, the production-topology archive-marker classifier in `scripts/governance/audit_repo_entropy.py`, `.claude/hooks/large-file-guard/`, `scripts/scheduler_state_index_copyback_replay.py`, and focused/static tests. The Python default changes local `uv` behavior immediately; node-22's tracked default changes now while its active shared environment changes only during the operator-approved maintenance-window cutover. Node-27 is already on Python 3.11. No application API, database, display, Slurm scheduling semantics, replay exit-code, or receipt contract changes.