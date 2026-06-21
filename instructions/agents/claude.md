## Claude Code Notes

- 知识域类 skill（如调试方法论）自动触发率低，优先显式 `/skill-name` 调用。
- 安装重叠 skill 时剪枝旧/被取代项，保持技能列表清晰。
- `dual-end-issue-workflow` 是项目特有的旧 skill，与 `subagent-workflow` 功能重叠时以后者为准。
