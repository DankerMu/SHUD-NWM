# Tasks

## Implementation

- [ ] T1 Add the canonical `description` (design D2, verbatim) to the five schema properties: `recovery.invocation` (`:90`), `migration.first_invocation` / `second_invocation` (`:128-129`), `receipts.dry_run_invocation` / `enforce_invocation` (`:157-158`) in `schemas/timeseries_compression_live_evidence.schema.json`.
- [ ] T2 Add one comment line at each verifier overwrite point — `scripts/node27_timeseries_compression_live_evidence.py:3535`, `:3566-3567`, `:3619-3620` — pointing at `scripts/node27_timeseries_compression_bundle_author.py:21-25`. Comments only; no logic edits.
- [ ] T3 Rewrite `docs/runbooks/tier-node27-timeseries-storage.md:770-771` so the five keys read as mandatory, naming them, per design D4. The top-level key list at `:755-763` stays a top-level list.
- [ ] T4 Add the guard test (design D3) to `tests/test_node27_timeseries_compression_live_evidence.py`, reusing the schema already loaded at `:45`. The slot list is **derived** from the schema (properties named `*invocation` whose subschema `$ref`s `#/$defs/artifact_ref`) and asserted to equal the five known slots, not hardcoded.

## Evidence Floor

- [ ] E1 Local: `uv run ruff check .`
- [ ] E2 Local: `uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py tests/test_node27_timeseries_compression_supervisor.py tests/test_node27_timeseries_compression_capture.py` green, including the new guard test and the untouched `test_legacy_authored_invocations_do_not_contribute_to_v3_truth`.
- [ ] E3 Local: `uv run pytest -q tests/test_migrations.py -k terminal_state || true` is not the oracle — instead prove the annotations are inert for validation: `uv run python -c "from packages.common.compression_terminal_state import CANONICAL_SCHEMA; import json; json.loads(CANONICAL_SCHEMA.read_text())"` plus the committed-receipt/terminal-state tests that already exercise `validate_terminal_document` run green in E2.
- [ ] E4 Local: `openspec validate compression-invocation-slot-truth-source --strict --no-interactive`.
- [ ] E5 Fence self-evidence from `git diff master...HEAD`: `scripts/node27_timeseries_compression_live_evidence.py` diff is exactly three added comment lines; `:1148` (#1261 ruling) unchanged; `_validate_exact_command_argv` / `_concrete_argv` unchanged; schema `database_audit_proof` `{"const": false}` pins unchanged; `schemas/examples/timeseries_compression_live_evidence.example.json` not in the diff; no new launcher/interpreter gate.
- [ ] E6 Carrier sweep, re-run after **every** fix commit: the canonical sentence (design D2) is present and identical in all six carriers (five schema descriptions, three verifier comments, runbook, spec delta, design D2, PR body); and the three refuted formulations have zero hits outside D2/proposal's explicit record of them — `输入值被忽略` / `input value is ignored`, `never reaches the terminal state`, and `never parsed` / `content never interpreted` / `constrained only as an artifact reference`.
- [ ] E7 CI green on the final head.
- [ ] E8 PR description records 方案 a and its reason, and does not present the change as adding launcher/interpreter identity validation (#1261 fence).

## Not required

- No node-27 live receipt. 方案 a is pure-local by the issue's own "执行成本澄清"; a live receipt is a 方案-b cost only.
