---
name: entropy-review
description: >-
  Review code changes for entropy introduction: naming drift,
  error-model splits, dependency violations, doc staleness, and
  pattern duplication. Activate when the user asks for "entropy review",
  "consistency check", "drift check on this PR", "check if this change
  introduces new patterns", or wants a review focused on naming,
  error handling, or structural consistency rather than correctness.
  Do NOT activate for general code review (use review), full repo audit
  (use repo-entropy-audit), control-plane audit (use control-plane-auditor),
  brainstorming, or implementation tasks.
invocation_posture: manual-first
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
4. If none found, note "no formal constraints available; reviewing against surrounding code patterns"

### 1c. Read surrounding context

For each changed file, read enough surrounding code to understand the local patterns:
- What naming style does this module use?
- What error handling pattern does this module use?
- What imports exist in neighboring files?

---

## Phase 2 — Analyze

Check the change set against six dimensions. Use the [Entropy Checklist](references/entropy-checklist.md) for detailed detection guidance.

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

---

## Phase 3 — Synthesize

### Severity Levels

| Level | Name | Meaning |
|-------|------|---------|
| **E0** | Pattern Fork | Introduces a new error model, state definition, or return format that directly conflicts with the project standard |
| **E1** | Naming Drift | Introduces a new name for an existing concept, or uses inconsistent casing/style |
| **E2** | Structural Deviation | Violates dependency direction, penetrates layer boundaries, or adds business logic to shared packages |
| **E3** | Doc Desync | Changes behavior but does not update corresponding documentation or instruction files |
| **—** | Suggestion | Not a problem, but could improve consistency |

### Finding Format

For each finding:

```
**[E1] Naming Drift** — `apps/api/src/auth/handler.ts:42`
New identifier `fetchUserProfile` uses "User" instead of glossary term "Member".
Surrounding code uses `getMember*` pattern.
→ Consider: rename to `fetchMemberProfile`
```

### Verdict

- ✅ **Clean** — no entropy findings (or only Suggestions)
- ⚠️ **Drift detected** — E1-E3 findings; recommend fixing before merge
- ❌ **Pattern fork** — E0 findings; strongly recommend fixing before merge

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

## Example Prompts

- "Run an entropy review on my staged changes"
- "Check this PR for consistency issues"
- "Does this diff introduce any naming drift?"
- "Entropy review the last commit"
- "Check if this change is consistent with our error model"

## Caveats

- This skill is read-only. It reports findings but does not modify files.
- Effectiveness depends heavily on the quality of constraint information in instruction files. Without a glossary, naming drift detection relies on surrounding code patterns (less reliable).
- This skill reviews for consistency, not correctness. A perfectly consistent but logically wrong change will pass entropy review. Use `review` for correctness.
- False positives are possible, especially for pattern duplication. The reviewer should use judgment about whether structural similarity indicates actual duplication or coincidental resemblance.
- Generated code and vendored dependencies should be excluded from review scope.
