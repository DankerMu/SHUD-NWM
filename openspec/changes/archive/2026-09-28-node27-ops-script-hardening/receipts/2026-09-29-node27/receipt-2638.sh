#!/usr/bin/env bash
# #2638 live receipt: restart display API from a foreign git cwd; a :8081 decoy
# uvicorn from another checkout must survive with the same PID.
set -u
D=/home/nwm/tmp/yd-NWM-decoy
export PATH=$HOME/.local/bin:$PATH
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
echo "receipt_start=$(ts) head=$(git -C /home/nwm/NWM rev-parse HEAD)"
rm -rf "$D"; mkdir -p "$D/apps/api"
git -C "$D" init -q
ln -s /home/nwm/NWM/.venv "$D/.venv"
cat > "$D/apps/api/main.py" <<'PY'
async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"decoy-8081"})
PY
cd "$D"
setsid nohup "$D/.venv/bin/python" -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8081 >"$D/decoy.log" 2>&1 < /dev/null &
for _ in $(seq 1 20); do curl -sf http://127.0.0.1:8081/ >/dev/null && break; sleep 0.5; done
decoy_pid=$(pgrep -f -- "^$D/\.venv/bin/python -m uvicorn apps\.api\.main:app")
echo "decoy_pid_before=$decoy_pid decoy_body=$(curl -sf http://127.0.0.1:8081/)"
echo "decoy_cmdline=$(tr '\0' ' ' < /proc/$decoy_pid/cmdline)"
old_main=$(systemctl --user show nhms-display-api.service --property MainPID --value)
echo "display_main_pid_before=$old_main"
echo "--- restart (cwd=$D, a foreign git repo) ---"
bash /home/nwm/NWM/scripts/ops/start-display-api.sh; rc=$?
echo "restart_rc=$rc"
new_main=$(systemctl --user show nhms-display-api.service --property MainPID --value)
echo "display_main_pid_after=$new_main"
echo "display_umask=$(grep -i umask /proc/$new_main/status)"
echo "health_8080=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health)"
decoy_after=$(pgrep -f -- "^$D/\.venv/bin/python -m uvicorn apps\.api\.main:app")
echo "decoy_pid_after=$decoy_after decoy_body=$(curl -sf http://127.0.0.1:8081/)"
[[ "$decoy_pid" == "$decoy_after" ]] && echo "DECOY_SURVIVED=yes" || echo "DECOY_SURVIVED=no"
kill -TERM "$decoy_after" 2>/dev/null; sleep 1
echo "decoy_cleanup=$(pgrep -f -- "^$D/" || echo none) ss_8081=$(ss -ltn | grep -c ':8081 ')"
echo "receipt_end=$(ts)"
