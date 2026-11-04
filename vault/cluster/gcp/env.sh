# Shared settings for the GCP scripts. Source it, do not run it.
PROJECT="${PROJECT:-canton-vault-bench}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="$(cd "$HERE/../.." && pwd)"
# gcloud runs from its Docker image. GCLOUD_HOME holds the login and ssh keys
# and must never be committed.
GCLOUD_HOME="${GCLOUD_HOME:?set GCLOUD_HOME to a private folder for gcloud credentials}"
# The zone up.sh found capacity in, saved for the other scripts.
ZONE="${ZONE:-$(cat "$GCLOUD_HOME/zone" 2>/dev/null || echo us-central1-a)}"
ZONES=(${ZONES:-us-central1-b us-central1-c us-central1-f us-central1-a us-east1-b us-east1-c us-east4-a us-east4-c us-west1-a us-west1-b})
CANTON_HOME="${CANTON_HOME:-$HOME/.dpm/cache/components/canton-open-source/3.5.15}"
STORAGE="${STORAGE:-postgres}"
# name:machine-type
NODES=("vault-sync:n2-standard-4" "vault-custodian:c2d-standard-16" "vault-holders:n2-standard-4")

gc() {
  docker run --rm -v "$GCLOUD_HOME/config:/root/.config/gcloud" -v "$GCLOUD_HOME/ssh:/root/.ssh" \
    -v "$VAULT:/vault" -v "${TRANSFER_DIR:-$GCLOUD_HOME/transfer}:/transfer" \
    gcr.io/google.com/cloudsdktool/google-cloud-cli:slim gcloud --project "$PROJECT" "$@"
}

on() {  # on <vm> <command>
  local vm="$1"; shift
  gc compute ssh "$vm" --zone "$ZONE" --quiet --command "$*" < /dev/null
}

internal_ip() {
  gc compute instances describe "$1" --zone "$ZONE" --format='value(networkInterfaces[0].networkIP)'
}
