---
name: control-plane-auditor
description: >-
  Audit a repository's control-plane health and produce an actionable
  improvement report. Use when the user asks to "audit the repo",
  "check repo health", "assess control plane", "entropy audit for
  the control system", "check what's missing for agents", or wants
  to know what's missing in their instruction files, rules, and
  verification infrastructure. Do NOT use for code review (use review),
  dependency audit (use dependency-audit), general documentation work
  (use project-documentation), or reviewing a specific PR or diff
  (use review or entropy-review).
invocation_posture: manual-first
---

# Control Plane Auditor

Audit a repository's control plane — instruction files, rules, guards, verification infrastructure, and governance posture — across a seven-layer model. Produce a structured report with coverage status, gaps, and prioritized improvement recommendations.

This skill examines the **control system itself** (memory, invariants, protocols, permissions, sensors, GC, governance), not the code those systems govern. For code-level entropy scanning, use `repo-entropy-audit`. For PR-level consistency checks, use `entropy-review`.

## When To Use

- User asks to audit repo health, control plane, or agent readiness
- User wants to know what instruction files, rules, or infrastructure are missing
- User is setting up a new project for agent-driven development and wants a gap analysis
- User asks "what should I add to make this repo work better with agents?"

## When Not To Use

- Reviewing a specific PR or diff → `review` or `entropy-review`
- Auditing dependencies for security/licensing → `dependency-audit`
- Restructuring the docs/ directory → `project-documentation`
- Designing the AI tool setup → `agent-architect`
- Scanning code itself for entropy patterns → `repo-entropy-audit`

---

## Phase 1 — Scan

Gather raw facts about the repository's control infrastructure. Do not evaluate yet.

### 1a. Instruction Files

Find all instruction files and note their locations, sizes, and last-modified dates:

```bash
find . -name "AGENTS.md" -o -name "CLAUDE.md" -o -name ".cursorrules" | head -50
```

For each file found: note line count, last git modification date, which directory it covers.

### 1b. Documentation Tree

Map the docs/ structure (or equivalent). Look for:

- Architecture overview (ARCHITECTURE.md, docs/architecture/)
- Design docs and ADRs (adr/, docs/decisions/, docs/design/)
- Execution plans (docs/plans/, docs/exec-plans/)
- Runbooks (docs/runbooks/, docs/operations/)
- Product specs (product/, docs/specs/)
- Quality/reliability docs (QUALITY.md, RELIABILITY.md, SECURITY.md)

### 1c. Rule Enforcement Infrastructure

Identify what automated enforcement exists:

- Lint configuration (eslint, stylelint, custom plugins)
- Structural tests (architecture tests, dependency guards)
- Schema validation (JSON Schema, Zod, Prisma)
- Readonly/generated path guards
- Git hooks (husky, lefthook, .githooks/)
- CI workflows and their gate criteria

### 1d. Test and Verification Infrastructure

Map test types and coverage:

- Test frameworks and configuration files
- Test directories and their organization (unit, integration, e2e, contract, etc.)
- Coverage tooling
- Smoke test / dev server availability
- Health-check / doctor scripts

### 1e. Module Structure

Identify the module/package layout:

- Top-level apps, packages, libraries, services
- Which modules are "critical" (contain core business logic, handle user data, manage state)

---

## Phase 2 — Diagnose

Evaluate each layer using the [Seven-Layer Checklist](references/methodology/seven-layer-checklist.md). For each layer, check the "standing indicators" against what Phase 1 found.

### Memory Layer

Check:
- Does a root instruction file exist? Is it concise and directive (not a manual)?
- Do critical modules have their own instruction files with substantive content?
- Is there an architecture overview that agents can read?
- Are there execution plans stored as files (not just chat)?
- Are there runbooks for common operations?

Assess: instruction file count, content quality (boilerplate vs substantive), docs tree completeness, freshness (last modified vs corresponding code last modified).

### Invariant Layer

Check:
- Are dependency direction rules enforced by lint/guard/test?
- Is there schema validation for data models?
- Is the type system used meaningfully (not `any` / `unknown` everywhere)?
- Is there a unified error model with documentation?
- Are readonly/generated paths protected?
- Are naming conventions documented and enforced?
- Do code changes require doc updates (freshness rules)?

Assess: rule count, enforcement ratio (documented vs automated), guard coverage.

### Protocol Layer

Check:
- Is there a plan template for non-trivial changes?
- Are there skills or runbooks for common operations?
- Is work typically done in isolated environments (worktrees, branches)?
- Are plan/status artifacts updated during execution?

Assess: plan template existence, evidence of plan usage in recent PRs.

### Permission Layer

Check:
- Are readonly boundaries enforced?
- Are high-risk operations gated (DB migrations, deploys, secret access)?
- Is external input treated as untrusted?

Assess: guard count, high-risk operation identification.

### Sensorium Layer

Check:
- Do tests span multiple types (unit, integration, e2e, contract)?
- Can the application be started and observed locally?
- Are logs structured?
- Are there executable "done" definitions?

Assess: test type coverage, dev server presence, log structure.

### Evaluation / GC Layer

Check:
- Does a health-check/doctor script exist?
- Is there drift detection tooling?
- Are cleanup tasks tracked?
- Do review learnings flow back into durable artifacts?

Assess: GC tooling presence, cleanup backlog visibility.

### Governance Layer

Do not auto-evaluate. Instead, note observations:
- Is there any explicit autonomy level configuration?
- Are escalation paths defined in instruction files?
- Is need autonomy distinguished from execution autonomy?

---

## Phase 3 — Report

Produce the audit report using the [Report Template](references/audit-report-template.md).

### Coverage Summary Table

For each layer, assign:
- ✅ **Covered**: most checklist indicators present and active
- ⚠️ **Partial**: some indicators present, significant gaps remain
- ❌ **Missing**: layer not meaningfully established
- ℹ️ **Informational**: (Governance only) observations without judgment

### AGENTS.md Constraint Dimensions

Using the [AGENTS.md Spec](references/methodology/agents-md-spec.md), audit all instruction files for six constraint dimensions:

1. **Glossary** — canonical terms with prohibited aliases
2. **Dependency Rules** — allowed/prohibited import directions
3. **Error Model** — standard envelope, error codes, boundary behavior
4. **Naming Conventions** — case rules, verb conventions, file naming
5. **Doc Freshness Rules** — "if X changes, update Y"
6. **State Model References** — pointers to canonical state definitions

For each dimension: report present/partial/missing, quality notes, and file location.

### Priority Actions

Rank the top 3–5 improvement actions by impact on **future agent correct-change cost**. The most impactful actions are typically:

1. Adding missing constraint dimensions to instruction files (reduces agent freedom → less entropy introduced)
2. Automating enforcement of existing documented rules (prevents drift)
3. Adding instruction files to critical modules that lack them (reduces re-discovery cost)
4. Establishing freshness checks for key documentation (prevents context rot)

### Tool Recommendations

Based on gaps found, recommend which other skills can help:
- `entropy-review` — for PR-level consistency checks (requires constraint dimensions in AGENTS.md)
- `repo-entropy-audit` — for code-level entropy scanning
- `project-documentation` — for docs/ tree restructuring
- `dependency-audit` — for dependency health
- `review` — for general code review

---

## Methodology References

- [Six Entropy Axes](references/methodology/six-entropy-axes.md) — diagnostic framework
- [Seven-Layer Checklist](references/methodology/seven-layer-checklist.md) — layer assessment criteria
- [AGENTS.md Spec](references/methodology/agents-md-spec.md) — constraint dimension definitions
- [Metric Definitions](references/methodology/metric-definitions.md) — proxy metrics

## Caveats

- This skill is read-only. It does not modify the target repository.
- Governance Layer is informational only — autonomy decisions require human judgment.
- The seven-layer model is a diagnostic tool, not a maturity ladder. Layers can be built independently.
- Quality assessment of instruction file content involves judgment; coverage counts alone can be misleading (a 100-line boilerplate AGENTS.md may be less useful than a 20-line directive one).
- This skill assesses the control system, not the code. Run `repo-entropy-audit` for code-level analysis.
