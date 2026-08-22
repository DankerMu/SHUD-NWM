## ADDED Requirements

### Requirement: The provider snapshot read SHALL reject a replacement that restores the destination's metadata

`read_provider_snapshot` SHALL bind its returned bytes to one stable physical
preimage and SHALL raise `provider_preimage_changed` (phase `precommit`) when
the destination is replaced between the preimage capture and the payload read,
**including when the replacement is subsequently reverted so that every captured
metadata field — device, inode, mode, uid, gid, size, and `mtime_ns` — is
identical before and after**. The content-digest comparison against the
captured preimage SHALL be the guard that holds in that case, and its coverage
SHALL NOT depend on filesystem timestamp granularity: the covering test SHALL
fail if that comparison is removed, on both a nanosecond-timestamp filesystem
(APFS) and a coarse-tick filesystem (ext4 at 4 ms).

#### Scenario: Content replaced during the read and metadata restored before the second capture

- **WHEN** the destination holds `generation-a`, is replaced with an
  equal-length `generation-b` after the preimage capture but before the payload
  read, and is then restored to `generation-a` with its original `mtime_ns`
  reapplied before the second capture
- **THEN** the two captured preimages compare equal
- **AND** `read_provider_snapshot` raises `ProviderAtomicError` with reason
  `provider_preimage_changed`
- **AND** the raise is produced by the content-digest comparison alone, such
  that removing that comparison makes the read succeed

#### Scenario: Content replaced during the read with a different length and not restored

- **WHEN** the destination holds `generation-a` and is replaced with a
  different-length payload after the preimage capture but before the payload
  read, with no restoration
- **THEN** the two captured preimages differ on `size`
- **AND** `read_provider_snapshot` raises `ProviderAtomicError` with reason
  `provider_preimage_changed`
- **AND** the divergence is observable independently of `mtime_ns` granularity
