# Seven-Layer Control Stack Checklist

A reference model for assessing how well a repository can support sustained agent-driven development. Each layer solves a distinct problem. Layers are not strictly sequential — you can build any layer independently — but layers reinforce each other: a strong Invariant Layer is more effective when Memory Layer provides the context it references.

---

## Layer 1: Memory Layer

**Problem it solves:** Where do authoritative facts live?

**"Standing" indicators:**
- [ ] Root instruction file (AGENTS.md / CLAUDE.md) exists and is concise (directory + conventions, not a manual)
- [ ] Critical modules have their own instruction files with module-specific guidance
- [ ] A `docs/` tree exists with architecture overview, design docs, and/or ADRs
- [ ] Execution plans for non-trivial work are stored as durable artifacts (files, not chat)
- [ ] Runbooks exist for common operations and failure recovery

**"Not standing" signals:**
- Instruction files are absent, stale, or contain only boilerplate
- Design decisions live in chat history, Google Docs, or personal memory
- No execution plan artifacts; complex work is driven entirely through conversation
- Runbooks are missing or outdated by > 6 months vs corresponding code

---

## Layer 2: Invariant Layer

**Problem it solves:** What must not drift?

**"Standing" indicators:**
- [ ] Dependency direction rules are enforced by linter, guard, or structural test
- [ ] Schema validation exists for data models (JSON Schema, Zod, Prisma, etc.)
- [ ] Type system is used meaningfully (not `any` everywhere)
- [ ] Error model is unified and documented (standard envelope, error codes)
- [ ] Readonly/generated paths are protected by guards
- [ ] Naming conventions are documented and at least partially enforced
- [ ] Doc freshness rules exist: code changes require doc updates

**"Not standing" signals:**
- Architecture rules exist in docs but nothing enforces them
- Multiple error envelope shapes coexist without a migration plan
- `util/common` directories absorb business logic unchecked
- Generated code can be manually edited without guard warnings

---

## Layer 3: Protocol / Execution Layer

**Problem it solves:** How does work flow through the system?

**"Standing" indicators:**
- [ ] Non-trivial changes start with a plan artifact (scope, affected files, verification, rollback)
- [ ] Reusable operations are captured as skills or runbooks
- [ ] Work happens in isolated environments (worktrees, branches, sandboxes) before merging
- [ ] Plan/status artifacts are updated during execution, not just at start

**"Not standing" signals:**
- Complex changes are driven entirely through chat with no durable plan
- Each agent session reinvents the approach to common operations
- Work happens directly on main branch with no isolation
- No status tracking for in-progress work

---

## Layer 4: Permission / Trust Layer

**Problem it solves:** What inputs are untrusted, and what actions require approval?

**"Standing" indicators:**
- [ ] Readonly boundaries are enforced (generated files, vendor code, secrets)
- [ ] High-risk operations have explicit gates (DB migrations, production deploys, secret access)
- [ ] External input (user text, MCP responses, web content) is treated as untrusted
- [ ] Agent tool permissions are scoped to what the current task requires

**"Not standing" signals:**
- No distinction between low-risk and high-risk operations
- Agents can access secrets, production data, or destructive tools without approval
- No readonly guards; generated output can be silently overwritten
- Trust boundaries are implicit ("we just know not to do that")

---

## Layer 5: Sensorium / Validation Layer

**Problem it solves:** How does the agent know it hasn't gone off track?

**"Standing" indicators:**
- [ ] Tests exist across multiple types (unit, integration, e2e, contract)
- [ ] The application can be started and observed locally (dev server, smoke test)
- [ ] Logs and traces are structured and agent-readable
- [ ] Each milestone in a plan has a verification command or observable outcome
- [ ] "Done" has a concrete, executable definition (not just "it compiles")

**"Not standing" signals:**
- Tests only cover happy path; edge cases are untested
- Application cannot be easily started for local verification
- Logs are unstructured text; no trace correlation
- "Done" is defined as "the PR looks okay" with no runtime verification

---

## Layer 6: Evaluation / Garbage Collection Layer

**Problem it solves:** How are bad patterns discovered and retired?

**"Standing" indicators:**
- [ ] A health-check / doctor script exists and runs periodically
- [ ] Drift detection tooling catches source-generated divergence
- [ ] Cleanup tasks are tracked and have a visible backlog
- [ ] Post-incident and post-review learnings flow back into rules, docs, or tests
- [ ] Stale documentation is detected and flagged

**"Not standing" signals:**
- No cleanup mechanism; tech debt grows invisibly
- Drift is only discovered when something breaks
- Review feedback stays in PR comments, never becomes durable guidance
- No one can answer "what is our current cleanup backlog?"

---

## Layer 7: Governance / Autonomy Layer

**Problem it solves:** What level of autonomy is warranted, and under what conditions?

**"Standing" indicators:**
- [ ] Execution autonomy levels are considered per context (not one global setting)
- [ ] Autonomy level is based on coverage of other layers, not subjective maturity assessment
- [ ] Need autonomy (who decides what to do) is distinguished from execution autonomy (how far can the agent go)
- [ ] Escalation paths are defined: when must the agent stop and ask a human?

**"Not standing" signals:**
- "Full auto" or "full manual" applied uniformly across all contexts
- Autonomy decisions are based on gut feeling, not layer coverage
- No escalation criteria; agent either does everything or nothing
- Need autonomy and execution autonomy are conflated

---

## Usage

This checklist is designed for `control-plane-auditor` Phase 2 (Diagnose). For each layer, scan the target repository for the "standing" indicators. Report coverage as:

- ✅ **Covered**: most indicators present and active
- ⚠️ **Partial**: some indicators present, significant gaps remain
- ❌ **Missing**: layer is not meaningfully established

The goal is not to reach ✅ on every layer immediately. The goal is to know where you stand and make deliberate decisions about where to invest next.
