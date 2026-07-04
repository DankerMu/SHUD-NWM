# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-02

### Fixed
- Heatmap sample now obeys its own priority rules: `apps/web` and `packages/db` (two 🟡 each) are `⚠️ Attention`, and `packages/ui` (a ❌ cell) is `❌ Critical`.
- Utility-directory `find` command groups its `-path` branches with `\( … \)` so the `-o` alternatives no longer escape the `-type`/`-name` filter, and `node_modules` is pruned (previously matched stray files repo-wide).
- Closing caveat no longer claims the skill only writes `.entropy-baseline/`: the audit itself never edits source/tests/docs, but the optional Phase 3d handoff can create GitHub issues or route to `stage-change-pipeline` (external work-tracking side effects, on explicit confirmation only).
- Removed the dangling `dependency-audit` routing target (no such skill exists).
- Dead-link detection resolves each Markdown link against its containing file's directory (not the CWD) and scans nested AGENTS.md/CLAUDE.md files, not just the repo root.

### Changed
- Portability: replaced GNU-only `grep -P`/`\b`/`\s` with POSIX `grep -E`/`-Eo` plus `[[:space:]]`/`[[:alnum:]]` classes so the scan commands run on stock macOS/BSD.
- Unified scoring thresholds between the heatmap and scan-dimensions: 🔴 bands are numeric and cross-linked per axis, the naming off-by-one is fixed (🟡 = 1-2, 🔴 = ≥ 3), and `❌` has a general definition plus a per-axis trigger.
- Honest capability language: "SCC" reframed as cycle detection (pairwise 2-cycles via grep; full SCC via `madge`/`dependency-cruiser` when available); naming diversity notes there is no embedding clustering in a single session; contagion is ranked by occurrence count with recency as a tiebreaker/"actively spreading" flag rather than an unbounded `count × recency` product.
- Baseline: retitled "JSON Schema" to "Baseline instance format" (it is an illustrative instance), persisted the trend-table metrics in `summary` (`naming_variant_total`, `error_envelope_variants_max_per_module`, `stale_docs_total`) with documented aggregations, added a written regression rule, and renamed `scc_count` → `cycle_count`.
- Baseline archive filename now uses the timestamp stored inside the old snapshot (`<timestamp>.json`) so same-day reruns do not collide.

### Added
- Language-adaptation preamble in scan-dimensions: commands are TypeScript-flavored, with a Python/Go/Rust/Java substitution mapping (axes and thresholds are language-independent).
- Structure-axis commands scan the Phase 1b layout (`apps/ packages/ src/`) and exclude `node_modules` consistently.
- Named `openspec/glossary.md` as the canonical glossary location in the semantic axis (consistent with Phase 1d).
- Phase 3d issue creation is sequenced after the Phase 4 baseline snapshot so a committed audit trail exists first.
- `skill.json`: `gh` added to `requirements.tools` (Phase 3d handoff only) and `requirements.network` set to `optional`.

## [0.2.0] - 2026-07-02

### Added
- Findings-to-delivery handoff in Phase 3d: after the user confirms cleanup priorities, offer `gh-create-issue` for direct cleanup issues or `stage-change-pipeline` for targets that need a reviewed OpenSpec change first. Audits previously stopped at report-only recommendations.
- Recognize `openspec/glossary.md` as the canonical domain glossary in constraint loading (Phase 1d), aligning the semantic axis with the persistence convention maintained by `grill-with-docs`.

## [0.1.0] - 2026-05-24

### Added
- Six-axis entropy scanning: structure, semantics, behavior, context, protocol, control
- Module-level heatmap output with color-coded scores and priority ranking
- Baseline snapshot management with trend comparison
- High-spread pattern detection with replication risk ranking
- Prioritized cleanup target list with tool recommendations
- Scan dimensions reference with shell-based detection methods per axis
