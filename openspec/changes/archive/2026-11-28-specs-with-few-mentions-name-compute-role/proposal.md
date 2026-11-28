# The specs with one to three mentions name the compute role (#3428)

Slice 10.1 of `openspec/changes/sweep-compute-role-narration` (#3345, epic #3217). The umbrella
change is the design of the sweep (`design.md` §1 vocabulary, §3 rules); this change carries the
deltas of one slice, generated from the main spec text of 2026-11-28.

## Why

"node-22" is the codename of the compute role. Until 2026-11-23 it was also the host that carried
the role, and 31 capability specs still use it in one to three sentences as if it were a place:
"node-22 SHALL run a daily user-level oneshot unit", "the node-22 clone CLI", "on node-22 or
node-27". The role runs on `w3` (the compute control host) and `w2` today, so a reader of these
sentences is sent to a machine that no longer does the work.

## What Changes

- **47 sentences in 31 specs** use the terms of `openspec/glossary.md` ("Topology terms"):
  `sentences.md` lists every one with its text before and after and the term chosen.
  - 39 are requirement text in 34 requirements of 29 specs, each replaced whole by a MODIFIED block
    (35 blocks in 30 specs: one more requirement, in `python-environment-truth`, changes in its title
    only).
  - 8 are Purpose lines, which a delta cannot carry; they are edited in the main spec in the push
    that archives this change. `filesystem-permission-determinism` changes in its Purpose only and
    has no delta.
- **Three requirement titles are renamed** (RENAMED delta), as `appendix-titles.md` of the umbrella
  assigns to this slice:
  - `forcing-copyback-backfill`: "Node-22 operator documentation describes execution and recovery"
    becomes "Operator documentation of the compute role describes execution and recovery".
  - `python-environment-truth`: "Active node-22 entrypoints preserve the deferred environment"
    becomes "Active entrypoints of the compute role preserve the deferred environment".
  - `real-shud-slurm-execution`: "Node-22 Slurm submission path" becomes "Slurm submission path of
    the compute role".
- **Ten sentences are about the retired host and say so** (`design.md` D3): they keep the codename
  inside "the retired node-22 host".

## What does not change

- No requirement is added, dropped or weakened; no scenario is added, dropped or retitled. Every
  MODIFIED block is its whole requirement with the listed fragments replaced and nothing else.
- Capability names, and every name that carries the codename (script, unit, runbook, env key, change
  names in a placeholder Purpose).
- The specs of the other tasks: the contract specs and the specs named after the codename (10.2),
  the scheduler and pipeline specs (10.3), `ops-monitoring-api`, `display-readonly-ops-ui`,
  `node22-unit-tracking`, `evidence-boundary-hardening`.
- Code, tests, runbooks. No test reads a sentence of this slice (`design.md` D6).

## Impact

- `openspec/specs/**`: 31 files. The counter's `openspec/specs/` row goes from 186 / 1 / 36 to
  147 / 1 / 40 (bare / quoted / name), the bare-22 shorthand from 3 to 2 (`design.md` D7).
- Nothing is deployed and no behaviour changes.
