# Tasks: the specs with one to three mentions name the compute role (#3428)

## Must preserve

- Every requirement and every scenario of the 31 specs: none added, dropped, retitled or weakened.
  A MODIFIED block is its whole requirement with the fragments of `sentences.md` replaced.
- Capability names and every name that carries the codename.
- The specs of tasks 10.2, 10.3, 8.1, 8.2, 9.2, 5.1 and 5.2.
- "archived/stopped" and `:55433` on the line of `file-orchestration-journal` that names the retired
  host's PostgreSQL.
- Code, tests and runbooks: untouched.

## 1. Deltas

- [x] 1.1 The substitution table: 47 sentences and 3 titles in 31 specs; each old fragment asserted to
  occur exactly once; `sentences.md` rendered from it.
- [x] 1.2 The generated deltas: 30 capabilities, 35 MODIFIED blocks (3 of them under a RENAMED title).
- [x] 1.3 Rehearsal on a copy of the specs tree: validate, archive, Purpose edits, comparison.

## 2. Archive

- [x] 2.1 `openspec archive specs-with-few-mentions-name-compute-role --yes` in the PR's last push.
- [x] 2.2 The eight Purpose lines edited in the main specs.
- [x] 2.3 Every main spec compared with its expectation: identical, or different in blank lines only
  (`design.md` D5).
- [x] 2.4 Task 10.1 ticked in `openspec/changes/sweep-compute-role-narration/tasks.md`.

## Evidence Floor

- `openspec validate specs-with-few-mentions-name-compute-role --strict --no-interactive` before the
  archive.
- After the archive: the comparison of 2.3 with its counts; for each of the 31 specs the issue list of
  `openspec validate <spec> --type spec --strict --no-interactive --json` equal to the one taken
  before any edit (`design.md` D6; the PR records the replaced acceptance line as a deviation);
  `openspec validate sweep-compute-role-narration --strict --no-interactive`.
- `git diff --stat`: the 31 main specs, the archived change, the umbrella `tasks.md`; nothing outside
  `openspec/`, and not the untracked local file `openspec/config.yaml`.
- The umbrella's "Must preserve" checks on the rewritten lines: no line ends in "compute" with the
  term continued on the next; no hyphenated compound with a term; every rewritten line that denies a
  database, listed (`runtime-evidence-and-operations`, `file-orchestration-journal`).
- `narration_count.py` plain and `--tokens` before and after: the `openspec/specs/` row as
  `design.md` D7 predicts, no other row changed except `history` (the archived change), no name token
  moved; the bare-22 shorthand count.
- Hard gate: the finding multiset (check id, path, line) identical before and after, or each
  difference listed and judged (`design.md` D5 for moved lines; a finding on a rewritten line is
  compared by its matched text as well).
- The static readers of specs and the archive, locally and on node-27: the
  `tests/test_entropy_audit_*.py` files, `tests/test_node22_entrypoint_invariant.py` with its python
  scan, `tests/test_python_environment_truth*.py`, `tests/test_role_boundary_static.py`, and the
  tests that read `openspec/specs`.
- `uv run ruff check --no-cache .`; markdownlint on the edited documents; CI green on every shard.

Suggested fixture level: none - spec text; no code, no test expectation changes.
Minimal mergeable slice: atomic - one generated change; the deltas, the Purpose edits and the archive land in one push so that no main spec is left half rewritten.
