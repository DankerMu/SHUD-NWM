## REMOVED Requirements

### Requirement: The gateway proof cancel stage requires an observed RUNNING state

**Reason**: The M24 gateway live-proof emitter (`scripts/m24_gateway_proof.py`) is deleted (#3221): the m24 chain has no caller outside its own tests and is no longer an acceptance mechanism.
**Migration**: None.

### Requirement: Live submit and cancel receipts are produced on node-22

**Reason**: The only emitter of these receipts, the M24 gateway live-proof (`scripts/m24_gateway_proof.py`), is deleted (#3221).
**Migration**: None.
