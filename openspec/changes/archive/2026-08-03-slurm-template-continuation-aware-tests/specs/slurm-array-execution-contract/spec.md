# slurm-array-execution-contract — delta for slurm-template-continuation-aware-tests (#1272)

## MODIFIED Requirements

### Requirement: Real-template smoke tests cover mock blind spots

The test suite SHALL include smoke tests that render real Slurm
templates and validate worker CLI compatibility without depending on
the mock Slurm backend executing scripts. The smoke tests SHALL judge
the rendered command text after folding line continuations — any
horizontal whitespace before the backslash, the backslash at
end-of-line, the newline, and the next line's leading spaces or tabs,
folded together to a single space — and nothing else. That
fold matches bash for continuations at token boundaries in unquoted
command text, which is the only place the templates use them; it is
deliberately NOT a full bash-continuation emulator (bash splices with
nothing and preserves quoted leading whitespace, and performs no
continuation inside single quotes or quoted heredocs — the fold is
applied only to command-form judgments, never to heredoc or export
assertions). It grants no other tolerance, so a template may lay its
command out across continued lines without disarming the guard, while
any change to the command's tokens still fails, and a continuation
followed by a blank line stays two separate commands exactly as bash
executes it. Command extraction
SHALL recover the full invocation including the executable name
(`c5496c07` reformatted three templates' commands as continued lines
and the last-line extraction silently lost the executable, leaving the
parser-compatibility guard non-functional for those templates while
its tests were red for an unrelated-looking verbatim mismatch). For
every template that branches on `target_python_runtime`, the smoke
tests SHALL assert both branches at argument level — the full
console-entrypoint command when the runtime is unset, and when it is
set the full `"$NHMS_TARGET_PYTHON_RUNTIME" -m <module> <command>
--manifest-index …` invocation, with the per-template module as the
templates render it (`workers.forcing_producer.cli` /
`workers.shud_runtime.cli` / `packages.common.state_cli` — the state
template's module is not under `workers.`). Prefix-level presence
checks alone do not satisfy this: the pre-existing gateway tests
already assert prefixes for both branches, and the residual this
closes is the unasserted subcommand and arguments.

#### Scenario: Mock backend does not hide CLI drift

- **WHEN** worker CLI arguments change
- **THEN** a template/CLI smoke test MUST fail if any real template
  still calls the old argument contract

#### Scenario: Line-continued commands are judged whole

- **WHEN** a template lays its worker command out with backslash
  continuations at token boundaries in unquoted command text
- **THEN** the smoke test MUST extract the same argv as the canonical
  single-line expected command — executable name included — and MUST
  still execute the worker CLI parser against it

#### Scenario: Continuation tolerance does not weaken discrimination

- **WHEN** a rendered command carries a wrong executable token or a
  wrong option name, in continued or single-line layout
- **THEN** the smoke test MUST fail — the normalization folds only
  backslash-newline sequences and grants no other tolerance

#### Scenario: Both target_python_runtime branches are covered at argument level

- **WHEN** a template branches its worker command on
  `target_python_runtime`
- **THEN** the smoke tests MUST assert each branch's full rendered
  command including subcommand and arguments — prefix-level presence
  of the runtime or entrypoint alone does not satisfy this
