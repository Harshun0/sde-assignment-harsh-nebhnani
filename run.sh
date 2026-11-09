#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs the controller until the scenario finishes, no manual steps.
# No Docker needed: pure Python (numpy/pyyaml/fastapi/uvicorn/httpx already in requirements.txt),
# state kept in a local SQLite file so a kill + re-run of this same command resumes correctly.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL> [state_db_path]}"
DB="${2:-controller_state.db}"
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python -m controller.main "$SIM_URL" --db "$DB"
