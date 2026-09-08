# #2137 fixture review — 2026-09-08

## Scope

Read-only expanded/high fixture and issue-alignment review for the atomic 96-file
#2137 local G0 child. This is not implementation review and contains no node-27
or node-22 execution.

## First verdict

`revise`

The reviewer found that proposal/migration/task 4.0 named #2137, but the risk-pack
evidence mapping, Invariant Matrix, capability requirement, bring-up C4 checklist,
G0 merged-content list and parent #1895 issue still described the old monolithic
live rollout. In particular, the checklist could direct an operator to use legacy
`e2e/monitoring.spec.ts` as C4.

## Repair mapping

- `tasks.md` now separates task 4.0 local ownership from task 4.1-4.8 live work,
  adds the G0 seam, and maps every selected pack to census/C1-C3/G8 owners and
  CLIs, schemas/binders, shared file primitives, readonly compatibility and CI
  selection/removal-mutant evidence.
- `design.md` now includes #2137 and promoted #2123 C4 in producers, validators,
  entrypoints, failure paths, evidence boundaries, regression rows and boundary
  surfaces. It explicitly states that local evidence cannot claim live PASS.
- `specs/compressed-chunk-cold-residency/spec.md` now requires merge-before-access,
  current identity-bound C1-C3 evidence, C4 consumption without duplication,
  readonly seam compatibility and assertion-bearing CI partitions.
- `docs/runbooks/node-27-bringup-checklist.md` now names
  `test:e2e:live-c4-display` as the only C4 lane and removes legacy monitoring and
  #389/#342 work from this window's C4 checkbox.
- `docs/runbooks/tier-node27-timeseries-storage.md` G0 now lists #1970, #2123,
  #2130 and #2137 as merged prerequisites and marks Step 2 SSH as task 4.1+
  only, forbidden to #2137.
- Parent issue #1895 now depends on #1929/#1970/#2123/#2130/#2137, states the
  no-node-before-#2137 rule and carries a live-only task 4.1-4.8 PR boundary.

## Preserved boundaries

- #2137 remains local-only and does not execute census/probe/install/live C1-C4.
- Shared tasks 4.0-4.8 remain unchecked until their actual completion.
- The shared change remains active until #1895 live closure.
- Historical retro, preservation and extraction records remain unchanged.
- Protected path `2`, root `node_modules`, and
  `scheduler-cohort-init-state-visibility` remain outside the child.

## Re-review

`pass`

The same reviewer confirmed every first-verdict condition now reaches an
executable surface: task ownership, all selected risk packs, Invariant Matrix,
capability requirements, C4 checklist, G0 prerequisites, Step 2 boundary and
parent #1895 dependencies/PR boundary. No remaining fixture path can direct
#2137 to perform live work, run legacy `e2e/monitoring.spec.ts`, or reimplement
C4.

This is the second and final fixture-repair review. It did not review the
implementation, run tests or validation, modify files, or access either node.

After the verdict, the orchestrator ran
`openspec validate compressed-chunk-cold-tablespace-tiering --strict
--no-interactive`; the change was valid. Scoped `git diff --check` and the edit-
artifact scan also passed. No live or remote oracle was run.
