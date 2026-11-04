#!/usr/bin/env bash
# Start one Canton node: run-node.sh <sync|custodian|holders> [memory|postgres]
# Needs CANTON (path to the canton launcher) and, for participants, SYNC_HOST.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
role="$1"
storage="${2:-memory}"
case "$role" in
  sync) node="$here/sync.conf"; boot="$here/bootstrap-sync.canton" ;;
  custodian|holders) node="$here/participant-$role.conf"; boot="$here/bootstrap-participant.canton" ;;
  *) echo "unknown role $role" >&2; exit 2 ;;
esac
export JAVA_OPTS="${JAVA_OPTS:-} -Xms1g -Xmx${HEAP:-8g} -Dpekko.http.server.request-timeout=600s -Dpekko.http.server.idle-timeout=660s"
exec "${CANTON:-canton}" daemon --no-tty -c "$here/storage-$storage.conf" -c "$node" --bootstrap "$boot" \
  --log-file-name "${LOG_DIR:-/tmp}/canton-$role.log"
