# Tasks: compression-timeout-spec-defaults-sync

- [x] 1.1 spec delta：MODIFIED requirement 全文（默认 3600000/3900/3940，旧值标 former + #1352 来源），两 scenario 数值同步。
- [x] 1.2 `openspec validate compression-timeout-spec-defaults-sync --strict --no-interactive` 通过。
- [x] 1.3 archive 回写 `openspec/specs/hypertable-compression/spec.md` 后复核：`grep -n "840000" openspec/specs/hypertable-compression/spec.md` 仅剩显式标注 former 的叙述行；归档目录未动；diff 无 `scripts/`/`infra/`/`tests/`。
