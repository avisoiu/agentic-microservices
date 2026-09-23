#!/usr/bin/env bash
# Bring up the kind cluster and deploy the agent stack. Idempotent: safe to re-run.
#
# Optional env:
#   OPENAI_API_KEY  enables the cloud fallback behind "agent-model"
#   GITHUB_TOKEN    raises the GitHub API rate limit for the toolbox
#   SKIP_BUILD=1    don't rebuild/load the service images
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER=agents
NS=agents
CHAT_MODEL=qwen2.5:7b
EMBED_MODEL=nomic-embed-text

log() { printf '\n==> %s\n' "$*"; }

log "Checking prerequisites"
for bin in docker kind kubectl ollama; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

log "Ensuring host Ollama is running on 0.0.0.0:11434"
if ! curl -fs localhost:11434/api/version >/dev/null; then
  mkdir -p ~/.local/state
  OLLAMA_HOST=0.0.0.0:11434 nohup ollama serve >~/.local/state/ollama.log 2>&1 &
  for _ in $(seq 1 30); do curl -fs localhost:11434/api/version >/dev/null && break; sleep 1; done
fi
for m in "$CHAT_MODEL" "$EMBED_MODEL"; do
  ollama list | awk '{print $1}' | grep -qE "^${m}(:latest)?$" || ollama pull "$m"
done

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "Creating kind cluster '$CLUSTER'"
  kind create cluster --config "$ROOT/k8s/kind-config.yaml" --wait 120s
else
  log "kind cluster '$CLUSTER' already exists"
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

if [[ -z "${SKIP_BUILD:-}" ]]; then
  log "Building and loading service images"
  for dir in "$ROOT"/services/*/; do
    [[ -f "$dir/Dockerfile" ]] || continue
    name=$(basename "$dir")
    docker build -q -t "agent-stack/$name:dev" "$dir"
    kind load docker-image --name "$CLUSTER" "agent-stack/$name:dev"
  done
fi

log "Applying namespace and secrets"
kubectl apply -f "$ROOT/k8s/namespace.yaml"
kubectl -n "$NS" create secret generic llm-secrets \
  --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY:-sk-placeholder-no-cloud-fallback}" \
  --from-literal=GITHUB_AUTHORIZATION="${GITHUB_TOKEN:+Bearer $GITHUB_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -

log "Applying manifests"
kubectl apply -k "$ROOT/k8s"

# Pods reach the host through the gateway of the Docker network kind runs on.
HOST_IP=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}' |
  tr ' ' '\n' | grep -E '^[0-9]+\.' | head -1)
log "Pointing Service 'ollama' at host Ollama ($HOST_IP:11434)"
kubectl -n "$NS" apply -f - <<EOF
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: ollama-host
  labels:
    kubernetes.io/service-name: ollama
    app.kubernetes.io/part-of: agent-stack
addressType: IPv4
ports:
  - name: http
    port: 11434
    protocol: TCP
endpoints:
  - addresses: ["$HOST_IP"]
EOF

if [[ -z "${SKIP_BUILD:-}" ]]; then
  # Images keep the same :dev tag, so restart to pick up freshly loaded builds.
  for dir in "$ROOT"/services/*/; do
    name=$(basename "$dir")
    kubectl -n "$NS" get deployment "$name" >/dev/null 2>&1 &&
      kubectl -n "$NS" rollout restart deployment "$name"
  done
fi

log "Waiting for workloads"
kubectl -n "$NS" rollout status statefulset --timeout=300s
kubectl -n "$NS" rollout status deployment --timeout=600s

log "Done"
echo "Agent:     http://localhost:8080"
echo "Jaeger UI: http://localhost:16686"
