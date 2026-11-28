# Tasks: the contract specs and the specs named after the codename name the compute role (#3429)

## Must preserve

- Every requirement and every scenario of the seven specs, with its title, apart from the eight
  renamed requirement titles.
- The fragments other slices own or that define the codename (`design.md` D5), byte for byte.
- Capability names, identifiers in code font, every sentence the table of `sentences.md` does not
  list.
- The hard gate's reading of the seven specs.

## 1. Deltas

- [x] 1.1 The substitution table (`sentences.md`), each fragment occurring exactly once in its spec.
- [x] 1.2 The deltas generated from the main specs of the day: RENAMED for the eight titles,
  MODIFIED with every changed requirement whole.
- [x] 1.3 Rehearsal on a copy of the specs tree with the final table: validate, archive, Purpose
  edits, comparison with the expectation.

## 2. Archive

- [x] 2.1 `openspec archive` in the PR's only push; the three Purpose lines edited in the main
  specs; every main spec compared with its expectation.
- [x] 2.2 Task 10.2 ticked in `openspec/changes/sweep-compute-role-narration/tasks.md`.

## Evidence Floor

- `openspec validate contract-and-codename-specs-name-compute-role --strict --no-interactive`
  before the archive.
- After the archive: each of the seven main specs equals its expectation (the spec before, with the
  listed replacements applied), blank lines aside; the same number of requirements and scenarios
  and the same scenario titles as before.
- Per spec, `openspec validate <spec> --type spec --strict --no-interactive --json` reports the same
  issues before and after (`design.md` D7: one of the seven has a placeholder Purpose today).
- Hard gate: "before" is the base commit `cb27ddc70` (this change's directory absent), "after" is
  the head with the change archived; no push carries this change unarchived. The finding multiset
  (check id, path, line) identical before and after, and the
  topology check run on the specs tree alone before and after, with a proof that it reads these
  files (a synthetic sentence that it flags).
- `narration_count.py` plain and `--tokens` before and after; no `name` token moves.
- No line ends in a split term; no hyphenated compound with a term; the kept fragments of D5
  byte-identical.
- A repository search for each old title and each old sentence: no test, script or runbook quotes
  one.
- `openspec validate sweep-compute-role-narration --strict --no-interactive` after the tick;
  `uv run ruff check --no-cache .`; markdownlint on this change's documents and the umbrella
  `tasks.md`.
- node-27: the static readers of the specs and the hard gate on the pushed head.
