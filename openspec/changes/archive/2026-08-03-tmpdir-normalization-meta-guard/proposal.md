# Enforce the run_docker_smoke TMPDIR-normalization invariant with an in-file AST meta-guard (#1211)

## Why

`openspec/specs/two-node-docker-runtime/spec.md:109` asserts a
file-wide invariant: in `tests/test_two_node_docker_runtime.py`,
EVERY test that invokes `run_docker_smoke` normalizes the process
`TMPDIR` into the approved evidence root. Its only acceptance oracle
ever run was a one-shot grep on the day PR #1210 delivered 8/8
(archived change `2026-08-01-fix-classc-smoke-tmpdir-cleanup`,
tasks 2.1). The invariant has already been broken once in the wild:
#1126 shipped 7/8 and the missing site
(`test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`)
was found by a human review side-sweep, not by automation
(→ #1128 → PR #1210).

When a site is missed, all three execution paths give wrong signals
(issue #1211, verified): GitHub CI never exports `TMPDIR`, so
`_approved_preflight_tmpdir`
(`scripts/validate_two_node_docker_runtime.py:5286-5289`) falls back
to the approved root and the test stays green; macOS skips the
Class C cases via the `/scratch/frd_muziyao` skipif; only node-22
reds — and it reds with the misleading `BLOCKED != PASS` diagnostic
(the #1106 failure shape), pointing at scratch-layout contracts
instead of environment hygiene.

## What Changes

One in-file AST meta-test (~15 lines, the issue's recommended route)
in `tests/test_two_node_docker_runtime.py`:

- Parse this file's own source (`ast.parse` of the UTF-8-read
  `__file__`), walk all function definitions (sync AND async —
  `asyncio_mode = "auto"` would collect a future async test the
  guard must not miss), and collect every function whose body calls
  `run_docker_smoke` (matched by callee name, so both
  `docker_runtime.run_docker_smoke(...)` and a bare call match; the
  guard itself references the name only as a string, so it can never
  match itself).
- Assert each collected function's body also contains a
  `monkeypatch.setenv("TMPDIR", ...)` call (matched as an `.setenv`
  attribute call whose first argument is the constant `"TMPDIR"`)
  whose second argument carries the approved-root shape (its
  `ast.unparse` contains the bare substrings `artifacts` and `tmp`
  — needles carry no quotes; unparse renders single-quoted string
  literals) — presence
  alone must not green: the file's own `:3809` deliberately sets
  `TMPDIR` to the unapproved `"/tmp"`, and a smoke test copying
  that idiom would re-create the node-22 `BLOCKED != PASS` shape.
- Assert the collected set is non-empty (an AST-matching bug that
  collects nothing must red the guard, not green it).
- Failure message lists the offending function names — the guard
  reds on macOS/CI/node-22 identically (pure static analysis; NO
  skipif, no `/scratch` dependency).

Explicitly NOT adopted (issue's alternative): the shared
`normalized_tmpdir` fixture route — it touches 10 green test bodies
and is itself not a guard (a 9th case can still forget to declare
it); and any AUTOUSE normalization is forbidden outright because
`test_preflight_defaults_tmpdir_to_repo_artifacts` (`:3779`) and two
siblings (`:3857`/`:3885` delenv sites) deliberately UNSET `TMPDIR`
to verify the production fallback contract — an autouse fixture
would fake-green or red them.

Out of scope: the 8 existing `run_docker_smoke` tests' assertion
sets and verdict semantics (zero edits); `scripts/
validate_two_node_docker_runtime.py` production logic (fallback
policy, approved-root whitelist); `.github/workflows/ci.yml`;
skipif guards; `tests/test_two_node_docker_source_trust.py`
(tracked by #1127/#1209).

## Impact

- Affected code: `tests/test_two_node_docker_runtime.py` only (one
  added meta-test; zero edits to existing tests). Final surface
  checked against `git diff master...HEAD --name-only` at evidence
  time (expected: that file + this openspec change).
- Affected specs: `two-node-docker-runtime` (1 ADDED requirement:
  the TMPDIR-normalization invariant is machine-enforced in-file).
- Frozen surfaces (zero diff): `scripts/validate_two_node_docker_runtime.py`,
  `.github/workflows/ci.yml`, all existing tests in the target file.
