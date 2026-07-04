# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-02

### Added
- Promoted the two agent-era checklist sections to first-class Phase 2 dimensions — **2g Pattern Contagion** and **2h Agent Verifiability** — each with a summary pointing to the checklist. `SKILL.md` and the `skill.json` description now describe **eight** dimensions (was six), resolving the six-vs-eight contradiction with `entropy-checklist.md`.
- `version: 0.2.0` to `SKILL.md` frontmatter (previously absent).
- Pointer from the verdict section to the Severity Crosswalk in `risk-adaptive-cross-review`'s `finding-contract.md`, so E-grades can be folded into P0/P1/P2/Note cross-reviews.
- Protocol-axis note to the `entropy-checklist.md` mapping table: no diff-level dimension maps to the Protocol axis; instruction-file/CI/agent-protocol changes route to `control-plane-auditor`.

### Changed
- Redefined the E-grade system from dimension-coded to **harm-based**: E0 = direct conflict/fork with an established standard (fails the verdict); E1 = new divergent pattern with high replication risk (fix before merge); E2 = localized inconsistency (fix or accept with note); E3 = doc/metadata desync (note). Any of the eight dimensions can now yield any grade. Added one worked example per grade and made the verdict use the tiers distinctly (E0 → fail, E1 → fix-before-merge, E2/E3 → notes).

## [0.1.1] - 2026-07-02

### Changed
- Recognize `openspec/glossary.md` as the canonical domain glossary in constraint loading (step 1b), aligning naming-drift checks with the persistence convention maintained by `grill-with-docs`. AGENTS.md-level constraints remain supplementary.
- Add a dimension-to-axes mapping table to `entropy-checklist.md`: the review dimensions are the diff-granularity instantiation of the suite's shared six-axes vocabulary (owned by `control-plane-auditor`'s methodology reference). Dimension names stay as-is for findings; the axes are for cross-change aggregation and comparison with `repo-entropy-audit`.

## [0.1.0] - 2026-05-24

### Added
- Six-dimension entropy analysis: naming consistency, error handling consistency, dependency direction, documentation sync, state model, pattern duplication
- Severity grading system: E0 (Pattern Fork), E1 (Naming Drift), E2 (Structural Deviation), E3 (Doc Desync), Suggestion
- Constraint gap detection with handoff to control-plane-auditor
- Entropy checklist reference with detection patterns and false-positive guidance
