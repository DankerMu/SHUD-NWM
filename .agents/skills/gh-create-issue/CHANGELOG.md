# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-14

### Added
- New `references/agent-brief.md` (canonical home): the **agent-brief durability contract** for issue bodies consumed by AFK implementer agents — durability over precision (interfaces, types, behavioral contracts; never file paths or line numbers, which rot while the issue waits in the DAG queue), behavioral not procedural, independently verifiable acceptance criteria, explicit out-of-scope against gold-plating. Adapted from `mattpocock/skills` v1.1.0 `triage` AGENT-BRIEF.md.
- Sub-issue template gains `## Current behavior` / `## Desired behavior` / `## Key interfaces` fields (replacing free-form `## Description` for agent-implemented issues); `Current/Desired behavior` join the required-fields contract for automated-workflow issues. Prototype-snippet exception carried over: decision-rich snippets (state machine, schema, type shape) may be inlined under `Key interfaces`.

## [0.3.1] - 2026-07-11

- Move the Epic/Sub-issue body templates to `references/issue-templates.md` and the gh command recipes (including label bootstrap) to `references/command-reference.md` (slimming batch 5). The required-fields contract, dependency rules, and label table stay in the body.

## [0.3.0] - 2026-07-11
- Add "Wide Refactor Slicing": when one mechanical change's blast radius spans the whole codebase and no single-module issue can land green, decompose as **expand–contract** — an expand issue (new form beside the old), migrate issues batched by blast radius each depending on the expand, and a contract issue depending on every batch; with an integration-branch fallback when batches can't stay green alone. Adapted from `mattpocock/skills` v1.1.0 `to-tickets`.

## [0.2.0] - 2026-07-02
- Add a runnable **Command Reference** section (`gh label create --force` bootstrap, `gh issue create --body-file - <<EOF`, epic-first then sub-issue ordering, issue-number capture via URL parsing or `gh issue view --json number -q .number`, optional `gh project item-add`).
- Standardize dependency **detection and emission** on one `Depends on #NN` line per dependency — the exact literal the `subagent-workflow` DAG reader greps; keep `Blocked by #NN` / `After #NN` / `Requires #NN` as accepted input aliases only. Replaces the old `**Dependencies:** #a, #b` emission.
- Extend the sub-issue template with the downstream implementation-ready contract fields (`Implementation Ready`, `Module / Scope`, `In Scope` / `Out of Scope`, `PR Boundary`, per-line `Depends on #NN`, optional `OpenSpec change:`); mark them required when issues feed `stage-change-pipeline` Stage 5 → `subagent-workflow`, optional otherwise.
- Add the `needs-followup` label and a `stage_label` passthrough parameter, plus a `--force` label-bootstrap block.
- Operationalize complexity auto-detect: epic + sub-issues when >5 subtasks, more than one module touched, or any cross-issue dependency; single issue otherwise; ask when borderline.
- Add YAML frontmatter `invocation_posture: hybrid` and `version`, align the frontmatter description with `skill.json`, and clarify that "epic + sub-issues" is a body-text convention (`Part of #`, checklists), not GitHub's native sub-issue API.

## [0.1.1] - 2026-06-15
- Update stale cross-reference: `codex-codeagent-workflow` -> `subagent-workflow` (skill renamed).

## [0.1.0] - 2026-05-25
- Initial canonical package for creating GitHub issues from PRDs or requirements.
- Supports simple issue creation and epic plus sub-issue structures using `gh` CLI.
