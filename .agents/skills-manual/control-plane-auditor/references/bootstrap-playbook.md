# Bootstrap Playbook

Write-mode procedure for `control-plane-auditor` (Phase 4–5). Audit mode never reaches this file.

## Mode Decision

| Mode | When | Writes |
|------|------|--------|
| audit | default for every run | nothing |
| bootstrap | greenfield or sparse control plane: no root instruction file, no unified command entries, no guards | the initial scaffold set |
| repair | existing control plane with named gaps (usually a prior audit's Priority Actions) | only the named gaps |

Write modes require the user's explicit request. Phases 1–3 always run first — the audit report is the evidence that authorizes the spec preview. If Phase 1 finds a substantive control plane where the user expected none, stop and report instead of scaffolding over it.

## Spec Preview (gate before any write)

Present, then wait for confirmation:

```text
Bootstrap/Repair spec preview
Mode: <bootstrap | repair>
Deliverables:
- <file to create/modify> -> <which gap it closes> -> <gate it wires into (hook/CI/command), or "review-only">
Non-goals:
- <explicitly out of scope this round>
Readiness gaps that will REMAIN after this round:
- <gap> -> <why deferred>
```

Rules:

- Only deliverables listed in the confirmed preview may be written. Scope changes require a new preview.
- Fix in place. Never create parallel `_v2`/`_new`/`_backup` variants of an existing file.
- **No phantom enforcement**: every rule written must cite the real config, command, hook, or CI gate that enforces it, or carry an explicit `review-only` label. Writing an unenforced rule as if it were gated creates the worst gap class the audit knows.

## Root Instruction File Scaffold

For repos without a root AGENTS.md/CLAUDE.md (bootstrap) or with a boilerplate one (repair).

**Ownership split with `project-instruction-bootstrap`**: this skill decides the control-plane CONTENT (the sections below); `project-instruction-bootstrap` owns the instruction-file WRITE MECHANICS (shared-source generation, incremental merge, diff-before-write). Consequences:

- If the root instruction file carries a generated/do-not-edit marker, or the project maintains `instructions/agents/{shared,claude,codex}.md` sources, write the control-plane sections into the source fragments and regenerate via `project-instruction-bootstrap` — never edit the generated file directly.
- When `project-instruction-bootstrap` is installed, prefer delegating the instruction-file write to it, passing the sections below as required content.
- Write the file directly only when neither applies (no marker, no sources, skill absent) — and stay incremental: merge into what exists, never overwrite.

Sections, all short and directive:

```markdown
# <Project> — Agent Guide

## Project Snapshot
<2–4 lines: what this is, primary language/build system, the one thing an agent must not break>

## Command Entry Points
<setup/dev/check/lint/typecheck/test/build -> the real commands. No placeholders.>

## Verification Matrix
<surface> -> <command> -> <evidence type>
<one row per major surface, plus the default build+test row>

## Boundaries & Forbidden Actions
<readonly/generated paths; files agents must not edit; actions that require a human>

## Human Gates
<operations that always stop for explicit approval: deploys, migrations, deletes, publishing>
```

Placement of the verification matrix: when the project uses the OpenSpec delivery flow (`subagent-workflow` installed / `openspec/` present), the canonical home is `openspec/project-profile.md` — the template and size budget are owned by `subagent-workflow`'s `references/project-profiles.md`; the instruction file then links to it instead of duplicating rows. Otherwise inline the matrix in the instruction file as above.

## Guardrail Wiring

Recommend (and with authorization, install) mechanical guards matched to the gaps found:

- `worktree-guard` hook — write-fence for parallel worktree delegation.
- `large-file-guard` hook — commit-time ratchet against oversized files.
- Native git pre-commit / lint / typecheck wiring where the repo has none.
- CI required status checks for gates the repo claims but does not enforce.

Every wired guard becomes the `mechanical` citation for the rule it enforces; rules with no wired guard stay `review-only` in the instruction file.

## Guardrail Self-Test (Phase 5)

For each newly wired guard, trigger it once where safe and record the outcome:

- Hooks: perform a deliberate violating call in a scratch path (or dry-run mode where supported) and confirm the block plus the fed-back reason.
- Pre-commit/CI: run the check command against a synthetic violation and confirm the failure.

Then run the recorded check/test command entry points and capture exit status. Completion claims cite this evidence per deliverable; anything unverified is reported as a remaining readiness gap.
