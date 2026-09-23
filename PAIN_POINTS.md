# Pain points log

What we ran into while building this stack. Recording these was the goal of the exercise.
Numbers are from a laptop with a Ryzen CPU, 30 GB RAM and an RTX 3070 (8 GB), running kind v0.33 / K8s 1.37.

## 1. Local LLM and GPU

- **GPU inside kind is a project of its own.** Using the GPU in-cluster needs
  `nvidia-container-toolkit` on the host (sudo), a kind node config that mounts the
  devices, and the NVIDIA device plugin. We avoided it: Ollama runs on the host and a
  **selector-less Service + hand-made EndpointSlice** gives it the DNS name `ollama`.
  The catch: the host IP (the kind Docker network gateway, `172.19.0.1` here) is only
  known at runtime, so it can't be in the static manifests. `cluster-up.sh` looks it up
  and applies the EndpointSlice.
- **Host exposure:** for kind nodes to reach Ollama it must listen on `0.0.0.0`, which
  also exposes it to the LAN.
- **Ollama footprint:** the release tarball is 1.4 GB (2.1 GB unpacked, mostly bundled
  CUDA libraries) before any model. `qwen2.5:7b` adds 4.7 GB, and chat plus embedding
  models use about 5.3 GB of the 8 GB of VRAM.
- **Cold start:** the first completion after installing took **51 s** while the model
  loaded into VRAM. Later reloads with the files in the OS page cache take about 5 s.
  Warm calls take about 0.25 s. Ollama unloads idle models after 5 minutes, so
  `keep_alive: 30m` is set in the LiteLLM config.
- **Fallback is easy to configure but hard to test without a paid key.** With Ollama
  stopped, LiteLLM moved to `agent-model-cloud` as configured and failed only on the
  placeholder API key. The error message is the only evidence the fallback happened.

## 2. Images and cold start

| Image | Uncompressed | Compressed on node |
|---|---|---|
| LiteLLM `v1.101.0` | **1.63 GB** | 383 MB |
| memory (FastAPI + mem0) | 769 MB | 171 MB |
| agent (FastAPI + httpx + OTel) | 438 MB | 107 MB |
| prompt-registry (FastAPI + SQLite + OTel) | 434 MB | 106 MB |
| mcp-toolbox | 435 MB | 92 MB |
| qdrant / redis / jaeger / collector | 74 / 54 / 55 / 113 MB | |

- A CRUD service backed by SQLite ends up at **434 MB**. About 180 MB of that is
  `python:3.12-slim`, and the OpenTelemetry + uvicorn[standard] dependencies add a lot
  more. Every Python service pays this cost.
- LiteLLM is a large Python app that starts slowly (plus a 1.6 GB image pull on a new
  node), so it has a generous `startupProbe` (up to 5 min).
- **Dev loop:** `kind load docker-image` copies every image into the node on each build.
  Our images keep the `:dev` tag, so the script also has to `rollout restart`, and it
  restarts every service, changed or not. Old image layers pile up inside the node.
- **The first request after a rollout returned an empty reply.** kube-proxy was still
  sending NodePort traffic to the terminating pod. We fixed this with a
  `preStop: sleep 5s` on the agent.

## 3. Tracing and observability

Getting **one trace across all 5 services** (agent → prompt-registry, memory → llm-proxy,
toolbox → GitHub) needed a different fix for each component:

- **Python services:** zero-code `opentelemetry-instrument` works, but the knobs you can
  set by env var are incomplete. The ASGI `http send`/`http receive` sub-spans (one per
  message!) can only be disabled in code (`exclude_spans=`). We **drop them in the
  collector** with a `filter` processor, together with the Redis `PING`s from readiness
  checks. `/healthz` and `/readyz` are excluded at the source through
  `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`.
- **LiteLLM** respects an incoming `traceparent` **header**. Its OTel config uses its own
  env vars (`OTEL_EXPORTER`/`OTEL_ENDPOINT`), and it also accepts the standard
  `OTEL_EXPORTER_OTLP_*` names.
- **mcp-toolbox ignores the HTTP `traceparent` header.** It only continues a trace from
  `params._meta.traceparent` inside the JSON-RPC body, which is the MCP convention. The
  standard httpx instrumentation can't inject it there, so the agent has a small
  hand-written MCP client that puts the trace context in `_meta`.
- **mcp-toolbox `--telemetry-otlp`:** its help text shows an example with `http://`, but
  a scheme breaks it (`parse "https://http:%2F%2F..."`, crash loop). It needs bare
  `host:port`, plus `OTEL_EXPORTER_OTLP_INSECURE=true` because it defaults to HTTPS.
- **Jaeger v2.21 API changes:** `/api/services` and trace search now return 404, but
  `/api/traces/{id}` still works. Search moved to `/api/v3/...`, which uses different
  query parameters and returns OTLP JSON. Scripts written for v1 break without warning.
- Jaeger all-in-one stores traces in memory: **every pod restart loses all traces**.
- A single agent run produces **50–75 spans**: LiteLLM alone emits about 6 spans per
  LLM call (`proxy_pre_call`, `router`, `litellm_request`, `raw_gen_ai_request`, ...).
  The waterfall is readable but noisy.
- Only traces were set up. Metrics would need yet another pipeline (collector
  → Prometheus); we set `OTEL_METRICS_EXPORTER=none` everywhere.

## 4. Stateful setup

- Redis and Qdrant as StatefulSets with `volumeClaimTemplates` worked first time on
  kind's `standard` StorageClass. That volume is a `hostPath` inside the node
  container, so `kind delete cluster` deletes all the data.
- The prompt registry uses SQLite on an RWO PVC, which forces `replicas: 1` and a
  `Recreate` rollout strategy. That means brief downtime on every deploy.
- Jaeger is in-memory (see above), and mem0's change history is an SQLite file in
  `/tmp` of the memory pod. So 4 components have 4 different answers to "where does
  state live".
- `kubectl apply` reports the StatefulSets as `configured` on every run, even when
  `kubectl diff` shows no change.

## 5. Memory (mem0) behaviour

- **Adding one memory takes about 14 s**: mem0 makes several LLM calls to extract facts
  and decide add/update/delete, plus embedding calls. That's too slow to do inline, so
  the agent stores long-term memory **in a background task** after it replies. The trace
  still contains that work because the server span waits for background tasks.
- **Self-reinforcing memory:** we first sent both user and assistant messages to mem0.
  It stored the *assistant's* (partly wrong) answer as a "fact". That fact was recalled
  into the next prompt and the model repeated it instead of trusting the fresh tool
  result. Fix: send only the user's messages to long-term memory.
- mem0 2.x prints warnings about optional spaCy/fastembed extras (lemmatization, BM25)
  that aren't installed. Adding them would make the 769 MB image much bigger.
- mem0's API changed between major versions (`search`/`get_all` now take `filters={...}`),
  so older tutorials don't work.

## 6. Tools and wiring

- mcp-toolbox's **HTTP source** handles a REST API such as GitHub with no code
  (`pathParams`, headers from env vars), but:
  - A header can't be conditional. `Authorization: Bearer ${GITHUB_TOKEN}` with an empty
    token makes GitHub return **401**, while an empty header is ignored. So the Secret
    holds the whole header value (`GITHUB_AUTHORIZATION`), and the script leaves it
    empty when there's no token.
  - Tool results are the raw upstream JSON, re-serialized with **keys in alphabetical
    order**, and there's no field selection. A repo lookup returns about 7 KB, mostly
    `*_url` links. For a release, the huge `assets` list sorts before `tag_name`, so
    naive truncation cut off exactly the field the model needed, and the model then
    answered without the version. The agent now compacts each value separately.
  - Read-only GET tools are marked `destructiveHint: true`.
  - Unauthenticated GitHub allows 60 requests per hour, which is easy to use up while
    iterating.
- **Service configs in kustomize:** config files must live under `k8s/` (kustomize won't
  load files outside its root), so `k8s/config/` holds the LiteLLM, collector and
  toolbox configs. Using `configMapGenerator` means a config change also changes the
  ConfigMap name and rolls the pods. With a hand-written ConfigMap, the collector kept
  running its old config until we restarted it by hand.
- A new FastAPI/Starlette version made `TestClient` require an extra package
  (`httpx2`). This kind of dependency churn is typical of the Python AI stack.
- Wiring is simple at runtime (every service is `http://<name>:<port>`), but the shared
  `stack-env` ConfigMap (OTel settings, service URLs) is what keeps it from drifting.

## 7. If this were a standard edge stack

On a laptop these are annoyances. Across a fleet of unattended sites on metered links,
they become operational hazards. What an edge orchestration framework should own:

| Pain point | Edge risk | Platform primitive |
|---|---|---|
| §1 host Ollama via hand-patched EndpointSlice | every app re-invents device plumbing | `HostService`: node-local endpoint reconciled by the node agent, bound node-local (never `0.0.0.0`) |
| §1 4.7 GB weights, 5.3/8 GB VRAM, 51 s cold start | WAN cost; second app thrashes the GPU | model weights as content-addressed artifacts (node cache, pin/GC, P2P within a site); VRAM-aware `ModelClaim`; readiness = real completion |
| §2 1.63 GB proxy image, restart-everything deploys | metered-link blowout, needless downtime | routing as a small platform binary; blessed runtime base in the node image; digest-pinned rollouts; resumable/lazy pulls |
| §3 50–75 spans/run, in-memory Jaeger | can't ship it all; reboot erases the evidence | default noise/normalization profile; site-side tail sampling; store-and-forward + durable local store with disk quota; auto site/node attributes |
| §3 MCP `_meta` propagation | every app hand-rolls protocol glue | propagation in the platform SDK / MCP gateway |
| §4 four answers to "where does state live"; §5 background-task writes | power cut = silent data loss | declared durability tiers (cache / durable-local, encrypted / site-replicated); durable idempotent post-turn queue |
| §1 fallback visible only in an error string | regulated data silently egresses | routing policy with data-egress rules, cost caps and offline behaviour; fallback as an event/metric |
| §6 no field projection, rate limits, conditional auth | tools actuate machinery at edge | MCP gateway: projection/caps, cache + rate budget, auth injection, egress allowlist, audit log |
| §5 self-reinforcing memory | memory poisoning once multi-tenant | provenance tags (`user`/`assistant`/`tool`) + retrieval that prefers fresh tool data |
| §2 `preStop` race; placeholder key | unattended boxes, nobody to debug | graceful drain by default; deploy-time preflight ("capability degraded"); canary + auto-rollback; `smoke-test.sh` as a per-site conformance check; fault-injection drills (WAN loss, power cut) |

First four worth building: model distribution + residency, tail sampling with
store-and-forward, durable queue + state tiers, egress/routing policy.
Stays app-level: prompt content, agent control flow, memory semantics.
