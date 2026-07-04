# Finding Contract

Cross-review findings must be actionable enough to drive a fix without another
round of interpretation.

## Severity

| Severity | Meaning | Gate |
| --- | --- | --- |
| P0 | Security hole, data loss, broken core behavior, invalid spec fixture | Must fix |
| P1 | Correctness, contract, integration, migration, or evidence gap likely to break users or implementation | Should fix before merge |
| P2 | Meaningful maintainability, coverage, or consistency issue that may compound | Usually fix or explicitly defer |
| Note | Non-blocking observation, polish, or unclear concern | Does not block |

## Required Fields

P0/P1/P2 findings must include:

- **Severity**
- **Failure class**: one label from the Failure-Class Vocabulary below
- **Violated contract or invariant**
- **Evidence**: file path, line, diff hunk, spec requirement, issue text, command output, or artifact section
- **Concrete scenario**: how the issue fails or becomes ambiguous
- **Consequence**
- **Fix direction**
- **Required verification**: test, command, spec change, or evidence update
- **Sibling surfaces to audit**, if any
- **Blocking status**

## Failure-Class Vocabulary

Assign every finding exactly one failure class from this whitelist. The closed
set makes deduplication, synthesis, and cross-run logging consistent across
reviewers and workflows. Use the most specific applicable label; use `other`
only for a real defect that fits none, and name the concrete invariant in the
finding text. Add a new class here before using it, the same way the repo
governs its category whitelist.

Implementation/code classes:

- `authorization` — auth, tenant, ownership, permission, OAuth, CSRF, or session invariant
- `async` — missing await, fire-and-forget work, cancellation, ordering, or unhandled rejection
- `concurrency` — race, TOCTOU, lost update, unsafe shared state, lock/cancellation ordering
- `injection` — SQL, shell, path, URL, template, HTML, regex, parser, or deserialization injection
- `ssrf` — server-side fetch/webhook/preview reaching internal/metadata networks via input
- `path-safety` — traversal, symlink, unsafe overwrite, or destination-binding error
- `data-integrity` — migration, backfill, transaction, pagination, cursor, lost update, or corruption
- `contract` — API/schema/serializer/config/CLI signature, shape, or semantic break
- `state-transition` — state-machine ordering, terminal/active state, retry limit, or stale-state boundary
- `cache` — cache key, invalidation, stale read, or cross-tenant/locale/user collision
- `wrapper` — wrapper/proxy/decorator/adapter forwarding, recursion, or non-faithful preservation
- `resource` — leaked file, stream, connection, lock, handle, or skipped cleanup path
- `performance` — repeated I/O, N+1, serialization, blocking hot path, or avoidable copy
- `compatibility` — broken backward compatibility for unchanged consumers or older persisted state
- `reuse` — duplicate source of truth or missed existing project helper
- `simplification` — unnecessary abstraction, compatibility path, feature flag, fallback, or dead code
- `altitude` — fix at the wrong ownership layer or patch-over of the real invariant owner
- `test-evidence` — missing/overbroad/misleading test, verification command, or evidence claim
- `conventions` — exact violation of an applicable instruction file (quote file + rule)

Spec/OpenSpec-artifact classes (for OpenSpec and Hybrid review):

- `design-consistency` — names, IDs, endpoints, schema fields, enum values, or counts that disagree across artifacts
- `spec-completeness` — missing requirement/scenario, untestable WHEN/THEN, uncovered edge case, or cross-spec gap
- `task-executability` — task granularity, dependency order, spec coverage, or unclear verification
- `task-boundary` — scope creep or module-boundary violation in a task or planned PR

- `other` — a real defect that fits none of the above; name the concrete invariant

## Severity Crosswalk

When a finding arrives from a sibling review pack, map its grade into this
contract's canonical P0/P1/P2/Note before synthesis so dedup, gating, and
cross-run logging stay consistent.

- **`review` skill**: P0 → P0, P1 → P1, P2 → P2; P3/Nit → Note.
- **`entropy-review`**: E0 → P1 (P0 when it breaks a selected risk-pack
  invariant); E1 → P2; E2 → P2; E3 → Note.

Map `entropy-review` dimensions onto this contract's Failure-Class Vocabulary:

- naming drift → `conventions`
- error-model fork → `contract`
- state split → `state-transition`
- duplication → `reuse`
- dependency-direction violation → `altitude` (business logic or imports at the
  wrong ownership layer); if a specific declared import boundary is broken, use
  the most specific existing class that fits.

## Non-Blocking Notes

Keep a concern non-blocking when it lacks a concrete scenario, cannot name the
violated contract, or is a style preference without behavioral or governance
risk. Do not hide clear bugs as questions.

## Downgrading

You may downgrade a reviewer concern only with explicit rationale:

- the cited behavior is outside scope or a documented non-goal
- the scenario is impossible under the artifact's stated preconditions
- the finding duplicates another stronger finding
- the required change would expand scope beyond the accepted fixture

## Reject When (Precision Gate)

Recall is the priority during finding generation, but a finding must still be
rejected (drop it, or keep it only as a non-blocking note) when any of these
holds. These are precision rules, not excuses to suppress a real bug:

- it is speculative with no realistic trigger input, state, or timing
- it is only style, naming, formatting, or preference with no behavioral effect
- it is not anchored to a changed line, touched behavior, or a named artifact section
- it duplicates another approved finding
- the suggested fix would require changing the anchor itself to work
- it asks for generic error handling/guards without naming the failing input and the wrong result
- it describes a race without naming the shared state and the concurrent access pattern
- it flags code visible in the diff but unrelated to the change's primary intent

A concern that fails the precision gate but names a plausible mechanism becomes a
non-blocking note, not a dropped finding.

## Oracle Integrity

The review's oracle — the spec/OpenSpec fixture, acceptance criteria, existing
tests, and applicable project rules — is immutable during review and fix. It is
the source of truth a finding is measured against; it is not a knob to turn to
make the work pass.

- Never weaken, delete, or rewrite a test, spec scenario, acceptance criterion,
  or project rule to clear a finding. Fix the code or the artifact under review,
  not the thing that judges it.
- Treating "the test was wrong" or "the spec is too strict" as the resolution
  requires explicit, recorded rationale tied to a documented non-goal or an
  out-of-scope decision — the same bar as Downgrading — never a silent edit.
- A merge/exit gate is a deterministic check against this frozen oracle: either
  the required evidence exists and matches, or the gate blocks. There is no
  "probably fine" verdict.
