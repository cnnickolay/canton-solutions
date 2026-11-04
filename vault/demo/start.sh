#!/usr/bin/env bash
# Build the vault, start a fresh local sandbox and open the dashboard on it.
set -euo pipefail
cd "$(dirname "$0")/.."
DPM="${DPM:-$HOME/.dpm/bin/dpm}"
PYTHON="${PYTHON:-python3}"
BASE="${DEMO_LEDGER_PORT:-18801}"
PORT="${DEMO_PORT:-8088}"
DAR=packages/vault/.daml/dist/canton-vault-0.1.0.dar
RUN="$(mktemp -d "${TMPDIR:-/tmp}/vault-demo.XXXXXX")"
SANDBOX=""
SERVER=""

stop() {
  trap - EXIT INT TERM
  [[ -n "$SERVER" ]] && { kill "$SERVER" 2>/dev/null || true; wait "$SERVER" 2>/dev/null || true; }
  [[ -n "$SANDBOX" ]] && { kill -- "-$SANDBOX" 2>/dev/null || true; wait "$SANDBOX" 2>/dev/null || true; }
  echo "Demo stopped. Logs and journals are in $RUN"
}
trap stop EXIT INT TERM

# Refuse to start if any port is taken. Nothing else gets stopped.
"$PYTHON" - "$BASE" "$PORT" <<'PY'
import socket, sys
base, port = int(sys.argv[1]), int(sys.argv[2])
for p in [*range(base, base + 6), port]:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', p))
        except OSError:
            sys.exit(f'Port {p} is in use')
PY

[[ -x demo/.venv/bin/python ]] || "$PYTHON" -m venv demo/.venv
demo/.venv/bin/python -m pip install -q -r batcher/requirements.txt
"$DPM" build --all > "$RUN/build.log" 2>&1 || { cat "$RUN/build.log"; exit 1; }

setsid "$DPM" sandbox --ledger-api-port "$BASE" --admin-api-port "$((BASE + 1))" \
  --json-api-port "$((BASE + 2))" --sequencer-public-port "$((BASE + 3))" \
  --sequencer-admin-port "$((BASE + 4))" --mediator-admin-port "$((BASE + 5))" \
  --dar "$DAR" --log-file-name "$RUN/canton.log" > "$RUN/sandbox.log" 2>&1 &
SANDBOX=$!
echo "Starting a fresh sandbox. Logs in $RUN"

# Ready means the vault package is registered and the participant can allocate parties.
demo/.venv/bin/python - "$((BASE + 2))" "$DAR" <<'PY'
import sys, time, uuid
sys.path.insert(0, 'batcher')
from jsonapi import ApiError, JsonApi
from sandbox_run import package_id
api = JsonApi(f'http://127.0.0.1:{sys.argv[1]}')
package = package_id(sys.argv[2])
for _ in range(180):
    try:
        status = api.post(f'/v2/packages/{package}/status')
        if status.get('packageStatus') == 'PACKAGE_STATUS_REGISTERED':
            api.post('/v2/parties', {'partyIdHint': f'ready-{uuid.uuid4().hex[:8]}'})
            break
    except (OSError, ApiError):
        pass
    time.sleep(1)
else:
    sys.exit('The sandbox did not become ready. See its logs.')
PY

demo/.venv/bin/python -u demo/server.py --ledger "http://127.0.0.1:$((BASE + 2))" --port "$PORT" \
  --data "$RUN/actions" > >(tee "$RUN/server.log") 2>&1 &
SERVER=$!
wait "$SERVER"
