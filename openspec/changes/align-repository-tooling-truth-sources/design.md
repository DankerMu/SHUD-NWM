## Context

Issues #1571, #1634, and #1619 are independent symptoms of one tooling invariant: a repository tool must derive decisions from the identity domain it is actually acting on. CI runs Python 3.11 while an unpinned local `uv` selects 3.14; the hook receives a worktree `cwd` but loads config through `CLAUDE_PROJECT_DIR`; and the replay audit binds reasons to shifting line numbers. The batch is one PR because all three are local repository-governance fixes with no business-runtime dependency, while each keeps its own test seam.

Fixture level: expanded. Repair intensity: high because the hook is a shared commit entrypoint and the interpreter pin changes defaults on local/node-22 environments.

## Goals / Non-Goals

**Goals:**

- Make `uv` default to the merge-gate Python major/minor while retaining the declared `>=3.11` support range and explicit 3.14 verification.
- Make hook config and staged-file discovery use the same Git worktree identity.
- Make every pre-commit index reason auditable through stable owning function names and a completeness test.
- Preserve current production, CI dependency resolution, merge-parent filtering, and replay classification behavior.

**Non-Goals:**

- Do not migrate CI from pip to uv, add vermin, or narrow `requires-python`.
- Do not change Slurm scheduling, SHUD runtime, database/display behavior, replay allowlists, exit codes, or receipts.
- Do not keep hand-maintained source line numbers.

## Decisions

1. Track `.python-version` containing `3.11`. This shares the CI major/minor without pretending a patch release is portable. Multi-version checks use `uv run --python <version> ...`. Node-22 will intentionally converge from 3.12 to 3.11 on its next controlled sync; no Slurm job is needed because scheduling/runtime code is unchanged.
2. Treat the tool-call `cwd` as the operation root, then resolve its Git top level before reading `.large-file-guard.json`. This binds config and `git -C` inspection to one worktree. `CLAUDE_PROJECT_DIR` remains only the fallback when `cwd` is absent, preserving non-worktree callers. Diagnostics print the effective config path.
3. Replace the replay comment table with a reason-to-one-or-more-owning-functions mapping, preserving every function-level raise point represented by the old citations. Function names survive unrelated insertions and remain grep/AST-auditable. Focused tests compare mapping keys with `_PRE_COMMIT_INDEX_REASONS`, pin the known multi-owner rows, and confirm every mapped function contains the reason literal; the allowlist itself remains unchanged.
4. Keep implementation serial. The surfaces are independent, but one implementer is cheaper than parallel write-worktree integration and avoids generated instruction conflicts.

## Risk Packs Considered

- Public API / CLI / script entry: selected — hook and replay script are shared tooling entrypoints.
- Config / project setup: selected — `.python-version` and worktree-local guard config are the main contracts.
- File IO / path safety / overwrite: selected — the hook must bind config reads to the active Git worktree and preserve merge filtering.
- Schema / columns / units / field names: not selected — no external payload/schema changes.
- Auth / permissions / secrets: not selected — no credential or authorization surface.
- Concurrency / shared state / ordering: not selected — no concurrent mutation or state transition changes.
- Resource limits / large input / discovery: selected — the line-count guard's threshold/exclusion behavior must remain bounded and unchanged.
- Legacy compatibility / examples: selected — main-checkout callers, merge commits, and existing replay classification remain compatible.
- Error handling / rollback / partial outputs: selected — a blocked commit must name the effective config path; no output mutation occurs.
- Release / packaging / dependency compatibility: selected — default Python aligns to 3.11 while `>=3.11` support and CI pip resolution remain unchanged.
- Documentation / migration notes: selected — generated instructions document default/multi-version use and node-22 convergence.
- All NHMS domain packs: not selected — no geospatial, hydro-met, SHUD numerical, PostGIS/Timescale, Slurm lifecycle, provider snapshot, run manifest, or display identity behavior changes.

## Invariant Matrix

- Governing invariant: each repository tool SHALL derive its decision from the same stable identity domain as the operation it governs.
- Source-of-truth identity/contract: `.python-version=3.11`; Git top-level for tool-call `cwd`; replay reason literal plus owning function name.
- Producers: uv interpreter selection; hook input JSON; state-manager reason raises.
- Validators/preflight: uv pin assertion; large-file hook; replay mapping completeness test.
- Storage/cache/query: tracked config and source files only; no DB/cache.
- Public routes/entrypoints: `uv run`; `large-file-guard.sh`; replay script constants.
- Frontend/downstream consumers: developers/agents, CI parity, replay operators.
- Failure paths/rollback/stale state: unsupported 3.13-only API fails on 3.11; wrong-root exclusion must no longer block; unmapped/moved reason fails focused tests.
- Evidence/audit/readiness: issue verification commands, hook shell suite, replay pytest, ruff, OpenSpec validation, node-22 interpreter receipt.
- Regression rows:
  - tracked pin + `uv run python` -> Python 3.11.x; explicit `--python 3.14` remains available.
  - nested worktree `cwd` + main-checkout `CLAUDE_PROJECT_DIR` -> config resolves from the worktree Git top level for both exclusion and block diagnostics.
  - unchanged main-checkout/merge fixture -> current block/pass and merge-parent filtering remain unchanged.
  - every `_PRE_COMMIT_INDEX_REASONS` member -> stable mapped function that contains that reason literal.

## Risks / Trade-offs

- [Existing local `.venv` uses 3.14] → `uv sync` recreates it once under 3.11; run the full required regression afterward.
- [node-22 currently uses 3.12] → document and verify a controlled 3.11 sync without launching Slurm work.
- [tool-call `cwd` might be nested] → resolve `git rev-parse --show-toplevel` rather than assuming `cwd` itself is the root; fail-safe fallback preserves legacy callers.
- [function names can also change] → completeness and literal-ownership tests fail on rename/move, forcing an intentional update without unrelated line churn.

## Migration Plan

1. Commit pin, hook behavior/tests, stable replay mapping/tests, and generated instructions together.
2. Verify locally on Python 3.11 and explicitly confirm Python 3.14 remains selectable.
3. After push, fast-forward node-22 only if its worktree is clean; run `uv sync --all-extras --dev` and record `uv run python -V`. Do not trigger a compute job.
4. Rollback is a normal revert of this PR; no persisted business data changes.

## Open Questions

None. The issue's node-22 choice is resolved in favor of convergence to Python 3.11, matching CI and node-27.