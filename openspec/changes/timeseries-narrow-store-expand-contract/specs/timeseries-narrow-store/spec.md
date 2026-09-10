## ADDED Requirements

### Requirement: The river fact table SHALL be a narrow surrogate-key hypertable with three indexes and key-based compression

`hydro.river_timeseries` SHALL carry exactly: `run_key INTEGER NOT NULL`, `basin_version_key INTEGER NOT NULL`, `river_network_version_key INTEGER NOT NULL`, `river_segment_key INTEGER NOT NULL`, `valid_time TIMESTAMPTZ NOT NULL`, `lead_time_hours INTEGER NULL`, `variable_e hydro.river_variable NOT NULL`, `value DOUBLE PRECISION NOT NULL`, `unit_e hydro.river_unit NOT NULL`, `quality_flag_e hydro.river_quality_flag NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`; it MUST NOT carry any text identity column. Its primary key SHALL be `(run_key, river_segment_key, variable_e, valid_time)`; its only secondary indexes SHALL be `river_ts_segment_time_key_idx (river_segment_key, variable_e, valid_time DESC)` and `river_ts_run_discovery_key_idx (run_key, basin_version_key, river_network_version_key, variable_e, valid_time DESC)`; the TimescaleDB default `valid_time` index MUST NOT be created (`create_default_indexes => false`). Foreign keys SHALL exist on `run_key` (→ `hydro.hydro_run`) and `river_segment_key` (→ `core.river_segment`) only. The authority-table surrogate keys created by migration 000050 (`hydro.hydro_run.run_key`, `core.basin_version.basin_version_key`, `core.river_network_version.river_network_version_key`, `core.river_segment.river_segment_key`) remain the identity authority; no fact-table backfill runner or cutover function exists after the contract batch. Compression SHALL be `segmentby (run_key, river_segment_key)`, `orderby (variable_e, valid_time)`, configured as the LAST schema DDL of the table (after the primary key, foreign keys and secondary indexes exist), and the chunk time interval SHALL be one day.

#### Scenario: Catalog shape after the expand migration
- **WHEN** the expand migration has been applied to an empty database or to node-27
- **THEN** `hydro.river_timeseries` has exactly the column set above with the stated `attnotnull` per column, exactly three indexes, exactly two foreign keys, `timescaledb_information.compression_settings` lists `run_key`, `river_segment_key` as segmentby and `variable_e`, `valid_time` as orderby, and `timescaledb_information.dimensions.time_interval` for hypertable `hydro.river_timeseries` equals `1 day`

#### Scenario: Analysis run rows carry NULL lead time
- **WHEN** the parser writes an analysis run
- **THEN** its rows insert with `lead_time_hours IS NULL` and no NOT NULL violation is raised

#### Scenario: A text identity column cannot reappear
- **WHEN** any migration after the expand migration adds a column named `run_id`, `basin_version_id`, `river_network_version_id`, `river_segment_id`, `variable`, `unit` or `quality_flag` to `hydro.river_timeseries`
- **THEN** `tests/test_migrations.py` fails naming the migration

### Requirement: Every run SHALL be routed to exactly one store recorded on hydro_run

`hydro.hydro_run.timeseries_store` SHALL be `NOT NULL DEFAULT 'narrow'` with values `legacy` or `narrow`. The expand migration SHALL set `legacy` on every run matching the parse-fact predicate `parsed_at IS NOT NULL OR status IN ('parsed','published')` (the runs that completed a parse into the renamed table); runs registered but not yet parsed at expand time, and every run created afterwards, are `narrow`. Legacy rows of a run outside that predicate (a parse that failed mid-way before this change) are an accepted transitional state: they are unreachable by every store-bound read branch and are cleared by retention; the migration MUST NOT probe the legacy table row-by-row to find them. The parser SHALL keep `narrow` in the same transaction that writes the run's rows and marks the run parsed. A run's rows MUST exist only in the table its store names, except in the recorded rollback state (`timeseries-store-expand-contract`) and in the accepted transitional state above (legacy remnants of a run routed `narrow`).

#### Scenario: Parsed runs become legacy, in-flight runs stay narrow
- **WHEN** the expand migration runs on a database holding one published run and one run still `running`
- **THEN** the published run has `timeseries_store = 'legacy'` and the legacy table still answers its reads; the running run has `timeseries_store = 'narrow'`

#### Scenario: First parse after expand succeeds into the narrow table
- **WHEN** a run created after the expand deployment is parsed for the first time
- **THEN** its rows are in `hydro.river_timeseries`, none are in `hydro.river_timeseries_legacy`, and `timeseries_store = 'narrow'` is visible together with `parsed_at` after the same transaction commits

### Requirement: The parser SHALL write only the narrow table and refuse legacy runs fail-closed

The parser's replace chain SHALL DELETE and INSERT only against `hydro.river_timeseries`, writing only its column set, with `ON CONFLICT (run_key, river_segment_key, variable_e, valid_time) DO UPDATE`. Replace granularity is unchanged: the DELETE SHALL be located by `run_key`, `river_network_version_key`, `variable_e` and a closed `valid_time` window whose two bound literals appear in the same statement, and the guard's window inputs SHALL be the same union window as before; no text pushdown aid is needed because `run_key` leads the narrow segmentby. Before any write it SHALL read `hydro_run.timeseries_store`; for a `legacy` run it MUST raise `LegacyStoreWriteRefused` without issuing any DELETE. The autopipeline SHALL record such a run in `ops.ingest_recompute_decline` with reason `legacy_store_refused` and finish the tick with `rc = 0`; that decline is a store-level permanent terminal state that MUST NOT be reopened by a newer `product_mtime`, only by the run's `timeseries_store` becoming `narrow`. The parser CLI SHALL expose a dedicated exit code for this refusal, distinct from the compressed-chunk-blocked and guard-internal codes.

#### Scenario: Narrow write shape
- **WHEN** the parser upserts a batch for a `narrow` run
- **THEN** the rendered INSERT names no text identity column, its `ON CONFLICT` target is the key primary key, and the DELETE carries both `valid_time >=` and `valid_time <=` literals with `run_key`, `river_network_version_key` and `variable_e`

#### Scenario: Legacy run re-parse is refused before mutation and stays declined
- **WHEN** a recompute targets a run whose `timeseries_store` is `legacy`
- **THEN** the parser raises `LegacyStoreWriteRefused`, no DELETE statement is executed, the tick exits 0 with a `legacy_store_refused` decline row, and a later product regeneration for the same run does not reopen the decline

### Requirement: Read paths SHALL render routed per-store variants and compose spanning fact rows locally

Every reader of the river fact table SHALL keep one store-parameterized SQL template per distinct authored fact-row read shape, reusing identical probe bodies within that caller. Its transitional aids SHALL be normalised: each aid is one conjunct on its own line, immediately preceded by exactly one line carrying the verbatim marker `-- transitional compressed-chunk pushdown aid, remove with #1342`, never a marker on a `WHERE` or other keyword line, and never one marker for several aids. The shared renderer SHALL produce the `legacy` variant (table `hydro.river_timeseries_legacy`, otherwise verbatim) and the `narrow` variant (table `hydro.river_timeseries`; every marker line and the aid line immediately following it removed; the renderer MUST fail closed when the following line is not an aid), then assert the result parses, references no text identity column, and keeps every key/enum predicate of the legacy variant. A caller that already holds one run's `timeseries_store` SHALL render and execute exactly that one variant. A query that may span runs of both stores SHALL build an occurrence-local `UNION ALL` of the two rendered **fact-row subrelations**, each branch restricted by `hydro_run.timeseries_store`; the union MUST sit before any outer aggregate, `DISTINCT`, ordering, truncation, response-decision layer or data-modifying statement and MUST NOT combine two complete statements. The per-basin MVT source relation, each national LATERAL probe, both valid-time source relations and display coverage's `river_sample_rows` CTE own those combinations; MVT binds store inside its existing `hydro_run` authority scope rather than threading API metadata across module ownership, while the hydro-display identity probe already owns its run metadata and uses that known route's single variant. This composition is caller-side (tasks.md 1.4 Stage 5 re-entry), never a shared text-level combinator. Branches reuse the same named parameters, project the same columns in the same order, and touch only their matching physical table. External response payloads MUST be field-identical between stores for the same run shape. The wave-2 reader code MUST NOT be deployed or executed against the pre-expand catalog, where neither the route column nor the renamed legacy table exists; I7 activates it together with the expand migration. Non-template consumers (`_has_table` prechecks, copyback `required_columns`, statistics-guard hypertable lists, QHH smoke reset/summary scripts, plan-shape fixtures) SHALL branch on store or accept both names during the transition.

#### Scenario: Normalised templates are semantically identical to the pre-change statements
- **WHEN** the normalisation lands and the census and shape pins are re-pinned
- **THEN** the `legacy` variant of every registered template, with `hydro.river_timeseries_legacy` replaced by `hydro.river_timeseries`, is equal to the pre-normalisation statement modulo whitespace, marker placement and conjunct order within one AND-chain (proven by a golden captured at the change base, compared as a conjunct multiset per chain), and every pre-existing test on those readers still passes

#### Scenario: Narrow variant carries no text identity
- **WHEN** the shape oracle renders every registered template with store `narrow`
- **THEN** no rendered statement references `run_id`, `basin_version_id`, `river_network_version_id`, `river_segment_id`, `variable`, `unit` or `quality_flag` as a column of the fact table, no marker line remains, and every key predicate present in the legacy variant is present in the narrow variant

#### Scenario: PostgreSQL qualified-name separator whitespace cannot bypass text-identity refusal
- **WHEN** a fact alias declared as a bare identifier qualifies any text identity column through a PostgreSQL-valid dot separator containing spaces, tabs, newlines, a block comment or a newline-terminated line comment, with either side using its already-modelled bare or exact lower-case quoted token form
- **THEN** the renderer attributes the canonical text identity member through the same public helper responsibilities as the direct-dot form and refuses the narrow variant before returning SQL; comments and whitespace inside string literals/comments remain data, non-exact quoted tokens and other-relation or unmodelled multi-part references do not become fact-column attribution, including when the candidate alias is preceded by another dot-separated component

#### Scenario: Parenthesized whole-row alias field selection cannot bypass text-identity refusal
- **WHEN** scanner-visible outer-query code field-selects a text identity member from exactly one parenthesized bare-declared fact-alias token, with the alias and member written in their bare-folded or exact lower-case quoted forms and with legal scanner-normalised whitespace/comments inside the group or around the dot
- **THEN** both public helpers attribute the canonical member and the renderer refuses the narrow variant as a text-identity predicate before returning SQL; if a balanced parenthesized selection of a known text member contains the attributed fact alias but is a nested, cast, function, row or multipart expression outside that exact grammar, the guarded helper and both render variants instead refuse it as an unmodelled parenthesized fact-alias field selection, while other-relation, non-exact quoted and enum/key expressions retain their prior no-attribution answers
- **AND** this scenario makes no claim about an outer fact-alias reference inside a comparison-position scalar subquery, whose visibility is tracked independently by #2114 before reader wiring

#### Scenario: Correlated outer fact aliases inside comparison-position scalar subqueries cannot bypass guarded rendering
- **WHEN** a comparison-position scalar subquery body contains a known text-identity member reference through a fact alias already attributed in the surrounding query, using the shared direct/exact-quoted/separator, exact/unsupported parenthesized or exact/unsupported unqualified functional grammar
- **THEN** guarded `fact_table_text_identity_columns` and both render variants fail closed before returning an answer, naming the entry and the correlated scalar-subquery reference reason; every nested and later comparison-position scalar body is inspected, including when an earlier independent body is authority-clean and only a later body contains the candidate, a local relation that shadows the outer alias is conservatively refused rather than guessed, and an inner fact-table reread retains the existing count-delta refusal precedence
- **AND** `outer_predicates` and alias-scoped `text_fact_columns` remain outer-query-only; authority-local and other-relation columns, enum/key siblings and non-code bytes retain their prior outcomes, while an unaliased outer fact read with an unqualified scalar-body name remains independently tracked by #2148

#### Scenario: Functional composite field notation cannot bypass text-identity refusal
- **WHEN** scanner-visible code uses an unqualified known text-identity member as PostgreSQL functional field notation with exactly one bare-declared fact alias argument, with the member and alias written in their bare-folded or exact lower-case quoted forms and legal scanner-normalised whitespace/comments
- **THEN** both public helpers attribute the canonical member, legacy remains rename-only, and narrow refuses with the existing entry-named text-identity reason before returning SQL; the shared comparison-position scalar-body seam applies the same grammar without a private matcher
- **AND** a known-member call that contains the fact alias but is outside the exact one-argument grammar — including an unclosed body whose EOF tail ends on an inner close — is refused by guarded helper and both render variants as unmodelled functional fact-alias field notation before a render-only structural error, while tight/spaced/comment-separated/quoted schema-qualified calls, other-relation and alias-superstring arguments, non-exact quoted identifiers, enum/key siblings, placeholder/type/argument-label roles and non-code bytes retain their prior outcomes

#### Scenario: Structural parenthesis checks ignore complete quoted identifiers
- **WHEN** `assert_structurally_intact` receives otherwise valid SQL whose complete double-quoted identifier contains an unmatched `)` or `(` byte
- **THEN** it accepts the statement because quoted identifier bytes are not structural grouping; a direct helper-level regression test owns this guarantee, while the traversal/scanner commutation oracle retains its separate t1/m26 responsibility and is not evidence for the structural helper's double-quote branch

#### Scenario: Whole-row fact-alias star expansion cannot bypass the output contract
- **WHEN** scanner-visible outer-query code projects an exact bare-declared fact alias as `alias.*` or `(alias).*` at the top level of a SELECT output list, with bare ASCII case folding, exact lower-case quoted references, and scanner-normalised whitespace/comments
- **THEN** both public identity helpers report all seven `TEXT_IDENTITY_COLUMNS`, narrow rendering refuses with an entry-named text-identity error before returning SQL, and legacy rendering changes only the physical table name without expanding or rewriting the projection
- **AND** a scanner-visible fact-alias-rooted star whose exact output-expansion grammar cannot be established is refused by the guarded helper and both render variants as unmodelled whole-row star exposure, not reported clean; non-fact relation/CTE stars, complete quoted identifier data, non-exact quoted references, literals/comments and named key/enum projections retain their existing outcomes, and quoted fact-alias declarations retain their existing refusal
- **AND** this guards cross-store result-schema drift rather than asserting that star necessarily names a nonexistent column; comparison-position scalar-body star visibility and unaliased scalar scope remain outside this outer-output slice, while the named-field and correlated named-member contracts remain intact

#### Scenario: Unaliased fact reads cannot hide ambiguous unqualified scalar identity
- **WHEN** a surrounding fact read is unaliased with no attributed fact alias, and any comparison-position scalar body contains a bare-folded or exact lower-case quoted unqualified text-identity candidate not proven safe by the existing finite authority key-resolution shape
- **THEN** guarded `fact_table_text_identity_columns` and both render stores refuse before returning a clean set or SQL, with an entry-named unaliased/unqualified scalar-scope reason; the renderer does not guess whether an inner relation or the outer fact owns the name
- **AND** qualified non-fact references and the existing run/basin/network authority key-resolution shapes remain accepted, outer unaliased comparisons keep existing attribution, and alias-scoped `text_fact_columns` plus `outer_predicates` remain unchanged
- **AND** bare-folded and exact quoted candidates across all seven identity members, nested/later scalar bodies and ambiguous inner SELECT/derived namespaces are covered; literal/comment data, non-exact quoted names and key/enum tokens are not fact attribution, and aliased/scalar named-member, whole-row-star and structural sibling contracts are preserved

#### Scenario: Renderer refuses a mis-shaped marker
- **WHEN** a template places the marker above a line that is not a single aid conjunct
- **THEN** the renderer raises before returning SQL and the shape oracle fails naming the template

#### Scenario: A caller that owns run metadata executes one routed variant
- **WHEN** the hydro-display MVT source-identity probe receives a run whose metadata names one store
- **THEN** it renders and executes only that store's variant, retains one `SELECT 1 ... LIMIT 1` result decision, and preserves the existing not-found response contract without a statement-level union

#### Scenario: Forecast segment discovery combines routed facts below its semantic operators
- **WHEN** any of the eight forecast-store segment queries may see runs from both stores
- **THEN** each fact-source branch is rendered for and bound to its authoritative run store, legacy aids remain only in legacy, common projections and named parameters are shared, and the single outer latest-cycle, maximum-time, distinct winner, window and ordering logic operates on the combined rows without duplicated aggregates or limits
- **AND** equivalent data in either store yields field-identical forecast/analysis/hindcast responses and unchanged lineage; a selected scenario/cycle is not mistaken for a unique run's store

#### Scenario: Latest-product fallback routes its pinned run from real header metadata
- **WHEN** the existing latest-product fallback header identifies one candidate run
- **THEN** its actual timeseries_store selects exactly one rendered river_sample_rows source on the same transaction snapshot, while station coverage, strict identity, scan scalars and response fields remain unchanged; missing/null/unknown store is refused before heavy SQL, and an empty header still short-circuits
- **AND** no constant legacy or missing-column fallback is introduced; this reader code is activated only with the I7 expand migration

#### Scenario: Per-basin hydro MVT aggregates routed source rows once
- **WHEN** a per-basin hydro tile is requested for a legacy or narrow run
- **THEN** each source branch binds its store literal inside the existing run-key authority lookup and reads only its matching physical table with the same twelve output columns and named parameters; the two fact-row sources are unioned inside source_rows, below one unchanged MVT aggregate, statistics, geometry, ordering and budget pipeline
- **AND** the consumer still receives one result row, with unchanged decoded feature properties/geometry and existing empty, invalid-property and over-budget outcomes; no store argument is added to the API

#### Scenario: National MVT routes three probes below unchanged semantic layers
- **WHEN** a non-truncating national tile request covers eligible networks whose selected runs use both physical stores, in either zoom regime
- **THEN** both existing candidate-run scopes project their authoritative store without duplicating discovery; the identity probe and both identical data probes each union matching-store fact rows below one existing probe limit
- **AND** the separate identity EXISTS limit and its 0/1 presence result, existing zoom unions, ranking, geometry, budget and one-row MVT aggregate remain unchanged; decoded payload and statistics match the pre-transition logical snapshot
- **AND** the live raw registry contains the distinct authored identity/data inputs rather than the composed national statement, while the historical golden remains immutable

#### Scenario: Opposite-store facts cannot satisfy a national identity gap
- **WHEN** display coverage contains the requested instant but no selected run has an authoritative-store fact at that instant, even though opposite-store decoys do
- **THEN** the exact-instant identity probe reports source_identity_count zero and the route retains its probe-specific 424 response, not an empty-tile 200

#### Scenario: Benchmark consumers preserve current reader named bindings
- **WHEN** the public curve owner emits named SQL parameters for benchmark capture
- **THEN** capture preserves the actual parameter Mapping, records canonical unique name/value pairs in the existing receipt fields, and passes a Mapping to the database driver; repeated placeholders reuse one recorded value rather than restoring positional aliases
- **AND** the verifier rejects missing, extra, duplicate, misnamed or value-drifted bindings while retaining exact production-query/source checks, the issue_time lower bound, the seven-day request and existing strict selected-chunk overlap guards; no other store lifecycle or production activation work is implied

#### Scenario: Limited discovery applies its semantic operators once
- **WHEN** either valid-time discovery branch may read runs from both stores
- **THEN** its legacy and narrow fact-row branches are unioned below one outer `DISTINCT valid_time`, descending order and `LIMIT :limit`, so `sample_limit + 1` and caller-side truncation are unchanged
- **AND** named branches bind the requested run and store in their run-key authority lookup, while any-identity branches associate each fact run key with the matching store without adding user identity or coverage filters; safe enum lookup and empty unknown-identity/enum behavior remain unchanged

#### Scenario: Discovery retains newest samples before returning ascending values
- **WHEN** either named or any-identity `valid_times_for_layer` call uses sample_limit two and its captured SQL returns three distinct instants T2, T1, T0 in descending order
- **THEN** the query binds limit three, the response retains T1 and T2 in ascending order, observed_count is three and truncated is true; sorting the whole sample before truncating is not equivalent

#### Scenario: Coverage unions only its river fact-row CTE
- **WHEN** display coverage refresh scans candidate runs from both stores
- **THEN** `candidate_runs` carries each run's route, `river_sample_rows` unions one matching branch per store, and the station legs, river aggregation, overwrite guard and single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` remain outside and unchanged

#### Scenario: Mixed-store discovery returns both
- **WHEN** a national-tile, coverage, or named/any-identity valid-time discovery query runs against a real database holding one `legacy` and one `narrow` published run
- **THEN** both runs appear in the national-tile and coverage result, the enclosing aggregate/result/DML layer executes once, and `EXPLAIN` shows each fact branch touching only its matching table
- **AND** separate named-identity requests for the legacy and narrow run each return only that routed run's `valid_time`, while one any-identity request includes `valid_time` values contributed by both stores in its fact-row union before one outer `DISTINCT valid_time ORDER BY valid_time DESC LIMIT :limit`; each form's `EXPLAIN` shows every fact branch references only its matching physical table and the non-matching named branch produces no rows

#### Scenario: Reader transition activates with expand, not before
- **WHEN** wave-2 reader changes have merged but the I7 maintenance-window deployment and expand migration have not run
- **THEN** those transition statements are not deployed or executed against the pre-expand catalog; I7 deploys them together with the route column and `_legacy` table name
- **AND** explicitly authorized verification may prepare a post-expand catalog in a fresh per-test disposable database, preserving frozen pre-transition comparisons and original assertions; unmigrated sibling tests keep their pre-expand isolation, and this is not production deployment, Timescale-plan or live-receipt evidence

#### Scenario: Smoke reset clears both stores
- **WHEN** `scripts/reset_qhh_smoke_db.py` deletes a legacy run
- **THEN** its rows are removed from `hydro.river_timeseries_legacy` and the run's summary from `scripts/summarize_qhh_smoke_results.py` reads the same store

### Requirement: Per-segment curve access SHALL be index- or segmentby-pruned on both chunk states, and every disappearing index SHALL pass the hygiene evidence gate

For the largest registered river network (SHJ-NJ pin) and one small network, the per-segment forecast-series SQL SHALL show `river_segment_key` in an `Index Cond` on narrow uncompressed chunks and in segmentby batch pruning on narrow compressed chunks, with `Rows Removed by Filter / rows returned ≤ 10`, `shared hit ≤ 5000`, SQL warm P95 ≤ 300 ms over at least five warm samples, and the node-27 local single-source `forecast-series` warm P95 ≤ 500 ms. Because the narrow table omits `river_timeseries_valid_time_idx`, the identity-existence probe's interior-gap miss branch SHALL be measured before (legacy) and after (narrow) with `EXPLAIN (ANALYZE, BUFFERS)` and every coverage loss enumerated, as `timeseries-index-hygiene` requires.

#### Scenario: EXPLAIN receipt on node-27
- **WHEN** the rollout receipt runs the curve SQL for both pins on a narrow uncompressed chunk, a narrow compressed chunk and the legacy baseline, and the identity-existence probe miss branch on legacy and narrow
- **THEN** each curve plan meets every bound above and the receipt records plan text, buffers, filtered rows, index or segmentby name, chunk compression state, the registry counts (active/runnable/selected/excluded) at capture time, and the probe's before/after plans with the coverage-loss list

#### Scenario: Regression halts the contract
- **WHEN** any pinned curve, MVT or display shape regresses by an order of magnitude against the legacy baseline, or a Seq Scan appears on the fact table
- **THEN** the contract batch MUST NOT proceed; the runbook records the missing access path to rebuild first
