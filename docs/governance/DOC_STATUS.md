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

## Archive And Supersession Markers

Archived and superseded material is retained as evidence, not as current
instruction. A reader or agent must resolve the named current authority before
treating any preserved route, path, topology, environment, or validation text as
actionable.

The preferred whole-document marker is YAML front matter at the top of the
file:

```yaml
---
status: archived
current_authority:
  - path: docs/governance/DOC_STATUS.md
    section: Archive And Supersession Markers
    reason: document authority and marker semantics
superseded_by:
  - path: docs/runbooks/two-node-deployment-overview.md
    section: Current deployment topology
    reason: current runtime topology
status_since: 2026-06-24
archive_scope: whole-document
retained_for: audit evidence
---
```

For section-level archive material, or for files where front matter would break
tooling, use this standardized block immediately before the preserved text:

```text
Archive status:
- status: superseded
- current_authority: docs/governance/DOC_STATUS.md#conflict-resolution-order
- superseded_by: openspec/specs/single-map-shell-routing/spec.md
- status_since: 2026-06-24
- archive_scope: section
- retained_for: compatibility evidence
```

Required marker semantics:

- `status` is required and must be one of `historical baseline`, `superseded`,
  or `archived` for non-current material.
- `current_authority` is required whenever preserved text could look like a
  current route, path, topology, environment, validation, or operational
  instruction. It must name the current source path and, when helpful, the
  section or requirement.
- `superseded_by` is required for `superseded` material. It is also required
  for `archived` material when a known replacement exists. If no single
  replacement exists, use `superseded_by: none` and make `retained_for`
  explicit.
- `status_since` records the date the marker became valid.
- `archive_scope` must state whether the marker applies to the whole document
  or only the following section.
- `retained_for` explains why the stale-looking text remains in the repository.

Incomplete markers do not make text safe to ignore. Until the classifier learns
the marker semantics, and whenever required fields are missing, archived-looking
material remains visible for triage instead of being globally suppressed by
path.

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
   for audit context. If they contain archive or supersession markers, follow
   `current_authority` and `superseded_by` before using the preserved text.

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
  **mocked** visual evidence and are **not** node-27 live display proof. They are
  the 6 M11 route-review screenshots (basin + overview at 1280×900 / 1440×900 /
  1920×1080), introduced by PR #160 (`3e6fc48`, M11 route-review gap closure),
  produced under mocked Playwright visual regression. The M15 visual lane is the
  spec `apps/frontend/e2e/m15-visual-conformance.spec.ts` plus the explicit
  manual `.github/workflows/m15-visual-evidence.yml` workflow (pinned by
  `M15_EVIDENCE_SHA`); both are historical mocked visual evidence, not live
  display receipts (classification in
  `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`). Provenance and old paths are
  preserved by keeping these tracked assets in place; they are not moved by
  default. New files under `apps/frontend/artifacts/**` are local/generated
  visual evidence by default (ignored via `.gitignore`) unless a later issue or
  PR explicitly promotes them.
- Root `artifacts/` remains local/generated production or review evidence and
  stays ignored. `services/artifacts/*.py` is source code and stays trackable.
- Docker build context policy is documented in `.dockerignore`; it excludes
  non-runtime agent/evidence paths including `.agents`, `.codex`, and
  `apps/frontend/artifacts`.

## Current Notes

- `IMPLEMENTATION_PLAN.md` at the repository root is a historical / superseded
  baseline. Use current entrypoints, active OpenSpec changes, runbooks, and
  validation matrices for implementation decisions.
- High-impact node-27 MVT facts and display env/compose guidance were
  reconciled by #368.
- `docs/bugs.md` is governed as a status ledger after #369 triage.
- `.agents`, `.codex`, `apps/frontend/artifacts`, and root `artifacts/`
  ownership policy is defined above by path family.

## Display Route Authority (M26 single-map)

The current display frontend (`apps/frontend`) is converged to a **single-map**
entrypoint. This is the authoritative route status for current docs; the matrix
is defined by `apps/frontend/src/App.tsx`.

- **Active routes**: `/` (the single-map display entrypoint — overview, basin/
  segment drill-down, and river-flow popups all live here), plus the role-gated
  pages `/monitoring`, `/ops`, and `/system/model-assets`.
- **Legacy redirect aliases** (compatibility only; not active independent pages —
  they `replace`-redirect to `/` preserving search + added semantic params):
  `/overview`, `/hydro-met`, `/forecast` → `/`; `/meteorology` →
  `/?metStations=1`; `/flood-alerts` → `/?layer=flood-return-period`;
  `/basins/:basinId` → `/?basinId=…`; `/segments/:segmentId` → `/?segmentId=…`.
- Current entrypoint docs (`README.md`, `progress.md`, `CLAUDE.md`) must not
  present the legacy aliases as active independent pages. Remaining old-route
  mentions in current docs are valid only as historical, redirect-alias, or
  compatibility context. Dated `docs/plans/**` and historical runbooks are
  historical by definition and out of this current-entrypoint scope.
- Route-authority grep (current docs only):
  `rg -n '/hydro-met|/forecast|/meteorology|/flood-alerts|/segments/|/basins/' README.md progress.md CLAUDE.md`
  — every hit must be a redirect-alias/historical/compatibility mention, never an
  active-page claim.
