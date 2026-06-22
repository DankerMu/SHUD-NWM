# node-27 live receipt - object-store station series PR-C health probe

- Date: 2026-06-21 23:37 UTC approximate
- Context: PR-C for `object-store-station-series-read` (#632 / issue #624)
- Node: node-27 (`nwm@210.77.77.27:32099`)
- Target: `http://127.0.0.1:8080/health`
- Remote branch/head: `master` / `9765e2b`
- HTTP status/body: 200 / `{"status":"ok","service":"nhms-api","version":"0.1.0"}`

## Command

```bash
ssh -p 32099 nwm@210.77.77.27 'cd /home/nwm/NWM && printf "branch=" && git branch --show-current && printf "head=" && git rev-parse --short HEAD && printf "health=" && curl -fsS http://127.0.0.1:8080/health'
```

## Output

```text
branch=master
head=9765e2b
health={"status":"ok","service":"nhms-api","version":"0.1.0"}
```

## Interpretation

`curl -fsS` returned successfully against `/health`, so the captured health
probe is treated as HTTP 200. This receipt confirms the node-27 display API was
still serving the real root health endpoint during PR-C. It does not claim PR
632 code was deployed on node-27; the remote worktree was on `master` at
`9765e2b`.
