# Tasks — audit-producer-window-containment (#1158)

Fixture level: compact (S scale: one function's comparison semantics + four
test legs; retention-gating evidence surface — fail-closed direction must
be provably preserved).

Risk triage: the audit is the retention gate's evidence source (ADR 0002:
the retention gate IS the archive receipt gate — never bypassed). The risk
axis is accidentally weakening fail-closed: containment must ONLY admit
the superset direction; subset, unparseable, and identity mismatches must
keep blocking. Seam under test: `audit.verify_product_archive` (the
producer check is reached through it; no test calls the private function
directly today). Risk packs:
contract pack (fail-closed invariant, spec alignment) + test-integrity
pack (red proof for the superset leg, block-preserving legs pinned). Not
selected: perf/security — no IO or trust-boundary change.

## 1. Implementation (implementer)

- [x] 1.1 `scripts/node27_storage_inventory_audit.py`
  `_verify_product_producer_provenance` (~:610-630): identity fields
  (`kind`, `subject_id`, `manifest_path`, `model_id`,
  `basin_version_id`) keep the equality loop and current message shape;
  `start_time`/`end_time` move out of the loop into a containment check:
  parse both producer values with the module's `_parse_time` INSIDE a
  `try/except AuditBlocked` that re-raises the subject-level message
  (`_parse_time`'s own messages lack subject_id and are not the promised
  sanitized shape); producer window must satisfy `producer_start <=
  subject.start AND producer_end >= subject.end`; violation or
  unparseable/missing value → `AuditBlocked("product archive producer
  window does not contain DB inventory window for <subject_id>")` (a NEW
  stable message). `manifest_sha256` binding and everything after the
  loop unchanged. Do NOT add an upper sanity bound on how much larger
  the producer window may be: the embedded/outer producer equality
  binding (`node27_product_archive.py:1948-1959`) and the mover's
  `start <= cycle <= end` check (`:1904`) already pin the window to the
  real source package declaration; an extra bound would over-tighten.
- [x] 1.2 Constraints: no receipt schema/outcome/reason-code changes; no
  other audit check touched; function stays raise-based (`AuditBlocked`)
  with sanitized messages (no secrets/paths beyond subject_id).

## 2. Tests (implementer; red-provable)

- [x] 2.1 Seam is `audit.verify_product_archive(...)` in
  `tests/test_node27_storage_inventory_audit.py` (NO test today calls
  `_verify_product_producer_provenance` directly). Fixture shape is
  MANDATED (fixture-review P1-1): reuse the UNTAMPERED archive from
  `_write_forcing_and_run_product_archives(tmp_path)` (`:242-308`,
  package window START..END). For legs (a)(b)(c) vary the DB SUBJECT,
  not the manifest — mutating the outer manifest's producer window is
  FORBIDDEN (the embedded/outer producer equality binding at
  `node27_product_archive.py:1948-1959` intercepts it first and
  invalidates the leg). Leg (d) is the EXCEPTION: identity mismatch
  stays a manifest-identity-field mutation (the existing parametrized
  cases at `:346-351` already cover `model_id`/`subject_id`/
  `basin_version_id` — reuse, don't rewrite); do NOT do (d) by changing
  `subject.model_id` — that points at a nonexistent archive leaf and
  `verify_product_archive` returns `None` (ordinary absence), not
  `AuditBlocked`. Four legs:
  (a) equality — original subject unchanged → passes (guards against
  over-tightening);
  (b) superset (the incident shape) — `replace(subject, end=END -
  timedelta(hours=3))` → passes after the fix; MUST be red on unfixed
  code with message `product archive producer end_time differs from DB
  inventory for <subject>` (red proof captured);
  (c) subset — `replace(subject, end=END + timedelta(hours=3))` →
  `AuditBlocked` with the new window message (single-value pin on the
  message prefix);
  (d) identity mismatch (e.g. wrong `model_id`) → `AuditBlocked` with
  the existing per-field message (fail-closed preserved).
- [x] 2.2 Red proof: leg (b) fails on the pre-change code for the right
  reason (`producer end_time differs`); captured output goes into the
  report → PR body.
- [x] 2.3 Existing parametrized case `("start_time",
  "2026-04-30T00:00:00Z")` (`tests/test_node27_storage_inventory_audit.py:349`)
  SATISFIES containment (producer start earlier than subject start
  2026-05-01) and would silently drift to being caught by the
  embedded/outer binding instead, masked by the loose
  `match="producer|schema"` — adjust it DELIBERATELY (recorded, not a
  weakening) to a containment-violating value (e.g.
  `"2026-05-01T03:00:00Z"`, producer start AFTER subject start). Two
  distinct expectations: PRE-fix it blocks with the OLD per-field
  message (`product archive producer start_time differs from DB
  inventory for <subject>` — this is the red-side evidence for the
  case); POST-fix it blocks with the NEW window message, which gets the
  precise pin. No other existing assertion weakened; whole audit module
  passes.

## 3. Verification (orchestrator)

- [x] 3.1 `uv run pytest -q tests/test_node27_storage_inventory_audit.py`
  green (local fast loop; authoritative backend pytest oracle is node-27
  per CLAUDE.md — `_archive_mount_id` takes the Linux branch there — the
  node-27 run happens with 5.1).
- [x] 3.2 `uv run ruff check .` clean.
- [x] 3.3 `openspec validate audit-producer-window-containment --strict
  --no-interactive` valid.

## 4. Spec delta (orchestrator, this fixture)

- [x] 4.1 ADDED requirement (new title; umbrella lives as unarchived
  ADDED in `tier-node27-timeseries-storage`) with scenarios: superset
  passes, subset blocks, identity mismatch blocks.

## 5. Ops follow-through (post-merge, node-27; not merge-gating)

- [ ] 5.1 `git pull --ff-only` on node-27, run
  `uv run pytest -q tests/test_node27_storage_inventory_audit.py`
  there (authoritative Linux oracle), rerun
  `nhms-node27-storage-inventory-audit.service`, capture the new receipt
  outcome; if non-blocked, continue the retention chain (dry-run →
  ENFORCE=1 → enable `nhms-node27-timeseries-retention.timer`). Note:
  the runs-lane hot-side equality (proposal Known residual) may surface
  as the next block — if so, report it, do not hack around it.

## Evidence mapping (issue AC → tasks)

- Superset admitted (incident subject unblocked) → 2.1(b) + 2.2 + 5.1
- Fail-closed preserved (subset/identity) → 2.1(c)(d)
- No over-tightening → 2.1(a)
- ruff/validate → 3.2/3.3
- Issue "建议方案" followed verbatim; test module name confirmed
  (`tests/test_node27_storage_inventory_audit.py`), no hedge remains.
