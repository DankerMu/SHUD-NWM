## Why

Production display requests exhaust the default connection pool (#2346), while PNG retention lacks its cache-root key (#2431). Independently, fresh raw cycles stop before forcing generation (#2432); diagnosis must identify that boundary without claiming recovery.

Issue type: bugfix (operational configuration and diagnosis)
Fixture level: expanded
Upstream suggested level: absent (production configuration, deletion and shared-state evidence require expanded)
Blast radius: node-27 display availability and cache retention; node-22 diagnosis is read-only.
Selected risk packs: public entry; config; file safety; secrets; concurrency; resource limits; compatibility; error handling; documentation.
Evidence floor: measured connection budget, restarted display smoke and read-only boundary, real PNG retention lane summary, upstream causal evidence, strict OpenSpec validation.

## What Changes

- Apply only #2346 first-step pool sizing, targeting 8+8 per worker after live capacity admission; leave isolation, prewarming, role tuning and SQL optimization outside this batch.
- Set the real node-27 retention cache root equal to the display cache root and observe plan and execution, preserving canonical lock safety.
- Diagnose #2432 from concrete forcing provenance and producer evidence; no scheduler repair, replay, environment migration or new alerting.
- Record receipts, rollback instructions and issue-specific completion boundaries in one PR.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `runtime-evidence-and-operations`: add bounded operational configuration and upstream diagnosis evidence requirements.

## Impact

Actual ignored node-27 environment files, existing display/retention services and their operational documentation. No public response schema, code default, database schema, role settings, worker count or node-22 runtime changes. #2346 remains open for its second step. #2432 diagnosis completion is distinct from pipeline recovery.
