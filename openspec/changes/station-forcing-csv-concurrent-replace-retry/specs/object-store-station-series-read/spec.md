# object-store-station-series-read Specification

## ADDED Requirements

### Requirement: The station CSV read SHALL absorb a concurrent atomic replacement with a bounded retry, and SHALL keep failing closed when it cannot

The producer that writes a station's forcing CSV replaces it atomically, which swaps the target's inode. The reader opens that file through a no-follow open whose mid-open identity comparison refuses exactly that swap. The two are the same shared artifact reached from two hosts, so the refusal is an expected consequence of ordinary production writes, not evidence of tampering or of a damaged file, and it SHALL no longer surface on the public API as a server error indistinguishable from a genuinely corrupt file. The reader SHALL re-attempt the open a bounded number of times when, and only when, the refusal carries the discriminator meaning the target's inode changed mid-open. The retry SHALL select on that discriminator field and SHALL NOT select on the refusal's message text, because the message is human-facing prose that may be reworded without notice while the discriminator is the contract. Every other refusal the open can raise — a symlinked or non-regular target, a containment violation, an I/O failure — SHALL propagate on the first attempt, since none of them is resolved by trying again and retrying them would delay a refusal the caller must see immediately.

The retry SHALL cover the open alone and SHALL NOT cover parsing the opened file. Once the descriptor exists it is bound to the pre-replacement inode, whose contents are a complete and self-consistent snapshot; any failure to parse what that descriptor yields is therefore a genuine content defect and SHALL surface on its first occurrence rather than being retried. Retrying the parse would both multiply the latency of a deterministic failure and hide defective producer output behind an apparent race.

When the bounded attempts are exhausted, the reader SHALL still fail closed with the same HTTP status and the same error code it returns for a malformed file, introducing no new status code and no new error type. Its reported reason SHALL nonetheless identify the concurrent replacement distinctly from a content defect, and that reason SHALL be derived from the discriminator rather than from the refusal's message text. Reasons reported for every other failure SHALL keep the operator-useful text they carry today.

The path where the file is absent SHALL remain untouched: an atomic replacement leaves no window in which the target path resolves to nothing, so a missing file is unrelated to this race and SHALL keep mapping to its existing not-found outcome without any retry.

#### Scenario: A replacement inside the open window is absorbed and the read succeeds

- **WHEN** the mid-open identity refusal is raised on an attempt, and a subsequent attempt within the bound opens the file successfully
- **THEN** the reader returns the parsed CSV content normally, and no error reaches the caller

#### Scenario: A persistent replacement exhausts the bound and fails closed

- **WHEN** every attempt within the bound raises the mid-open identity refusal
- **THEN** the reader raises the same malformed-file error, with the same HTTP status and error code as before this change
- **AND** the reported reason identifies the failure as a concurrent replacement, distinguishably from a content defect

#### Scenario: Refusals other than the identity change are not retried

- **WHEN** the open is refused for a symlinked target, a non-regular target, a containment violation, or an I/O failure
- **THEN** the reader attempts the open exactly once and surfaces the refusal immediately
- **AND** the reported reason keeps the operator-useful text it carried before this change

#### Scenario: The retry decision does not depend on message text

- **WHEN** a refusal carries the mid-open identity discriminator but wording unlike the primitive's current message, or carries another discriminator but wording resembling it
- **THEN** the retry decision follows the discriminator in both cases

#### Scenario: A content defect in the opened file is not retried

- **WHEN** the open succeeds and parsing the resulting descriptor fails because the file is empty, its header is unreadable, a declared data row is blank, or a declared bound is exceeded
- **THEN** the reader raises the malformed-file error on that first parse failure without re-opening the file

#### Scenario: A missing file keeps its existing outcome

- **WHEN** the expected station CSV path does not exist
- **THEN** the reader raises its existing not-found error with no retry
