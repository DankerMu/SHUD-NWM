## Why
PR #2462 (issue #2080) shipped the node-27 coverage-freshness alert lane and left two verified P2 findings that the workflow's P2-note rule kept out of that PR. Both were verdicted CONFIRMED / FIX_NOW by an independent verifier; the user has now asked for them together.

- **#2465** — a display-module `ImportError` exits 2 (it happens in `config_from_env`, before any database work), but design D6 and runbook §11.2 both file "展示模块报错" under exit 3, so an operator whose `PYTHONPATH` is broken reads the exit-2 row and is routed to §11.4, the threshold-knob section, which cannot fix it.
- **#2466** — the two new units are absent from `scripts/node27_resource_governance.py` `DEFAULT_SERVICES`, so a disabled timer is invisible to the governance receipt. `tests/test_node27_resource_governance.py` states the convention three times over for the prior lanes.

## What Changes
- Runbook §11.2: the exit-2 row names the import-time display-module / `PYTHONPATH` case with its own first step; the exit-3 row is qualified as *observation-stage*.
- Runbook §11.3: a third branch for the class D0 was rewritten around — coverage rows populated but unlistable — which both existing branches currently dead-end on.
- Runbook §11.5: the governance-registration sentence §10.8 carries for the sibling lane.
- `scripts/node27_resource_governance.py`: register both units in `DEFAULT_SERVICES`; `tests/test_node27_resource_governance.py`: the tuple pin and the collector twin, mirroring the #1368 pair.
- `tests/test_node27_coverage_freshness_alert.py`: pin the import-time failure's exit code and structured stderr code.
- The archived change `openspec/changes/archive/2026-09-18-node27-coverage-freshness-alert/` gets a dated correction rather than a silent rewrite: the record of what was decided stays, with the drift named.

## Capabilities
### New Capabilities
None.
### Modified Capabilities
- `display-coverage-freshness`: gains a failure-stage attribution requirement and a unit-registration requirement.

## Impact
`scripts/node27_resource_governance.py` (one tuple), two test files, `docs/runbooks/current-production-ops.md` (§11.2/§11.3/§11.5), the archived change's `design.md`/`tasks.md` correction notes, and one spec delta. The only edit to `scripts/node27_coverage_freshness_alert.py` is its D3 journal-budget comment at `:106-110`, whose line attribution contradicts the node-27 measurement; every executable line is unchanged, because its behaviour is the deliberate one and is not in question.

`design.md` is exempt at this fixture level; the decisions are small enough to carry in `tasks.md`.

Issue type: bugfix
Fixture level: compact
Upstream suggested level: absent (two follow-up issues filed by `issue-scribe`, no `stage-change-pipeline` contract fields)
Repair intensity: medium
Blast radius: operator documentation plus one monitoring-inventory constant; no runtime behaviour changes
Selected risk packs: Config / project setup; Documentation / migration notes; domain: Operator alerting lanes / observer-observed predicate parity
Evidence floor: targeted pytest on both test files, `uv run ruff check .`, `openspec validate close-coverage-freshness-alert-followups --strict --no-interactive`, and a node-27 live check that the governance audit receipt now carries both units.
