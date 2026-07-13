# Storage-inventory-audit live evidence

This directory holds node-27 live evidence for the inventory-audit lane. A file whose `evidence_type` is `node27-systemd-stderr-delta` is a bounded journal/stderr proof, not an archive-completeness receipt and must not be consumed by the retention gate.

## `wrapper-import-live-20260713T060353Z.json`

Issue #1067 proof captured on node-27 from PR #1073 head `586558c055b46bee98db4b4667d772cd4f0e133c`:

- The service was triggered through its installed user-systemd unit and repository wrapper.
- Evidence was isolated by recording the stderr file byte count before the run and retaining only bytes 407-579 appended by that invocation. This avoids mixing the historical `ModuleNotFoundError` traceback with the fixed run.
- The isolated delta contains one `AuditBlocked` JSON record for the independently tracked object-URI prefix defect (#1066).
- The isolated delta contains zero occurrences of `No module named 'scripts'`; the wrapper therefore crossed the former import failure and reached audit runtime behavior.
- The non-zero service result is expected until #1066 is fixed. A schema-valid archive-completeness receipt is intentionally not claimed here and will be captured after #1066/#1065 unblock the live lane.

The same PR head passed `tests/test_node27_wrapper_pythonpath.py` on node-27 (`11 passed`).

## `wrapper-invariant-live-20260713T065048Z.json`

Post-review invariant-closure proof from PR #1073 head `aa880b8d4378e68e3327a290f8fd0baae1a2a9ca`:

- Node-27 focused wrapper matrix passed (`89 passed`).
- The installed audit systemd lane passed the new root/checkout/namespace-origin preflight and reached the same independent #1066 `AuditBlocked` path.
- The isolated stderr delta is bytes 579-751 from that invocation, with zero historical import errors and zero import-origin refusals.
- The service remains non-zero solely because #1066 is still open; this file is not a completeness receipt.

## `wrapper-pathmodel-live-20260713T075745Z.json`

Final issue #1067 path-model proof from PR #1073 head `699d2921616cddf411cdc2d18d865ce392a7223e`:

- Node-27 passed the expanded real-interpreter wrapper matrix (`140 passed`), including `PYTHONSAFEPATH=1`, script-directory shadow packages, explicit entrypoints outside the governed checkout, and empty inherited path segments.
- The installed audit systemd lane passed the matching file-launch preflight and again reached the independent #1066 `AuditBlocked` path.
- The isolated stderr delta is bytes 751-923, with zero import errors and zero import-origin refusals.
- The service remains non-zero solely because #1066 is still open; this bounded stderr proof is not an archive-completeness receipt.

## `completeness-incomplete-live-20260713T093237Z.json`

Issue #1066 first schema 1.1 terminal receipt from node-27 at implementation head `1f7bba9d89a80b563fc51ff6ef407189ca5ee58b`:

- Both installed audit and product-archive env files use the node-27 producer/DB URI identity `s3://nhms`.
- The installed audit systemd unit completed successfully with no new stderr bytes and published a mode-0600 `incomplete` receipt.
- The receipt contains 1,585 real DB subjects: 1,357 `complete` and 228 `gap`; the 228 exact selectors are retained for the independently tracked #1065 archive-layout/mover closure.
- Draft 7 validation with date-time format checking and the audit runtime semantic validator both passed.
- The committed copy SHA-256 is `3b91c0821e1f0c8e3d9ffae7bbef07ab2e7c3a22d8a2910b7f5044a7438a236d`, byte-identical to the live stable receipt.
- This is an actual archive-completeness receipt, unlike the three earlier bounded stderr proofs. No #856 retention action was run.

## `terminal-receipt-live-20260713T093237Z.json`

Machine-readable execution envelope for the same #1066 run: code/config identity, systemd result, stderr byte boundary, receipt counts/hash/mode, and validator results.

## `completeness-incomplete-live-20260713T155314Z.json`

Final reviewed #1066 schema 1.1 terminal receipt from node-27 at frozen implementation head `bf9124aea6667fc116c872614d92de0e74a6cab1`:

- Node-27 passed the expanded affected-surface regression on the same checkout (`2123 passed, 1 skipped`).
- The installed user-systemd audit unit completed successfully with no new stderr bytes and published a mode-0600 `incomplete` receipt.
- The receipt contains 1,585 real DB subjects: 1,357 `complete` and 228 `gap`; the exact 228 selectors remain the input evidence for independently tracked issue #1065.
- Draft 7 validation with date-time format checking and the runtime semantic validator both passed.
- The live stable receipt and committed copy are byte-identical: 472,062 bytes with SHA-256 `e2d4f08150943f09af87d3e53e79cff26728fb438aabb545dabff07842497d04`.
- No #856 retention dry-run or enforce action was executed.

## `terminal-receipt-live-20260713T155314Z.json`

Machine-readable final execution envelope for the same frozen-SHA run: code/config identity, systemd result, zero-byte stderr delta, receipt counts/hash/mode, and validator results. This supersedes the earlier implementation-head envelope as the #1066 merge evidence.
