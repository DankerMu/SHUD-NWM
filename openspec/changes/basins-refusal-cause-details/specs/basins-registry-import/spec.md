## ADDED Requirements

### Requirement: Import refusal payloads carry the model's cause keys

The registry import refusal SHALL carry the refused model's health causes
in its structured payload: when a model is refused as not importable, the
error payload includes the model's status and its missing, invalid, and
unreadable required-file collections, copied from the inventory model
record (empty when a caller-supplied inventory predates a key) — the
first three key names match the scheduler registry publish channel, the
fourth matches the discovery payload. The refusal predicate, error code, and
message text stay byte-for-byte unchanged, pre-existing payload keys keep
their values, and the reingest passthrough (which already forwards
upstream details verbatim) surfaces the same cause keys in its own
payload without modification.

#### Scenario: an ineligible model's import refusal carries causes

WHEN a model whose status is not valid (or whose default import
eligibility is false) is refused for import
THEN the refusal payload carries status, missing-, invalid-, and
unreadable-required-files copied from the inventory model record

#### Scenario: the reingest passthrough surfaces the same causes

WHEN a reingest run wraps an import or package refusal
THEN the reingest error payload carries the same cause keys forwarded
verbatim through the existing details passthrough
