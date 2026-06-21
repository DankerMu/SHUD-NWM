# Object-store Forcing Series Read

本 runbook 是 PR #628 / issue #623 的最小占位，用于记录 display 侧读取 station-series CSV 的必要环境配置。完整 PR-C operator runbook、错误码排障、retention 说明和 follow-up 整理由 issue #624 完成。

## Env

node-27 display API 读取 object-store forcing CSV 时必须配置：

```bash
OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
```

该值应由 ops 写入 node-27 的 display runtime env（例如 `display.env`），不要提交实际 env 文件。`OBJECT_STORE_ROOT` 需要指向一个 display 进程可读、可遍历的目录；启动期校验失败时 display API 应拒绝启动。
