# Metric Definitions

Proxy metrics for approximating the latent variable: **future agent correct-change cost**. These are not precise measurements — they are trend signals. Watch direction over time, not absolute values.

---

## Operational Metrics (Primary Dashboard)

These metrics directly approximate how costly it is for an agent to make a correct change.

### Legibility Coverage

**Meaning:** What fraction of critical modules have an agent-readable source of truth in the repo.

**How to approximate:**
- List all directories containing > 5 source files or designated as a "package" / "app" / "module"
- For each: does it have an instruction file (AGENTS.md, CLAUDE.md, etc.) with substantive content (> 10 lines)?
- Coverage = (modules with instruction files) / (total critical modules)

**Interpretation:**
- Rising → agents need less re-discovery work per task
- Falling → new modules are being created without instruction files

### Plan Coverage

**Meaning:** What fraction of non-trivial changes are driven by a durable plan artifact.

**How to approximate:**
- Count merged PRs in the last N months that touch > 5 files or > 3 modules
- Of those: how many have a linked plan document, exec-plan, or design doc?
- Coverage = (planned changes) / (non-trivial changes)

**Interpretation:**
- Rising → agent work is more directed, less improvised
- Falling → complex work is being done without upfront structure

### Invariant Coverage

**Meaning:** What fraction of documented rules are enforced by automated checks.

**How to approximate:**
- List all rules stated in instruction files, ADRs, and architecture docs
- For each: is there a corresponding lint rule, guard, structural test, or CI gate?
- Coverage = (enforced rules) / (stated rules)

**Interpretation:**
- Rising → rules are moving from "documented" to "enforced"
- Falling → new rules are being written but not automated, or old enforcement is breaking

### Permission Coverage

**Meaning:** Are high-risk operations protected by explicit gates?

**How to approximate:**
- List high-risk operations: DB migrations, production deploys, secret access, destructive file operations
- For each: is there an explicit approval gate, readonly guard, or permission check?
- Coverage = (gated operations) / (high-risk operations)

**Interpretation:**
- This metric should be stable at high values; any drop requires immediate attention

### Validation Coverage

**Meaning:** Can agents self-verify their work on critical paths?

**How to approximate:**
- List critical user-facing paths or API endpoints
- For each: is there at least one test (unit, integration, e2e) that exercises this path?
- Also check: can the application be locally started and observed (dev server, smoke test)?
- Coverage = (tested critical paths) / (total critical paths)

**Interpretation:**
- Rising → agents can verify their changes without human intervention
- Falling → agents are flying blind on more paths

### Cleanup Half-life

**Meaning:** From discovery to retirement, how long does a known bad pattern survive?

**How to approximate:**
- Track issues/tasks tagged as cleanup, tech-debt, or refactor
- Measure median time from creation to resolution
- Also track: how many cleanup items are created vs resolved per period

**Interpretation:**
- Decreasing → the system is getting better at retiring bad patterns
- Increasing → cleanup debt is accumulating faster than it is being paid

### Human Attention Minutes per Merged Change

**Meaning:** How much human review time does each merged change require?

**How to approximate:**
- This is hard to measure precisely. Proxy: count review rounds per PR, count comments per PR
- If review tooling provides timing data, use that
- Alternative proxy: what fraction of PRs are merged with only a single review approval?

**Interpretation:**
- Rising when it shouldn't be → agents are producing changes that require more human scrutiny
- Falling appropriately → the control system is enabling agents to produce trustworthy output

---

## Diagnostic Metrics (Entropy Axes)

These metrics feed into the six entropy axes. They are useful for heat maps and for identifying which areas need attention, but they are upstream of the operational metrics.

### Structural Axis
- **SCC count and size**: number and size of strongly connected components in the module dependency graph
- **Util/common volume**: file count and line count in utility directories
- **Layer violation count**: imports that cross declared layer boundaries
- **Max file size**: largest files by line count (flag > 500 lines)

### Semantic Axis
- **Naming variant count**: for core domain concepts, how many distinct identifiers are used
- **State definition scatter**: how many files define status/state/phase enums for the same entity
- **Glossary coverage**: fraction of core concepts with a canonical name in the glossary

### Behavioral Axis
- **Error envelope variants**: distinct error response shapes across the API surface
- **Boundary behavior inconsistencies**: different null/empty handling patterns for similar cases
- **Error code fragmentation**: number of error codes used only once (likely ad-hoc)

### Context Axis
- **Instruction file coverage**: fraction of critical directories with substantive instruction files
- **Doc freshness gap**: median difference between doc last-modified and corresponding code last-modified
- **Dead reference count**: links in instruction files that point to non-existent targets

### Protocol Axis
- **Instruction template compliance**: fraction of module instruction files following the standard template
- **CI gate consistency**: fraction of CI workflows with equivalent gate criteria
- **Plan artifact usage**: fraction of non-trivial PRs with linked plan documents

### Control Axis
- **Rule enforcement ratio**: fraction of documented rules with automated enforcement
- **GC backlog size**: number of open cleanup items
- **Doctor/health-check recency**: days since last health-check run

---

## Usage Notes

1. **Start with operational metrics.** Diagnostic metrics are supplementary. If you only track six numbers, track the operational ones.

2. **Trend matters more than absolute value.** A legibility coverage of 40% that was 20% last quarter is healthier than 60% that was 80% last quarter.

3. **Don't compare across projects.** These metrics are calibrated per-repo. A monorepo with 50 packages and a single-app repo will have different baselines.

4. **Automate what you can; estimate the rest.** Some metrics (SCC count, file sizes, instruction file presence) are trivially automatable. Others (plan coverage, cleanup half-life) require judgment. Start with the automatable ones.

5. **Review quarterly.** Monthly is too noisy for most of these signals. Quarterly gives enough data to see real trends.
