# Agentic microservice stack on Kubernetes

A small but fully working agentic AI stack on a local **kind** cluster. It's a playground
for finding out what's hard about running agent workloads on K8s. Findings are in
[PAIN_POINTS.md](PAIN_POINTS.md), and the original spec is in [spec/](spec/).

```
                        localhost:8080 (NodePort)
                               │
                        ┌──────▼──────┐   GET prompt by label   ┌─────────────────┐
                        │    agent    ├────────────────────────►│ prompt-registry │ SQLite on PVC
                        │  (FastAPI)  │                         └─────────────────┘
                        │             │   history / facts       ┌─────────────────┐     ┌───────┐
                        │             ├────────────────────────►│     memory      ├────►│ redis │ short-term, TTL
                        │             │                         │ (FastAPI + mem0)├──┐  └───────┘
                        │             │   MCP tools/list, call  ├─────────────────┤  │  ┌────────┐
                        │             ├────────────────────────►│     toolbox     │  └─►│ qdrant │ long-term facts
                        │             │                         │  (mcp-toolbox)  │     └────────┘
                        │             │   /v1/chat/completions  ├─────────────────┤
                        │             ├────────────────────────►│    llm-proxy    │──► ollama (host GPU)
                        └─────────────┘                         │    (LiteLLM)    │──► OpenAI (fallback)
                                                                └─────────────────┘
   every service ──OTLP──► otel-collector ──► jaeger ◄── localhost:16686 (UI)
```

| Component | Implementation | Address in the cluster |
|---|---|---|
| LLM proxy | LiteLLM (stock image + [config](k8s/config/litellm.yaml)) | `http://llm-proxy:4000` |
| Memory | [services/memory](services/memory): Redis (short-term, TTL) + mem0/Qdrant (long-term) | `http://memory:8000` |
| Prompt registry | [services/prompt-registry](services/prompt-registry): versions + movable labels, SQLite | `http://prompt-registry:8000` |
| Toolbox | Google MCP Toolbox (stock image + [tools](k8s/config/toolbox.yaml)): GitHub repo and release tools | `http://toolbox:5000/mcp` |
| Agent | [services/agent](services/agent): plain httpx, no framework | `http://agent:8000`, external `localhost:8080` |
| Traces | OTel Collector ([config](k8s/config/otel-collector.yaml)) → Jaeger v2 | UI at `localhost:16686` |
| Local LLM | Ollama **on the host** (`qwen2.5:7b`, `nomic-embed-text`) | `http://ollama:11434` (selector-less Service) |

Each agent run: fetch the `production` system prompt → load the session history and
search the user's facts → list MCP tools → LLM ⇄ tool loop through the proxy → store the
exchange in session memory, and the user's message in long-term memory (in the
background). The whole run is **one distributed trace** across all five services.

## Prerequisites

- Docker, `kind`, `kubectl`, `jq` (and `helm`/`uv` for development).
- [Ollama](https://ollama.com) on the host. `scripts/cluster-up.sh` starts it on
  `0.0.0.0:11434` if it isn't running and pulls both models. **Note:** this exposes
  Ollama on your LAN.
- Around 6 GB of free disk for images, and a GPU helps. Everything runs with only the
  local model; no cloud key is needed.

## Deploy

```bash
scripts/cluster-up.sh                  # create cluster, build + load images, deploy, wait
OPENAI_API_KEY=sk-... scripts/cluster-up.sh   # optional: enable the cloud fallback
GITHUB_TOKEN=ghp_... scripts/cluster-up.sh    # optional: raise GitHub's 60 req/h limit
SKIP_BUILD=1 scripts/cluster-up.sh     # re-apply manifests only
scripts/cluster-down.sh                # delete the cluster (and all data)
```

The script is safe to re-run. Configuration lives in `k8s/kustomization.yaml` (the
shared `stack-env` ConfigMap with service URLs and OTel settings) and `k8s/config/*`.
Secrets (`llm-secrets`) are created from your environment and never committed.

## Run a demo request

The agent needs a system prompt with the label `production`. The smoke test seeds one;
you can also add one by hand (see below).

```bash
curl -s localhost:8080/run -H 'Content-Type: application/json' -d '{
  "user_id": "alice",
  "session_id": "s1",
  "message": "I mostly write Go. What is the latest release of kubernetes-sigs/kind?"
}' | jq
```

The response includes the `answer`, the `tool_calls` made, the `memories_used`, the
prompt version, and the `trace_id`. Send another message with the same `session_id` to
continue the conversation. Facts about `alice` are remembered across sessions. Add
`"wait_for_memory": true` to wait for fact extraction before the response returns
(about 15 s).

## See the traces

Open **http://localhost:16686**, choose service `agent` and click *Find Traces*, or go
straight to `http://localhost:16686/trace/<trace_id>` from a `/run` response. One run
shows spans from `agent`, `prompt-registry`, `memory` (including Redis commands and
mem0's LLM/embedding calls), `toolbox` (MCP `tools/call` → GitHub) and `llm-proxy`.

## Smoke test

```bash
scripts/smoke-test.sh
```

The script seeds the prompt, sends one agent request through the NodePort, and checks
that the trace in Jaeger includes all 5 services. It also checks that the session holds
2 messages and that mem0 stored at least one fact for the user.

## Service APIs

Internal services are ClusterIP only. Use `kubectl -n agents port-forward svc/<name> <port>`
to reach them from your machine.

**prompt-registry** (`:8000`)
```
POST   /prompts/{name}                 {"template": "...", "labels": ["production"]} → new version
GET    /prompts/{name}?label=production | ?version=2 | (latest)
GET    /prompts/{name}/versions
PUT    /prompts/{name}/labels/{label}  {"version": 2}   # promote / roll back
DELETE /prompts/{name}/labels/{label}
GET    /prompts
```
Templates can use `{{user_id}}`, `{{date}}` and `{{memories}}`.

**memory** (`:8000`)
```
POST   /sessions/{id}/messages         {"messages": [{"role": "user", "content": "..."}]}
GET    /sessions/{id}/messages?limit=20
DELETE /sessions/{id}
POST   /users/{id}/memories            {"messages": [...], "infer": true}
GET    /users/{id}/memories
GET    /users/{id}/memories/search?q=...&limit=5
DELETE /users/{id}/memories
```

**llm-proxy** (`:4000`): the OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings`
endpoints. Models: `agent-model` (Ollama, falls back to `agent-model-cloud`) and
`embed-model`.

**toolbox** (`:5000`): MCP streamable HTTP at `/mcp`, with the tools `get_github_repo`
and `get_github_latest_release`.

## Layout

```
k8s/                   kustomize base: one file per component, kind-config.yaml
k8s/config/            LiteLLM, OTel collector and mcp-toolbox configs (→ ConfigMaps)
services/agent/        demo agent (FastAPI, httpx, hand-rolled MCP client)
services/memory/       memory API (FastAPI, redis, mem0)
services/prompt-registry/
scripts/               cluster-up.sh, cluster-down.sh, smoke-test.sh
```

Python services use [uv](https://docs.astral.sh/uv/) (`uv sync` inside a service
directory for local development) and run under `opentelemetry-instrument`.
