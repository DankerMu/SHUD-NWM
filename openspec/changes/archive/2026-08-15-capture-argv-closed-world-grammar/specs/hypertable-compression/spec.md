# Delta: hypertable-compression（capture argv 闭世界文法）

## ADDED Requirements

### Requirement: Run-plan capture argv MUST parse as the closed production grammar

The live-evidence verifier SHALL reject any bundle whose run-plan
capture argv, beyond the anchored `argv[0:2]` (interpreter and
committed producer script), does not consume left-to-right as a
sequence of pairs — a registered production capture flag in its exact
full spelling (in `--flag value` or `--flag=value` form) followed by
its value — against a closed-world flag set restated as literals
inside the verifier (the module's non-derived-oracle posture, pinned
against the real capture CLI parser by a structural test rather than
imported from it). A bare argparse `--` separator token SHALL be
rejected position-independently — flag position or value position
alike — with its own refusal wording naming the token and its argv
index, and this check SHALL run before the option-value equality
gates (a value-position `--` that survived to a stop-at-`--` scanner
would blind the exactly-once tool-value pins to bindings after it — a
net regression the ordering forbids). Any other token at a flag
position — a single-dash cluster such as `-xh`, an unregistered long
option such as `--evidence-dirx`, or a dangling unpaired flag — SHALL
be rejected with an unregistered-token wording, and a value token
beginning with `-` SHALL be rejected with a value-position wording
(closing the exit-2 family's last survivors on the deliberately
value-unpinned `--schema-dump-*` options); all wordings SHALL name
the offending token and stay distinct from the established
seam/help/abbreviation refusal wordings, which fire before the pair
grammar and are not subsumed. The verifier's option-value scanner
SHALL stop scanning at the first bare `--` token — a
definition-consistency measure (one meaning of "binding" across the
grammar gate, the equality gates and the real parser), not a
load-bearing defense, since the pre-posed `--` rejection fires first.
The supervisor gains no grammar gate (hermetic execution
compatibility stands), and the capture CLI parser itself is
unchanged.

#### Scenario: An unparsable argv cannot back a PASS

- **WHEN** a run-plan capture argv satisfies every identity anchor,
  exactly-once binding, tool-value pin and per-token family scan, but
  additionally carries `-xh` or `--evidence-dirx`
- **THEN** `verify_bundle` refuses with the unregistered-token wording
  naming that token, and no PASS verdict is produced

#### Scenario: The `--` separator family is refused, both shapes

- **WHEN** a capture argv carries a trailing `-- /tmp/whatever`, or
  carries its entire correct `--evidence-dir <expected>` pair moved
  after a `--` separator
- **THEN** the pre-posed position-independent `--` check refuses both
  with the `--`-specific wording, the reported argv index
  distinguishing the two placements, and the refusal fires before any
  equality gate reads the argv

#### Scenario: A value-position `--` cannot blind the tool-value pins

- **WHEN** a capture argv carries `--schema-dump-host -- --psql
  /tmp/stub` — the `--` sitting in value position so a
  flag-position-only grammar would consume it as a value while a
  stop-at-`--` scanner hides the trailing `--psql` rebinding
- **THEN** the pre-posed `--` check refuses the argv before the
  `--psql` equality gate runs, so the tool-value pin surface
  established by the anchoring series is never weakened

#### Scenario: A `-`-leading value token is refused

- **WHEN** a capture argv carries `--schema-dump-host -xh` (a
  deliberately value-unpinned option bound to a token the real parser
  would refuse with "expected one argument", exit 2)
- **THEN** the pair grammar refuses it with the value-position
  wording naming both the flag and the offending value token

#### Scenario: Production plans are never refused by the grammar

- **WHEN** the plan author emits its default capture argvs for all
  twelve kinds, including `schema_dump_list` with the
  `--schema-dump-host`/`--schema-dump-container` extra pairs
- **THEN** every argv parses under the closed grammar and the
  whole-capture-gate positive control stays green

#### Scenario: The grammar's flag set cannot drift from the real parser

- **WHEN** a flag is added to or removed from the capture CLI parser
  without updating the verifier's restated literal flag set
- **THEN** the structural premise test fails (the literal set plus
  argparse's auto `-h`/`--help` pair plus the seam-prefixed flags
  must equal the parser's registered surface — 17 option strings
  today), so drift reddens instead of being silently followed
