#!/usr/bin/env bash
# Start all four independent processes. Logs go to logs/, pids to run/.
set -euo pipefail
cd "$(dirname "$0")"

DB="${IDRES_DB:-data/idres.db}"
export IDRES_DB="$DB"
mkdir -p data logs run

if [ ! -f "$DB" ]; then
  python3 run.py init-db
fi

start() {
  local name="$1" port="$2"
  if [ -f "run/$name.pid" ] && kill -0 "$(cat run/$name.pid)" 2>/dev/null; then
    echo "$name already running (pid $(cat run/$name.pid))"
    return
  fi
  nohup python3 run.py serve "$name" > "logs/$name.log" 2>&1 &
  echo $! > "run/$name.pid"
  echo "started $name on :$port (pid $!)"
}

start adj 8101
start rb  8102
start rpt 8103
sleep 1
start web 8100

echo
echo "Web console:  http://127.0.0.1:8100"
echo "Services:     adj:8101  rb:8102  rpt:8103"
echo "Stop with:    ./stop.sh"
