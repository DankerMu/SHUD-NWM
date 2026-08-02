# Spec Delta: hypertable-compression (residual non-production argv shapes)

## ADDED Requirements

### Requirement: Run-plan capture argv MUST be free of help early-exit tokens and MUST bind `--evidence-dir` relationally to its own output path

The live-evidence verifier SHALL reject any bundle whose run-plan capture
argv carries an argparse help early-exit token — `-h`, any single-dash
short-option cluster beginning with `-h` (e.g. `-hx`, `-hh`, which
argparse resolves to the same auto help action because `-h` is the only
registered single-dash flag), `--help` (either spelling, including
`--help=x`), or any unambiguous abbreviation of
`--help` (`--h`, `--he`, `--hel`) — because such an argv makes the
recorded producer exit before collecting anything (the bare spellings
print help and exit 0; the `--help=x` spelling is an argparse usage
error exiting 2 — every member of the family is non-production),
falsifying the forensic claim the identity anchor makes. The verifier
SHALL further require each capture argv to bind `--evidence-dir` exactly
once to the value derived from that capture's own verifier-bound
`output_path` (the directory containing `output_path`, suffixed
`/capture-artifacts` — the textual inverse of the production plan
author's same-root derivation), so the `os.statvfs` free-space
measurement feeding the `MIN_FREE_BYTES` hard gate is anchored to the
same volume the capture outputs claim, and SHALL reject
argparse-acceptable proper prefixes of `--evidence-dir` the same way it
does for every other value-pinned option. The argv[0] interpreter token
stays deliberately unpinned, with the residual trust root (argv[0] plus
the repo checkout) and its producer-side closure route recorded in the
verifier; no argv[0] gate is added.

#### Scenario: A help token cannot verify

- **WHEN** a run-plan capture argv that satisfies every identity anchor
  and value pin additionally carries `-h`, a `-h`-prefixed cluster form
  such as `-hx` or `-hh`, `--help`, `--help=x`, `--h`, `--he`, or
  `--hel` in any position
- **THEN** `verify_bundle` refuses with an error naming the offending
  token and the help early-exit refusal class, and no PASS verdict is
  produced

#### Scenario: `--evidence-dir` must be the output-path sibling

- **WHEN** a capture argv omits `--evidence-dir`, binds it more than
  once, binds it dangling, or binds it to any directory other than the
  one derived from that capture's `output_path`
- **THEN** the evidence-dir gate refuses with an error naming the
  option, the observed bindings and the derived expected value

#### Scenario: `--evidence-dir` cannot be rebound by abbreviation

- **WHEN** a capture argv carries a proper-prefix token such as `--ev`
  or `--e` that would rebind `--evidence-dir` last-wins
- **THEN** the existing pinned-option abbreviation branch refuses with
  its established wording (naming the token and the option it
  abbreviates) — the evidence-dir equality gate itself never fires on
  such a token, and the two refusal messages stay distinct

#### Scenario: Relational, not absolute — production and hermetic plans both pass

- **WHEN** a plan binds each capture's `--evidence-dir` to the
  `capture-artifacts` sibling of that capture's own `output_path` —
  whether the root is the production evidence root or a hermetic test
  tmp directory (the plan author derives both fields from the same
  `--root`, so all plan-author-authored plans satisfy the relation)
- **THEN** the bundle verifies exactly as before; no run-varying literal
  is pinned and the twelve-kind plan-author positive control stays green

#### Scenario: Structural premises are pinned against the real parser

- **WHEN** the capture CLI parser is inspected
- **THEN** tests pin that no registered business flag starts with `--h`
  (so rejecting the `--help` prefix family collides with nothing), that
  `--evidence-dir` is the only registered `--e*` flag, and that the
  existing anchored/pinned zero-collision premises still hold with the
  widened tuple

## MODIFIED Requirements

### Requirement: Run-plan capture argv tool-path values MUST match the committed production tooling

The live-evidence verifier SHALL reject any bundle whose run-plan
capture argv binds `--psql`, `--systemctl`, `--docker`,
`--journalctl`, `--git`, `--repo` or `--container` to anything other
than the committed production value (the `/usr/bin/*` host tools,
the production repo path, the production container name), or binds
`--database` to a value other than the run plan's validated
database — each binding present exactly once — and SHALL reject any
token that is an argparse-acceptable proper prefix of a pinned
option, so that an identity-anchored argv cannot point the committed
producer at substitute tooling. `--evidence-dir` is bound relationally
to the capture's own output path (see the residual-shapes
requirement); the `--schema-dump-*` options stay deliberately
unpinned (legitimately parameterized data paths); the supervisor
stays value-unpinned on all tool options (the executor legitimately
runs hermetic plans with stub tools; the forensic claim is
verifier-owned).

#### Scenario: Stub tooling cannot verify

- **WHEN** a bundle's run-plan capture argv binds any pinned tool
  option to a non-production value (e.g. `--psql /tmp/stub-psql`),
  omits a pinned option, or binds it more than once (in either
  `--flag value` or `--flag=value` spelling)
- **THEN** `verify_bundle` refuses with an error naming the option,
  the observed bindings and the expected value, and no PASS verdict
  is produced

#### Scenario: A pinned option cannot be rebound by abbreviation

- **WHEN** a capture argv carries a token whose base is a proper
  prefix of any pinned option (e.g. `--ps`, `--do`, `--rep=/x`)
- **THEN** the verifier rejects the argv even though the full-name
  exactly-once binding looks clean, closing the last-wins rebinding
  class for the tool options the same way it is closed for the
  identity anchor

#### Scenario: The hermetic e2e still executes stub tools and verifies PASS

- **WHEN** the end-to-end test runs the real state machine with
  stub tools under a test checkout
- **THEN** the recorded plan carries production tool values while
  only the executed plan variant carries the stub paths, the ledger
  maps executed argv back to the recorded argv, and the bundle
  still verifies PASS — hermetic execution needs no gate weakening
