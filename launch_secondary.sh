#!/usr/bin/env bash
set -u

cd "$(dirname "$0")"
RUN_DIR=".run"
SERVICES=(cam_server)

mkdir -p "$RUN_DIR"

pid_file() { echo "$RUN_DIR/$1.pid"; }
log_file() { echo "$RUN_DIR/$1.log"; }

is_running() {
    local pid_file="$1"
    [ -f "$pid_file" ] || return 1
    local pid
    pid=$(cat "$pid_file" 2>/dev/null) || return 1
    kill -0 "$pid" 2>/dev/null
}

start_one() {
    local svc="$1"
    if is_running "$(pid_file "$svc")"; then
        echo "$svc already running (pid $(cat "$(pid_file "$svc")"))"
        return
    fi
    nohup python3 "$svc.py" > "$(log_file "$svc")" 2>&1 &
    echo $! > "$(pid_file "$svc")"
    echo "$svc started (pid $(cat "$(pid_file "$svc")"))"
}

stop_one() {
    local svc="$1"
    local pf
    pf="$(pid_file "$svc")"
    if is_running "$pf"; then
        kill "$(cat "$pf")" 2>/dev/null
        for _ in $(seq 1 50); do
            is_running "$pf" || break
            sleep 0.1
        done
        if is_running "$pf"; then
            kill -9 "$(cat "$pf")" 2>/dev/null
        fi
        echo "$svc stopped"
    else
        echo "$svc not running"
    fi
    rm -f "$pf"
}

status_one() {
    local svc="$1"
    local pf
    pf="$(pid_file "$svc")"
    if is_running "$pf"; then
        echo "$svc: running (pid $(cat "$pf"))"
        tail -n 3 "$(log_file "$svc")" 2>/dev/null | sed 's/^/    /'
    else
        echo "$svc: stopped"
    fi
}

start() { for svc in "${SERVICES[@]}"; do start_one "$svc"; done; }
stop() { for svc in "${SERVICES[@]}"; do stop_one "$svc"; done; }
status() { for svc in "${SERVICES[@]}"; do status_one "$svc"; done; }
restart() { stop; sleep 1; start; }

toggle() {
    local any_running=0
    for svc in "${SERVICES[@]}"; do
        if is_running "$(pid_file "$svc")"; then any_running=1; fi
    done
    if [ "$any_running" = 1 ]; then stop; else start; fi
}

case "${1:-toggle}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    restart) restart ;;
    *) toggle ;;
esac
