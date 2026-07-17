---
name: gh-create-issue
description: Create GitHub issues from PRDs, requirements, or feature descriptions, choosing between a single issue and an epic plus sub-issues using gh CLI labels and dependencies.
invocation_posture: hybrid
version: 0.4.0
---

# GitHub Issue Creation

## Role in Architecture

This skill directly creates GitHub issues from already supplied requirements. It does not implement the issues.

## Overview

Analyze PRD/requirements and create appropriate GitHub issues. Automatically determines complexity and creates either a single issue or an epic plus sub-issues.

The "epic + sub-issues" relationship is a **body-text convention** — sub-issues carry a `Part of #<epic>` line and the epic carries a `## Sub-tasks` checklist — not GitHub's native sub-issue API. Nothing here depends on that API.

## When to Use

- User provides PRD, requirements document, or feature description
- User requests creating GitHub issues
- Starting a new feature implementation workflow

## When Not to Use

- Do not use for implementing, reviewing, or merging an issue; use the appropriate implementation workflow.
- Do not use when requirements are still ambiguous enough to change the issue structure; clarify or run the upstream planning flow first.
- Do not use as a substitute for `stage-change-pipeline` when the user needs OpenSpec change creation, review, and implementation-ready issue contracts.

## Usage

**Invoke via skill call:**
```
Call gh-create-issue skill with:
  - input: PRD content or requirements text
  - output: Created issue numbers (epic + sub-issues if complex)
```

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| prd_content | Yes | PRD document or feature requirements |
| project_num | No | GitHub Project number to add issues to |
| labels | No | Additional labels (default: auto-detected) |
| stage_label | No | Milestone/phase label passed through to every created issue (e.g. `m0`, `p1`); useful when driven by `stage-change-pipeline` |

## Complexity Assessment

| Complexity | Criteria | Strategy |
|------------|----------|----------|
| Simple | Single feature, 1-2 days, no cross-module deps | Single issue |
| Medium | 2-5 subtasks, module dependencies | Epic + 3-5 sub-issues |
| Complex | Cross-team, multi-phase, 5+ subtasks | Epic + multi-level sub-issues |

**Auto-detect rule.** Create an **epic + sub-issues** when any of these hold:

- more than 5 distinct subtasks, **or**
- more than one module / package / directory boundary is touched, **or**
- any cross-issue dependency exists.

Otherwise create a **single issue**. When the signals are borderline or the split is unclear, ask the user rather than guessing.

## Wide Refactor Slicing

A **wide refactor** is one mechanical change — renaming a column, retyping a shared symbol — whose blast radius fans across the whole codebase, so no single-module issue can land green on its own. Don't force it into the standard epic decomposition; sequence it as **expand–contract**:

1. **Expand**: one issue adds the new form beside the old, so nothing breaks.
2. **Migrate**: move call sites over in batches sized by blast radius (per package / per directory), each batch its own issue with `Depends on #<expand>`. CI stays green batch to batch because the old form still exists.
3. **Contract**: one final issue deletes the old form once no caller remains, with a `Depends on` line for every migrate batch.

When even the batches can't stay green alone, keep the same issue sequence but state in each issue that the batches share an integration branch, all feeding a final integrate-and-verify issue — green is promised only there.

## Return Format

**Human-readable summary:**
```
Created Issues:
- Epic: #100 - [Epic] User Authentication Module
  - #101 - Login API implementation
  - #102 - JWT token management (depends on #101)
  - #103 - Permission middleware (depends on #102)

Labels: epic, feature, priority:high
Project: #1 - Development Board (added)
```

**Structured output:**
```json
{
  "epic_num": 100,
  "issues": [
    { "num": 101, "title": "Login API implementation", "priority": 2, "depends_on": [] },
    { "num": 102, "title": "JWT token management", "priority": 2, "depends_on": [101] },
    { "num": 103, "title": "Permission middleware", "priority": 2, "depends_on": [102] }
  ]
}
```

## Dependency Detection

**Canonical form:** one `Depends on #NN` line per dependency. This exact literal is what the downstream DAG reader in `subagent-workflow` greps for, so both detection and emission standardize on it.

**Explicit declaration (accepted inputs in issue body):**
- `Depends on #NN` → canonical form
- `Blocked by #NN`, `After #NN`, `Requires #NN` → accepted **input aliases only**; normalize each to a `Depends on #NN` line on emission

**Implicit inference (during PRD analysis):**
- Data layer before API layer
- API layer before UI layer
- Core modules before dependent modules

**Writing dependencies to the issue body:**

Emit one `Depends on #NN` line per dependency. Do not collapse multiple dependencies onto a single comma-separated line — the DAG reader matches per line.

```markdown
Part of #100

Depends on #101
Depends on #102

## Description
...
```

## Priority Mapping

| Label | Priority Value | Description |
|-------|---------------|-------------|
| `priority:critical` | 1 | Blocking other work |
| `priority:high` | 2 | Important, do first |
| `priority:medium` | 3 | Normal priority (default) |
| `priority:low` | 4 | Nice to have |

## Issue Templates

Full Epic and Sub-issue body templates live in [references/issue-templates.md](references/issue-templates.md) — read it before composing issue bodies.

**Agent-brief durability contract.** Issues feeding an automated workflow sit in a DAG while earlier issues reshape the codebase. Bodies must follow [references/agent-brief.md](references/agent-brief.md): durability over precision (interfaces, types, behavioral contracts — never file paths or line numbers), behavioral not procedural, independently verifiable acceptance criteria, explicit out-of-scope. Read it before composing agent-implemented issue bodies.

**Required vs optional fields.** When issues feed an automated implementation workflow (`stage-change-pipeline` Stage 5 → `subagent-workflow`), the following are **required** — the issue must be implementation-ready with no product decisions or requirement clarification deferred to the implementer:

- `Implementation Ready: yes`
- `Module / Scope` (a single module or ownership boundary)
- `In Scope` / `Out of Scope`
- `Current behavior` / `Desired behavior` (the behavioral contract per [references/agent-brief.md](references/agent-brief.md))
- `PR Boundary`
- one `Depends on #NN` line per dependency
- `OpenSpec change:` reference when the issue derives from an OpenSpec change

For standalone, manually-tracked issues these fields are optional — keep only the ones that add clarity.

## Labels

| Label | Purpose |
|-------|---------|
| `epic` | Epic-level task |
| `sub-task` | Sub-task of an epic |
| `feature` | New feature |
| `bug` | Bug fix |
| `priority:high/medium/low` | Priority level |
| `needs-followup` | Unresolved P0/P1 items carried into the issue body (e.g. when a pipeline review loop hits its cap) |
| `<stage_label>` | Milestone/phase passthrough (from the `stage_label` parameter, e.g. `m0`, `p1`) |

Bootstrap commands (idempotent `--force`): step 1 of [references/command-reference.md](references/command-reference.md).

## Command Reference

All creation uses the `gh` CLI. Order matters: bootstrap labels, create the epic, capture its number, then create sub-issues that reference it, and finally backfill the epic checklist.

The step-by-step recipes — label bootstrap, epic creation with number capture, heredoc sub-issues, checklist backfill, optional project add — live in [references/command-reference.md](references/command-reference.md). Read it before creating issues.

## Prerequisites

```bash
gh auth status  # Must be authenticated
gh issue list --limit 1  # Must have issue permissions
```

## Error Handling

| Error | Resolution |
|-------|------------|
| Not authenticated | Run `gh auth login` |
| No issue permission | Check repo permissions or token scope |
| Label not exists | Auto-creates required labels |

## Related Skills

- `stage-change-pipeline` - Creates reviewed OpenSpec changes and implementation-ready issue sets.
- `subagent-workflow` - Implements accepted issues through PR review and CI gates.
