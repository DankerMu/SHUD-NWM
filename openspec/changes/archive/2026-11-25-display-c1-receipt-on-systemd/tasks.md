# Tasks

Fixture level: `standard`. Risk packs: gate semantics (a receipt that passes
when a claim is false; a claim that is skipped instead of failed); test
evidence (moved tests must still run and still test the same thing);
operator-facing procedure (the checklist must say what the receipt does not
claim any more).

Must preserve: `scripts/ops/start-display-api.sh` and the unit file are not
edited; no production Python changes; every test that exists today still
runs with the same id apart from its file name (moved), or is listed as
changed in 3.3, or is one of the parser swaps and splits that 3.1 allows
(each listed in the report); the lane's own tests keep passing (the lane is deleted in
the next change, not here); the migration-order note of the checklist's C1.

## 1. Spec (orchestrator)

- [x] 1.1 `runtime-service-role-boundary`: the requirement "The display
  deployment receipt observes the running unit".

## 2. The receipt script

- [x] 2.1 `scripts/ops/display-c1-receipt.sh` (bash, executable): the ten
  claims of the requirement, one output line each (`<claim>=ok` or
  `<claim>=FAIL <short reason>`), preceded by `repo_root=`,
  `checkout_head=`, `main_pid=`, `port=`, `process_started=`,
  `head_moved=`, `env_modified=` (the three as unix seconds),
  `object_store_root=` and `published_artifact_root=`, followed by
  `C1=PASS` or `C1=FAIL`. Root from the script's path or the first
  argument. Port parsed from the main process's `ps -o args=` line. Start
  time as now minus `ps -o etimes=`; HEAD move from
  `git log -g -1 --date=unix --format=%gd HEAD` (prints
  `HEAD@{<seconds>}`; an empty answer is an unreadable input); env file
  time from `date -r`. Tracked changes from
  `GIT_OPTIONAL_LOCKS=0 git status --porcelain --untracked-files=no`.
  Listeners from `ss -Hltnp "sport = :<port>"`, each pid compared with
  the main pid and with its `ps -o ppid=`. Requests with `curl` against
  `127.0.0.1`, `GET` only, bounded by `--max-time`. The two storage roots
  read from `infra/env/display.env` by matching the key (last assignment
  wins); the file is not sourced. No other value of that file reaches the
  output. No `systemctl` verb except `show` / `is-active`; nothing is
  written.
- [x] 2.2 `tests/test_display_c1_receipt.py`: the script is run in a
  temporary checkout with `systemctl`, `ps`, `curl` faked on `PATH` (the
  pattern of the start-wrapper harness; `ss` faked too). One passing case;
  one case per scenario of the requirement; and for every claim and every
  unreadable input of the requirement at least one case in which exactly
  that claim is `FAIL` and the others are `ok` (the inactive-unit case
  excepted, where the requirement names the set). The three absolute
  paths of `no_slurm_client` cannot be created in a test and get no seam
  in the script: one test asserts that the script names each of them in
  an existence check. The "changes nothing" scenario is asserted on the
  recorded calls of the fakes (verbs, HTTP method) and on the temporary
  tree, `.git/index` included, being byte-identical. A case with a
  secret-looking value in the env file asserts that it is absent from
  stdout and stderr.
- [x] 2.3 Red runs: for each claim, the check is disabled in the script
  (one at a time), the named failing-case test goes red, the script is
  restored byte-identical (`cmp` against a pristine copy). Recorded in
  `.workplans/3231/red-runs.log`.

## 3. Tests leave the lane

- [x] 3.1 Classify all test functions of
  `tests/test_two_node_docker_runtime.py` by the file under test. Report
  the table (subject, functions, collected items). A function whose
  subject stays in the tree after the lane is deleted
  (`scripts/ops/start-display-api.sh`,
  `infra/env/compute.scheduler-dbfree.env.example`, `.gitignore`,
  anything else found) moves with its body unchanged: the wrapper tests
  and their helpers to `tests/test_start_display_api.py`; the others to
  the file that already owns that subject, or a new one named after it.
  Where a body used a helper of the validator only to parse an env
  template, it uses the repository's existing template parser
  (`tests/test_env_templates.py`) instead. A
  function that tests both a surviving file and a lane file is split: the
  surviving half moves, the lane half stays.
- [x] 3.2 `tests/test_start_display_api_restart_anchor.py` imports its
  harness from `tests/test_start_display_api.py`.
- [x] 3.3 `tests/test_role_boundary_static.py` no longer imports
  `scripts.validate_two_node_docker_runtime`.
  `test_display_env_blockers_align_with_compute_only_static_inventory` is
  replaced by a test on `infra/env/display.example`: the runtime guard
  reports no blocker for the template's assignments; the template sets
  the six keys the validator required of it, values written out in the
  test: `NHMS_SERVICE_ROLE=display_readonly`,
  `NHMS_REQUIRE_SERVICE_ROLE=true`, `NHMS_AUTH_MODE=production`,
  `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`,
  `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false` and a non-empty
  `OBJECT_STORE_ROOT` (nothing else pins the auth mode and the file-log
  switch of the template once the validator is gone); and the test bites (the same assignments plus one
  forbidden key yield a blocker). The assertions about the validator's
  compute-side constants are dropped; the report lists each dropped
  assertion.
- [x] 3.4 Counts: the collected ids of the four files (`test_two_node_docker_runtime`,
  `..._runbook_environment_invariant`, `..._source_trust`,
  `test_start_display_api_restart_anchor`) plus the new files, before and
  after; every id of before exists after under its old or its new file.
  `scripts/ci/full_test_durations.json` gets an entry for each new test
  file if the sharder needs one (say how it treats a file without one).

## 4. Docs (orchestrator)

- [x] 4.1 `docs/runbooks/node-27-bringup-checklist.md` C1: deployment is
  the wrapper (a production action, migrations first, the existing note
  kept); the receipt is the script, run from a login shell of the service
  user; what it proves; what it no longer claims and why; the rule that a
  receipt of a commit needs a process started after the checkout reached
  it; `checkout_head` is the `head_sha` of the operator's record.
- [x] 4.2 `docs/runbooks/tier-node27-timeseries-storage.md`: the three
  lines of the go/no-go block that run the validator are removed.

## 5. Evidence

- [x] 5.1 Local: the new and touched test files, the four files of 3.4,
  `tests/test_runtime_mode.py`; `ruff check --no-cache .`;
  `openspec validate display-c1-receipt-on-systemd --strict --no-interactive`;
  the entropy hard gate at 247 findings / 0 failing.
- [x] 5.2 node-27: the same test files in a detached worktree; and the
  script itself, run from the worktree against the production checkout
  (root given as the argument). It is read-only. Expected today: every
  claim `ok` except `process_loaded_head` (the process is older than the
  checkout's last move), so `C1=FAIL`. Measured beforehand, read-only:
  the port is held by the main process and its two workers, no tracked
  file is modified, both storage roots exist and are not writable. The output is the receipt of the
  script's first contact with the real `systemctl`, `ps` and `curl`.
- [x] 5.3 Archive the change in the PR's last push.
