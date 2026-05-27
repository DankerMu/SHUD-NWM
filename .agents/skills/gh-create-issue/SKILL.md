---
name: gh-create-issue
description: Create GitHub issues from PRD or requirements. Auto-detects complexity - simple tasks create single issue, complex tasks create epic + sub-issues structure. Uses gh cli with unified labels.
---

# GitHub Issue Creation

## Role in Architecture

This skill directly creates GitHub issues from already supplied requirements. It does not implement the issues.

## Overview

Analyze PRD/requirements and create appropriate GitHub issues. Automatically determines complexity and creates either a single issue or epic + sub-issues structure.

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

## Complexity Assessment

| Complexity | Criteria | Strategy |
|------------|----------|----------|
| Simple | Single feature, 1-2 days, no cross-module deps | Single issue |
| Medium | 2-5 subtasks, module dependencies | Epic + 3-5 sub-issues |
| Complex | Cross-team, multi-phase, 5+ subtasks | Epic + multi-level sub-issues |

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

**Explicit declaration (in issue body):**
- `Depends on #xxx` or `Blocked by #xxx` → adds dependency
- `After #xxx` or `Requires #xxx` → adds dependency

**Implicit inference (during PRD analysis):**
- Data layer before API layer
- API layer before UI layer
- Core modules before dependent modules

**Writing dependencies to issue body:**
```markdown
Part of #100

**Dependencies:** #101, #102

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

**Epic:**
```markdown
## Overview
<feature description>

## Sub-tasks
- [ ] #101 Task 1
- [ ] #102 Task 2

## Milestones
<phase goals>
```

**Sub-issue:**
```markdown
Part of #<epic_number>

## Description
<task details>

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2
```

## Labels

| Label | Purpose |
|-------|---------|
| `epic` | Epic-level task |
| `sub-task` | Sub-task of an epic |
| `feature` | New feature |
| `bug` | Bug fix |
| `priority:high/medium/low` | Priority level |

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
- `codex-codeagent-workflow` - Implements accepted issues through PR review and CI gates.
