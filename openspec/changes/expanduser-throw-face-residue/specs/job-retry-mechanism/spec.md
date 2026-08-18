## ADDED Requirements

### Requirement: DB-free selector and object-store probe lanes survive unexpandable tildes

The db-free selector adjudicators and the object-store probe lane SHALL
never let a home-directory-determination failure escape as a bare
RuntimeError. Specifically: (a) the db-free selector allowed-roots and
selector-path adjudicators SHALL treat an unexpandable tilde value as an
ordinary relative path and return their existing structured rejections
through the existing non-absolute arms; (b) constructing a local object
store with an unexpandable tilde root SHALL raise the domain
`ObjectStoreError` from within the constructor's existing error-conversion
boundary — never a bare RuntimeError and never a literal `~…` directory
created on disk — so the unified artifact probe and the sidecar provenance
tier, which already catch `ObjectStoreError`, keep producing their existing
distinguishable fail-closed attributions instead of crashing the scheduling
pass. Expandable and tilde-free values keep their existing behavior
byte-for-byte in all these lanes.

#### Scenario: unexpandable tilde in db-free selector values yields structured rejections

WHEN a db-free runtime-manifest allowed-roots entry or selector path value
is `~nosuchuser/…` (or a plain `~/…` with no determinable home directory)
THEN the adjudicators return their existing relative-path rejection shapes
(`db_free_allowed_root_*` / `db_free_selector_path_*` families) without any
exception escaping

#### Scenario: unexpandable tilde object-store root converts to the domain error

WHEN a local object store is constructed with an unexpandable tilde root
THEN the constructor raises `ObjectStoreError` (not a bare RuntimeError)
and creates no directory for the literal tilde value

#### Scenario: probe lanes keep their fail-closed attributions under a tilde root

WHEN the configured object-store root is an unexpandable tilde value and a
candidate's artifact probe or forcing-sidecar provenance runs
THEN the unified artifact probe returns the existing probe-error
missing-status attribution and the sidecar tier returns its existing
unreadable attribution — the scheduling pass continues and evidence is
written

#### Scenario: expandable and tilde-free values keep their behavior

WHEN selector values or the object-store root have no tilde or expand
normally
THEN adjudication results and constructor behavior are byte-for-byte
identical to the pre-change behavior
