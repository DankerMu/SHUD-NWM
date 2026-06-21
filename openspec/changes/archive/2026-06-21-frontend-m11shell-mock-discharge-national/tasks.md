## 1. Mock fixture realignment

- [ ] 1.1 `apps/frontend/src/pages/__tests__/M11Shell.test.tsx:326`: `m11MvtMetadataByLayer['discharge']` 改为引用 `dischargeNationalMvtMetadata`（替换 `dischargeMvtMetadata` legacy 引用）。
- [ ] 1.2 `apps/frontend/src/pages/__tests__/M11Shell.test.tsx:315`: `dischargeNationalMvtMetadata.min_zoom` 改 `7` → `3`（与 backend `services/tiles/mvt.py:748` `_NATIONAL_DISCHARGE_METADATA.min_zoom` 对齐）。同时更新 line 304 的注释 "min_zoom=7" → "min_zoom=3"。
- [ ] 1.3 `apps/frontend/src/pages/__tests__/M11Shell.test.tsx:285`: 在 `dischargeMvtMetadata` 常量声明上加一行 header comment：`// LEGACY deeplink-only shape — `/api/v1/tiles/hydro/{run_id}/...` 路由仍存在但不再是 canonical `/api/v1/layers` 的 discharge 形态（post-PR #602）；保留供 line 821/926 等测试 exercise 单 run 兼容代码路径。canonical 默认形态见 dischargeNationalMvtMetadata。`

## 2. Verify

- [ ] 2.1 `pnpm --filter apps/frontend tsc` clean
- [ ] 2.2 `pnpm --filter apps/frontend test M11Shell` PASS
- [ ] 2.3 `openspec validate frontend-m11shell-mock-discharge-national --strict --no-interactive` PASS

## 3. PR / merge hygiene

- [ ] 3.1 PR body `Closes #603`，附 Chinese 工作总结
- [ ] 3.2 review-loop log append 一行
- [ ] 3.3 OpenSpec archive：合并后 `openspec archive frontend-m11shell-mock-discharge-national --yes`（无 collision risk — 与近期归档的 `cleanup-docs-and-dead-source-refs` 同样 ADD 新 scenario 给 `mvt-tile-contract`，不重叠）
