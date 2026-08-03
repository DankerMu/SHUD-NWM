# Make the sbatch-template contract tests continuation-aware (#1272)

## Why

`c5496c07` (2026-07-23, PR #1114 / #1112) branched three array templates
on `target_python_runtime` and, in the same edit, reformatted the
console-entrypoint command in BOTH branches as a backslash-continued
two-liner (`infra/sbatch/produce_forcing_array.sbatch`,
`infra/sbatch/run_shud_forecast_array.sbatch`,
`infra/sbatch/save_state_snapshot_array.sbatch` — anchors verified at
master 93204659). Bash semantics are unchanged; node-22 has run these
templates since 2026-07-23 without incident. But 7 verbatim single-line
assertions across two test files were not updated and have been red on
master ever since:

- `tests/test_production_slurm_validation.py`
  `test_validate_slurm_fake_lane_writes_required_evidence_and_redacts`
  (one in-substring assertion).
- `tests/test_slurm_array_contract.py`
  `test_real_templates_render_supported_cli_commands` (3 of 6
  parametrize rows: forcing / shud / state; the parse, publish and
  convert templates were not touched by `c5496c07` and stay green),
  `test_run_shud_forecast_template_uses_shared_logs_resources_manifest_contract`
  (one in-substring assertion), and
  `test_rendered_template_command_parses_without_error` (2 of 3 rows).

The last two are the real defect: `_rendered_command_argv` extracts the
command as "the last non-comment, non-export line" then `shlex.split`s
it, so after continuation the extracted line is
`--manifest-index … --task-id …` — the executable name is gone, and the
"rendered command parses as a valid CLI invocation" guard (the
`slurm-array-execution-contract` smoke-test requirement) is currently
non-functional for all three continued templates. A real renderer
regression would drown in this standing noise. Master's
`Unit Tests (full)` job flagged exactly these 7 on 2026-07-24 (runs
30064101392 / 30082946818 / 30108746053) and has been skipped on every
master run since (concurrency cancels + docs-only pushes missing the
backend filter), so the redness is invisible — the same CI-shape family
as #1182/#1254, out of scope here.

Adjudication (from the issue, adopted): stale tests, not a renderer
regression. The templates and gateway stay frozen.

## What Changes

Adopted route (the issue's recommendation): **test-side continuation
normalization, single-sourced.**

1. One shared helper `_join_line_continuations(rendered: str) -> str`
   that folds exactly the bash continuation sequence — a backslash at
   end-of-line plus the following line's leading whitespace — into a
   single space. It folds nothing else: ordinary newlines, quoting and
   spacing inside a line are untouched, so an assertion still expresses
   "this exact command exists in the rendering", merely tolerating the
   one layout freedom bash itself grants. Single definition consumed by
   both test files (suggested home: a small shared test helper module;
   two private copies would just re-create the drift this issue is
   cleaning up — final placement is the implementer's call, recorded in
   the deviations log if it differs).
2. The 5 verbatim substring assertions compare against
   `_join_line_continuations(rendered)` (expected strings stay the
   canonical single-line spelling — the recorded contract vocabulary,
   unchanged).
3. `_rendered_command_argv` becomes continuation-aware: it applies the
   helper BEFORE selecting the last non-comment/non-export line, so the
   full `nhms-*` command — executable included — is extracted and
   `argv[0]` assertions (`nhms-forcing` / `nhms-shud-runtime`) bind
   again, and `_invoke_main` really exercises the CLI parser as the
   contract requires.
4. Argument-level coverage for the `target_python_runtime` IF branch.
   What exists today (added by `c5496c07`'s own PR in
   `tests/test_real_slurm_gateway.py`:
   `test_round18_http_gateway_renders_only_explicit_target_runtime_for_worker_stages`
   and its console-entrypoint twin) asserts both branches at PREFIX
   level only — `"$NHMS_TARGET_PYTHON_RUNTIME" -m <module>` present /
   console entrypoint absent, and vice versa. Nothing anywhere asserts
   the IF branch's subcommand or its
   `--manifest-index`/`--task-id` arguments, and
   `tests/test_slurm_array_contract.py` has no branch coverage at all.
   The new assertions close exactly that residual: for each branched
   template, the continuation-normalized rendering with the runtime
   set contains the full
   `"$NHMS_TARGET_PYTHON_RUNTIME" -m <per-template module> <command>
   --manifest-index "$NHMS_MANIFEST_INDEX" --task-id
   "${SLURM_ARRAY_TASK_ID:-0}"` command — where the module is
   `workers.forcing_producer.cli` / `workers.shud_runtime.cli` /
   `packages.common.state_cli` respectively (the state template does
   NOT live under `workers.`) — and with it unset, the full
   console-entrypoint command. The existing prefix-level gateway tests
   stay untouched (no duplicated rows); the parses-without-error
   battery keeps judging the else branch (its substitution map and
   `_mock_array_cli_dependencies` are keyed on entry-point names;
   extending real `python -m` invocation coverage is a non-goal).
5. Regression lock making "commands may be line-continued" an explicit
   pinned fact: for a rendering that contains a backslash continuation,
   the helper-normalized argv equals the argv of the equivalent
   single-line spelling — so any future re-layout of the templates
   cannot silently disarm the guard again, in either direction.
6. Discrimination stays load-bearing (oracle must not be weakened):
   the helper folds ONLY backslash-newline; a mutated rendering with a
   wrong executable token or a missing option still fails, proven by
   the evidence-floor red run, not asserted in prose.

Explicitly not adopted (per the issue): re-flattening the three
templates to single-line commands (a production-template edit needing
node-22 owner review, larger blast radius, zero test benefit);
deleting or xfail-ing the parse guard (it is the sole enforcement of
the contract's smoke-test requirement).

## Impact

- Affected code (tests only): `tests/test_slurm_array_contract.py`,
  `tests/test_production_slurm_validation.py`, plus the shared helper's
  home if it lands in a third test-support file. The final file set is
  checked against `git diff --name-only` at evidence time (E5), not
  against this sentence.
- Frozen surfaces (zero diff): `infra/sbatch/*.sbatch`,
  `services/slurm_gateway/**`, `workers/**`,
  `tests/test_real_slurm_gateway.py` (already updated by `c5496c07`;
  reruns as regression evidence only),
  `docs/appendices/D_sbatch_templates.md` (its single-line example
  remains a valid spelling of the same command).
- Affected specs: `slurm-array-execution-contract` (1 MODIFIED
  requirement: the real-template smoke-test requirement gains the
  continuation-awareness and both-branch coverage obligations).
