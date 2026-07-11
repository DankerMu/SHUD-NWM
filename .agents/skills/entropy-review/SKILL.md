---
name: entropy-review
description: >
  Review a change set for entropy: naming drift, pattern duplication and contagion, error-model and state-model splits, doc staleness. Consistency and drift only, not correctness. Invoke explicitly or via routing from review.
invocation_posture: manual-first
version: 0.2.2
---

# Entropy Review

Review code changes for entropy introduction — naming drift, error-model fragmentation, dependency-direction violations, documentation staleness, state-model splits, and pattern duplication. This skill focuses on **consistency and drift**, not correctness or security.

For general code review (correctness, security, performance), use `review`.
For full-repo entropy scanning, use `repo-entropy-audit`.
For control-plane infrastructure audit, use `control-plane-auditor`.

## When To Use

- User asks to review a PR, diff, or staged changes for consistency
- User wants to check if a change introduces naming drift or pattern fragmentation
- User asks "does this PR introduce new patterns?" or "is this consistent with our conventions?"
- After a larger coding session, before committing, to catch drift early

## When Not To Use

- General code review (correctness, security, performance) → `review`
- Full repository entropy scan → `repo-entropy-audit`
- Control plane / instruction file audit → `control-plane-auditor`
- Documentation restructuring → `project-documentation`
- Brainstorming or implementation → other skills

---

## Prerequisites

This skill's effectiveness depends on constraint information in the target repository's instruction files (AGENTS.md, CLAUDE.md, etc.). Before starting, check for:

- **Glossary** — canonical terms and prohibited aliases
- **Dependency rules** — allowed/prohibited import directions
- **Error model** — standard error envelope and boundary conventions
- **Naming conventions** — case rules, verb patterns, file naming

If these are missing or thin, note this in the report under "Constraint Gaps" and suggest running `control-plane-auditor` to establish them first. Proceed with what is available — even without formal constraints, structural consistency can be assessed against surrounding code patterns.

---

## Phase 1 — Scope

### 1a. Determine the change set

Detect what to review:
- Staged changes: `git diff --cached --name-only`
- Unstaged changes: `git diff --name-only`
- PR or branch diff: `git diff <base>...<head> --name-only`
- Specific files: as specified by the user

### 1b. Load constraint context

For each affected module/directory:
1. Read the nearest AGENTS.md (walk up from the changed file)
2. Read the root AGENTS.md
3. Extract: glossary, dependency rules, error model, naming conventions, doc freshness rules, state model references
4. When `openspec/glossary.md` exists, treat it as the canonical domain glossary (the convention maintained by `grill-with-docs`); AGENTS.md-level constraints supplement it
5. If none found, note "no formal constraints available; reviewing against surrounding code patterns"

### 1c. Read surrounding context

For each changed file, read enough surrounding code to understand the local patterns:
- What naming style does this module use?
- What error handling pattern does this module use?
- What imports exist in neighboring files?

---

## Phase 2 — Analyze

Check the change set against eight dimensions. Use the [Entropy Checklist](references/entropy-checklist.md) for detailed detection guidance.

### 2a. Naming Consistency

Compare new identifiers against:
1. Glossary terms (if available) — is the canonical name used?
2. Surrounding code — does the naming style match?
3. Prohibited aliases — is a banned name being introduced?

Flag: new variants of existing concepts, mixed casing styles, non-canonical names.

### 2b. Error Handling Consistency

Compare new error handling against:
1. Error model (if defined) — correct envelope? correct codes?
2. Surrounding handlers — same pattern as adjacent code?
3. Boundary conventions — consistent null/empty/throw behavior?

Flag: new envelope shapes, ad-hoc error codes, silent error swallowing, inconsistent boundary behavior.

### 2c. Dependency Direction

Compare new imports against:
1. Dependency rules (if defined) — does this import violate a declared direction?
2. Module layer — is this an upward or lateral import that breaks layering?
3. Utility growth — is business logic being added to shared packages?

Flag: layer violations, bypassed adapters, business logic in util/common.

### 2d. Documentation Sync

Check:
1. Doc freshness rules (if defined) — does this change trigger an "if X then update Y" rule?
2. AGENTS.md references — if behavior changed, is the instruction file still accurate?
3. API/interface changes — is corresponding documentation updated?

Flag: behavioral changes without doc updates, stale instruction file references.

### 2e. State Model

Check:
1. New status/state/phase values — are they added to the canonical definition?
2. New transitions — are they consistent with the documented state machine?
3. Shadow definitions — is a new enum duplicating an existing state model?

Flag: shadow state definitions, undocumented state values, inconsistent transitions.

### 2f. Pattern Duplication

Check:
1. Semantic duplicates — is similar logic already implemented elsewhere?
2. Retired patterns — is the new code copying a pattern marked for retirement?
3. Missed abstractions — could an existing shared function/type be reused?

Flag: reimplemented logic, copied deprecated patterns.

### 2g. Pattern Contagion (agent-era)

Check whether the change replicates a suboptimal, deprecated, or flagged pattern that future agents may treat as the project standard. The core test is not "is this duplicated?" but "if an agent sees this merged and copies it N more times, is that acceptable?"

Flag: new code modeled on a `@deprecated`/`LEGACY`/`HACK` template, or a weak pattern likely to replicate with no enforcement brake. See the [Entropy Checklist](references/entropy-checklist.md).

### 2h. Agent Verifiability (agent-era)

Check whether the behavioral change can be verified by an agent without human help — via tests, a smoke check, or an observable outcome.

Flag: behavior verifiable only in production, changed routes with no integration/contract test, or config/environment-dependent behavior with no local check. See the [Entropy Checklist](references/entropy-checklist.md).

---

## Phase 3 — Synthesize

### Severity Levels

Grades are **harm-based, not dimension-coded**: any of the eight dimensions can yield any grade. A naming change is E0 when it forks the glossary's canonical term, but E2 when it is a one-off local inconsistency. Grade by the harm the change does, then name the dimension in the finding.

| Level | Name | Meaning | Gate |
|-------|------|---------|------|
| **E0** | Standard Conflict | Direct conflict or fork with an established project standard (error model, state definition, glossary term, dependency rule) | Fails the entropy verdict |
| **E1** | Divergent Pattern | Introduces a new divergent pattern with high replication risk | Fix before merge |
| **E2** | Localized Inconsistency | Local inconsistency that does not fork a standard | Fix or accept with a note |
| **E3** | Doc/Metadata Desync | Documentation or metadata out of sync with the changed behavior | Note |
| **—** | Suggestion | Not a problem, but could improve consistency | — |

### Finding Format

For each finding, tag the grade, name the dimension, cite the location, and give a fix direction. One worked example per grade:

```
**[E0] Error-model fork** — `apps/api/src/orders/handler.ts:88`
New handler returns `{ error: string }` instead of the project's standard
`{ code, message, details }` envelope (AGENTS.md → Error Model).
→ Fix before merge: return the standard envelope. E0 fails the entropy verdict.

**[E1] Naming Drift** — `apps/api/src/auth/handler.ts:42`
New identifier `fetchUserProfile` uses "User" instead of glossary term "Member".
Surrounding code uses `getMember*` pattern; likely to be copied by later code.
→ Consider: rename to `fetchMemberProfile` before merge.

**[E2] Localized inconsistency** — `apps/api/src/reports/util.ts:15`
Uses `snake_case` keys in a module that is otherwise `camelCase`; no standard is
forked and the impact stays local.
→ Fix or accept with a note: align to `camelCase`.

**[E3] Doc desync** — `apps/api/src/auth/handler.ts:42`
The endpoint signature changed but its API doc still lists the old parameter.
→ Note: update the API doc to match the new signature.
```

### Verdict

Grade the change by its highest-severity finding, using the tiers distinctly:

- ❌ **Standard conflict** — any E0 finding. The change forks an established project standard; the entropy verdict fails until it is resolved.
- ⚠️ **Fix before merge** — no E0, but one or more E1 findings. Divergent patterns with replication risk; recommend fixing before merge.
- ✅ **Clean** — no E0 or E1. E2 (localized inconsistency) and E3 (doc/metadata desync) are reported as notes: fix opportunistically or accept with a recorded note.

To fold these E-grades into a P0/P1/P2/Note cross-review, use the Severity Crosswalk in `risk-adaptive-cross-review`'s `references/finding-contract.md` (E0 → P1, or P0 if it breaks a selected risk-pack invariant; E1 → P2; E2 → P2; E3 → Note).

### Constraint Gaps

If prerequisite constraints were missing, list them at the end:

```
## Constraint Gaps

The following constraint dimensions are not defined in this repository's
instruction files. Adding them would make future entropy reviews more effective:

- [ ] Glossary (no canonical terms defined)
- [ ] Error Model (no standard envelope documented)

→ Run `control-plane-auditor` to get a full gap analysis and improvement plan.
```

---

## Phase 4 — Recommend

For each E0 or E1 finding:
- Provide a specific fix suggestion (what to rename, which error model to use, which import path to use)
- Reference the project standard if one exists (AGENTS.md section, glossary entry, error model doc)

For E2 and E3 findings:
- Describe what needs to change and why
- If the fix is straightforward, suggest it; if it requires broader refactoring, note that

Do not offer to apply fixes automatically — this skill is read-only. If the user wants fixes applied, they can use the normal implementation workflow.

---

## Caveats

- This skill is read-only. It reports findings but does not modify files.
- Effectiveness depends heavily on the quality of constraint information in instruction files. Without a glossary, naming drift detection relies on surrounding code patterns (less reliable).
- This skill reviews for consistency, not correctness. A perfectly consistent but logically wrong change will pass entropy review. Use `review` for correctness.
- False positives are possible, especially for pattern duplication. The reviewer should use judgment about whether structural similarity indicates actual duplication or coincidental resemblance.
- Generated code and vendored dependencies should be excluded from review scope.
