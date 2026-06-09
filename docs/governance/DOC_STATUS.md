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
   current only where they have been reconciled by current issue evidence such
   as #368.
5. **Architecture/spec documents**: specs define intended contracts when current
   code or generated artifacts do not already settle the question.
6. **Module decomposition docs**: module docs are implementation guidance and
   lose to current specs, current runbooks, validation evidence, and code.
7. **Historical baseline, superseded, and archived documents**: use these only
   for audit context unless they point to a current source.

If two current sources at the same level conflict, prefer the more specific
source for the affected role, path, or issue. Record the conflict in the next
docs or OpenSpec change rather than silently choosing a stale fact.

## Agent And Artifact Ownership

- Tracked `.agents/skills/**` are reviewed project assets. New or changed
  project skills require normal PR review before they are treated as governed
  repository assets. Local installed skills and scratch skill work remain
  local/generated unless a later PR explicitly promotes them.
- Unpromoted `.agents/skills/**` additions remain ignored as local/generated
  installed or scratch skill copies. Promoting a new project skill file
  requires intentional force-add and PR review.
- `.codex/tmp/`, `.codex/cache/`, `.codex/evidence/`, and new
  `.codex/reviews/**` files are local/generated workflow evidence by default.
  Existing tracked `.codex/reviews/**` fixtures remain historical project
  evidence; they do not make future generated review outputs tracked by
  default.
- Existing tracked `apps/frontend/artifacts/m11-*.png` files remain historical
  visual project evidence. New files under `apps/frontend/artifacts/**` are
  local/generated visual evidence by default unless a later issue or PR
  explicitly promotes them.
- Root `artifacts/` remains local/generated production or review evidence and
  stays ignored. `services/artifacts/*.py` is source code and stays trackable.
- Docker build context excludes non-runtime agent/evidence paths including
  `.agents`, `.codex`, and `apps/frontend/artifacts`.

## Current Notes

- `IMPLEMENTATION_PLAN.md` at the repository root is a historical / superseded
  baseline. Use current entrypoints, active OpenSpec changes, runbooks, and
  validation matrices for implementation decisions.
- High-impact node-27 MVT facts and display env/compose guidance were
  reconciled by #368.
- `docs/bugs.md` is governed as a status ledger after #369 triage.
- `.agents`, `.codex`, `apps/frontend/artifacts`, and root `artifacts/`
  ownership policy is defined above by path family.
