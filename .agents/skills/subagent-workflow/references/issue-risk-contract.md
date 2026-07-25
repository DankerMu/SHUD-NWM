# Issue Risk + OpenSpec Fixture Reference

Use this reference in Phase 0.5. Keep outputs short. The goal is to make the OpenSpec change a useful fixture without turning fixture creation into the main token sink.

## Inputs

Use whichever inputs exist:

- GitHub issue body and comments
- Existing OpenSpec `proposal.md`, `design.md`, `tasks.md` if present
- Existing tests and docs
- Project profile inferred from repo files
- Nearby code and downstream consumers

OpenSpec is mandatory for implementation. If missing, create it with the local CLI before coding.

## OpenSpec CLI

Use local CLI commands:

```bash
openspec new change <change-name> --description "<short issue summary>"
# artifact is required: proposal | design | specs | tasks - run once per artifact being authored
openspec instructions <artifact> --change <change-name>
openspec validate <change-name> --strict --no-interactive
openspec show <change-name>
```

If `openspec new change` creates only a skeleton, the orchestrator should directly fill `proposal.md`, `design.md`, `tasks.md`, and any required spec deltas according to the per-artifact `openspec instructions <artifact> --change <change-name>` output. Keep this concise; implementation code and runtime tests still go through the `implementer` subagent.

## Project Profiles

Map the generic risk packs and triggers below onto the concrete project using a
profile from `project-profiles.md`. Infer the matching profile from repo files,
or use the **Generic** profile when none matches. A profile adds project-specific
entry surfaces, contracts, risk axes, evidence, optional domain risk packs, and
optional domain expanded-triggers. Keep project-specific surfaces in the profile,
not in this contract, so the core stays portable.

## Risk Triage

Output at most 20 lines.

```text
Issue type: feature|bugfix|refactor|docs|release|test
Project profile: Generic|SHUD|rSHUD|AutoSHUD|... (from project-profiles.md)
Blast radius: low|medium|high|critical
Fixture level: none|compact|expanded
Upstream suggested level: <none|compact|expanded|absent> (agree | override: <one-line reason>)
Why:
- <1-4 concrete triggers>
Selected risk packs:
- <pack names only>
OpenSpec change: <name> (existing|generated)
Evidence floor:
- <minimum verification commands/tests>
```

## Fixture Level Rules

- `none`: docs-only, metadata-only, typo, isolated test expectation update, no runtime behavior. OpenSpec still exists, but may be minimal.
- `compact`: ordinary isolated code change; no shared entrypoint, no format/schema change, no file publish/delete behavior.
- `expanded`: any shared entrypoint, public API, file format/schema, solver/runtime/numerical behavior, geospatial/time-series data, external data discovery, file publish/delete/rollback, legacy compatibility, ambiguous issue scope, or high user-visible risk.

Prefer `compact` when uncertain unless a trigger above is present. Do not expand merely because the issue is important. When the issue carries an upstream `Suggested fixture level` (`stage-change-pipeline` Stage 5), the triage starts from it and any divergence — up or down — is recorded with a one-line reason on the `Upstream suggested level:` line; the upstream author saw the module boundary and expected PR scope, so silent re-triage in either direction is a contract drift, not a free choice.

**Canonical vocabulary is this file, and log lines use one token.** The levels are exactly `none | compact | expanded`, with `high`/`broad-expanded` as repair-intensity escalations. The accountability log's `fixture` field records the effective tier as a single token from `none|compact|expanded|high|broad-expanded` (`high`/`broad-expanded` subsume `expanded`); composites (`expanded/high`) and ad-hoc labels (`standard`, `light`) are rejected by `evidence_check.py --loop-log-entry` because they fragment the per-level keep/cut sample that `loop_log_audit.py` adjudicates.

Artifact set per fixture level (validated against openspec CLI 1.3.x — fixture weight scales with risk, fixture existence does not):

- `none` / `compact`: `proposal.md` (a few lines of why/what) + `tasks.md` + one **minimal spec delta** — a single requirement with one `#### Scenario:` block covering the actual behavior change. The delta is never skippable: `openspec validate` hard-requires at least one delta, and the delta is the only artifact `openspec archive` folds into the main specs. `design.md` is exempt at these levels (CLI-verified optional); state the one-line exemption in `proposal.md` and record risk-pack selections in `tasks.md`.
- `expanded`: full set — `proposal.md`, `design.md`, `tasks.md`, spec deltas.
- `high` / `broad-expanded` repair intensity: full set plus the `Invariant Matrix` (unchanged).

Mandatory `expanded` triggers. If the issue text, expected change surface, or likely diff touches any of these core triggers, do not downgrade:

- `dispatcher`, `routing`, `entrypoint`, `public API`, `CLI`
- `parser`, `reader`, `writer`, `schema`, `column`, `field`, `unit`, `format`
- `file output`, `delete`, `overwrite`, `publish`, `rollback`, `temp`, `symlink`, `path`
- `auth`, `permission`, `secret`, `token`, `credential`
- `migration`, `backward compatibility`, `legacy`, `example`
- concurrency, retry, cancellation, persisted/shared state transitions

The active project profile may add domain expanded-triggers (for example
solver/numerical or geospatial keywords). Treat the profile's triggers as
mandatory too. See `project-profiles.md`.

## Risk Packs

Every pack must be considered. Mark each as `selected` or `not selected` with a short reason in `design.md` or `tasks.md`. Only selected packs need evidence, but unjustified non-selection is a fixture defect.

Core packs (apply to every profile):

- Public API / CLI / script entry
- Config / project setup
- File IO / path safety / overwrite
- Schema / columns / units / field names
- Auth / permissions / secrets
- Concurrency / shared state / ordering
- Resource limits / large input / discovery
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Release / packaging / dependency compatibility
- Documentation / migration notes

Domain packs: the active profile may add packs (for example geospatial/CRS,
time-series/forcing, numerical/conservation, solver/threading). Include the
profile's domain packs in the considered set. See `project-profiles.md`.

## High-Risk Pack Closure Checklists

Use these as definition-of-done guidance when a pack is selected. The OpenSpec fixture does not need to paste every bullet, but `tasks.md` must include enough evidence to prove the relevant bullets or state a precise non-goal.

### File IO / Path Safety / Overwrite

Closure requires the implementation and tests to address every applicable item:

- Path containment is checked before access and again at the point of use when roots are attacker/user/config controlled.
- Symlink leaf and symlink ancestor components are rejected where following links would cross trust boundaries.
- For externally supplied roots or shared workspaces, reads use descriptor-bound access where available: bind trusted parent/root identity, open by `dir_fd` + basename, use `O_NOFOLLOW`/`O_CLOEXEC`, and verify with `fstat`.
- Non-regular files such as directories, devices, and FIFOs are rejected before blocking reads; if opened, use non-blocking flags and stable error handling.
- Reads and writes are bounded by byte count, entry count, depth, timeout, or scoped prefixes as appropriate.
- Existing output paths are classified before `mkdir`, `iterdir`, delete, overwrite, or publish operations; stale regular-file lanes get stable lane-specific errors.
- Atomic write or no-clobber behavior is explicit for generated evidence/artifacts.
- Rollback/cleanup only touches objects created by the current validation/run scope.
- Regression tests cover the applicable matrix: symlink leaf, symlink ancestor/parent swap, traversal/encoded traversal, non-regular file, oversized file, existing regular-file lane, no-clobber, scoped cleanup.

### Evidence / JSON / Schema Ingestion

Closure requires:

- Byte-size limits before parsing and during streaming/bounded reads.
- UTF-8 decode, JSON decode, and `RecursionError` failures map to stable domain-specific blockers/errors.
- Parsed external JSON has maximum depth and node/item count limits before recursive walking or semantic trust.
- Required schema/version/issue fields are checked before acceptance.
- Acceptance receipts, manifests, checksums, or audit summaries are bound to the exact producer evidence bytes they claim to accept.
- Deterministic/fallback evidence cannot silently satisfy live-production readiness unless the OpenSpec explicitly marks that as a non-goal.
- Regression tests cover invalid UTF-8, malformed JSON, deeply nested JSON, overly wide JSON, oversized JSON, schema mismatch, digest/receipt mismatch, and unchanged accepted evidence.

### Resource Limits / Large Input / Discovery

Closure requires:

- Directory discovery is scoped to run/prefix-specific paths or bounded by max depth and max entries.
- Large file reads are streamed or bounded; checksums do not require full unbounded memory reads.
- Polling, retry, and subprocess waits have explicit timeout/interval bounds.
- Tests cover broad unrelated roots, large-but-valid inputs, over-limit inputs, and timeout/stale-state handling.

### Error Handling / Rollback / Partial Outputs

Closure requires:

- Every expected failure path returns a stable error code or typed exception at the public boundary.
- Evidence lanes are written or intentionally omitted consistently on failure, with tests for each behavior.
- Partial writes, partially published objects, and cleanup/quarantine behavior are explicitly verified.
- Retried operations do not duplicate irreversible side effects unless idempotency is proven.

## Compact OpenSpec Fixture Content

Target 10-20 lines inside `tasks.md` (`design.md` is exempt at this level).

```text
Fixture level: compact
Change surface:
- <files/functions/entrypoints>
Must preserve:
- <existing caller/output/test behavior>
Must add/change:
- <new behavior>
Seams under test:
- <public boundary each test targets; declared upstream with rationale, fewest and highest possible>
Risk packs:
- Public API / CLI / script entry: selected|not selected - <reason>
- File IO / path safety / overwrite: selected|not selected - <reason>
- Schema / columns / units / field names: selected|not selected - <reason>
- Legacy compatibility / examples: selected|not selected - <reason>
- Other packs: selected|not selected - <summarized reason>
Required evidence:
- <test or command>
Non-goals:
- <explicit exclusions>
```

## Expanded OpenSpec Fixture Content

Target 25-45 lines across `design.md` and `tasks.md`.

```text
Fixture level: expanded
Project profile: <Generic|SHUD|rSHUD|AutoSHUD|... from project-profiles.md>
Change surface:
- <entrypoints and modules>

Must preserve:
- <legacy caller/API/format/output behavior>
- <downstream consumer expectations>

Must add/change:
- <new behavior>
- <new config/schema/files>

Seams under test:
- <public boundary each test targets; declared upstream with rationale (stage-change-pipeline Stage 2 design.md or issue author), fewest and highest possible — implementer consumes, never renegotiates>

Selected risk packs:
- <pack>: <project-specific checks>
- <pack>: <project-specific checks>

Risk packs considered (core):
- Public API / CLI / script entry: selected|not selected - <reason>
- Config / project setup: selected|not selected - <reason>
- File IO / path safety / overwrite: selected|not selected - <reason>
- Schema / columns / units / field names: selected|not selected - <reason>
- Auth / permissions / secrets: selected|not selected - <reason>
- Concurrency / shared state / ordering: selected|not selected - <reason>
- Resource limits / large input / discovery: selected|not selected - <reason>
- Legacy compatibility / examples: selected|not selected - <reason>
- Error handling / rollback / partial outputs: selected|not selected - <reason>
- Release / packaging / dependency compatibility: selected|not selected - <reason>
- Documentation / migration notes: selected|not selected - <reason>
Domain packs (from active profile, if any):
- <profile pack>: selected|not selected - <reason>

Required evidence:
- <scenario/test name or command>: <input> -> <expected output>
- <failure scenario>: <input> -> <expected error/no partial output>

Invariant Matrix:
- Governing invariant: <one sentence end-to-end contract; required for high or broad-expanded repair intensity>
- Source-of-truth identity/contract: <field/path/role/digest/schema/version>
- Producers: <files/functions or "none - reason">
- Validators/preflight: <files/functions or "none - reason">
- Storage/cache/query: <files/functions or "none - reason">
- Public routes/entrypoints: <files/functions or "none - reason">
- Frontend/downstream consumers: <files/functions or "none - reason">
- Failure paths/rollback/stale state: <files/functions or "none - reason">
- Evidence/audit/readiness: <files/functions/artifacts or "none - reason">
- Regression rows:
  - <surface + valid input/identity> -> <expected behavior>
  - <surface + mismatched/stale/unauthorized/boundary input> -> <expected stable failure>
  - <unchanged sibling consumer> -> <expected compatibility behavior>

Non-goals:
- <what this issue intentionally does not cover>

Review focus:
- <2-5 items reviewers must verify>
```

## OpenSpec Fixture Review

Run one short read-only `reviewer` subagent review for every OpenSpec fixture before implementation. The review checks only whether the selected risk packs and evidence are sufficient.

Reviewer prompt must include:

```text
Subagent boundary:
- You are a leaf reviewer subagent in a parent issue workflow.
- Do not invoke this workflow or the subagent-workflow skill.
- Do not spawn further subagents, parallel agents, nested reviewers, or any other AI/code agent.
- Do not ask another agent to implement, fix, review, or plan.
- Use ordinary shell/read-only tools only. Do not edit files.
- If the task cannot be completed without nested AI delegation, stop and report the blocker.

Review the OpenSpec change fixture for issue #<N>.
Inputs: issue body/comments, the OpenSpec files proposal.md, tasks.md, spec deltas (plus design.md when the fixture level requires it), relevant nearby docs/tests.
Check:
- Fixture level is not under-classified, especially mandatory expanded triggers.
- Every risk pack is marked selected/not selected with a defensible reason.
- Every selected risk pack maps to evidence in tasks.md or an explicit non-goal.
- High or broad-expanded fixtures include an Invariant Matrix with concrete surfaces and regression rows.
- Must-preserve and downstream compatibility axes are concrete.
- Tests have explicit input and expected output.
```

Output:

```text
Fixture review: pass|revise
Missing axes:
- None.
Required additions:
- <only if revise>
```

The fixture reviewer is read-only and has no write access: it returns this verdict as its final message, and the orchestrator records the outcome (and reruns the fixture review once if `revise`, per Phase 0.5).

Then run `openspec validate <change-name> --strict --no-interactive`.

Do not run a four-agent fixture review. If fixture content takes more than a few hundred tokens to explain, it is too large for this workflow.
