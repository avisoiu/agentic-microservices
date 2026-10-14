#!/usr/bin/env bash
# Tear down what up.sh brought up: undeploy the agentstack app, stop the local worker, and
# stop the Oakestra root + cluster containers. Safe to re-run.
#
#   ./down.sh          stop everything; orchestrator containers and their data are kept,
#                      so ./up.sh can start them again
#   KEEP_APP=1 ./down.sh   don't delete the app, only stop the platform
#
# Asks for sudo once (worker services).
set -euo pipefail

cd "$(dirname "$0")"
APP=agentstack

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
oakq() { oak "$@" 2>&1 | grep -v '^ *[░▒▓█╗╔═╚╝║ ]*$' || true; }

sudo -v

if [[ -z ${KEEP_APP:-} ]]; then
  log "Deleting the $APP app"
  if ! oak a ls >/dev/null 2>&1; then
    echo "Oakestra API not reachable; skipping app deletion"
  elif oakq a ls | awk '{print $2}' | grep -qx "$APP"; then
    oak a delete "$APP" -y
    # Let the worker undeploy the instances before it's stopped.
    for _ in $(seq 1 40); do
      oakq s ls | grep -q " $APP " || break
      sleep 3
    done
    oakq s ls | grep -q " $APP " && echo "WARN: $APP services still listed; continuing"
  else
    echo "$APP not deployed"
  fi
fi

log "Stopping worker (NodeEngine + NetManager)"
sudo systemctl stop nodeengine netmanager
# Anything still running in the worker's containerd namespace (e.g. if the app couldn't be deleted).
leftover=$(sudo ctr -n oakestra tasks ls -q 2>/dev/null || true)
if [[ -n $leftover ]]; then
  echo "Killing leftover Oakestra tasks: $(echo "$leftover" | tr '\n' ' ')"
  for t in $leftover; do sudo ctr -n oakestra tasks kill -s SIGKILL "$t" 2>/dev/null || true; done
  sleep 2
  for t in $leftover; do sudo ctr -n oakestra tasks delete "$t" 2>/dev/null || true; done
fi

log "Stopping Oakestra root + cluster orchestrators"
if docker ps -a --format '{{.Names}}' | grep -qx system_manager; then
  docker compose -p oakestra stop
else
  echo "No Oakestra orchestrator containers found"
fi

log "Done. Run ./up.sh to bring everything back."
