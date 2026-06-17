## Context

Issue #497 is part of #492 and depends on #495 and #496. Scheduler now hard-gates
business cycles to UTC `00/12`, and forecast strict warm-start now forbids
cold/latest fallback. GFS/IFS adapters still default to probing `00/06/12/18`,
and production docs still need an explicit artifact-location and strict
warm-start operations contract.

Risk triage:

- Issue type: feature / docs / production remediation
- Project profile: NHMS
- Blast radius: high
- Fixture level: expanded
- Repair intensity: high
- Why: external provider discovery, production env config, operator runbooks,
  scheduler/adapter consistency, and strict warm-start operational handling.

## Goals

- Allow GFS and IFS adapters to read configured UTC cycle hours from env.
- Production config should narrow adapter discovery/probes to UTC `00/12` while
  retaining the scheduler hard gate as the authoritative execution boundary.
- Document node-22/node-27 artifact locations accurately: object-store contains
  `runs/` and `forcing/`; `published/` contains only display tiles/logs/display
  manifests.
- Document strict warm-start checks and failure handling: verify the current
  cycle's exact successor checkpoint (`lead_hours=12`) and fix or rerun the
  producer `state_save_qc` checkpoint instead of cold-starting the next forecast.
- Provide operator verification commands for forcing packages, run output, state
  snapshots, scheduler evidence, and published display artifacts.

## Non-Goals

- No change to scheduler hard-gate semantics from #495.
- No change to strict warm-start runtime semantics from #496.
- No production command execution.
- No DB schema migration.
- No frontend changes.
