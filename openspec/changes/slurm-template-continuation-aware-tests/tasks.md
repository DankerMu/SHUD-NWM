# Tasks — slurm-template-continuation-aware-tests (#1272)

Cross-file anchors below were verified at master 93204659; anchors into
the two edited test files are named (function/parametrize id), never
numbered — this change's own edits shift line numbers.

Risk triage: fixture level **compact** (issue suggests none — it even
says no openspec change is needed; the workflow's fixture mandate
overrides that, recorded as a divergence. S-size, tests-only, frozen
production surfaces — but the edit reshapes the sole enforcement of a
spec requirement, so the one risk that matters is oracle
discrimination). Risk pack selected: **oracle-discrimination** (the
normalization must tolerate exactly bash's continuation freedom and
nothing else; red proofs are load-bearing). Not selected:
forensic-verbatim-posture (no recorded values, no forensic lane),
performance / UI / migration (n/a), guard-soundness in the #1269 sense
(no runtime gate is edited).

Must-preserve behavior:

- The three untouched templates' parametrize rows
  (`parse_output_array`, `publish_tiles`, `convert_canonical` in
  `test_real_templates_render_supported_cli_commands`) stay green with
  their expected strings byte-identical.
- `test_rendered_template_command_parses_without_error` keeps calling
  `_invoke_main` — the guard stays an execution of the CLI parser,
  never a string comparison.
- Every expected-command string keeps the canonical single-line
  spelling (the contract vocabulary; only the comparison side
  normalizes).
- `tests/test_real_slurm_gateway.py` zero diff.
- All currently-green tests in both edited files stay green.

Seams under test (upstream-declared, consumed not renegotiated): the
rendered-template text of the three `c5496c07`-branched templates, in
both `target_python_runtime` branches; bash's backslash-newline
continuation rule as the only tolerated layout freedom.

Non-goals: editing `infra/sbatch/**` or `services/slurm_gateway/**`;
running `python -m workers.…` mains under the parses battery; the 4
unrelated full-suite reds (#1274); the CI-shape family (#1182/#1254).

Minimal mergeable slice: the whole change — helper + 7 assertion
sites + if-branch coverage + regression lock; landing the substring
fixes without the `_rendered_command_argv` fix would green the noise
while leaving the guard disarmed, the worst outcome the issue names.

## 1. Shared helper

- [x] 1.1 `_join_line_continuations(rendered: str) -> str`: folds the
  sequence horizontal whitespace + backslash + newline + the next
  line's leading SPACES/TABS (`[ \t]*\\\n[ \t]*` — horizontal
  whitespace ONLY on both sides, never `\s`, which would cross
  newlines; the pre-backslash `[ \t]*` is required because the
  templates render `… produce \` with a space before the backslash,
  and the fold must land on the canonical single-space form the
  expected constants spell — adjudicated during implementation) into
  one space; touches nothing else. The blank-line case is load-bearing:
  bash TERMINATES a command when a continuation is followed by an
  empty line (two separate commands), and the renderings are dense
  with Jinja-emitted blank lines, so a `\s*` fold would splice two
  distinct commands into one. Single definition; both test files
  consume the same object (no second copy). Direct unit tests beside
  it: folds the two-line continued command into the canonical
  single-line form; leaves a no-continuation rendering byte-identical;
  folds a continuation at the very last line without error; does NOT
  splice across a blank line after a continuation (the two commands
  stay two lines); does NOT fold a backslash in a line's interior or
  collapse ordinary newlines.

## 2. Assertion sites

- [x] 2.1 The 5 verbatim substring assertions (fake-lane evidence test
  in test_production_slurm_validation.py; 3 changed parametrize rows
  and the shared-contract shud test in test_slurm_array_contract.py)
  assert against the helper-normalized rendering; expected strings
  unchanged. Apply the helper AT the command-form assertion only —
  never rebind `rendered` for a whole multi-assertion test: the
  fake-lane test's heredoc-region assertions and the shud test's
  SBATCH/export assertions keep judging the raw rendering
  (run_shud_forecast_array carries a quoted `<<'PY'` heredoc where
  bash performs no continuation; normalizing those pinned strings
  would be a false-green vector).
- [x] 2.2 `_rendered_command_argv` normalizes via the helper before
  last-line selection; its substitution map and callers unchanged.
  `argv[0]` binds again for all three battery rows and `_invoke_main`
  runs.

## 3. New coverage

- [x] 3.1 Argument-level IF-branch assertions for the three branched
  templates: with `target_python_runtime` set, the normalized
  rendering contains the FULL command
  `"$NHMS_TARGET_PYTHON_RUNTIME" -m <module> <command>
  --manifest-index "$NHMS_MANIFEST_INDEX" --task-id
  "${SLURM_ARRAY_TASK_ID:-0}"`, where `<module> <command>` is
  `workers.forcing_producer.cli produce` /
  `workers.shud_runtime.cli execute` /
  `packages.common.state_cli save` (per the templates; the state
  module is NOT under `workers.`); with it unset, the full
  console-entrypoint command. Both branches asserted per template.
  The existing prefix-level both-branch gateway tests
  (test_real_slurm_gateway.py round18 pair) stay untouched — this
  closes the argument-level residual they leave, not a duplicate of
  them.
- [x] 3.2 Regression lock: a rendering containing a backslash
  continuation yields, after normalization, the same argv as the
  canonical single-line expected constant already recorded in the
  parametrize table (`shlex.split` of that constant under the same
  substitution map) — derive the single-line side from the recorded
  constant, NOT by re-normalizing the rendering, which would compare
  the helper against itself. Pin the equivalence explicitly, so a
  future layout change in either direction cannot disarm the
  extraction silently.

## 4. Spec + validation

- [x] 4.1 Spec delta: MODIFIED "Real-template smoke tests cover mock
  blind spots" requirement in `slurm-array-execution-contract` —
  smoke tests judge the continuation-normalized rendering, extract
  the full argv including the executable, and cover both
  `target_python_runtime` branches of every branched template at
  argument level.
- [x] 4.2 `openspec validate slurm-template-continuation-aware-tests
  --strict --no-interactive` green.

## Evidence Floor

- [x] E1 `uv run pytest -q tests/test_slurm_array_contract.py
  tests/test_production_slurm_validation.py
  tests/test_real_slurm_gateway.py` green; the 7 issue-table ids all
  pass.
- [x] E2 `uv run ruff check .` green.
- [x] E3 openspec strict validation green (4.2).
- [x] E4 **Red proofs (discrimination)**, each via backup-copy
  mutation restored byte-identical (`cmp`) afterwards:
  (i) the `[ \t]*`-vs-`\s*` discrimination proof runs at helper
  unit-test level on a SYNTHETIC input (no current rendering
  distinguishes them — after every real continuation the next char is
  `-`): on `'nhms-forcing produce \\\n\nmkdir -p /x\n'` the pinned
  `[ \t]*` fold keeps two lines/two commands (bash's behavior) while
  a `\s*` mutation splices them into one — the 1.1 blank-line unit
  test MUST red under the `\s*` mutation; extraction-level red
  coverage comes from (ii)/(iii), not from this item;
  (ii) with a rendered command's executable token corrupted (mutate
  the template copy in a scratch dir or patch the rendering in-test),
  `test_rendered_template_command_parses_without_error` reds on its
  `argv[0]` assertion — the re-armed guard demonstrably guards;
  (iii) with the `--manifest-index` option name corrupted the same
  way, `_invoke_main` reds — parser execution, not string luck.
- [x] E5 Surface check: `git diff --name-only` lands only under
  `tests/` plus this openspec change; `infra/sbatch/**`,
  `services/slurm_gateway/**`, `tests/test_real_slurm_gateway.py`
  zero diff. Check against the command's own output, not this
  sentence.
- [x] E6 Full-suite spot: `uv run pytest -q
  -m "not e2e and not grib and not integration"` no longer lists any
  of the 7 ids (the 4 known #1274 reds are out of scope and expected
  to persist locally on macOS).
