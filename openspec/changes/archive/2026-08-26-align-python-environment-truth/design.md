## Context

Issue #1571 is one environment-identity invariant across repository defaults,
automatic/operator entrypoints, and validation oracles. Repair intensity is
high because node-22 live processes share the active `.venv`; an implicit uv
rebuild has already produced a partial environment removal. The parent PR #1822
hit the five-round ceiling and was superseded by split. This child opens
post-ceiling escalated and retains the complete #1571 invariant rather than
splitting the Python pin from its safety/oracle replacement.

## Goals / Non-Goals

**Goals:**

- Make default `uv` select Python 3.11 while preserving supported explicit versions.
- Prevent pre-maintenance node-22 commands from creating, updating, or replacing the shared active environment.
- Use detached diagnostic checkouts and the current node-27 test oracle.
- Preserve failed guard/pytest status through the operator's receipt pipeline.
- Separate historical topology evidence from current operational authority.

**Non-Goals:**

- Do not migrate CI from pip to uv, add vermin, or narrow `requires-python`.
- Do not stop services, deploy installed units, cut over the active node-22 venv, run Slurm/SHUD, or touch live DB/display state.
- Do not change scheduler/scientific behavior.

## Decisions

1. Track `.python-version` containing `3.11`; explicit cross-version commands use `uv run --python <version>`.
2. Keep the active node-22 `.venv` on 3.12.7 until #1831's approved maintenance
   window. Required operations use an exact interpreter or checked-in wrapper
   and fail closed if it is missing. `uv run --no-sync` is observation-only and
   not pin evidence; `--active`, bare `uv run`, and system Python are unsafe
   substitutes.
3. Reject the canonical active physical root in all four QHH diagnostic entrypoints before state, mkdir, Python, or uv actions. Backend-smoke direct Python binds to the detached root's `.venv`.
4. Make node-27 the e2e/grib oracle: interactive ssh, remote `set -euo pipefail`, existing 3.11 assertion, then `uv run --no-sync pytest | tee`.
5. Treat shell status preservation as a bounded lane-state contract. Reconstruct
   logical commands, tokenize unquoted shell operators independently of
   whitespace, split command segments, reject every `||`, and scan every `set`
   segment for disable flags. Pure enables and quoted/escaped/comment literals
   remain green.
6. Use complete whole-document status markers for historical QHH material. Incomplete markers and canonical/dynamically declared current authority remain visible to topology checks.
7. The entropy implementation/test pre-existed above the 1000-line hook threshold. Add only their exact grandfather paths; #1823/#1842 own decomposition and eventual exclusion removal.

## Invariant Matrix

- Governing invariant: every Python/environment decision SHALL use the stable identity domain of the operation it governs.
- Producers: `.python-version`, active checkout `.venv`, detached diagnostic root, node-27 environment, status markers.
- Validators: static entrypoint inventories, shell lane classifier, entropy authority classifier, exact generated projections.
- Entrypoints: node-22 systemd/operator commands, QHH continuous/cycle/sbatch/backend-smoke, Docker runbook fences, node-27 e2e/grib lane.
- Consumers: developers, operators, automatic services, pytest opt-in guidance, production-topology governance.
- Failure/stale paths: implicit uv recreation, missing interpreter, active-root symlink alias, guard failure, pytest through tee, incomplete/forged marker, current-authority self-exemption.
- Evidence: focused static/mutation suites, entropy tests/hard gate, full pytest/Ruff/OpenSpec, bounded node-22 receipt.

## Regression Rows

- Default `uv run python` reports 3.11; explicit 3.14 remains selectable; 3.13-only API fails under default.
- Active node-22 required command before maintenance uses exact Python/wrapper and never environment-updating uv.
- QHH canonical active root or physical alias fails before action; detached root uses its own exact interpreter.
- Node-27 failed 3.11 guard stops pytest; failed pytest remains non-zero through `tee`.
- Spaced, token-start, glued, and mixed `set -euo ... +disable` status-swallowing forms turn the same static seam red.
- Complete historical baseline suppresses preserved topology; incomplete/current-authority marker does not.

## Risks / Trade-offs

- The tracked pin changes local environment resolution immediately; node-22 active convergence is deliberately deferred.
- A shell classifier is bounded, not a full Bash parser. It owns the checked-in node-27 fence and its concrete operator/state mutation matrix.
- Exact grandfather exclusions allow a confirmed classifier fix to ship without an unrelated split; they do not resolve structural debt.

## Migration Plan

1. Commit tracked pin, instructions, exact-interpreter entrypoints, detached boundaries, node-27 route, topology classifier, and regression inventories together.
2. Verify Python 3.11/default behavior locally and the implementation commit in a disposable node-22 worktree only.
3. Keep active node-22 3.12.7 until #1831 stops owning services, synchronizes, asserts 3.11, and restarts with receipts.
4. Roll back by reverting this child; no persisted business data migration exists.

## Open Questions

None. The active cutover is explicitly deferred to #1831.
