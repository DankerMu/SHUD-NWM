# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-07-16

### Changed

- 登记新 skill `handoff` 0.1.0：跨会话一节由"手工产出交接摘要"改为指向 `/handoff`（交接文档含工作流计数器与已排除路径，issue 工作流中写到证据束旁）；user-invoked 清单加入 `handoff`。

## [0.1.1] - 2026-07-14

- On-ramps 新增 `diagnosing-bugs` 路由（"出了毛病、原因不明"——难缠 bug、性能回归、CI 挂但本地复现不了），注明 `subagent-workflow` 修复环节内部消费它，以及"原因已确诊直接修、不付诊断税"的反路由。

## [0.1.0] - 2026-07-11
- Initial router skill, modeled on `mattpocock/skills` `ask-matt`: situation → flow routing over this repo's skills (main delivery flow, on-ramps, repo health, research flow, governance, cross-session), plus the authoritative list of user-invoked (`disable-model-invocation: true`) skills.
- User-invoked itself: zero standing context cost; reachable via `/ask-danker`.
- Carries the maintenance covenant: any skill add/rename/remove or flow change must update this map; validation enforces that every user-invoked skill is named here.
