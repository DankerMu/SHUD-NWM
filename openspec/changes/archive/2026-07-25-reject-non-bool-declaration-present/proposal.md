# Reject non-bool declaration_present in the cutover_gate normalizer (#1131)

## Why

`normalize_cutover_gate_audit` (`packages/scheduler/registry_audit.py`, the
single definition point extracted by #1097/PR #1130) is fail-closed for two
of its three fields — `mode` outside `CUTOVER_GATE_MODES` raises
`SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` (`:67-72`), non-str
`declaration_env` raises the same (`:74-79`) — but the third field is a
silent truthy coercion: `declaration_present = bool(cutover_gate.get(...))`
(`:80`). A forged/buggy `"no"` becomes `true` in every persisted audit
channel — exactly the silent-audit-rewrite class #1097 eliminated for
`mode`. The merged capability spec's requirement sentence reads as if all
three fields reject, but its scenarios enumerate only three rejection arms;
spec is wider than implementation, and neither direction is pinned by any
test. No production trigger exists today (both in-tree producers pass real
bools), so this is defense-in-depth + contract disambiguation.

## Decision (triage recorded)

Issue #1131 was filed needs-triage on "reject vs documented-coerce". This
change adopts the issue's RECOMMENDED route — **reject, aligned with the
other two fields** — on repo-context grounds: the alternative would
formalize a known silent-rewrite surface, which the issue itself notes is
contrary to #1097's intent, and both in-tree callers
(`scripts/publish_scheduler_file_registry.py:1206/:1216`,
`scripts/scheduler_file_provider_refresh.py:811-817`) pass real bools so
reject breaks nothing. Recorded here so the merge audit sees the call.

## What Changes

- `packages/scheduler/registry_audit.py`: replace the truthy coercion with
  a fail-closed check — `declaration_present` missing/None defaults to
  `False` (unchanged observable for the absent case), any present non-bool
  value raises `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` (mind the
  `isinstance(True, int)` trap the other way around: `isinstance(x, bool)`
  is the check, ints/floats/strings/lists all reject). Docstring updated.
- `tests/test_registry_audit.py`: rejection test for non-bool inputs
  (`"no"`, `1`, `[]`-style truthy/falsy non-bools) + a pin that a MISSING
  key still normalizes to `False`.
- `tests/test_publish_scheduler_file_registry.py`: one `pytest.param`
  appended to `_MALFORMED_CUTOVER_GATES` pinning the manifest channel's
  fail-closed behavior on the new arm (fixture-review P2-2).
- Capability spec (change already archived → MODIFIED requirement on
  `openspec/specs/scheduler-registry-refresh/spec.md`): requirement
  sentence and scenarios brought into agreement — add the
  `declaration_present` rejection scenario so the enumeration matches the
  sentence.

## Out of Scope

- The existing three rejection arms and the `None -> not_wired` fallback
  (#1097 final).
- Whether the runner receipt persists the audit block (#1132).
- Cutover declaration file schema/semantics.

## Impact

- Affected specs: `scheduler-registry-refresh` (MODIFIED requirement:
  shared strict normalizer).
- Affected code: `packages/scheduler/registry_audit.py` (one field's
  handling), `tests/test_registry_audit.py`.
- Callers unaffected: both producers pass real bools; re-exports in
  `scripts/publish_scheduler_file_registry.py` are name-level only.
  No sibling normalizer copies (#1097 deleted the inline mirror).
