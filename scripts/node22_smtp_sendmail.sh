#!/usr/bin/env bash
# node-22 SMTPS sendmail transport for the unit-failure alert lane (#3127,
# OpenSpec node22-alert-smtp-wrapper). Point NHMS_FRONTIER_SENDMAIL in
# infra/env/node22-unit-failure-alert.env at this file.
#
# Runs the node-27 authenticated-SMTPS shim (scripts/node27_frontier_smtp_sendmail.py,
# #1368) under the exact node-22 interpreter: that shim's own shebang resolves
# an interpreter through PATH, which node-22 must not use. Arguments, stdin,
# environment and exit status pass through unchanged via exec; this wrapper
# reads no credential and prints nothing on success.
#
# Fail closed: a missing or non-executable interpreter, or a missing shim,
# prints one `node22-smtp-sendmail: ...` line to stderr and exits 64 (the
# shim's own usage/config code). There is no fallback interpreter and no
# override knob; the paths below are the node-22 active checkout.

set -u

PY=/scratch/frd_muziyao/NWM/.venv/bin/python
SHIM=/scratch/frd_muziyao/NWM/scripts/node27_frontier_smtp_sendmail.py

if [ ! -x "$PY" ]; then
    printf 'node22-smtp-sendmail: INTERPRETER_MISSING path=%s\n' "$PY" >&2
    exit 64
fi
if [ ! -f "$SHIM" ]; then
    printf 'node22-smtp-sendmail: SHIM_MISSING path=%s\n' "$SHIM" >&2
    exit 64
fi

exec "$PY" "$SHIM" "$@"
