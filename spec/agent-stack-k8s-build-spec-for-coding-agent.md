# Spec: Agentic Microservice Stack on Kubernetes

## Goal

Build a microservice-based agentic AI stack and deploy it on a local Kubernetes cluster (e.g. kind, minikube, or k3s). This is a learning exercise to discover pain points of running agentic workloads on K8s. All services communicate via cluster DNS names.

## Services to build

Each service is a small containerized microservice (Python/FastAPI is fine). Keep implementations minimal but functional.

1. **LLM proxy** — OpenAI-compatible chat completions endpoint (`/v1/chat/completions`). Use **LiteLLM**. Must support two backends behind one virtual model name: a local model via **Ollama**, and a cloud provider (OpenAI) as fallback. Routing/fallback configurable via env or config file.
2. **Memory service** — REST API with:
   - short-term memory: per-session message history with TTL (backed by **Redis**)
   - long-term memory: per-user facts with semantic search (backed by **Qdrant** + **mem0**)
3. **Prompt registry** — versioned prompt templates (CRUD + fetch by name/label, e.g. `production`). Storage can be simple (SQLite or JSON file in a volume).
4. **Toolbox** — tool service exposing tools to agents. Use **Google GenAI Toolbox (mcp-toolbox)** or a simple FastAPI equivalent; expose at least one example tool (e.g. fetch GitHub repo info). MCP endpoint preferred.
5. **Observability** — all services export **OpenTelemetry traces** (W3C trace context propagated end-to-end); an **OTel Collector** receives spans; a trace store/UI (e.g. **Jaeger** or **Langfuse**) displays them.
6. **Demo agent (harness)** — a small agent service that wires it all together: fetches its system prompt from the prompt registry, loads memory, calls the LLM via the proxy, invokes a tool, stores the exchange. Simple REST endpoint to trigger a run. No heavy framework needed — plain HTTP client is fine.

## K8s deployment

- Deployments for stateless services (proxy, memory API, prompt registry, toolbox, agent); StatefulSets or persistent volumes for Redis and Qdrant.
- ClusterIP Services for every component; the agent talks to others only via service names (e.g. `http://llm-proxy:4000`).
- One ingress or NodePort to expose the demo agent externally.
- Config via ConfigMaps/Secrets (API keys, backend URLs).
- Optional: resource requests/limits, readiness probes, labels.

## Deliverables

- Source tree with one directory per service (Dockerfile each) + a shared k8s manifests directory (or Helm chart).
- A `kind`/`minikube`/`k3s` setup script or instructions to bring the cluster up.
- A short README: how to deploy, how to run a demo agent request, where to see traces.
- A smoke-test script: seed a prompt, run one agent request, verify it appears in traces and memory.

## Constraints & notes

- Prefer off-the-shelf OSS components (LiteLLM, Redis, Qdrant, mem0, OTel Collector, Jaeger) over custom code.
- LLM backends: Ollama (local, can run in-cluster or on host) and OpenAI-compatible cloud (key via Secret). Everything must work with the local backend alone.
- Keep it small: this is a playground, not production. Minimal but real — services must actually work end-to-end.
- While building, note pain points (image sizes, cold start, stateful setup, metrics plumbing, wiring complexity) — these observations are the point of the exercise.