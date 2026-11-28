# Tasks: the scheduler and pipeline specs name the compute role (#3430)

## Must preserve

- Every requirement and every scenario of the eleven specs, with its title, apart from the two
  renamed requirement titles.
- The four scenario titles and the requirement title of `design.md` D5, byte for byte.
- Capability names, identifiers in code font, every sentence the table of `sentences.md` does not
  list.
- The hard gate's reading of the eleven specs.

## 1. The superseded change

- [x] 1.1 `openspec/changes/fix-node22-keliya-forecast-output-recompute/` removed before the deltas
  are generated; what the removal alone moves in the counter and the hard gate recorded
  (`design.md` D6).
- [x] 1.2 The umbrella's sentences that call the decision open amended with the date of the
  decision.

## 2. Deltas

- [x] 2.1 The substitution table (`sentences.md`), each fragment occurring exactly once in its spec.
- [x] 2.2 The deltas generated from the main specs of the day: RENAMED for the two titles,
  MODIFIED with every changed requirement whole.
- [x] 2.3 Rehearsal on a copy of the specs tree with the final table: validate, archive, Purpose
  edits, comparison with the expectation, and the strict report of each spec before and after.

## 3. Archive

- [x] 3.1 `openspec archive` in the PR's only push; the six Purpose texts edited in the main
  specs; every main spec compared with its expectation.
- [x] 3.2 Task 10.3 ticked in `openspec/changes/sweep-compute-role-narration/tasks.md`.

## Evidence Floor

- `openspec validate scheduler-and-pipeline-specs-name-compute-role --strict --no-interactive`
  before the archive.
- After the archive: each of the eleven main specs equals its expectation (the spec before, with
  the listed replacements applied), blank lines aside; the same number of requirements and
  scenarios and the same scenario titles as before.
- Per spec, `openspec validate <spec> --type spec --strict --no-interactive --json` reports the same
  issues before and after (`design.md` D7: two of the eleven have a placeholder Purpose today).
- Hard gate: "before" is the base commit `78c3ba631`, "after" is the head with this change archived
  and the superseded change removed; no push carries this change unarchived. The finding multiset
  (check id, path, line) identical before and after; the topology check run on the specs tree alone
  before and after, with a proof that it reads these files (synthetic sentences that it flags).
- `narration_count.py` plain and `--tokens` before and after. The one `name` token that moves is
  the removed change's own name; no other.
- No line ends in a split term; no hyphenated compound with a term; the kept titles of D5
  byte-identical.
- A repository search outside `openspec/` for each old title and each old sentence; every hit is
  listed with what it is.
- No other active change holds a delta of a requirement this slice modifies.
- `openspec validate sweep-compute-role-narration --strict --no-interactive` after the tick and the
  amended sentences; `uv run ruff check --no-cache .`; markdownlint on this change's documents and
  the umbrella's `tasks.md` and `design.md`.
- node-27, on the pushed head: the static readers of the specs (every `tests/test_*.py` that names
  `openspec/specs` or `openspec/changes`, `tests/test_entropy_audit_*.py`,
  `tests/test_node22_entrypoint_invariant.py`, `tests/test_node22_entrypoint_invariant_python_scan.py`,
  `tests/test_python_environment_truth.py`, `tests/test_role_boundary_static.py`), no database, and
  the hard gate.
