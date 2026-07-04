# Six Entropy Axes

Six diagnostic lenses for identifying where a repository is losing order. Each axis is independent but interacts with the others; a problem on one axis often amplifies problems on adjacent axes.

**Shared vocabulary, per-object instantiation.** This file is the entropy suite's canonical abstract vocabulary, consumed by `repo-entropy-audit` (whole-repo scans), `entropy-review` (per-change reviews, via its diff-granularity dimension mapping), and `control-plane-auditor` (which additionally applies its own seven-layer model to the control *system* itself). The axes are shared; the detection methods are not — each consumer instantiates them for its own object and granularity. Do not force one checklist across objects: a unified abstract language with divergent concrete probes is the intended design.

**Agent-era calibration note:** Several metrics in this framework have been recalibrated for agent-first development. Traditional proxies for human cognitive limits (file line count, function length, nesting depth, comment density) are replaced by metrics that measure the actual underlying problems (mixed responsibilities, implicit dependencies, uncovered branches, constraint accessibility). See `docs/decisions/agent-era-metric-recalibration.md` for the full rationale (repo-level doc; not shipped with standalone skill installs).

---

## 1. Structural Entropy

**What it measures:** whether the system still has a legible skeleton — layers, dependency directions, module boundaries, and evolution path.

**Engineering signals:**
- Dependency graph approaching a random graph (high out-degree variance, large SCCs)
- `util/common/helpers` directories swallowing business logic and growing unbounded
- Files with high import-source diversity (pulling from many unrelated modules — proxy for mixed responsibilities)
- Layer violations: UI calling repository directly, domain importing infra
- Implicit dependencies: functions relying on global state, mutable singletons, or undeclared side effects
- Evolution history unreadable: no coherent narrative from git log

**How to detect:**
- `grep -r "import\|require" --include='*.ts' | sort` → build an import matrix, check direction
- Per-file import source diversity: count distinct top-level modules imported (flag files importing from > 5 unrelated modules)
- Count files per directory; flag directories with > 30 files or growing > 20% per quarter
- Scan for global/mutable state: `grep -rn "global\|singleton\|Singleton\|let " --include='*.ts'` (heuristic)
- SCC detection on the module graph (tarjan on import edges)
- Check for architecture lint rules (eslint-plugin-import, dependency-cruiser, custom guards)

**Interactions:** high structural entropy amplifies semantic entropy (concepts leak across boundaries) and behavioral entropy (each layer invents its own error model).

---

## 2. Semantic Entropy

**What it measures:** whether the system speaks one language — consistent naming, unified domain model, single source of truth for state machines and enums.

**Engineering signals:**
- Same concept called by multiple names (`user` / `member` / `account` / `profile`)
- Multiple status/state/phase enums describing the same lifecycle in different files
- No glossary or domain dictionary; terminology drifts per author or per agent session
- Bounded contexts without explicit mapping between their local languages

**How to detect:**
- Collect all exported identifiers; cluster by embedding similarity; count variants per cluster
- `grep -rn "Status\|State\|Phase" --include='*.ts'` → group by semantic domain, count definitions
- Check for a glossary file, domain model doc, or `AGENTS.md` glossary section
- Check if bounded-context boundaries are documented with explicit translation rules

**Interactions:** semantic entropy directly feeds structural entropy (unclear concepts → unclear module boundaries) and context entropy (agents cannot tell which name is canonical).

---

## 3. Behavioral Entropy

**What it measures:** whether the system behaves consistently — error models, boundary handling, API contracts, environment parity.

**Engineering signals:**
- Multiple error envelope shapes across the same service (`{code, message}` vs `{error: string}` vs bare HTTP status)
- Inconsistent null/empty handling: some paths return `[]`, some `null`, some throw, some silently default
- Different retry/timeout/backoff strategies in adjacent modules
- Local/staging/production behavioral divergence beyond configuration

**How to detect:**
- `grep -rn "catch\|throw\|Error\|err\|reject" --include='*.ts'` → classify handling patterns
- Inspect API response types: count distinct response shapes for error cases
- Check for a unified error model definition (shared error class, error enum, response envelope)
- Compare environment config files for behavioral differences beyond secrets

**Interactions:** behavioral entropy is the axis most likely to cause production incidents. It is amplified by semantic entropy (no unified error taxonomy) and control entropy (no lint rule enforcing the error model).

---

## 4. Context Entropy

**What it measures:** whether the authoritative facts needed to complete a task are locatable, loadable, and current inside the repository — not trapped in chat logs, personal memory, or stale wikis.

**Engineering signals:**
- Key design decisions exist only in Slack threads or meeting notes, not in ADRs or docs/
- AGENTS.md files reference docs that no longer exist or have drifted from implementation
- No instruction files in critical modules; agents must guess conventions
- Runbooks and specs are > 6 months old while the code has changed significantly

**How to detect:**
- List all `AGENTS.md` / `CLAUDE.md` locations; compare against the list of critical directories
- For each doc file: `git log -1 --format=%ci <file>` vs `git log -1 --format=%ci <corresponding source>`
- Check for dead links in instruction files (`grep -r '\[.*\](.*\.md)' AGENTS.md | xargs test -f`)
- Check for ADR directory and whether recent architectural changes have corresponding ADRs

**Interactions:** context entropy is the primary driver of agent thrash — without accessible facts, agents waste tokens re-discovering constraints or inventing conflicting solutions.

---

## 5. Protocol Entropy

**What it measures:** whether agents, tools, clients, and reviewers share stable interfaces, event flows, approval semantics, and handoff formats.

**Engineering signals:**
- Different instruction files use incompatible section structures or conflicting rules
- CI workflows have inconsistent gate criteria (some check types, some don't; some require coverage, some don't)
- Multiple agent surfaces (IDE, cloud, CLI) see different repo state or have different tool permissions
- No standardized plan/task/status format across non-trivial changes

**How to detect:**
- Compare all AGENTS.md files for structural consistency (same sections? same verification commands?)
- Diff CI workflow files for gate criteria divergence
- Check for plan/exec-plan templates and whether they are actually used
- Check for skill/runbook definitions and their consistency

**Interactions:** protocol entropy multiplies with scale — two agents on one repo is manageable; five tools with five different conventions quickly becomes unworkable.

---

## 6. Control Entropy

**What it measures:** whether rules are actually in the execution plane — enforced by linters, CI, guards, structural tests — or merely written in documents that nobody reads and nothing checks.

**Engineering signals:**
- Architectural rules exist in docs but no linter or test enforces them
- Cleanup tasks are tracked but never executed; GC backlog grows indefinitely
- Readonly/generated path protections exist for some paths but not others
- Doc freshness rules exist in policy but no automated check validates them

**How to detect:**
- List all documented rules (in AGENTS.md, ADRs, ARCHITECTURE.md); check which have corresponding lint/test/guard
- Check for doctor/health-check scripts and when they last ran
- Check for drift-detection tooling (source-generated freshness, schema sync checks)
- Measure the gap between "rules we say we follow" and "rules with automated enforcement"

**Interactions:** control entropy is the meta-axis — when it is high, all other axes drift faster because there is no enforcement to slow them down.
