# Design: qhh-bootstrap-provenance-parameterized

## Change surface

`workers/model_registry/qhh_production_bootstrap.py` @ origin/master
(issue verified @ 9ff9e563; re-verified at branch base — coordinates
identical): `_seed_station_rows` :1422, hardcoded literals :1441 and
:1452; correct neighbors :1440 (`project_name`), :1446
(`forcing_source_identity` f-string); asset path :737; entrypoints
:196/:642-646/:654/:1053/:1057; sibling correct lane
`_output_segment_expected_properties` :1582 with :1597/:1601; ops
exposure `scripts/seed_qhh_forcing_stations.py:17/:21/:31`. NO existing
default-value oracle exists (fixture-review P1-2): tests :1557 is an
INSERT input fixture inside integration-gated
`test_bootstrap_blocks_stale_qhh_sibling_rows_before_scheduler_visibility`
(skips without NHMS_INTEGRATION_DATABASE_URL) — the new parametrized
test's qhh leg becomes the first real pin. Harness pattern :723-781:
FakeCursor + monkeypatch on execute_values, no disk file, no DB;
properties read via `calls[0]["argslist"][0][8].adapted` (psycopg2
Json).

Risk triage: compact fixture, minimal end of the scale — 2-line
literal→f-string change whose default-path output is byte-identical.
Highest risks: (1) touching #1359's lane by accident (acceptance
forbids any file_store.py change), (2) breaking the default-value pin
:1557, (3) a downstream literal consumer of `"qhh.tsd.forc"` from
properties_json (never-break-userspace pre-check).

## Key decisions

1. **Recommended route only**: both literals become
   `f"{project_name}.tsd.forc"`, copying :1597's shape.
   `project_name` is ALREADY a keyword-only parameter of
   `_seed_station_rows` (:1427, consumed at :1440/:1446) — the change
   is exactly the two literals, no threading, no signature change
   (fixture-review P2-2 removed the hedge). No authority ruling needed
   because the value stays convention-derived, consistent with :1446
   by construction.
2. **Default output byte-identical**: with `project_name="qhh"` the
   f-string yields `"qhh.tsd.forc"` — guarded by the new parametrized
   test's qhh leg (first real pin); PR body states explicitly this
   satisfies (not violates) #1359's ":1441/:1452 unchanged" clause per
   the issue's instruction.
3. **Error-message literals untouched**: the `read_qhh_tsd_forc`
   diagnostics ("QHH qhh.tsd.forc is not valid UTF-8..." etc.) are
   error text, not persisted provenance — out of scope.

## Must preserve

- Default QHH invocation: persisted `properties_json` byte-identical
  (source, elevation_metadata.source, and every other key).
- Zero changes to `workers/forcing_producer/file_store.py` (acceptance
  item — diff-checked).
- Existing tests: zero modified assertions (the new test is additive).
- No behavior change to parsing/validation/binding.

## Seams under test

Harness :723-781: FakeCursor + `monkeypatch.setattr` on
`execute_values` — no disk file (tsd_forc_path only stringified at
:1442, checksum opaque), no DB. New test calls `_seed_station_rows(...,
project_name=<param>, tsd_forc_path=tmp_path / f"{param}.tsd.forc",
...)` and reads `calls[0]["argslist"][0][8].adapted`.

## Test plan (maps to acceptance)

1. ONE parametrized test over ("qhh", "qhh.tsd.forc") and ("heihe",
   "heihe.tsd.forc"): assert `source == expected`,
   `elevation_metadata["source"] == expected` (nested — its own
   assertion line, a shallow `properties["source"]` check would miss
   :1452), `forcing_source_identity` startswith `expected + ":"`,
   `project_name == param`. For the heihe leg only: assert none of
   those FOUR field values contains `"qhh"` — scoped to the fields,
   NOT a blanket `"qhh" not in json.dumps(properties)` (harness
   model/basin/station ids legitimately contain qhh:
   `basins_qhh_shud` / `qhh` / `qhh_forc_001`).
2. Full `uv run pytest -q tests/test_qhh_production_bootstrap.py`
   green — note the integration-marked tests SKIP locally; "green"
   means non-skipped tests pass and the new test actually EXECUTES
   (not skipped).
3. Red evidence: heihe leg fails against unmodified source (source
   stays "qhh.tsd.forc"); qhh leg is naturally green (it pins existing
   behavior) — label honestly.

## Risks to watch

- Never-break-userspace pre-check COMPLETED by fixture review (see
  proposal Impact) — implementer records, does not re-derive.
- Backfill deferral is explicit: follow-up issue (node-27) for sizing
  (GROUP BY over met.met_station seed-lane rows) + remediation
  decision on the already-persisted mislabeled heihe rows.
