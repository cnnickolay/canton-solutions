#!/usr/bin/env bash
# Runs at every boot: Java, Python, Postgres and CPU logging. Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if [ ! -f /opt/bench/provisioned ]; then
  apt-get update -q
  apt-get install -y -q openjdk-21-jre-headless python3-venv sysstat postgresql
  sudo -u postgres psql -c "CREATE USER canton WITH PASSWORD 'canton' SUPERUSER;"
  for db in sequencer mediator participant; do sudo -u postgres createdb -O canton "$db"; done
  # Postgres tuned a little for a benchmark box.
  cat >> /etc/postgresql/*/main/postgresql.conf <<'PG'
max_connections = 200
shared_buffers = 2GB
synchronous_commit = on
PG
  systemctl restart postgresql
  mkdir -p /opt/bench && chmod 777 /opt/bench
fi
systemctl is-active --quiet vmstat-log || systemd-run --unit=vmstat-log bash -c 'vmstat -t 5 >> /opt/bench/vmstat.log 2>&1'
touch /opt/bench/provisioned
