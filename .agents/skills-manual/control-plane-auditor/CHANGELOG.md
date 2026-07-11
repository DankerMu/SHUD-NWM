# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-11
- Mark the skill user-invoked: SKILL.md frontmatter now sets `disable-model-invocation: true`, enforcing the manual-first posture at the platform level. Claude Code drops the description from standing context and only explicit `/control-plane-auditor` invocation reaches it; the Codex projection/install target moves to `.agents/skills-manual/` (skill-lifecycle-manager 0.10.0 tooling). Discoverability lives in the `ask-danker` router.

## [0.3.0] - 2026-07-06

### Added
- **Bootstrap/repair write modes** (eng-init alignment): new Phase 0 mode gate (audit remains the read-only default; write modes require the user's explicit request and still run Phases 1–3 first), Phase 4 (spec preview listing deliverables/gates/non-goals/remaining gaps, user confirmation before any write, authorized artifacts only, fix-in-place — no `_v2`/`_new`/`_backup` variants), and Phase 5 (run recorded command entry points, guardrail self-test, evidence per deliverable — no completion claim without evidence). Procedure, mode-decision table, root-instruction-file scaffold (Project Snapshot / Command Entry Points / Verification Matrix / Boundaries & Forbidden Actions / Human Gates), and guardrail-wiring guidance live in the new `references/bootstrap-playbook.md`.
- **Enforcement Classification in the audit report (No Phantom Enforcement)**: every documented rule is classified `mechanical` (cites the real config/command/hook/CI gate), `review-only` (legitimate but unenforced), or `phantom` (claims a gate that does not exist — its own gap class, worse than review-only).
- Verification-matrix placement rule: canonical home is `openspec/project-profile.md` when the OpenSpec delivery flow is present (template owned by `subagent-workflow`'s `project-profiles.md`); otherwise inline in the instruction file. No duplicated rows.
- Ownership split with `project-instruction-bootstrap`: this skill decides control-plane section content; that skill owns instruction-file write mechanics (shared-source generation, do-not-edit markers, incremental diff-before-write). Write modes respect generated-file markers, edit source fragments instead, and delegate the file write when the skill is present; direct writes only when neither applies.

### Changed
- `skill.json`: `capabilities.filesystemWrite` true (write modes), description covers the write modes and enforcement classification, tag `bootstrap` added.
- Caveats: "read-only" narrowed to audit mode; write modes write only spec-preview-authorized artifacts.

## [0.2.0] - 2026-07-02

### Added
- Seventh AGENTS.md constraint dimension (**Implicit Dependencies**) wired into the audit procedure: SKILL.md's constraint-dimension list and the report template now audit all seven dimensions, with a single-source pointer to `agents-md-spec.md`.
- Implicit-dependencies standing indicator in the Invariant layer (SKILL.md diagnose lines + `seven-layer-checklist.md` Layer 2): global/mutable state, undeclared side effects, and environment dependencies must be declared or eliminated — a top agent-era bug source (per agents-md-spec Dimension 7).
- "Two Frameworks: Seven Layers and Six Axes" section: the seven-layer model IS this skill's audit procedure, while the six-axes file is the entropy suite's shared vocabulary this skill only applies when aligning findings with `repo-entropy-audit`/`entropy-review`; includes an orientation-only seven-layers→six-axes alignment line.
- Severity-semantics note near Priority Actions (SKILL.md + report template): Priority Actions are repo-setup improvements outside the P0/P1/P2/Note change-review scale; blocking gaps route through `entropy-review` E-grades and the `risk-adaptive-cross-review` finding-contract Severity Crosswalk.
- Missing per-layer diagnose indicators: Permission "tool permissions scoped to task", Sensorium "each milestone has a verification command", Governance "autonomy based on layer coverage".

### Changed
- Recalibrated `metric-definitions.md` for the agent era: import-source diversity (files importing from > 5 unrelated top-level modules) is now the primary structural diagnostic; max file size is demoted to a secondary informational signal (1000+ lines informational, never a red flag alone). Added an agent-era calibration note mirroring `six-entropy-axes.md`, and updated the "trivially automatable" list.
- Reframed the boundary with `repo-entropy-audit` as depth-vs-breadth on the context/protocol/control axes (this skill deep-dives those three; `repo-entropy-audit` covers all six at breadth; code structure/semantics/behavior stay `repo-entropy-audit`'s territory), replacing the overstated "control system vs code" split.
- Governance Layer 7 is now explicitly informational (ℹ️) in `seven-layer-checklist.md` and its Usage glyph list, matching SKILL.md's treatment. Added a legend note that coverage glyphs grade layer coverage, not finding severity (distinct from `entropy-review` verdict glyphs).

### Fixed
- Removed all four dangling `dependency-audit` references (SKILL.md frontmatter, When Not To Use, Tool Recommendations; report template), routing dependency concerns to the real `entropy-review` / `repo-entropy-audit` skills. Added `repo-entropy-audit` (full-repo code/entropy scan) to the frontmatter Do-NOT list.

## [0.1.1] - 2026-07-02

### Changed
- Declare the sharing contract at the top of `six-entropy-axes.md`: the six axes are the entropy suite's canonical abstract vocabulary (consumed by `repo-entropy-audit`, `entropy-review`, and this skill), while detection methods stay per-object — the repo scan, the diff review, and this skill's seven-layer control-system model instantiate the axes at their own granularity by design. Also qualify the `docs/decisions/agent-era-metric-recalibration.md` link as a repo-level doc not shipped with standalone installs.

## [0.1.0] - 2026-05-24

### Added
- Seven-layer control stack audit (Memory, Invariant, Protocol, Permission, Sensorium, Evaluation/GC, Governance)
- AGENTS.md constraint dimension audit (glossary, dependency rules, error model, naming conventions, doc freshness, state model references)
- Structured report output with coverage status, gap analysis, and prioritized improvement recommendations
- Shared methodology references: six entropy axes, seven-layer checklist, AGENTS.md spec, metric definitions
