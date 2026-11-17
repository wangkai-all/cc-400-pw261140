#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for pidfile in run/*.pid; do
  [ -e "$pidfile" ] || continue
  name="$(basename "$pidfile" .pid)"
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "stopped $name (pid $pid)"
  fi
  rm -f "$pidfile"
done
