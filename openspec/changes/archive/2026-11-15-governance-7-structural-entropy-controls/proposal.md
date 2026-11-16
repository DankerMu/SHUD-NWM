## Why

The 2026-06-23 entropy review shows that the immediate gate risk is low, but
the repository still has structural entropy sources that can keep growing:
large compatibility facades, production-closure aggregators, active OpenSpec
route/path drift, missing archive machine semantics, and missing scoped agent
context. The project needs governance controls that reduce future entropy
without deleting useful historical evidence or breaking compatibility.

## What Changes

- Add a repository source-file entropy budget: source files over 1000 lines
  enter mandatory governance, files between 500 and 1000 lines enter review,
  and explicit exemptions are required for generated/data/fixture-like files.
- Freeze growth of compatibility facades such as `services/orchestrator/scheduler.py`
  and `services/orchestrator/chain.py` through inventories, owner mapping, and
  guard tests before deeper extraction.
- Decompose production-closure evidence validation by lane so aggregators stop
  holding every Docker, DB, API/browser, logs, identity, dependency, and final
  readiness rule.
- Burn down active, budget-counted document entropy by updating canonical paths
  or adding machine-readable historical/compatibility markers instead of
  deleting archived evidence.
- Add archive/superseded document status semantics that narrow allowlists
  without globally ignoring archived material.
- Add scoped agent context and glossary coverage for the highest-entropy
  ownership boundaries.

## Capabilities

### New Capabilities

- `structural-entropy-file-budget`: Repository line-count and responsibility
  budgets for source files, including >1000 mandatory governance and 500-1000
  review semantics.
- `compatibility-facade-governance`: Compatibility inventory and guardrails for
  scheduler/chain facades and future facade-like modules.
- `production-closure-lane-decomposition`: Lane ownership and decomposition
  rules for production-closure evidence/readiness validation.
- `active-document-entropy-burndown`: Active OpenSpec/docs drift cleanup using
  canonical wording or machine-readable historical/compatibility markers.
- `archive-status-semantics`: Machine-readable archive/superseded semantics for
  historical documents and OpenSpec archive material.
- `scoped-agent-context-governance`: Scoped `AGENTS.md` and glossary coverage
  for high-entropy directories.

### Modified Capabilities

- None. Existing structural-burndown and entropy-baseline specs remain valid;
  this change adds a governance layer that constrains future work.

## Impact

- OpenSpec governance artifacts under
  `openspec/changes/governance-7-structural-entropy-controls/`.
- Future source changes in `services/orchestrator/`,
  `services/production_closure/`, `apps/api/`, and `apps/frontend/`.
- Governance scripts/tests around entropy reporting, large-file budgets,
  retired-path/route token classification, archive markers, and scoped
  instruction coverage.
- Active docs/specs that currently consume the 36 non-archive budget-counted
  findings from the 2026-06-23 report.
