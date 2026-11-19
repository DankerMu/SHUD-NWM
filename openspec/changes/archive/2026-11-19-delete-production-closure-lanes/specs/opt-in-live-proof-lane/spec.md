## REMOVED Requirements

### Requirement: Opt-in Live Proof Lane

**Reason**: The readiness (`validate-readiness`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: The gateway live-proof emitter SHALL authenticate its mutations and SHALL NOT leak the token

**Reason**: The M24 gateway live-proof emitter (`scripts/m24_gateway_proof.py`) is deleted (#3221): the m24 chain has no caller outside its own tests and is no longer an acceptance mechanism.
**Migration**: None.
