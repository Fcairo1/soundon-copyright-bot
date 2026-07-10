#!/usr/bin/env bash
set -u

BOT_DIR="/workspace/iris_ddc11f6d-0341-45c3-a7d8-42c5bc34776c/soundon-copyright-bot"
DAEMON_SCRIPT="$BOT_DIR/copyright_alert/persistent_callback.py"
RUNTIME_DIR="$BOT_DIR/runtime"
LOG_DIR="$BOT_DIR/logs"
DAEMON_OUT="$RUNTIME_DIR/persistent_callback.out"
PID_FILE="$RUNTIME_DIR/persistent_callback.pid"
LOCK_DIR="$RUNTIME_DIR/watchdog.lock"

BOT_APP_ID="cli_aa94690b12b81cde"
FILIPE_OPEN_ID="ou_dec135cea21446b576b21d911df61f53"
LARK_AUTH_URL="https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_SEND_URL="https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=open_id"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

# Prevent overlapping cron runs without depending on non-portable flock.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(TZ=America/Sao_Paulo date '+%Y-%m-%d %H:%M:%S %Z') watchdog already running; exiting"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

is_daemon_alive() {
  pgrep -f "persistent_callback\.py" >/dev/null 2>&1
}

load_bot_secret() {
  if [ -n "${BOT_SECRET:-}" ]; then
    printf '%s' "$BOT_SECRET"
    return 0
  fi
  if [ -n "${LARK_APP_SECRET:-}" ]; then
    printf '%s' "$LARK_APP_SECRET"
    return 0
  fi
  if [ -n "${APP_SECRET:-}" ]; then
    printf '%s' "$APP_SECRET"
    return 0
  fi

  for env_file in "$BOT_DIR/.env" "$BOT_DIR/copyright_alert/.env"; do
    if [ -f "$env_file" ]; then
      secret_line=$(grep -E '^(BOT_SECRET|LARK_APP_SECRET|APP_SECRET|app_secret)=' "$env_file" | tail -n 1 || true)
      if [ -n "$secret_line" ]; then
        secret=${secret_line#*=}
        secret=${secret%$'\r'}
        secret=${secret%\"}; secret=${secret#\"}
        secret=${secret%\'}; secret=${secret#\'}
        if [ -n "$secret" ]; then
          printf '%s' "$secret"
          return 0
        fi
      fi
    fi
  done

  return 1
}

json_escape() {
  # Escape enough for the static notification payload used here.
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

send_lark_dm() {
  pid="$1"
  timestamp="$2"
  secret=$(load_bot_secret || true)
  if [ -z "${secret:-}" ]; then
    echo "$timestamp failed to send Lark DM: bot secret not found in env or repo config"
    return 1
  fi

  token_resp=$(curl -sS -X POST "$LARK_AUTH_URL" \
    -H 'Content-Type: application/json; charset=utf-8' \
    -d "{\"app_id\":\"$BOT_APP_ID\",\"app_secret\":\"$(json_escape "$secret")\"}")
  token=$(printf '%s' "$token_resp" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("tenant_access_token") or ""))' 2>/dev/null || true)
  if [ -z "$token" ]; then
    echo "$timestamp failed to get tenant_access_token: $token_resp"
    return 1
  fi

  text="⚠️ Copyright bot daemon was down and has been restarted automatically.\nNew PID: $pid\nTime: $timestamp"
  content=$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}, ensure_ascii=False))' "$text")
  payload=$(python3 -c 'import json,sys; print(json.dumps({"receive_id": sys.argv[1], "msg_type": "text", "content": sys.argv[2]}, ensure_ascii=False))' "$FILIPE_OPEN_ID" "$content")

  send_resp=$(curl -sS -X POST "$LARK_SEND_URL" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json; charset=utf-8' \
    -d "$payload")
  code=$(printf '%s' "$send_resp" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("code", -1))' 2>/dev/null || echo -1)
  if [ "$code" != "0" ]; then
    echo "$timestamp failed to send Lark DM: $send_resp"
    return 1
  fi
  echo "$timestamp Lark DM sent for restarted PID $pid"
}

if is_daemon_alive; then
  echo "$(TZ=America/Sao_Paulo date '+%Y-%m-%d %H:%M:%S %Z') persistent_callback.py is alive; nothing to do"
  exit 0
fi

cd "$BOT_DIR" || exit 1
nohup python3 "$DAEMON_SCRIPT" >> "$DAEMON_OUT" 2>&1 &
new_pid=$!
printf '%s\n' "$new_pid" > "$PID_FILE"
sleep 2

timestamp=$(TZ=America/Sao_Paulo date '+%Y-%m-%d %H:%M:%S %Z')
if ! ps -p "$new_pid" >/dev/null 2>&1; then
  echo "$timestamp attempted restart, but new daemon PID $new_pid is not running"
  exit 1
fi

echo "$timestamp restarted persistent_callback.py with PID $new_pid"
send_lark_dm "$new_pid" "$timestamp" || true
