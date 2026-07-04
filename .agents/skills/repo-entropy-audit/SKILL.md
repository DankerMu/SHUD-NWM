---
name: repo-entropy-audit
description: >-
  Full-repository entropy scan across six axes: structure (dependency graph,
  layering, cycles), semantics (naming diversity, state model fragmentation),
  behavior (error model variants, boundary handling), context (doc freshness,
  instruction-file coverage), protocol (instruction-file consistency), and
  control (rule enforcement gaps). Produces a module-level heatmap, trend
  comparison, and prioritized cleanup targets. Activate when the user asks for
  "entropy audit", "repo health scan", "heatmap", "tech debt scan", or
  "full entropy report". Do NOT use for PR-level review (use entropy-review)
  or control-plane audit (use control-plane-auditor).
invocation_posture: manual-first
---

# Repo Entropy Audit

Scan an entire repository for entropy across six axes and produce a module-level heatmap, trend comparison against a stored baseline, and prioritized cleanup targets.

This skill examines the **code itself** for entropy patterns. For auditing the control system (instruction files, rules, guards), use `control-plane-auditor`. For PR-level consistency checks, use `entropy-review`.

## When To Use

- Periodic health check (monthly, quarterly, or before major releases)
- User asks for a full entropy scan, heatmap, or tech debt overview
- User wants to compare current state against a previous baseline
- User asks "where should we focus cleanup effort?"

## When Not To Use

- Reviewing a specific PR or diff → `entropy-review`
- Auditing instruction files and control infrastructure → `control-plane-auditor`
- Reviewing code for correctness → `review`

---

## Phase 1 — Scope

### 1a. Determine scan scope

- **Full repo** (default): scan all source directories
- **Specific modules**: user can specify a subset of modules/directories
- Exclude: `node_modules/`, `dist/`, `.git/`, vendored code, generated output

### 1b. Identify module structure

Detect the project's module/package layout:

```bash
# Monorepo packages
ls apps/ packages/ libs/ services/ 2>/dev/null

# Or single-app with directory-based modules
ls src/
```

Build a module list. Each module becomes a row in the heatmap.

### 1c. Check for existing baseline

```bash
ls .entropy-baseline/latest.json 2>/dev/null
```

If a baseline exists, load it for trend comparison in Phase 3.

### 1d. Load constraint context

Read available instruction files and extract any defined constraints (glossary, dependency rules, error model, naming conventions). These improve scan accuracy but are not required. When `openspec/glossary.md` exists, treat it as the canonical domain glossary (the convention maintained by `grill-with-docs`); AGENTS.md-level constraints supplement it.

---

## Phase 2 — Scan

For each module, scan across six axes. Use [Scan Dimensions](references/scan-dimensions.md) for detailed methods.

Adapt commands to the project's tech stack. The reference methods target TypeScript/JavaScript but the principles apply to any language.

### 2a. Structure

For each module:
- **Dependency direction**: identify imports, check for layer violations
- **Circular dependencies**: detect import cycles (pairwise 2-cycles via grep; full SCCs via `madge`/`dependency-cruiser` when available)
- **Utility coupling**: measure util/common/helpers directory size and who depends on them
- **File size distribution**: find oversized files (> 500 lines)

Score: 🟢 low / 🟡 medium / 🔴 high per [Heatmap Format](references/heatmap-format.md).

### 2b. Semantics

For each module:
- **Naming diversity**: for core domain concepts, count identifier variants
- **State definition scatter**: count files defining status/state enums per entity
- **Glossary compliance**: if glossary exists, check canonical vs alias usage

Score per heatmap guidelines.

### 2c. Behavior

For each module:
- **Error handling patterns**: classify (try-catch, promise catch, Result type, null return, silent default)
- **Error envelope variants**: count distinct error response shapes (API modules)
- **Boundary behavior**: sample null/empty handling for consistency

Score per heatmap guidelines.

### 2d. Context

For each module:
- **Instruction file presence**: does the module have a substantive AGENTS.md/CLAUDE.md?
- **Doc freshness**: compare doc last-modified vs code last-modified
- **Dead references**: check for broken links in instruction files

Score per heatmap guidelines.

### 2e. Protocol

For each module:
- **Template compliance**: does the instruction file follow the project's standard template?
- **Verification consistency**: are verification commands present and consistent with peers?
- **CI gate alignment**: does CI enforce the same gates for this module as for others?

Score per heatmap guidelines.

### 2f. Control

For each module:
- **Rule enforcement ratio**: documented rules vs automated enforcement
- **Cleanup markers**: count TODO/FIXME/HACK/deprecated markers
- **Health check coverage**: is this module included in doctor/health scripts?

Score per heatmap guidelines.

---

## Phase 3 — Synthesize

### 3a. Module-Level Heatmap

Produce the heatmap table using the format in [Heatmap Format](references/heatmap-format.md).

Each module gets a row with scores on all six axes plus a derived priority column.

### 3b. Trend Comparison

If a previous baseline exists:
- Compare module scores: which improved, which worsened, which are new
- Compare aggregate metrics: total high-entropy modules, instruction file count, etc.
- Highlight the most significant changes

Format per [Baseline Format](references/baseline-format.md) § Trend Comparison.

### 3c. High-Spread Patterns

Identify the patterns with highest replication risk:
- Which problematic patterns appear in multiple modules?
- Which patterns are still being actively copied (evidence from recent commits)?
- How many files/modules are affected?

Rank by occurrence count. Use recency — any replication within the last N commits (e.g., the last 30 days) — as a tiebreaker and an "actively spreading" flag, not as a multiplier.

### 3d. Cleanup Priorities

Rank cleanup targets by (impact × inverse effort):

1. High-spread, low-effort items first (e.g., naming unification with search-replace)
2. High-spread, medium-effort items next (e.g., error model unification)
3. High-spread, high-effort items last (e.g., structural decomposition of dependency cycles)

For each target, suggest which tool or skill can help:
- Naming unification → refactoring task
- Error model → `review` + architecture decision
- Structural issues → `implementation-planning` for phased refactor
- Doc gaps → `project-documentation` or `control-plane-auditor`

After the user confirms the priority list, offer to make the top targets trackable instead of leaving them as report-only recommendations: `gh-create-issue` for direct cleanup issues (epic + sub-issues for multi-module efforts), or `stage-change-pipeline` when a target needs a reviewed OpenSpec change and design review before implementation. Do not start implementing cleanups inside the audit. Sequence this after Phase 4: record the baseline snapshot first, then create the issues or route to the pipeline, so every tracked item points back to a committed audit trail.

---

## Phase 4 — Baseline

### 4a. Generate snapshot

Write a baseline snapshot to `.entropy-baseline/latest.json` using the format in [Baseline Format](references/baseline-format.md).

If a previous `latest.json` exists, archive it first: rename it to `.entropy-baseline/<timestamp>.json`, where `<timestamp>` is the `timestamp` value stored inside that old snapshot with colons replaced by hyphens (e.g. `2026-05-24T12-00-00Z.json`). Using the snapshot's own timestamp rather than today's date keeps same-day reruns from colliding.

### 4b. Confirm with user

Before writing the baseline file, show the user:
- Summary of what will be stored
- File location
- Whether a previous baseline will be archived

Proceed only with user confirmation.

---

## Methodology References

- [Scan Dimensions](references/scan-dimensions.md) — detection methods per axis
- [Heatmap Format](references/heatmap-format.md) — output table format and scoring
- [Baseline Format](references/baseline-format.md) — snapshot JSON schema and trend comparison
- [Six Entropy Axes](../control-plane-auditor/references/methodology/six-entropy-axes.md) — theoretical framework
- [Metric Definitions](../control-plane-auditor/references/methodology/metric-definitions.md) — proxy metrics

## Example Prompts

- "Run an entropy audit on this repo"
- "Generate an entropy heatmap"
- "Where should we focus cleanup effort?"
- "Compare entropy against last month's baseline"
- "Scan packages/api for entropy"

## Caveats

- Full-repo scans on large monorepos may consume significant tokens. Consider scanning modules individually for very large codebases.
- Scores are trend signals, not absolute measurements. A 🟡 in one project may represent a different absolute level than 🟡 in another. Do not compare across projects.
- The heatmap identifies **where** entropy is concentrated, not **why**. Understanding root causes requires reading the code. Use findings as starting points for investigation, not as definitive judgments.
- Detection methods use heuristic shell commands and pattern matching. They will miss some issues and may flag false positives. Judgment is still required.
- Baseline files contain project metadata. Consider whether they should be gitignored or committed, depending on your project's preferences.
- The audit itself writes only to `.entropy-baseline/` and never modifies source code, tests, or documentation. The optional Phase 3d handoff — offered only on explicit user confirmation — may create GitHub issues (`gh-create-issue`) or route a target to `stage-change-pipeline`; those are external work-tracking side effects, not edits to repository content.
