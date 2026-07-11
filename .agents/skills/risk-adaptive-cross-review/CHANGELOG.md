# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] - 2026-07-11

- Tighten the hybrid trigger description from 489 to 387 characters (slimming batch 6), eval-gated by the new cross-skill routing suite (`skill-lifecycle-manager/eval/cross-skill-routing-cases.json`): three A/B runs, zero per-case routing regressions, candidate 27/27 on the final run (deepseek-v4-pro-guan judge via dmxapi). All negative redirects preserved.

## [0.4.0] - 2026-07-02
- Add a **Severity Crosswalk** section to `finding-contract.md` mapping sibling pack vocabularies into the canonical P0/P1/P2/Note: `review` P0/P1/P2 map 1:1 and P3/Nit → Note; `entropy-review` E0 → P1 (P0 when it breaks a selected risk-pack invariant), E1 → P2, E2 → P2, E3 → Note; plus a dimension→failure-class map (naming drift → `conventions`, error-model fork → `contract`, state split → `state-transition`, duplication → `reuse`, dependency-direction violation → `altitude`). Lets consumers fold sibling grades into this contract without re-deriving them.
- Add `invocation_posture: hybrid` to `SKILL.md` frontmatter, matching the sibling `review` and `entropy-review` skills.
- Fix the under-specified output template: each finding now points at all ten finding-contract fields instead of four, and the merge-blocking field list in `SKILL.md` adds the missing **Consequence**.
- Give Risk Triage a concrete Low/Medium discriminator — Medium when the change touches more than 3 files, more than one module, or any public/exported API; otherwise Low — so the mandatory removed-behavior audit (`reviewer-packages.md`) no longer flips on a fuzzy boundary. High enumeration unchanged.

## [0.3.0] - 2026-06-18
- Add a closed **Failure-Class Vocabulary** to `finding-contract.md` (implementation/code classes plus spec/OpenSpec-artifact classes plus `other`) so the `Failure class` field draws from a whitelist; this makes dedup, failure-class synthesis, and cross-run logging consistent across reviewers and the workflows that consume this skill. Adapted (clean-room, with author permission) from the stellarlinkco/skills `code-review` category taxonomy and broadened to cover the OpenSpec review modes.
- Add a **Reject When (Precision Gate)** to `finding-contract.md`: explicit precision rules (no speculative/unanchored/style-only findings; no "add guards" without naming the failing input and wrong result; no race without naming shared state and concurrent access) that turn weak items into non-blocking notes instead of blocking findings.
- Add an **Oracle Integrity** rule to `finding-contract.md`: the spec/fixture, acceptance criteria, existing tests, and project rules are the immutable oracle a finding is measured against — never weaken them to clear a finding, and a merge/exit gate is a deterministic check against that frozen oracle, not a "probably fine" verdict.
- Add a **Gap Sweep** step to `SKILL.md` synthesis: one final clean-slate pass for real defects the recall-biased first passes systematically miss (removed behavior, contract drift, boundary/error/async/auth/migration/cache/wrapper paths), verified through the same standard and the Reject When gate. Wire the vocabulary, precision gate, and oracle rule into the Finding Contract pointer.
- Drop the stale `codeagent-wrapper` dependency (skill.json `requirements.tools`) and the `codeagent-wrapper --backend codex` parallel-execution instruction in `SKILL.md`, replacing it with the orchestrator's native parallel-subagent mechanism and a read-only reviewer-leaf boundary. This aligns the canonical review skill with the native-subagent migration already done in `subagent-workflow` and `stage-change-pipeline`, which consume it.

## [0.2.0] - 2026-06-18
- Add a "Cross-Cutting Review Lenses" section to `reviewer-packages.md`: change-triggered lenses (removed-behavior audit, wrapper/proxy faithfulness, altitude/ownership) that named reviewers apply on top of pack scope, each tagged with owning reviewers, triggers, and failure classes, and bound to the existing finding contract. Adapted (clean-room, with author permission) from the stellarlinkco/skills `code-review` finder angles.
- Promote the detailed per-reviewer checklists into `reviewer-packages.md` as the canonical "Reviewer Checklists" (Correctness, Integration, Security/Performance, Test & Evidence, Spec Compliance, Invariant/State-Machine/Compatibility), so workflows inline them instead of forking their own copies. This is the single source `subagent-workflow` Phase 4 now consumes.
- Wire the lenses into `SKILL.md` reviewer-pack guidance and into the per-reviewer prompt requirements.

## [0.1.1] - 2026-06-15
- Update stale cross-reference: `codex-codeagent-workflow` -> `subagent-workflow` (skill renamed).

## [0.1.0] - 2026-05-25
- Initial risk-adaptive cross-review skill.
- Supports PR/diff/branch review and OpenSpec/stage-change review modes.
- Adds reviewer-pack selection, actionable finding contracts, and failure-class synthesis references.
