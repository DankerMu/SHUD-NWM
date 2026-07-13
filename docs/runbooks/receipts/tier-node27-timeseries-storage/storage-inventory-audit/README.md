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
