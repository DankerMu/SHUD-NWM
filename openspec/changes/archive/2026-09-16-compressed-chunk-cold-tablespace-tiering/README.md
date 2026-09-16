# compressed-chunk-cold-tablespace-tiering

Current intent: **mandatory selective-cold code retirement**, proposed and not
implemented. Code remains present. [tasks.md](tasks.md) is the sole R1-R5
implementation/closure contract; [proposal.md](proposal.md) and
[design.md](design.md) explain scope and safety. The deltas under `specs/` are
proposed target contracts, not proof that canonical specs or runtime changed.

Old pending G0-G8 rollout is withdrawn, not executed. Completed delivery ledger,
`evidence/`, `fixtures/` and `probe-1892-throwaway.md` are historical, not active
install/move instructions or retirement approval. Fresh retirement review and
regression gates are required before implementation.

Final disposition must preserve surviving-spec updates without promoting the
withdrawn cold-enabling ADDED delta. A reviewed `--skip-specs` archive requires
prior explicit preservation/validation of survivor updates; never use
`--no-validate`. No archive, issue closure or deployment is performed here.
