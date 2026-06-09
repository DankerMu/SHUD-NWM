# Document Status Authority

This file defines how repository documents declare authority and how conflicts
between documents are resolved. It does not update stale facts by itself; it
only tells readers which source should win when two sources disagree.

## Statuses

| Status | Meaning | Typical paths |
|---|---|---|
| `current entrypoint` | A maintained orientation page for current project state, onboarding, or navigation. This status covers the page's role as an entrypoint; it does not assert every embedded milestone fact is freshly reconciled. | `README.md`, `progress.md`, `CLAUDE.md` |
| `current runbook` | Operational procedure that is current for a role, node, deployment, or validation lane. | `docs/runbooks/**` |
| `current validation matrix` | Maintained validation source of truth for what evidence is required or accepted. | `docs/VALIDATION.md`, active OpenSpec `tasks.md` |
| `architecture/spec` | Design contract for architecture, API, database, time semantics, operations, or product behavior. | `docs/spec/**`, `openapi/**`, `schemas/**`, `db/migrations/**` |
| `module decomposition` | Module-level design or implementation guide. It explains a slice, but does not override current specs, code, runbooks, or validation evidence. | `docs/modules/**` |
| `historical baseline` | Earlier plan, design-freeze note, receipt, or worklog kept for audit context. It may explain why the project moved in a direction but is not current guidance. | old root plans, worklogs, historical OpenSpec notes |
| `superseded` | A document or section whose guidance has been replaced by a named current source. | any path with an explicit superseded notice |
| `archived` | Historical material moved out of the active tree or clearly marked as archive-only. | `docs/archived/**` |

## Conflict-Resolution Order

When sources disagree, use the highest applicable source in this order:

1. **Merged source code, generated contracts, migrations, and tests**: runtime
   behavior and enforced contracts win over narrative docs.
2. **Current validation matrix and active OpenSpec tasks**: required evidence and
   issue acceptance criteria win over general onboarding text.
3. **Current runbooks for the affected role or node**: operational procedures
   win for live deployment, diagnosis, and receipt generation.
4. **Current entrypoints**: `README.md`, `progress.md`, and `CLAUDE.md` orient
   readers, but they do not override role-specific runbooks or validation
   requirements. For `CLAUDE.md`, use its workflow and entrypoint guidance as
   current, while treating embedded milestone/current-active sections as
   potentially stale until reconciled by the deferred stale-fact update in #368.
5. **Architecture/spec documents**: specs define intended contracts when current
   code or generated artifacts do not already settle the question.
6. **Module decomposition docs**: module docs are implementation guidance and
   lose to current specs, current runbooks, validation evidence, and code.
7. **Historical baseline, superseded, and archived documents**: use these only
   for audit context unless they point to a current source.

If two current sources at the same level conflict, prefer the more specific
source for the affected role, path, or issue. Record the conflict in the next
docs or OpenSpec change rather than silently choosing a stale fact.

## Current Notes

- `IMPLEMENTATION_PLAN.md` at the repository root is a historical / superseded
  baseline. Use current entrypoints, active OpenSpec changes, runbooks, and
  validation matrices for implementation decisions.
- High-impact stale fact updates for node-27 MVT facts and display env/compose
  are deferred to #368.
- `docs/bugs.md` triage is deferred to #369.
- `.agents` / `.codex` / artifact ownership policy is deferred to #370.
