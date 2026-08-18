## ADDED Requirements

### Requirement: Package refusal payloads carry the model's cause keys

The package publication refusal SHALL carry the refused model's health
causes in its structured payload: when a model is refused as not
publishable, the error payload includes the model's status and its
missing, invalid, and unreadable required-file collections, copied from
the inventory model record (empty when a caller-supplied inventory
predates a key) — the first three key names match the scheduler registry
publish channel, the fourth matches the discovery payload; no new
aliases.
The refusal predicate, error code, and message text stay byte-for-byte
unchanged, and every pre-existing payload key keeps its value, so receipt
consumers remain backward compatible; error instances raised without
cause details keep their existing payload byte-for-byte.

#### Scenario: a malformed-IC-header model's refusal names the file

WHEN a model whose IC header failed the discovery shape gate is refused
as not publishable
THEN the refusal payload's invalid-required-files entry names the
offending `*.cfg.ic` file alongside the model's status

#### Scenario: an unreadable-required-file model's refusal names the file

WHEN a model carrying an unreadable required file (partial status) is
refused as not publishable
THEN the refusal payload's unreadable-required-files entry names that
file

#### Scenario: pre-existing payload keys survive unchanged

WHEN any package refusal is raised
THEN error_code, message, model_id, version, and path keep their existing
values, and refusals raised without details keep their payload
byte-for-byte
