## ADDED Requirements

### Requirement: Resource-governance receipts MUST pin archive_root absence at artifact level

The node-27 resource-governance audit receipt SHALL NOT carry a top-level
`archive_root` block (ADR 0002 revision: the audit must not claim observation
of a volume no lane uses), and this absence SHALL be pinned by a regression
test that constructs the receipt artifact itself, not merely by asserting the
absence of retired collector functions or config attributes.

#### Scenario: constructed receipt carries no archive_root key

WHEN the governance receipt is built via `build_receipt()` with its
filesystem/postgres/systemd collectors stubbed
THEN the resulting receipt dict has no top-level `archive_root` key

#### Scenario: reintroduction by another path fails the pin

WHEN any change reintroduces a top-level `archive_root` block through a
renamed or generic collector path
THEN the artifact-level pin test fails even though the retired-attribute
assertions still pass
