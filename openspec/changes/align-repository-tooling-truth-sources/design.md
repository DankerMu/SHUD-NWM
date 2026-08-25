## Context

Issues #1571, #1634, and #1619 are independent symptoms of one tooling invariant: a repository tool must derive decisions from the identity domain it is actually acting on. CI runs Python 3.11 while an unpinned local `uv` selects 3.14; the hook receives a worktree `cwd` but loads config through `CLAUDE_PROJECT_DIR`; and the replay audit binds reasons to shifting line numbers. The batch is one PR because all three enforce repository-tooling identity without changing business API, data, scheduler, or scientific semantics; the Python pin also requires tracked node-22 runtime entrypoints to preserve the deferred active environment, and each surface keeps its own test seam.

Fixture level: expanded. Repair intensity: high because the hook is a shared commit entrypoint and the interpreter pin changes defaults on local/node-22 environments.

## Goals / Non-Goals

**Goals:**

- Make `uv` default to the merge-gate Python major/minor while retaining the declared `>=3.11` support range and explicit 3.14 verification.
- Preserve node-22's deferred active environment before maintenance across every tracked automatic/operator entrypoint: exact active interpreter or checked-in wrapper for required operations, while routing e2e/grib validation to the current node-27 Python 3.11 oracle.
- Make hook config and staged-file discovery use the same Git worktree identity.
- Make every pre-commit index reason auditable through stable owning function names and a completeness test.
- Preserve current production, CI dependency resolution, merge-parent filtering, and replay classification behavior.

**Non-Goals:**

- Do not migrate CI from pip to uv, add vermin, or narrow `requires-python`.
- Do not change Slurm scheduling, SHUD runtime, database/display behavior, replay allowlists, exit codes, or receipts.
- Do not keep hand-maintained source line numbers.

## Decisions

1. Track `.python-version` containing `3.11`. This shares the CI major/minor without pretending a patch release is portable. Multi-version checks use `uv run --python <version> ...`. Node-22's active checkout intentionally remains on 3.12.7 until the next operator-approved service maintenance window because three live processes currently map the shared `.venv`; the exact implementation commit is independently accepted on node-22 with Python 3.11.15 in a disposable worktree. No Slurm job is needed because scheduling/runtime code is unchanged.
2. Treat the tool-call `cwd` as the operation root, then resolve its Git top level before reading `.large-file-guard.json`. This binds config and `git -C` inspection to one worktree. `CLAUDE_PROJECT_DIR` remains only the fallback when `cwd` is absent, preserving non-worktree callers. Diagnostics print the effective config path.
3. Replace the replay comment table with a reason-to-one-or-more-owning-functions mapping, preserving every function-level raise point represented by the old citations. Function names survive unrelated insertions and remain grep/AST-auditable. Focused tests compare mapping keys with `_PRE_COMMIT_INDEX_REASONS`, pin the known multi-owner rows, and confirm every mapped function contains the reason literal; the allowlist itself remains unchanged.
4. Keep implementation serial. The surfaces are independent, but one implementer is cheaper than parallel write-worktree integration and avoids generated instruction conflicts.
5. Treat the pre-maintenance node-22 command rule as an entrypoint invariant, not root-instruction advice. A tracked automatic unit or current runbook that invokes bare `uv` from `/scratch/frd_muziyao/NWM` overrides prose and can recreate the shared environment. Required active-checkout operations therefore invoke a checked-in wrapper or the exact active `.venv` interpreter and fail if it is missing; environment-coupled e2e/grib validation follows the current node-27 oracle and uses `uv run --no-sync` only after asserting its existing interpreter is Python 3.11. The disposable node-22 worktree remains bounded pin-acceptance evidence only. Historical/diagnostic runbooks must carry standard authority markers or explicitly require that isolation.
6. Reuse the central complete whole-document marker classifier for production-topology authority. `docs/governance/DOC_STATUS.md` makes `status`, `current_authority`, `status_since`, `archive_scope`, and `retained_for` the common fields; `historical baseline` does not require `superseded_by`, while `superseded` and `archived` retain the existing stricter replacement field. Incomplete markers fail visible. Named current authority paths are checked before marker classification and cannot self-exempt by adding non-current front matter.

## Risk Packs Considered

- Public API / CLI / script entry: selected — hook and replay script are shared tooling entrypoints.
- Config / project setup: selected — `.python-version` and worktree-local guard config are the main contracts.
- File IO / path safety / overwrite: selected — the hook must bind config reads to the active Git worktree and preserve merge filtering.
- Schema / columns / units / field names: not selected — no external payload/schema changes.
- Auth / permissions / secrets: not selected — no credential or authorization surface.
- Concurrency / shared state / ordering: selected — automatic node-22 services and live processes share one active `.venv`; the pre-maintenance command path must not race an implicit environment replacement.
- Resource limits / large input / discovery: selected — the line-count guard's threshold/exclusion behavior must remain bounded and unchanged.
- Legacy compatibility / examples: selected — main-checkout callers, merge commits, and existing replay classification remain compatible.
- Error handling / rollback / partial outputs: selected — a blocked commit must name the effective config path; no output mutation occurs.
- Release / packaging / dependency compatibility: selected — default Python aligns to 3.11 while `>=3.11` support and CI pip resolution remain unchanged; tracked systemd/CLI entrypoints must preserve the deferred active environment.
- Documentation / migration notes: selected — generated instructions and current node-22 runbooks document default/multi-version use, isolated validation, safe pre-window operations, and convergence.
- All NHMS domain packs: not selected — no geospatial, hydro-met, SHUD numerical, PostGIS/Timescale, Slurm lifecycle, provider snapshot, run manifest, or display identity behavior changes.

## Invariant Matrix

- Governing invariant: each repository tool SHALL derive its decision from the same stable identity domain as the operation it governs.
- Source-of-truth identity/contract: `.python-version=3.11`; complete whole-document status marker under `docs/governance/DOC_STATUS.md`; Git top-level for tool-call `cwd`; replay reason literal plus owning function name.
- Producers: uv interpreter selection; node-22 systemd timers/operator commands; hook input JSON; state-manager reason raises.
- Validators/preflight: uv pin assertion; static node-22 entrypoint/runbook audit; large-file hook; replay mapping completeness test.
- Storage/cache/query: tracked config and source files plus node-22's active shared `.venv`; no DB/cache.
- Public routes/entrypoints: `uv run`; node-22 systemd units/current runbooks; e2e/grib runbook and pytest skip guidance; `large-file-guard.sh`; replay script constants.
- Frontend/downstream consumers: developers/agents, node-22 operators and automatic services, node-27 validation operators, CI parity, replay operators.
- Failure paths/rollback/stale state: unsupported 3.13-only API fails on 3.11; a missing exact node-22 interpreter fails closed without creating an environment; wrong-root exclusion must no longer block; unmapped/moved reason fails focused tests.
- Evidence/audit/readiness: issue verification commands, static entrypoint contract tests, hook shell suite, replay pytest, ruff, OpenSpec validation, bounded node-22 interpreter receipt.
- Regression rows:
  - tracked pin + `uv run python` -> Python 3.11.x; explicit `--python 3.14` remains available.
  - active node-22 automatic/operator entrypoint before maintenance -> exact active `.venv` interpreter or checked-in wrapper; no bare/environment-updating `uv`.
  - e2e/grib validation -> current node-27 oracle, fail-closed existing Python 3.11 assertion, then `uv run --no-sync`; pytest failure survives receipt piping and both project environments remain untouched.
  - complete whole-document `historical baseline` marker -> preserved topology text is non-current; incomplete marker or marker on a named current authority -> text remains scanned and gate-eligible.
  - nested worktree `cwd` + main-checkout `CLAUDE_PROJECT_DIR` -> config resolves from the worktree Git top level for both exclusion and block diagnostics.
  - unchanged main-checkout/merge fixture -> current block/pass and merge-parent filtering remain unchanged.
  - every `_PRE_COMMIT_INDEX_REASONS` member -> stable mapped function that contains that reason literal.

## Risks / Trade-offs

- [Existing local `.venv` uses 3.14] → `uv sync` recreates it once under 3.11; run the full required regression afterward.
- [node-22's shared `.venv` is used by live services and automatic units] → do not replace it in place; remove environment-updating uv from tracked active entrypoints, verify Python 3.11 in a disposable worktree, restore any interrupted 3.12 packages, and defer the active cutover plus installed-unit deployment to an operator-approved maintenance window with service stop/restart receipts tracked by #1831.
- [tool-call `cwd` might be nested] → resolve `git rev-parse --show-toplevel` rather than assuming `cwd` itself is the root; fail-safe fallback preserves legacy callers.
- [function names can also change] → completeness and literal-ownership tests fail on rename/move, forcing an intentional update without unrelated line churn.
- [the entropy implementation and its test file already exceeded the 1,000-line commit guard before this change] → register only `scripts/governance/audit_repo_entropy.py` and `tests/test_entropy_audit_script.py` as exact grandfather exclusions so the confirmed authority-classifier fix can ship without an unrelated high-risk split. This is a recorded deviation, not a generated/vendor classification or a claim that the debt is resolved: test-file decomposition is tracked by #1823 and implementation decomposition by #1842; both follow-ups must preserve parity/invariant coverage and remove their exact exclusions when the files are below the guard.

## Migration Plan

1. Commit pin, hook behavior/tests, stable replay mapping/tests, and generated instructions together.
2. Verify locally on Python 3.11 and explicitly confirm Python 3.14 remains selectable.
3. After push, verify the exact commit on node-22 in an isolated worktree. Keep the active shared `.venv` on 3.12.7 while live services use it; at the next operator-approved maintenance window, stop its owning processes, run `uv sync --all-extras --dev`, assert Python 3.11.x, and restart/verify services. Do not trigger a compute job.
4. Before that maintenance window, node-22 instructions and tracked entrypoints SHALL NOT permit any command that implicitly or explicitly creates/updates/replaces/syncs the shared `.venv`: this prohibits both `uv sync` and bare or environment-updating `uv run` in the active checkout (uv recreates a 3.12 venv under the 3.11 pin, verified in a disposable env). Pre-window read-only Python observation uses `uv run --no-sync ...` only, which executes the still-active 3.12.7 environment and is not pin proof; required operational mutations use only a checked-in wrapper or exact active `.venv` interpreter. `--active` and system Python are not safe substitutes. The node-22 disposable worktree is bounded pin acceptance only; environment-coupled e2e/grib validation uses the current node-27 environment only after a fail-closed Python 3.11 assertion, runs through `uv run --no-sync`, and preserves pytest's failure status through receipt piping. Root instructions, current runbooks, automatic units, script usage, and pytest guidance must agree.
5. Before the active checkout first receives the pin in production, deploy or otherwise stage the fixed exact-interpreter units so no installed daily service retains a bare `uv` command; the authorized service-stop/cutover/restart execution remains tracked by #1831 and is not performed by this PR.
6. Rollback is a normal revert of this PR; no persisted business data changes.

## Open Questions

None. The issue's node-22 choice is explicitly resolved: the tracked default converges to Python 3.11 now; the active shared environment remains on 3.12.7 only until its next operator-approved service maintenance window. See `evidence/node22-python-pin-receipt.md`.