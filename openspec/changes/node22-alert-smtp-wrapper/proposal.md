# node22-alert-smtp-wrapper (#3127)

## Why

The node-22 unit-failure alert lane (#2913/#2914) was never deployed; #3121's prune failure was dropped by systemd
(`OnFailure= ... not found`). Deployment needs a delivery transport with proof. node-22's local postfix exit 0 only
means "queued" (no relayhost, mail.log unreadable to the service user) — the same false-success class #1368 fixed on
node-27 with `scripts/node27_frontier_smtp_sendmail.py` (authenticated SMTPS, synchronous `SMTP-ACCEPTED` line). That
shim has an `#!/usr/bin/env python3` shebang, which node-22 must not use (system Python is not a substitute; the
example already says a shim must run through `/scratch/frd_muziyao/NWM/.venv/bin/python`).

Owner decision 2026-11-12 (#3127 D1-A): reuse the node-27 shim on node-22 through a tiny wrapper; credentials and
addresses are copied host-to-host by the operator into the 0600 env file (never committed, never displayed).

## What

- New `scripts/node22_smtp_sendmail.sh` (mode 0755): `exec /scratch/frd_muziyao/NWM/.venv/bin/python
  /scratch/frd_muziyao/NWM/scripts/node27_frontier_smtp_sendmail.py "$@"`; stdin/argv/exit code pass through
  unchanged. Missing/non-executable interpreter or missing shim -> one `node22-smtp-sendmail: ...` stderr line and
  exit 64 (the shim's own usage/config code), never a fallback to another Python.
- Tests for the wrapper; `select_ci_tests` routing for the wrapper (and the shim also selects the node-22 suite).
- `infra/env/node22-unit-failure-alert.example` comment: point at the wrapper instead of "no shim delivered". The
  example key set and the `/usr/sbin/sendmail` example value stay exactly as specified (no spec change there).

Non-goals: the deployment itself (operator action with receipt, recorded in the post-merge archive PR); the handler,
template and units (unchanged); `nhms-compute-scheduler.service` OnFailure (design Q1); credential rotation.

Fixture level: compact (upstream: none; one isolated wrapper script, no shared entrypoint/schema change).
Blast radius: a wrong wrapper makes the alert lane exit non-zero (handler `SEND_FAILED`, visible) or silently use the
wrong interpreter (guarded by tests).
Selected risk packs: Public CLI/script entry (exit codes), Auth/secrets (env pass-through only; wrapper never reads or
prints NHMS_SMTP_*), Config (node-22 interpreter rule).
Evidence floor: `uv run pytest -q tests/test_node22_unit_failure_alert.py tests/test_select_ci_tests.py
tests/test_node22_entrypoint_invariant.py tests/test_node22_entrypoint_invariant_python_scan.py
tests/test_env_templates.py tests/test_node22_unit_tracking.py` (oracle: node-27, `TMPDIR=/home/nwm/tmp`; also run locally); `uv run ruff check .`; `openspec validate node22-alert-smtp-wrapper --strict --no-interactive`.

design.md omitted (compact).
