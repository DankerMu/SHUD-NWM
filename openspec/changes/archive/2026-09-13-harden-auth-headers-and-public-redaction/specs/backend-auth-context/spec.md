## ADDED Requirements

### Requirement: Internal live-proof secret header SHALL compare in constant time and fail closed

The shared request auth context SHALL compare `X-NHMS-Internal-Live-Proof` with the configured
`NHMS_INTERNAL_LIVE_PROOF_TOKEN` using a constant-time byte comparison. A header or token that cannot be
ASCII-encoded SHALL be treated as a non-match (release-blocked context), never as a server error.

#### Scenario: Non-ASCII live-proof header is release-blocked

- **WHEN** trusted live proof is enabled and a request carries `X-NHMS-Internal-Live-Proof` with a non-ASCII value
- **THEN** the auth context is release-blocked and no `TypeError` or HTTP 500 occurs

#### Scenario: Matching token still authenticates

- **WHEN** trusted live proof is enabled and the header equals the configured token with live user id and roles
- **THEN** the auth context is `live_idp` with the mapped roles
