#!/usr/bin/env bash
# End-to-end smoke test:
#   1. seed the agent's system prompt in the prompt registry (label: production)
#   2. run one agent request through the external NodePort
#   3. verify the trace reached Jaeger and spans every service
#   4. verify the exchange landed in short-term and long-term memory
set -euo pipefail

NS=agents
AGENT_URL=${AGENT_URL:-http://localhost:8080}
JAEGER_URL=${JAEGER_URL:-http://localhost:16686}
REGISTRY_PORT=18000
MEMORY_PORT=18001
RUN_ID=$(date +%s)
USER_ID="smoke-user-$RUN_ID"
SESSION_ID="smoke-session-$RUN_ID"

pass() { printf '  \e[32mPASS\e[0m %s\n' "$*"; }
fail() { printf '  \e[31mFAIL\e[0m %s\n' "$*"; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

# Poll `cmd` every 2s until it succeeds or `timeout` seconds pass.
wait_for() {
  local timeout=$1; shift
  local deadline=$((SECONDS + timeout))
  until "$@"; do
    ((SECONDS < deadline)) || return 1
    sleep 2
  done
}

# prompt-registry and memory are ClusterIP-only; reach them via port-forward.
PF_PIDS=()
cleanup() { for pid in "${PF_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT
kubectl -n "$NS" port-forward svc/prompt-registry "$REGISTRY_PORT:8000" >/dev/null 2>&1 &
PF_PIDS+=($!)
kubectl -n "$NS" port-forward svc/memory "$MEMORY_PORT:8000" >/dev/null 2>&1 &
PF_PIDS+=($!)
REGISTRY="http://localhost:$REGISTRY_PORT"
MEMORY="http://localhost:$MEMORY_PORT"
wait_for 30 curl -fs "$REGISTRY/healthz" -o /dev/null || fail "prompt-registry port-forward"
wait_for 30 curl -fs "$MEMORY/healthz" -o /dev/null || fail "memory port-forward"
wait_for 30 curl -fs "$AGENT_URL/healthz" -o /dev/null || fail "agent not reachable at $AGENT_URL"

step "Seeding system prompt 'agent-system' (label: production)"
TEMPLATE='You are a concise assistant that helps developers learn about open-source projects.
Today is {{date}}. You are talking to user {{user_id}}.

What you remember about this user:
{{memories}}

When the user asks about a GitHub repository, call the available tools to get live data instead of guessing. Answer in at most 4 sentences.'
CURRENT=$(curl -fs "$REGISTRY/prompts/agent-system?label=production" | jq -r .template || true)
if [[ "$CURRENT" == "$TEMPLATE" ]]; then
  pass "production prompt already up to date"
else
  jq -n --arg t "$TEMPLATE" '{template: $t, labels: ["production"]}' |
    curl -fs -XPOST "$REGISTRY/prompts/agent-system" -H 'Content-Type: application/json' -d @- |
    jq -r '"  created version \(.version) labels=\(.labels)"'
  pass "prompt seeded"
fi

step "Running agent request (user=$USER_ID session=$SESSION_ID)"
MESSAGE="I'm a Rust developer based in Cluj. How popular is the qdrant/qdrant repository on GitHub?"
START=$SECONDS
RESULT=$(jq -n --arg u "$USER_ID" --arg s "$SESSION_ID" --arg m "$MESSAGE" \
  '{user_id: $u, session_id: $s, message: $m, wait_for_memory: true}' |
  curl -fsS --max-time 300 -XPOST "$AGENT_URL/run" -H 'Content-Type: application/json' -d @-) ||
  fail "agent request failed"
echo "$RESULT" | jq '{answer, tool_calls: [.tool_calls[] | "\(.name) \(.arguments | tostring)"], prompt, trace_id}'
echo "  took $((SECONDS - START))s"
TRACE_ID=$(echo "$RESULT" | jq -r .trace_id)
[[ -n "$(echo "$RESULT" | jq -r '.answer // empty')" ]] && pass "agent answered" || fail "empty answer"
[[ $(echo "$RESULT" | jq '.tool_calls | length') -gt 0 ]] && pass "agent called a tool" ||
  fail "agent did not call a tool"

step "Verifying trace $TRACE_ID in Jaeger"
EXPECTED='["agent","llm-proxy","memory","prompt-registry","toolbox"]'
trace_complete() {
  curl -fs "$JAEGER_URL/api/traces/$TRACE_ID" 2>/dev/null |
    jq -e --argjson want "$EXPECTED" \
      '([.data[0].processes[].serviceName] | unique) as $have | ($want - $have) == []' >/dev/null
}
if wait_for 60 trace_complete; then
  curl -fs "$JAEGER_URL/api/traces/$TRACE_ID" |
    jq -r '.data[0] | "  \(.spans | length) spans across: \([.processes[].serviceName] | unique | join(", "))"'
  pass "trace spans all services"
else
  echo "  services seen: $(curl -fs "$JAEGER_URL/api/traces/$TRACE_ID" | jq -c '[.data[0].processes[].serviceName] | unique' 2>/dev/null)"
  fail "trace incomplete after 60s"
fi
echo "  open: $JAEGER_URL/trace/$TRACE_ID"

step "Verifying memory"
SESSION=$(curl -fs "$MEMORY/sessions/$SESSION_ID/messages")
[[ $(echo "$SESSION" | jq '.messages | length') -eq 2 ]] &&
  pass "short-term: 2 messages in session (ttl $(echo "$SESSION" | jq .ttl_seconds)s)" ||
  fail "short-term: expected 2 messages, got $(echo "$SESSION" | jq -c .messages)"
FACTS=$(curl -fs "$MEMORY/users/$USER_ID/memories")
if [[ $(echo "$FACTS" | jq '.results | length') -gt 0 ]]; then
  echo "$FACTS" | jq -r '.results[] | "  fact: \(.memory)"'
  pass "long-term: facts extracted for $USER_ID"
else
  fail "long-term: no facts stored for $USER_ID"
fi

printf '\n\e[32mSmoke test passed.\e[0m\n'
