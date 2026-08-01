# Tasks: docs-retention-tick-bracket-grep-anchor

Fixture level: compact · Repair intensity: low · Issue #1215

Change surface:
- docs/runbooks/tier-node27-timeseries-storage.md (§8.6 item 5 only,
  currently the bracket paragraph around lines 1941-1952)
- tests/test_node27_timeseries_retention.py (the
  `_MEASURE_WARNING_GREP_FENCE` constant only, currently line 3238)

Must preserve:
- scripts/node27_timeseries_retention.py AND
  scripts/node27_timeseries_retention_once.sh zero diff
- §8.2.1 untouched; receipt schema/gate logic untouched
- `_MEASURE_WARNING_GREP_TOKEN` value unchanged ("freed_bytes measurement
  failed") — this change derives the fence, it does not rename anything
- The fence's quoting rationale comment (why the quoted form pins §8.6
  specifically and not §8.2.1's backtick form)
- `test_measure_warning_byte_identical_with_runbook` assertions unchanged
  (the derivation changes the constant, not the test)

Must add:
- §8.6 item 5: generated_at↔bracket correlation rule + path-alone-
  insufficient caveat (cite infra/env/node27-timeseries-retention.example)
  + receipt-less bracket warnings (start without done rc=; rc=2 config
  refusal)
- §8.6 item 5: refuse-then-retry misread window + conservative direction
- `_MEASURE_WARNING_GREP_FENCE` as f-string derivation of the token

Seams under test:
- `test_measure_warning_byte_identical_with_runbook` — the existing
  byte-identity test is the oracle; the derivation must keep it green on the
  real tree and make it red under the rename-campaign mutation

Risk packs (compact):
- Error handling / rollback / partial outputs: not selected — no runtime
  change; the documented refuse semantics live in unchanged production code.
- Public API / CLI / script entry: not selected — no entrypoint change.
- File IO / path safety / overwrite: not selected — docs+test only.
- Schema / columns / units / field names: not selected — no schema change.
- Auth / permissions / secrets: not selected — no secret surface.
- Legacy compatibility / examples: selected (docs) — §8.6 must stay
  consistent with the wrapper's actual start/done line wording at
  scripts/node27_timeseries_retention_once.sh:143,151 and with the shipped
  env example it now cites; cited paths/line refs must be real.
- Other packs: not selected — no runtime behavior change.

## Implementation tasks

- [x] 1. Rewrite the §8.6 item 5 bracket paragraph with the `generated_at`
  correlation rule: pick the bracket whose `start`/`done` timestamps
  (wrapper `ts()`, UTC ISO-8601) contain the receipt's `generated_at`
  (schemas/timeseries_retention_receipt.schema.json requires it,
  format date-time); state the shipped env pins the receipt path (cite
  infra/env/node27-timeseries-retention.example) so the path alone cannot
  discriminate ticks; warn that a `start` without a matching `done rc=`
  (tick in flight / wrapper died) or a receipt-less `rc=2` config-refused
  tick wrote no receipt and is not the bracket to read.
- [x] 2. Add the refuse-then-retry caveat sentence(s) to §8.6 item 5: prior
  `RETENTION_DROP_FAILED` tick's warning may name a chunk this tick
  genuinely measures 0 and drops; the `dropped_chunks[]` criterion does not
  exclude it; misread direction is conservative.
- [x] 3. Derive the fence:
  `_MEASURE_WARNING_GREP_FENCE = f"grep '{_MEASURE_WARNING_GREP_TOKEN}'"`,
  preserving the quoting-rationale comment.
- [x] 4. Mutation proof (rename campaign), run AFTER task 3 (the escape
  being closed is green today; it must be red only WITH the derivation
  applied). Method: in-tree mutate-then-restore per the issue recipe —
  `_ROOT`/`_RUNBOOK_PATH` resolution (tests/:54-56) breaks partial scratch
  copies, so mutate the working tree, capture the red run, then restore via
  `git checkout -- <files>` and prove restoration with an empty
  `git status --porcelain` for those files. Rename the warning string in
  scripts/node27_timeseries_retention.py together with `_MEASURE_WARNING`,
  the token, and §8.2.1's literal — leaving §8.6's grep command line alone —
  then `test_measure_warning_byte_identical_with_runbook` MUST fail; capture
  output for the PR body; final diff contains no such rename.

## Required evidence

- Command: `uv run pytest -q tests/test_node27_timeseries_retention.py` all
  green (139 passed / 1 skipped baseline on current master).
- Command: `uv run ruff check .` clean.
- Mutation output (rename campaign sparing §8.6 → byte-identity test fails)
  in PR body.
- Grep proof (scoped to live anchors — archived openspec changes under
  openspec/changes/archive/ carry the literal in immutable prose and are
  excluded): `git grep "grep 'freed_bytes" -- docs scripts tests` returns
  exactly one hit (the runbook §8.6 fenced block); the test-side fence is a
  derivation, verified by the derivation line itself.
- `git diff --stat`: runbook + test file + openspec only; `scripts/` zero
  diff.
- Docs render check: changed runbook section passes the repo's
  markdown-lint gate (CI `markdown-lint` job triggers on docs/**; locally
  spot-check formatting consistency with the surrounding section).

## Non-goals

- Production logic/wrapper, §8.2.1, schema, receipt-path convention
  decision, wrapper start/done byte-identity anchor (tracked separately).
