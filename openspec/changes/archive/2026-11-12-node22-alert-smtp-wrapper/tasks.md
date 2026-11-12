# Tasks — node22-alert-smtp-wrapper (#3127)

## 1. Wrapper

- [x] 1.1 `scripts/node22_smtp_sendmail.sh` (git mode 100755, `#!/usr/bin/env bash`): constants `PY=/scratch/frd_muziyao/NWM/.venv/bin/python`, `SHIM=/scratch/frd_muziyao/NWM/scripts/node27_frontier_smtp_sendmail.py`; `[ -x "$PY" ]` else stderr `node22-smtp-sendmail: INTERPRETER_MISSING path=<PY>` exit 64; `[ -f "$SHIM" ]` else `node22-smtp-sendmail: SHIM_MISSING path=<SHIM>` exit 64; then `exec "$PY" "$SHIM" "$@"`. No env override knobs, no `uv`, no `python3`, never reads or echoes `NHMS_SMTP_*`.
- [x] 1.2 Tests (stdlib + pytest, in `tests/test_node22_unit_failure_alert.py` or a sibling file routed by the same rule): static (exists, executable bit, exact `PY`/`SHIM` lines once each, no `python3`/`uv`/`NHMS_SMTP_` tokens); behavioural via a copy whose `PY`/`SHIM` constants are textually substituted with tmp fakes: argv `-t -i` forwarded after the shim path, stdin bytes forwarded byte-exact, fake exit 69 propagated, missing interpreter -> 64 + `INTERPRETER_MISSING` without invoking anything, interpreter present but not executable (mode 0644) -> 64 + `INTERPRETER_MISSING`, missing shim -> 64 + `SHIM_MISSING` without invoking the fake interpreter; plus a static assertion that the `SHIM` constant minus `/scratch/frd_muziyao/NWM/` is a git-tracked existing file (so the shim route below has a real reader).
- [x] 1.3 `select_ci_tests` rules: `scripts/node22_smtp_sendmail.sh` and `scripts/node27_frontier_smtp_sendmail.py` select the suite holding 1.2; in `tests/test_select_ci_tests.py` the wrapper row joins `NODE22_UNIT_FAILURE_ALERT_NEW_ROWS` (the complement test counts rows exactly) and the shim row is asserted separately as an extra target; the shim row MUST NOT set `stop_on_match` so `tests/test_node27_frontier_smtp_sendmail.py` stays selected (rule matching is additive).

## 2. Docs

- [x] 2.1 `infra/env/node22-unit-failure-alert.example`: SMTP-shim comment names `/scratch/frd_muziyao/NWM/scripts/node22_smtp_sendmail.sh` as the node-22 `NHMS_FRONTIER_SENDMAIL` value for the SMTPS path; add one line that the address part of `NHMS_ALERT_EMAIL_FROM` must equal `NHMS_SMTP_USER` (the shim exits 64 at config time otherwise, `scripts/node27_frontier_smtp_sendmail.py:271-292`); key set and example values unchanged.

## 3. Post-merge (operator, archive PR)

- [x] 3.1 node-22 deploy receipt (template, env 0600, six wired units, daemon-reload, `systemd-analyze --user verify`, drift probe, one test mail with `SMTP-ACCEPTED`) under `docs/runbooks/receipts/`; `infra/env/README.md` row no longer "未部署".
