# Close the retention-env shape gate: closed-world line grammar (#1230)

## Why

`read_retention_window_days` (#1227/PR #1229) detects unsupported
assignment shapes by OPEN-WORLD enumeration: a non-comment line is only
refused when it contains the literal
`NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=` substring without being
accepted as that assignment (`packages/common/storage.py:334`). Eight
shell forms that export the window WITHOUT that substring (`VAR+=21`,
`: ${VAR:=21}`, nested `. other.env` / `source other.env`, `printf -v`,
`read`, `eval`, append-after-plain) pass the gate silently: the helper
resolves the runner-equivalent default 14 while `set -a; . file` exports
a larger window — the exact #1227 fail-open direction through a narrower
door, and `node27_product_archive.py` runs `--enforce` hourly on the
result with zero signal. Differentially reproduced 8/8 in the issue
(helper=14 vs runner larger). PR #1229 shipped with the spec claim
narrowed to "the detectable set" and these shapes recorded as a residual
tracked by #1230; this change closes all 8 enumerated shapes and every
other non-`KEY=VALUE` line shape. It does NOT close the multi-line
quoted-value class when every line happens to conform (design D5(a2),
fixture-review P1-1) — that variant stays a recorded, xfail-pinned
fail-open residual.

## What Changes

Closed-world file grammar (issue's recommended direction (b), verifier
pre-assessed):

- `_scan_env_assignment`: every non-empty, non-full-line-comment line
  MUST fullmatch `_ENV_ASSIGNMENT_PATTERN` (`[export ]KEY=VALUE`, any
  variable name). The first non-conforming line raises
  `ArchiveConfigurationError` naming the file path and the offending
  line (`{candidate!r}`) — replacing today's silent `continue`. This
  closes all 8 divergent shapes, including nested source lines that a
  bare-token detector (alternative (a)) cannot see.
- The per-line mention refusal (#1229 round-2 C2) is KEPT as a second
  layer (post-grammar it is reachable via two conforming-line paths:
  a VALUE embedding `NAME=`, e.g. `X=NODE27_..._WINDOW_DAYS=21`, and a
  KEY-suffix decoy, e.g. `OLD_NODE27_..._WINDOW_DAYS=99` — refusing
  stays fail-closed in both) and its message gains the offending line
  (`{line!r}`), matching the malformed-value message precedent.
- Acceptance set STRICTLY SHRINKS: every input accepted after the change
  was accepted before with the same result; every input refused before
  stays refused. No refuse→accept flip exists.
- Tests: the 8 shapes join `_UNSUPPORTED_SHAPE_ROWS` and flow into the
  differential oracle automatically; the
  `multi-line-quoted-value-known-exception` strict xfail is re-recorded
  as a plain refusal row (grammar refuses the closing bare-`"` line —
  XPASS must not remain) and REPLACED as class tripwire by a new strict
  xfail row for the all-conforming D5(a2) body; grammar-refusal and
  mention messages both get offending-line assertions; a new
  template-conformance test exercises all 15 `infra/env/*.example`
  files through the public helper (zero grammar-class refusals).
- Docs/spec, same commit: main spec `timeseries-product-archive`
  "Unreadable window source fails closed" scenario rewritten from
  "detectable substring" wording to the closed-world grammar; runbook
  `tier-node27-timeseries-storage.md` residual paragraph (~:204-215)
  rewritten: the 8 enumerated non-`KEY=VALUE` shapes are now refused,
  the file-format constraint (blank / `#` comment / `KEY=VALUE` only)
  becomes the enforced contract, and the residual list narrows to the
  multi-line quoted-value class — BOTH variants recorded: bare closing
  quote (over-strict, fail-closed) and all-conforming lines (still
  fail-open; quoted values MUST NOT span lines).

## Non-goals

- #1227 min-age comparison semantics (shipped in PR #1229).
- The retention runner's own parsing; replacing the helper with a real
  bash `source` (larger security surface — rejected in the issue).
- Unbalanced-quote detection to close the multi-line-quote residual in
  EITHER variant (a1 over-strict fail-closed, a2 still fail-open) —
  both stay recorded, xfail-pinned, not fixed here.
- The archive templates' pinned refusals (round-2 C1) — unchanged.
