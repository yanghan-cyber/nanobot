#!/usr/bin/env bash
set -euo pipefail

UV="/home/yanghan/.local/bin/uv"
WORKDIR="/home/yanghan/workspace/nanobot"
LOG_DIR="/home/yanghan/workspace/nanobot/logs"
LOG_FILE="$LOG_DIR/gateway.log"
PID_FILE="$LOG_DIR/gateway.pid"
NOTICE_FILE="/home/yanghan/workspace/nanobot/.restart-notice"
MAX_LOG_FILES=3

# Proxy settings (enabled by default)
PROXY_HOST="${PROXY_HOST:-192.168.31.3}"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}"
export http_proxy="${http_proxy:-$PROXY_URL}"
export https_proxy="${https_proxy:-$PROXY_URL}"
export all_proxy="${all_proxy:-socks5://${PROXY_HOST}:${PROXY_PORT}}"
export no_proxy="${no_proxy:-localhost,127.0.0.1,::1}"

case "${1:-}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "gateway already running (PID $(cat "$PID_FILE"))"
      exit 1
    fi

    mkdir -p "$LOG_DIR"

    # Rotate logs: keep only MAX_LOG_FILES generations
    # gateway.log.(MAX-1) → delete, gateway.log.(N) → gateway.log.(N+1)
    rm -f "$LOG_FILE.$((MAX_LOG_FILES - 1))"
    for ((i = MAX_LOG_FILES - 2; i >= 1; i--)); do
      [ -f "$LOG_FILE.$i" ] && mv "$LOG_FILE.$i" "$LOG_FILE.$((i + 1))"
    done
    [ -f "$LOG_FILE" ] && mv "$LOG_FILE" "$LOG_FILE.1"

    # Restore restart notice from file → env vars, so nanobot sends "Restart completed"
    if [ -f "$NOTICE_FILE" ]; then
      # shellcheck disable=SC2046
      export $(cat "$NOTICE_FILE")
      rm -f "$NOTICE_FILE"
    fi

    nohup "$UV" run --directory "$WORKDIR" nanobot gateway >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "gateway started (PID $!), log: $LOG_FILE"
    ;;

  stop)
    if [ -f "$PID_FILE" ]; then
      kill "$(cat "$PID_FILE")" && echo "gateway stopped" || echo "process not found"
      rm -f "$PID_FILE"
    else
      echo "no PID file found"
    fi
    ;;

  restart)
    if [ ! -f "$PID_FILE" ] || ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "gateway not running"
      rm -f "$PID_FILE"
      exit 1
    fi

    CHANNEL="${2:-}"
    CHAT_ID="${3:-}"
    if [ -z "$CHANNEL" ] || [ -z "$CHAT_ID" ]; then
      echo "usage: $0 restart <channel> <chat_id>"
      echo "example: $0 restart telegram 123456789"
      exit 1
    fi

    # Write restart notice to file so the new process can pick it up
    cat > "$NOTICE_FILE" <<EOF
NANOBOT_RESTART_NOTIFY_CHANNEL=$CHANNEL
NANOBOT_RESTART_NOTIFY_CHAT_ID=$CHAT_ID
NANOBOT_RESTART_STARTED_AT=$(date +%s.%N)
EOF

    # Stop old process
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "gateway stopped, waiting to restart..."

    # Brief pause to let the port free up
    sleep 2

    # Start new process (will read NOTICE_FILE → env vars)
    "$0" start
    echo "gateway restarted (PID $(cat "$PID_FILE"))"
    ;;

  log)
    tail -f "$LOG_FILE"
    ;;

  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "gateway running (PID $(cat "$PID_FILE"))"
    else
      echo "gateway not running"
      rm -f "$PID_FILE"
    fi
    ;;

  *)
    echo "usage: $0 {start|stop|restart|log|status}"
    echo ""
    echo "  start              Start the gateway"
    echo "  stop               Stop the gateway"
    echo "  restart <ch> <id>  Restart and notify channel/chat_id"
    echo "  log                Tail the log"
    echo "  status             Check if running"
    ;;
esac
