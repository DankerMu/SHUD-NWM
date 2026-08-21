# safe-filesystem-primitive-contract Spec Delta

## ADDED Requirements

### Requirement: The mid-open inode-identity refusal SHALL carry its own structured discriminator, and its documented meaning SHALL match what it actually detects

The no-follow file open compares the target's identity before and after opening and refuses when the inode changed in between. That refusal SHALL carry a discriminator distinct from the primitive's other refusals, because it is the only one a caller can legitimately choose to absorb, and a caller must be able to select it by field rather than by matching message text. The discriminator's documented meaning SHALL state what the check actually detects: the target was replaced by a different regular file while it was being opened — which an ordinary atomic rename and a hostile swap produce identically at this layer, since the primitive cannot distinguish them. The documentation SHALL NOT describe it as the symlink defense, because a symlink appearing in that window is refused by the no-follow open flag and the symlink mode checks and never reaches the identity comparison; describing it as the symlink defense would lead a later reader to treat absorbing it as a security regression when the actual symlink barriers are untouched. The comparison itself SHALL remain in place and SHALL keep refusing; only its labelling changes here. The primitive SHALL NOT retry internally, because it is shared by callers with opposite needs — some absorb a concurrent rename, others must reject any inode movement — and a retry policy fixed inside the primitive would deny one of those groups its required semantics. Adding this discriminator SHALL NOT alter the meaning of any existing discriminator value and SHALL NOT change which conditions are refused.

#### Scenario: The identity refusal is selectable by field

- **WHEN** a caller catches the refusal raised because the target's inode changed mid-open
- **THEN** the error carries a discriminator distinguishing it from safety refusals and from I/O failures, and the caller can branch on it without inspecting the message

#### Scenario: Safety refusals keep their existing discriminator

- **WHEN** the open is refused because the target is a symlink, is not a regular file, or violates containment
- **THEN** the discriminator is unchanged from before this change, so callers that branch on it see no behavioral difference

#### Scenario: The primitive itself does not retry

- **WHEN** the identity comparison fails
- **THEN** the primitive raises immediately, leaving any retry decision to the caller
