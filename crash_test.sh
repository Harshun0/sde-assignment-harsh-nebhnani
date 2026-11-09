#!/usr/bin/env bash
# Crash-recovery demonstration (C13 / deliverable #4).
# Starts a fresh sim + controller, kills the controller mid-run with SIGKILL after it has
# ingested history and started advancing simulated time, then re-runs the exact same
# command against the same --db and the same (still-running) simulator. The second run
# must resume from the cursor it left off at and finish the scenario correctly, with no
# duplicate proposal_ids and no double-applied config versions.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source .venv/bin/activate 2>/dev/null || true

PORT=8799
DB=crash_demo_state.db
RUNDIR=runs/crash_demo

pkill -f "sim serve.*--port $PORT" 2>/dev/null || true
sleep 1
rm -rf "$RUNDIR" "$DB" "$DB-wal" "$DB-shm"

echo "== starting simulator =="
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir "$RUNDIR" --port $PORT > sim_crash_demo.log 2>&1 &
SIMPID=$!
sleep 10

echo "== starting controller (will kill with SIGKILL after ~8s) =="
python -u -m controller.main "http://127.0.0.1:$PORT" --db "$DB" > crash_demo_part1.log 2>&1 &
CPID=$!
sleep 8
echo "---- progress before kill ----"
cat crash_demo_part1.log
kill -9 $CPID
sleep 2
echo "== killed controller pid $CPID with SIGKILL =="
echo "---- cursor/decisions state right after kill ----"
sqlite3 "$DB" "SELECT * FROM cursor;"
sqlite3 "$DB" "SELECT COUNT(*) AS decisions_so_far FROM decisions;"

echo "== resuming controller against the SAME db and the SAME (still-running) simulator =="
python -u -m controller.main "http://127.0.0.1:$PORT" --db "$DB" | tee crash_demo_part2.log

echo "== checking for duplicate proposal_ids or double-applied versions =="
sqlite3 "$DB" "SELECT proposal_id, COUNT(*) FROM proposal GROUP BY proposal_id HAVING COUNT(*) > 1;"
sqlite3 "$DB" "SELECT workflow_id, version, COUNT(*) FROM applied_version GROUP BY workflow_id, version HAVING COUNT(*) > 1;"
echo "(no rows above means: no duplicates found)"

kill $SIMPID 2>/dev/null || true
