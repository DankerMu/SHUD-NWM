## ADDED Requirements

### Requirement: Native explicit-cycle bindings remain executable and identity-bound
The #1895 recorder SHALL select exactly one primary named shipping forecast query, SHALL reject the `selected_cycles` branch, and SHALL preserve its named psycopg mapping through the live `EXPLAIN` boundary. A pure captured-query validator and the live SQL adapter SHALL also retain compatibility with a synthetic positional explicit-cycle SQL plus sequence; the shipping forecast owner itself remains named-only. The explicit `h.cycle_time` equality and the `h.run_id`, `h.model_id`, and SQL timeseries-segment predicates MUST bind the validator's canonical inputs. For named SQL, the system MUST resolve the placeholder name referenced by each required predicate and compare that mapping entry to its canonical value; key membership or mapping-value membership alone is not evidence. The mapping key set MUST equal the unique placeholder names referenced anywhere in the SQL. For positional SQL only, placeholder count MUST equal sequence length and the equality ordinal MUST bind the canonical issue time.

#### Scenario: Shipping named mapping reaches EXPLAIN unchanged in meaning
- **WHEN** the shipping forecast owner emits `h.cycle_time = %(issue_time)s`, repeats `%(issue_time)s` for the window start, references named run/model/SQL-segment placeholders, and supplies exactly the unique referenced keys in any insertion order
- **THEN** the recorder resolves `issue_time`, `run_id`, `model_id`, and the SQL segment placeholder by name to their canonical values, treats repeated `issue_time` references as one key rather than a count mismatch, returns a defensive mapping, and the live adapter passes a mapping with those bindings to psycopg

#### Scenario: Synthetic positional query remains supported at the validator and live adapter seams
- **WHEN** the pure captured-query validator receives synthetic explicit-cycle SQL using positional `%s` placeholders with a matching sequence whose required predicate ordinals bind the canonical issue time, run, model, and SQL segment
- **THEN** the validator accepts it, sequence order is preserved, and `execute_explain` and `make_sql_probe` pass that sequence to psycopg without mapping conversion while the shipping forecast owner remains named-only

#### Scenario: Binding drift fails before live execution
- **WHEN** the pure captured-query validator receives `selected_cycles`, mixed or malformed placeholder styles, a missing or extra named key, a named predicate referencing the wrong key or value, a positional count mismatch, an equality ordinal bound to another value, a wrong container type, or missing/wrong run, model, or timeseries-segment identity
- **THEN** it raises a stable query binding or identity error before any live `EXPLAIN` or PASS publication, while the real shipping forecast owner remains named-only and unchanged

### Requirement: Query digests are deterministic across supported containers
The recorder SHALL hash normalized SQL and a type-aware canonical representation of parameters. Mapping key order MUST NOT affect the digest, sequence order MUST remain significant, and runtime parameter containers MUST NOT be replaced by their digest representation.

#### Scenario: Equivalent mappings produce one frozen digest
- **WHEN** two captures contain identical named keys and values in different insertion orders, including timezone-equivalent issue-time values
- **THEN** they produce the same query digest and frozen lane identity while each live execution retains a mapping

#### Scenario: Semantic binding changes alter the digest
- **WHEN** a named value, key set, positional value, positional order, or normalized SQL changes
- **THEN** the query digest changes or the capture is rejected as invalid

### Requirement: Existing G7 evidence and safety controls remain intact
The repair SHALL NOT change the four-lane set, query window, warmup plus twenty accepted samples, readonly role proof, statement timeout, receipt schema, or the rule that only a fresh node-27 G7 run on the reviewed merged SHA constitutes live performance evidence.

#### Scenario: Existing downstream consumers accept the repaired record
- **WHEN** a valid named shipping record flows through lane binding, freeze/re-observation, SQL sampling, receipt validation, and publication tests
- **THEN** existing evidence fields and comparisons remain valid, all local contract tests pass, and no local result is labeled as node-27 live PASS
