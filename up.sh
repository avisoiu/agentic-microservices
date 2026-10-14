#!/usr/bin/env bash
# Bring up Oakestra (single machine: root + cluster + local worker) and deploy the agentstack app
# (ollama + llmproxy + LangGraph agent) from llm-proxy-sla.json. Safe to re-run.
#
#   ./up.sh               build the agent image, redeploy the app, run smoke tests
#   SKIP_BUILD=1 ./up.sh  reuse the agent image already imported into containerd
#
# Asks for sudo once (worker services and containerd image import).
set -euo pipefail

cd "$(dirname "$0")"
SLA=llm-proxy-sla.json
APP=agentstack
IMAGE=docker.io/agent-stack/langgraph-agent:dev
COMPOSE_FILE=${COMPOSE_FILE:-$HOME/.oakestra/1-DOC.yaml}
HOST_IP=${HOST_IP:-$(ip route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')}

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
# oak prints a banner on some errors; keep only real lines.
oakq() { oak "$@" 2>&1 | grep -v '^ *[░▒▓█╗╔═╚╝║ ]*$' || true; }
wait_for() { # wait_for <description> <timeout-seconds> <command...>
  local what=$1 timeout=$2; shift 2
  local deadline=$((SECONDS + timeout))
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for $what"
    sleep 3
  done
  echo "ok: $what"
}

for cmd in docker oak curl python3 ip; do command -v "$cmd" >/dev/null || die "missing command: $cmd"; done
[[ -n $HOST_IP ]] || die "could not detect host IP; run with HOST_IP=<ip> ./up.sh"
echo "Host IP: $HOST_IP"
sudo -v

log "Starting Oakestra root + cluster orchestrators"
if docker ps -a --format '{{.Names}}' | grep -qx system_manager; then
  # Existing containers already have their env baked in. 'start' keeps them (and their data).
  docker compose -p oakestra start
elif [[ -f $COMPOSE_FILE ]]; then
  SYSTEM_MANAGER_URL=$HOST_IP CLUSTER_ADDRESS=$HOST_IP \
    CLUSTER_NAME=${CLUSTER_NAME:-default_cluster} CLUSTER_LOCATION=${CLUSTER_LOCATION:-44.8500,24.8667,1000} \
    docker compose -p oakestra -f "$COMPOSE_FILE" up -d
else
  die "no Oakestra containers and no $COMPOSE_FILE; install Oakestra first (scripts/StartOakestraFull.sh)"
fi
wait_for "system manager API" 180 curl -s -o /dev/null "http://$HOST_IP:10000/api/docs"  # any HTTP reply means it is up
wait_for "cluster registered" 180 sh -c "oak cl ls 2>&1 | grep -q '● active'"

log "Starting worker (NetManager + NodeEngine)"
sudo systemctl start netmanager nodeengine
wait_for "worker node joined the cluster" 180 sh -c "[ \"\$(oak cl ls 2>&1 | awk '/● active/ {print \$(NF-2)}' | head -1)\" -ge 1 ]"

if [[ -z ${SKIP_BUILD:-} ]]; then
  log "Building agent image and importing it into the worker's containerd"
  docker build -q -t "$IMAGE" langgraph-agent
  # NodeEngine checks the 'oakestra' containerd namespace before pulling online,
  # so an imported image is used without needing a registry.
  docker save "$IMAGE" | sudo ctr -n oakestra images import -
fi
sudo ctr -n oakestra images ls -q | grep -qx "$IMAGE" || die "$IMAGE is not in containerd namespace 'oakestra'"

log "Deploying $APP from $SLA"
if oakq a ls | awk '{print $2}' | grep -qx "$APP"; then
  echo "Deleting existing $APP app"
  oak a delete "$APP" -y
  wait_for "old $APP app removed" 120 sh -c "! oak a ls 2>&1 | awk '{print \$2}' | grep -qx $APP"
fi
oak app create -f "$SLA" -d

expected=$(python3 -c "import json; print(len(json.load(open('$SLA'))['applications'][0]['microservices']))")
wait_for "all $expected services running (image pulls can take minutes)" 900 \
  sh -c "[ \"\$(oak s ls 2>&1 | grep -c ' $APP .*1/1 running')\" -eq $expected ]"
oakq s ls

log "Waiting for the services to be ready"
# Only the agent publishes a port. Services with a published port can't be reached over
# the Oakestra overlay, so LiteLLM and Ollama are internal and checked through the agent.
wait_for "agent" 120 curl -sf "http://$HOST_IP:8000/healthz"
wait_for "agent can reach LiteLLM over the overlay" 300 curl -sf -m 10 "http://$HOST_IP:8000/readyz"
# Ollama is 'running' before it has finished pulling its models; wait for a real answer.
wait_for "chat model pulled (agent /run answers)" 600 curl -sf -m 120 "http://$HOST_IP:8000/run" \
  -H 'Content-Type: application/json' -d '{"message":"ping"}'

log "Smoke tests"
python3 test_deployment.py "http://$HOST_IP:8000"
echo
echo "Agent (LangGraph → LiteLLM → Ollama):"
curl -s -m 120 "http://$HOST_IP:8000/run" -H 'Content-Type: application/json' \
  -d '{"message":"Say hello from Oakestra in one short sentence."}'
echo
log "Done. Agent: http://$HOST_IP:8000/run   (LiteLLM is internal: 10.30.10.2:4000)"
