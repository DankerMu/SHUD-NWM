# Tasks

Fixture level: `standard`. Risk packs: spec-to-code fidelity (requirement
text written after the fact must describe exactly what the code does, or a
later reader will "fix" the code to the spec); operator-facing environment
notes (a wrong statement about which component writes the database).

Must preserve: no change to production code or to any behaviour; existing
tests stay green unedited; the download template's keys and values (only
its comment changes); the README's existing statements about which unit
reads which file.

## 1. Spec (orchestrator)

- [ ] 1.1 `production-ops-readiness`: the monitor requirement added; the
  deferred-auth scenario replaced.
- [ ] 1.2 `output-parsing`: the non-finite requirement added.

## 2. Tests

- [ ] 2.1 `tests/test_live_monitoring.py`: one parametrised test over the
  five rows of the requirement's table that drives the alert builder with
  summaries satisfying exactly that row and asserts the alert's `alert`,
  `severity`, `runbook`, and that `threshold` is present or absent as the
  table says; a quiet-system case with no alert; the two mutual exclusions
  (an unreadable queue with a large last-known job count raises only
  `slurm_monitor_unavailable`; a scheduler summary that is not `ok` raises
  only `scheduler_evidence_missing` even with an old age); every `runbook`
  path of the five alerts is an existing file under the repository root.
  If the requirement's table disagrees with the code in any cell, stop and
  report the cell (the spec is corrected, not the code).
- [ ] 2.2 The 503 `RELEASE_BLOCKED` response: find the test that asserts
  it (status 503, code, `removal_criteria` in the details, and no
  mutation). If each of those four facts is asserted somewhere, list the
  tests; add only what is missing, in the file that owns the neighbouring
  cases.
- [ ] 2.3 The non-finite scenario: confirm the existing parser test covers
  `nan`, `inf` and `-inf`, the error code, the failed-run record and "no
  row written"; add only what is missing.

## 3. Environment notes

- [ ] 3.1 `infra/env/node27-download.example`: trace what the download
  chain does with `DATABASE_URL` (the entry script, the adapters it builds
  for each source, which tables they read or write) and rewrite the comment
  above `DATABASE_URL` to say exactly that. Keys and values unchanged. Any
  test that pins the comment text is updated and reported.
- [ ] 3.2 `infra/env/README.md` (orchestrator): the note on inert scheduler
  keys in `compute.host.env` / `compute.replay.env`.
- [ ] 3.3 `docs/runbooks/slurm-gateway-preflight.md` (orchestrator): the
  "Final readiness ..." sentence is cut.

## 4. Evidence

- [ ] 4.1 Mutations (by hand or script, restored and compared), each red
  on the 2.1 test: a severity changed; a runbook path changed to a file
  that does not exist; `threshold` added to `slurm_monitor_unavailable`;
  the `elif` of the queue branch turned into `if` (both alerts raised).
- [ ] 4.2 Local: `tests/test_live_monitoring.py`, the auth test file(s)
  touched or listed in 2.2, `tests/test_output_parser.py`, any test that
  reads `infra/env/node27-download.example`; `uv run --no-sync ruff check
  --no-cache .`; `openspec validate restore-spec-homes-after-lane-deletion
  --strict --no-interactive`; markdown lint of the edited documents; the
  entropy hard gate with 0 failing and master's finding count.
- [ ] 4.3 node-27, temporary detached worktree of the PR head: the test
  files of 4.2. Receipt on the PR.
