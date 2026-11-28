# Design: the specs with one to three mentions name the compute role (#3428)

## Context

The work list is Appendix B of the umbrella's `appendix-inventory.md`: the specs with one to three
mentions of the codename, minus the specs tasks 10.2 and 10.3 name and the specs another task owns,
plus `hydro-result-ingest-and-publish` ("node 22") and `multibasin-product-discovery` ("22 节点"),
which the appendix misses. Counted on master `44b706973` with the umbrella's `narration_count.py`
classifier: 31 specs, 50 lines (47 sentences and 3 requirement titles).

No other active change carries a delta for any of the 31 capabilities
(`fix-node22-keliya-forecast-output-recompute` has `job-retry-mechanism` and
`production-scheduler-orchestration`, both task 10.3).

## Decisions

### D1. The deltas are generated, and the result is compared with the expectation

One table holds, per capability, the fragments to replace and their replacements (`sentences.md` is
rendered from it). The generator

1. asserts that each old fragment occurs exactly once in its main spec and that no spec's line count
   changes;
2. writes the expected main spec (the spec of the day with the replacements applied);
3. cuts both texts into head (title and Purpose) and requirement blocks, and writes every block that
   differs, whole, into the capability's MODIFIED section, with a RENAMED entry where the title
   differs;
4. after `openspec archive`, applies the Purpose replacements to the main specs and compares every
   main spec with its expectation.

A rehearsal on a copy of the specs tree gave: 34 modified, 3 renamed; 27 of the 31 main specs
byte-identical to the expectation, 4 differing in blank lines only (D5).

### D2. The term is chosen by what the sentence is about

`sentences.md` gives the term of each sentence. The rule is the umbrella's (`design.md` §1):

- **the compute role** (29 sentences): who does or owns it. The scheduler, the clone CLI, the model
  succession tool, the manual-repair marker lane, the manual-retry script, the backfill command, the
  Slurm gateway and the host submission path, the writer of the copyback tree.
- **the compute control host** (5): the one machine where the units run and an operator acts. The
  two daily oneshot units of `file-state-snapshot-index`; the unit directory and "a fresh" host of
  `scheduler-evidence-retention`; the operator who follows a runbook (`adapter-cycle-hours`).
  The production user's user manager runs on that host only
  (`docs/runbooks/w2-w3-compute-topology.md` §1).
- **the compute hosts** (3): both machines; paths, accounts, the shared environment.
  `/volume/nwm/Basins` (`mapping-builder-cli`); the group `nwmuser`, gid 1107, of the writer account
  (`node27-raw-retention`; `docs/runbooks/production-ops/artifacts-and-storage.md`, the section on
  the canonical mirror's permissions, names the writer as the account on the compute hosts; read on
  2026-11-28: `getent group 1107` gives `nwmuser` on `w3`, on `w2` and on node-27, and the production
  user is a member on both compute hosts); the environment the Purpose of `python-environment-truth`
  protects.
- **the retired node-22 host** (10): D3.

A term stays on one source line; no term is hyphenated into a compound; a name that carries the
codename is kept in code font where the sentence had it.

### D3. Ten sentences are about the retired host

| Spec | Sentence | Why it is the retired host |
|---|---|---|
| `file-orchestration-journal` | the do-not-connect PostgreSQL `:55433` | the umbrella's rule for `:55433`; "archived/stopped" and the port stay on the line, which the topology gate's allowance needs |
| `filesystem-permission-determinism` (Purpose) | "the same code produced `0o755` on … and CI" | what the code produced before the fix, on that host; the sentence had no date, so "recorded 2026-08-20" is the day it entered the spec (`ca0fad788`) |
| `real-slurm-gateway-contract` | "the host timezone is east of UTC (the production node-22 case)" | no longer the production case: both compute hosts run on UTC (`date +%z` gives `+0000` on each, read 2026-11-28). The scenario was added for the UTC+8 host on 2026-08-18 (`765b8b025`) |
| `scheduler-pass-timing-instrumentation` | "13 basin models × 2 sources × 5 stages (the canonical node-22 workload)" | a figure of that host's workload; the text is in the repository's first commit (2026-07-25), so "recorded no later than 2026-07-25". Whether 13 models is still the workload is not claimed |
| `direct-grid-readiness-evidence` (3 sentences) | where the evidence package was staged, the `shud` binary path used, the runtime host recorded | the requirement is met by one completed evidence set (`openspec/changes/archive/2026-07-10-cmfd-direct-grid-platform-readiness/evidence/`), whose staging log names that host and was taken 2026-07-07 |
| `runtime-evidence-and-operations` (2 sentences) | "deployed to a quiescent node-22 runtime"; "no … DB connection from node-22" | the scenario is the one controlled recovery of #2439 (cycles 2026-09-14T00Z and T12Z); its archived evidence (`openspec/changes/archive/2026-09-17-preserve-pre-forecast-retry-stages/evidence/`) is from that host. Written with a role term the sentences would say that the proof ran on the hosts of today |
| `z-policy-verdict` | "the node-22 forensic command" | the verdict evidence (`openspec/changes/archive/2026-11-15-direct-grid-build-enablement/evidence/z-policy-solver-audit-verdict.md`) records it as a command run against that host |

**Rule for a requirement that a completed, one-off evidence set meets:** where the archived evidence
names the host, the sentence is a statement about the retired host and says so. The requirement is
not rewritten as if the evidence would be produced again on the hosts of today. **A normative clause
is never addressed to the retired host** (`openspec/glossary.md`: "no current operation is addressed
to it"; `production-topology-contract`: the host is kept as a rollback archive only): where the
codename stood in a SHALL clause, the clause loses the host and the host is given as a dated record
in brackets. That is the one SHALL of `direct-grid-readiness-evidence`: "SHALL stage … the evidence
package (staged on the retired node-22 host, 2026-07-07) and verify …".

### D4. Titles

The three titles of `proposal.md`. Each is renamed by a RENAMED entry and its requirement follows as
a MODIFIED block under the new title. Outside the archive, the only references to the three titles
are the umbrella's own: its two appendices, and `design.md` §3, which quotes "Node-22 Slurm
submission path" as its example of a title to rename.

### D5. Purpose lines and blank lines

Eight Purpose lines are edited in the main spec after `openspec archive`, in the same push.

`openspec archive` re-spaces the headings of a spec it rewrites: in the rehearsal four specs
(`adapter-cycle-hours`, `forcing-copyback-backfill`, `hydro-result-ingest-and-publish`,
`node27-window-admission`) differ from the expectation in blank lines and in nothing else. Their line
numbers shift by one or two. A hard-gate finding in one of them that moves with its line is listed in
the PR as a move, not as a new finding.

### D6. Tests and strict validation

No test reads a sentence of this slice: the four test files that name one of the 31 specs name it in
a docstring, and a search of `tests/`, `scripts/` and `docs/` for each old sentence finds no quote.

**Twelve of the 31 specs fail `openspec validate --type spec --strict` today and will fail it after
this change, for one reason this slice does not touch:** their Purpose is the placeholder
`openspec archive` wrote ("TBD - created by archiving change …"), which the validator reports as a
warning. 150 of the repository's 280 main specs fail strict validation today. Writing a Purpose is
authoring, not a change of narration, and is not done here. The acceptance is therefore: the structured
validation report (`--json`) of each of the 31 specs lists the same issues before and after; 19 are
valid before and after. This replaces the acceptance line of #3428 ("each changed main spec passes
… after archive"); the issue body is amended and the PR records it as a deviation.

### D7. The counter

`openspec/specs/`: 186 / 1 / 36 → 147 / 1 / 40. 49 bare lines are rewritten; 10 of them still carry
the codename inside "the retired node-22 host" and stay `bare` (39 fewer). Four lines keep a name in
code font next to the rewritten phrase and become `name` lines (the clone CLI twice, the
manual-retry script twice): the `name` line count rises by 4 and no name token moves. The
"22 节点" line of `multibasin-product-discovery` is not a `bare` line; the bare-22 shorthand count
goes from 3 to 2.

## Risks

- `direct-grid-readiness-evidence` describes a completed gate. Its SHALL clause no longer names a
  host; the two scenario lines that say what the evidence records name the retired host, which is
  what the evidence holds.
- The term "the compute control host" in `file-state-snapshot-index` binds the two units to one
  machine, which is what the topology runbook requires (the user manager is masked on `w2`).
