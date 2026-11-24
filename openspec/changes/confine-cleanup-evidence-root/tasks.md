# Tasks

Fixture level: `compact`. Risk packs: deletion safety (fail-closed), operator
contract of the cleanup CLI.

Must preserve: every existing test in `tests/test_cli_cleanup_frontier.py` and
`tests/test_retention_frontier.py` unchanged and green; an evidence root inside
the workspace behaves as today (frontier `ok`, deletion by bound); a missing
evidence directory still reports `evidence_dir_missing` with the probed path;
a blank `WORKSPACE_ROOT` still reports `evidence_dir_unresolved`; the cleanup
does not start requiring a lock root, an approved-roots list or a service
role; a workspace reached through a symlinked path prefix is still accepted.

## 1. Fix

- [ ] 1.1 `_cleanup_frontier` (`services/orchestrator/cli.py`): after the
  config is built and still inside the fail-closed guard, apply the pass-side
  evidence-root check to the derived root (`must_exist=True`,
  `allow_create=False`, `require_approved_root=False`,
  `require_under_workspace=True`, the config's workspace and raw evidence
  preflight paths). A blocker returns
  `FrontierReadResult(status="unavailable", reason="evidence_dir_unresolved")`
  and `None` for the directory, except the helper's `not_found` and
  `not_writable` verdicts when the same check reports `under_workspace` true,
  which fall through to the receipt read. The exemptions are matched on the
  blocker's `reason` value, not on message text; an unknown or new reason
  rejects. Update the function's docstring.

## 2. Tests (`tests/test_cli_cleanup_frontier.py`)

- [ ] 2.1 Evidence root outside the workspace, directory exists and holds a
  fresh receipt with a frontier bound: `cli._run_cleanup(dry_run=False)`
  returns `dry_run=True`, `frontier_blocker.reason == "evidence_dir_unresolved"`,
  `frontier_blocker.evidence_dir is None`, and the expired cycle directory
  still exists. Parametrized over the click and argparse entrypoints.
- [ ] 2.2 Evidence root inside the workspace whose final component is a
  symlink to an outside directory holding a fresh receipt: same outcome.
- [ ] 2.3 Workspace reached through an explicitly created symlinked parent
  directory (works on macOS and Linux), evidence root inside it: frontier
  `ok`, deletion by bound (no false rejection).
- [ ] 2.4 `NHMS_SCHEDULER_LOCK_ROOT`, `NHMS_SCHEDULER_ALLOWED_ROOTS` and
  `NHMS_SERVICE_ROLE` explicitly removed from the environment, evidence root
  inside the workspace: frontier `ok` (the check did not import unrelated
  preconditions; node-27 shells may carry a service role).
- [ ] 2.5 Evidence root inside the workspace whose final component is a
  symlink to another directory inside the workspace holding a fresh receipt:
  rejected as in 2.1 (the symlink verdict on its own).
- [ ] 2.6 Read-only evidence directory inside the workspace with a fresh
  receipt: frontier `ok`. Skipped when the process can write regardless of
  mode bits (root).
- [ ] 2.7 Missing evidence directory outside the workspace: rejected as
  `evidence_dir_unresolved`, not reported as `evidence_dir_missing`.
- [ ] 2.8 Evidence directory outside the workspace under a parent the
  process cannot traverse (the helper answers `not_writable` before it judges
  the workspace): rejected as `evidence_dir_unresolved` with a null
  directory, not `frontier_read_error`. Skipped as root.
- [ ] 2.9 The evidence-root check itself raises: the cleanup still fails
  closed as `evidence_dir_unresolved` with a null directory and deletes
  nothing (the check sits inside the guard).

Test names must avoid the substrings the helper treats as unsafe path
components (`pwd`, `token`, `secret`, …), because `tmp_path` embeds the test
name.

## 3. Evidence Floor

- [ ] 3.1 Red run: 2.1 and 2.2 fail on master (`c5a2fe52d`) with a real
  deletion (the expired cycle is gone and `dry_run` is false), and pass after
  the fix.
- [ ] 3.2 Mutations after the fix, each red: the new check removed (2.1,
  2.2, 2.5, 2.7); `require_under_workspace` set to false (2.1, 2.6 and the
  existing missing-directory tests — the helper then reports no
  `under_workspace`, so the exemptions refuse and 2.7 stays green); the
  symlink verdict added to the exemptions (2.5); the "not found" exemption
  removed (the existing missing-directory tests); the "not writable"
  exemption removed (2.6); the `under_workspace` condition on the
  exemptions removed (2.8); the check moved out of the fail-closed guard
  (2.9).
- [ ] 3.3 `uv run pytest -q tests/test_cli_cleanup_frontier.py tests/test_retention_frontier.py`
  locally and on node-27; `uv run ruff check .`;
  `openspec validate confine-cleanup-evidence-root --strict --no-interactive`.
- [ ] 3.4 Archive in the final push: `openspec archive confine-cleanup-evidence-root --yes`.
