## ADDED Requirements

### Requirement: Re-pointing node-27 runtime units at a different checkout MUST be verified from the units' effective configuration and MUST roll back as one set

When node-27's user units are moved between checkouts by adding or removing systemd drop-ins, the operator SHALL
treat every unit that carries such a drop-in as one set: the set SHALL be enumerated from
`~/.config/systemd/user/*.d/*.conf`, not from the two units people remember, and the move SHALL be verified from
`systemctl --user show` of every unit's effective `ExecStartPre`, `ExecStart`, `ExecStartPost`, `ExecStop`,
`WorkingDirectory`, `Environment`, `EnvironmentFiles` and `DropInPaths`, never from "the script exists in the target tree".

The expected post-move configuration SHALL be derived mechanically from the pre-move capture — substitute the old
checkout path with the new one and drop only the tokens the drop-in itself injected — and compared verbatim with the
post-move capture. Any other residual (a lost flag, a missing environment file, a unit still carrying a drop-in) is a
gate failure. On a gate failure, or on any failure of the display API's read surfaces after restart, every removed
drop-in and every edited env file SHALL be restored from the backup taken before removal and the display API
restarted under the restored configuration; the set SHALL NOT be left half-moved. Foreign operational fences
(capacity holds, other operators' drop-ins, state directories) SHALL NOT be created, removed or modified by the move;
their observed state SHALL be recorded.

The display API restart in such a move SHALL NOT sweep processes by a pattern that can match another checkout's
`apps.api.main:app` instance on the same host; when the canonical restart wrapper does so, the operator SHALL
reproduce its systemd branch by hand and record the deviation.

#### Scenario: eight units carry the same-named drop-in and only the display unit is unpinned

- **WHEN** an operator removes `nhms-display-api.service.d/60-reslice-pin-original-5a86841c.conf` and restarts the
  display API, but leaves the same-named drop-in under the seven timer-driven units
- **THEN** the display API serves the active checkout while ingest, download, retention, compression, governance and
  the frontier alert keep running the frozen copy, and the mechanical pre/post diff of all eight units' effective
  configuration is non-empty — the move is incomplete and MUST NOT be reported as an unpin

#### Scenario: the drop-in set is removed and the effective configuration matches the derived expectation

- **WHEN** all drop-ins of the set are backed up and removed, `daemon-reload` runs, and the post-move capture equals
  the pre-move capture with the checkout path substituted and the drop-in-injected tokens removed
- **THEN** the display API is restarted by the systemd branch of the canonical procedure, its main process's `cwd`,
  `exe` and `NHMS_MVT_FILE_CACHE_DIR` are read from `/proc/<MainPID>/`, the other instance on the host keeps its PID,
  and the read surfaces (`/api/v1/layers`, `river-network-national`, both `hydro-national` routes) answer with zero
  500 before any timer is re-enabled; a failure at any of these points restores the whole set from the backup
