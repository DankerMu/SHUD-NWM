---
name: control-plane-auditor
description: >-
  Audit a repository's control-plane health and produce an actionable
  improvement report. Use when the user asks to "audit the repo",
  "check repo health", "assess control plane", "entropy audit for
  the control system", "check what's missing for agents", or wants
  to know what's missing in their instruction files, rules, and
  verification infrastructure. Also use for the write modes: "bootstrap
  the control plane", "set up this repo for agents", "init AGENTS.md
  and guardrails", or fixing gaps from a prior audit (bootstrap/repair).
  Do NOT use for code review (use review),
  full-repo code/entropy scan (use repo-entropy-audit), general
  documentation work (use project-documentation), or reviewing a
  specific PR or diff (use review or entropy-review).
disable-model-invocation: true
invocation_posture: manual-first
version: 0.4.0
---

# Control Plane Auditor

Audit a repository's control plane — instruction files, rules, guards, verification infrastructure, and governance posture — across a seven-layer model. Produce a structured report with coverage status, gaps, and prioritized improvement recommendations. In its write modes (bootstrap/repair, Phase 0), it also turns those gaps into the control plane itself: spec-previewed, authorized artifacts only, validated by guardrail self-test.

This skill examines the **control system** — memory, invariants, protocols, permissions, sensors, GC, governance — in depth. Its relationship to `repo-entropy-audit` is depth-vs-breadth on the context, protocol, and control axes: this skill is the deep-dive of those three axes, while `repo-entropy-audit` covers all six axes at breadth. The structure, semantics, and behavior of the CODE itself remain `repo-entropy-audit`'s territory. For PR-level consistency checks, use `entropy-review`.

## When To Use

- User asks to audit repo health, control plane, or agent readiness
- User wants to know what instruction files, rules, or infrastructure are missing
- User is setting up a new project for agent-driven development and wants a gap analysis
- User asks "what should I add to make this repo work better with agents?"
- User explicitly asks to bootstrap or repair the control plane (write modes — see Phase 0)

## When Not To Use

- Reviewing a specific PR or diff → `review` or `entropy-review`
- Writing or aligning the project's root CLAUDE.md/AGENTS.md as such (post-pack-install alignment, shared-source generation) → `project-instruction-bootstrap`; in write modes this skill only decides control-plane section content and delegates instruction-file mechanics to it when present (see the Bootstrap Playbook ownership split)
- Restructuring the docs/ directory → `project-documentation`
- Designing the AI tool setup → `agent-architect`
- Breadth scan across all six entropy axes (code structure, semantics, behavior) → `repo-entropy-audit`

---

## Phase 0 — Mode

Default is **audit** (read-only). Enter a write mode only when the user explicitly asks for it:

- **audit** — grade the control plane and report. Read-only; the default for every run.
- **bootstrap** — greenfield or sparse control plane (no root instruction file, no unified command entries, no guards): scaffold the initial control plane.
- **repair** — an existing control plane with known gaps (typically a prior audit's Priority Actions): fix the named gaps only.

Mode selection facts come from Phase 1's scan, not assumption: a repo with a substantive root instruction file and working command entries is never `bootstrap`. Announce the selected mode. Write modes still run Phases 1–3 first — the report is the input that authorizes writes — then continue with Phase 4 (spec preview + write) and Phase 5 (validate) per the [Bootstrap Playbook](references/bootstrap-playbook.md).

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
- Are global/mutable state, undeclared side effects, and environment dependencies declared where they exist (or better, eliminated)? Undeclared implicit dependencies are a top agent-era bug source (per agents-md-spec Dimension 7).

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
- Are agent tool permissions scoped to what the current task requires?

Assess: guard count, high-risk operation identification.

### Sensorium Layer

Check:
- Do tests span multiple types (unit, integration, e2e, contract)?
- Can the application be started and observed locally?
- Are logs structured?
- Does each milestone in a plan have a verification command or observable outcome?
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
- Is the autonomy level based on coverage of other layers (not subjective maturity assessment)?
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

These glyphs grade layer **coverage** (covered / partial / missing), not finding severity — they are distinct from `entropy-review`'s verdict glyphs.

### AGENTS.md Constraint Dimensions

Using the [AGENTS.md Spec](references/methodology/agents-md-spec.md), audit all instruction files for seven constraint dimensions:

1. **Glossary** — canonical terms with prohibited aliases
2. **Dependency Rules** — allowed/prohibited import directions
3. **Error Model** — standard envelope, error codes, boundary behavior
4. **Naming Conventions** — case rules, verb conventions, file naming
5. **Doc Freshness Rules** — "if X changes, update Y"
6. **State Model References** — pointers to canonical state definitions
7. **Implicit Dependencies** — declared global/mutable state, env vars, and side effects (agent-era addition)

For each dimension: report present/partial/missing, quality notes, and file location.

Canonical dimension definitions: references/methodology/agents-md-spec.md

### Enforcement Classification (No Phantom Enforcement)

For every documented rule found in instruction files and conventions, classify its enforcement:

- **mechanical** — cite the real config, command, hook, or CI gate that enforces it (file path or workflow name).
- **review-only** — nothing mechanical enforces it; it holds only through review attention. Legitimate, but labeled.
- **phantom** — the rule claims or implies a gate that does not exist. Report phantom rules as their own gap class: they are worse than review-only rules because they teach agents that stated gates can be ignored.

### Priority Actions

Rank the top 3–5 improvement actions by impact on **future agent correct-change cost**. The most impactful actions are typically:

1. Adding missing constraint dimensions to instruction files (reduces agent freedom → less entropy introduced)
2. Automating enforcement of existing documented rules (prevents drift)
3. Adding instruction files to critical modules that lack them (reduces re-discovery cost)
4. Establishing freshness checks for key documentation (prevents context rot)

**Severity semantics:** Priority Actions are repo-setup improvements, deliberately outside the P0/P1/P2/Note change-review scale. When a control-plane gap blocks an active change, raise it through `entropy-review` (E-grades) and fold it via the Severity Crosswalk in `risk-adaptive-cross-review`'s `finding-contract.md`.

### Tool Recommendations

Based on gaps found, recommend which other skills can help:
- `entropy-review` — for PR-level consistency checks and dependency-direction drift in a change (requires constraint dimensions in AGENTS.md)
- `repo-entropy-audit` — for code-level entropy scanning, including whole-repo structural dependency scans
- `project-documentation` — for docs/ tree restructuring
- `review` — for general code review

---

## Phase 4 — Bootstrap / Repair (write modes only)

Never write anything in audit mode. In a write mode, follow the [Bootstrap Playbook](references/bootstrap-playbook.md):

1. **Spec preview first**: present the deliverables list (files to create/modify and the gate each wires into), non-goals, and the readiness gaps that will remain. Wait for user confirmation before writing anything.
2. **Write only authorized artifacts** from the confirmed preview. Fix in place; never create parallel `_v2`/`_new`/`_backup` variants.
3. **No phantom enforcement**: every rule written must point to a real config, command, hook, or CI gate — or carry an explicit review-only label.

## Phase 5 — Validate (write modes only)

- Run the command entry points recorded in the scaffold (at minimum the check/test entries) and capture outcomes.
- Guardrail self-test: trigger each newly wired guard once where safe and confirm it actually blocks with the expected fed-back reason.
- Report evidence per deliverable. No completion claim without evidence; whatever remains open is listed as a readiness gap, not silently dropped.

---

## Two Frameworks: Seven Layers and Six Axes

This skill hosts two methodology files that play different roles:

- **The Seven-Layer Checklist IS this skill's audit procedure.** Phase 2 diagnoses the control *system* — memory, invariants, protocols, permissions, sensors, GC, governance — layer by layer. That is what this skill does.
- **The Six Entropy Axes is the entropy suite's shared repo-level vocabulary.** This skill is its methodology home, but the axes are consumed equally by `repo-entropy-audit` and `entropy-review`. This skill applies the axes only when aggregating or aligning findings with those siblings — not as a standalone audit procedure.

For orientation only (not a formal mapping), the seven layers align loosely with the six axes:

Memory → Context · Invariant → Control/Behavioral · Protocol → Protocol · Permission → Control · Sensorium → Behavioral/Context · Evaluation/GC → Control · Governance → Control

Treat this as directional orientation; the layer model and the axis model are deliberately different objects at different granularities.

## Methodology References

- [Six Entropy Axes](references/methodology/six-entropy-axes.md) — diagnostic framework
- [Seven-Layer Checklist](references/methodology/seven-layer-checklist.md) — layer assessment criteria
- [AGENTS.md Spec](references/methodology/agents-md-spec.md) — constraint dimension definitions
- [Metric Definitions](references/methodology/metric-definitions.md) — proxy metrics
- [Bootstrap Playbook](references/bootstrap-playbook.md) — write-mode procedure: spec preview, instruction-file scaffold, guardrail wiring, self-test

## Caveats

- Audit mode is read-only and is the default. Bootstrap/repair modes write only the artifacts authorized in the confirmed Phase 4 spec preview.
- Governance Layer is informational only — autonomy decisions require human judgment.
- The seven-layer model is a diagnostic tool, not a maturity ladder. Layers can be built independently.
- Quality assessment of instruction file content involves judgment; coverage counts alone can be misleading (a 100-line boilerplate AGENTS.md may be less useful than a 20-line directive one).
- This skill assesses the control system, not the code. Run `repo-entropy-audit` for code-level analysis.
