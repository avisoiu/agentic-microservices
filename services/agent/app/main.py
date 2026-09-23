"""Demo agent harness.

One POST /run does a full agent turn using only the other services, addressed by
their cluster DNS names:

  1. system prompt       <- prompt-registry (by name + label)
  2. session history     <- memory (Redis), relevant user facts <- memory (mem0)
  3. tool definitions    <- toolbox (MCP tools/list)
  4. LLM loop            -> llm-proxy (OpenAI-compatible), executing tool calls via MCP
  5. store the exchange  -> memory (session history + long-term facts)
"""

import asyncio
import json
import os
from datetime import UTC, datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

from app.mcp_client import MCPClient, MCPError

LLM_PROXY_URL = os.getenv("LLM_PROXY_URL", "http://llm-proxy:4000")
MEMORY_URL = os.getenv("MEMORY_URL", "http://memory:8000")
PROMPT_REGISTRY_URL = os.getenv("PROMPT_REGISTRY_URL", "http://prompt-registry:8000")
TOOLBOX_URL = os.getenv("TOOLBOX_URL", "http://toolbox:5000")
MODEL = os.getenv("AGENT_MODEL", "agent-model")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "3"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))
TOOL_RESULT_MAX_CHARS = int(os.getenv("TOOL_RESULT_MAX_CHARS", "2000"))

tracer = trace.get_tracer("demo-agent")
http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))

app = FastAPI(title="agent")


class RunIn(BaseModel):
    user_id: str
    session_id: str
    message: str
    prompt_name: str = "agent-system"
    prompt_label: str = "production"
    # Wait for long-term memory extraction (slow: several LLM calls) before replying.
    wait_for_memory: bool = False


class ToolCallOut(BaseModel):
    name: str
    arguments: dict
    is_error: bool


class RunOut(BaseModel):
    answer: str
    trace_id: str
    prompt: dict
    memories_used: list[str]
    tool_calls: list[ToolCallOut]
    llm_rounds: int


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


async def get_json(url: str, **params) -> dict:
    resp = await http.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def render(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def mcp_to_openai_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        },
    }


SUMMARY_KEYS = ("full_name", "login", "tag_name", "name")
DROP_KEYS = {"url", "node_id", "gravatar_id"}


def compact(value, depth: int = 0):
    """Shrink API payloads for a small local context window.

    - drop nulls and the many hypermedia *_url fields GitHub returns (keep html_url)
    - below the top level, collapse objects to their identifying field
      (owner -> login, license -> name, release asset -> name)
    - cap long lists and strings individually

    Capping per value matters: mcp-toolbox re-serialises JSON with sorted keys, so
    e.g. a release's huge "assets" list comes before "tag_name" and a plain prefix
    truncation would cut off the fields the model actually needs.
    """
    if isinstance(value, dict):
        if depth >= 1:
            for key in SUMMARY_KEYS:
                if isinstance(value.get(key), str):
                    return value[key]
        return {
            k: compact(v, depth + 1)
            for k, v in value.items()
            if v is not None and k not in DROP_KEYS and (not k.endswith("_url") or k == "html_url")
        }
    if isinstance(value, list):
        items = [compact(v, depth + 1) for v in value[:5]]
        if len(value) > 5:
            items.append(f"...{len(value) - 5} more")
        return items
    if isinstance(value, str) and len(value) > 600:
        return value[:600] + "..."
    return value


def compact_tool_result(text: str) -> str:
    try:
        text = json.dumps(compact(json.loads(text)), separators=(",", ":"))
    except json.JSONDecodeError:
        pass
    if len(text) > TOOL_RESULT_MAX_CHARS:
        text = text[:TOOL_RESULT_MAX_CHARS] + "...[truncated]"
    return text


async def chat(messages: list[dict], tools: list[dict]) -> dict:
    body = {"model": MODEL, "messages": messages, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    resp = await http.post(f"{LLM_PROXY_URL}/v1/chat/completions", json=body)
    if resp.status_code != 200:
        raise HTTPException(502, f"llm-proxy returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()["choices"][0]["message"]


async def store_long_term(user_id: str, messages: list[dict]) -> None:
    resp = await http.post(f"{MEMORY_URL}/users/{user_id}/memories", json={"messages": messages})
    resp.raise_for_status()


@app.post("/run", response_model=RunOut)
async def run(req: RunIn, background: BackgroundTasks) -> RunOut:
    span = trace.get_current_span()
    span.set_attributes({"agent.user_id": req.user_id, "agent.session_id": req.session_id})
    trace_id = format(span.get_span_context().trace_id, "032x")

    # 1-3: gather context from the other services concurrently.
    mcp = MCPClient(http, f"{TOOLBOX_URL}/mcp")

    async def load_tools() -> list[dict]:
        await mcp.initialize()
        return await mcp.list_tools()

    try:
        prompt, history, facts, mcp_tools = await asyncio.gather(
            get_json(
                f"{PROMPT_REGISTRY_URL}/prompts/{req.prompt_name}", label=req.prompt_label
            ),
            get_json(f"{MEMORY_URL}/sessions/{req.session_id}/messages", limit=HISTORY_LIMIT),
            get_json(f"{MEMORY_URL}/users/{req.user_id}/memories/search", q=req.message, limit=5),
            load_tools(),
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            502, f"{e.request.method} {e.request.url} -> {e.response.status_code}: {e.response.text[:300]}"
        )
    except (httpx.HTTPError, MCPError) as e:
        raise HTTPException(502, f"dependency error: {e!r}")

    memories = [m["memory"] for m in facts.get("results", [])]
    system = render(
        prompt["template"],
        user_id=req.user_id,
        date=datetime.now(UTC).date().isoformat(),
        memories="\n".join(f"- {m}" for m in memories) or "(none yet)",
    )
    span.set_attributes(
        {"agent.prompt.name": prompt["name"], "agent.prompt.version": prompt["version"]}
    )

    user_msg = {"role": "user", "content": req.message}
    messages = [{"role": "system", "content": system}, *history["messages"], user_msg]
    tools = [mcp_to_openai_tool(t) for t in mcp_tools]

    # 4: LLM <-> tool loop. The final round is forced to answer without tools.
    tool_calls_out: list[ToolCallOut] = []
    rounds = 0
    answer = ""
    while True:
        rounds += 1
        allow_tools = rounds <= MAX_TOOL_ROUNDS
        with tracer.start_as_current_span(f"agent.llm_round {rounds}"):
            reply = await chat(messages, tools if allow_tools else [])
        calls = reply.get("tool_calls") or []
        if not calls or not allow_tools:
            answer = reply.get("content") or ""
            break
        messages.append(
            {"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls}
        )
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            with tracer.start_as_current_span(f"agent.tool {name}") as tool_span:
                tool_span.set_attribute("agent.tool.arguments", json.dumps(args))
                try:
                    text, is_error = await mcp.call_tool(name, args)
                except (httpx.HTTPError, MCPError) as e:
                    text, is_error = f"tool call failed: {e}", True
                tool_span.set_attribute("agent.tool.is_error", is_error)
            tool_calls_out.append(ToolCallOut(name=name, arguments=args, is_error=is_error))
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id", name), "content": compact_tool_result(text)}
            )

    # 5: persist. Session history is cheap and synchronous; fact extraction runs an
    # LLM several times, so by default it happens after the response is sent.
    # Only the user's own words go to long-term memory: feeding the assistant's
    # answers in makes mem0 store them as "facts", which are then recalled as context
    # on later turns and reinforce any earlier mistake.
    exchange = [user_msg, {"role": "assistant", "content": answer}]
    resp = await http.post(
        f"{MEMORY_URL}/sessions/{req.session_id}/messages", json={"messages": exchange}
    )
    resp.raise_for_status()
    if req.wait_for_memory:
        await store_long_term(req.user_id, [user_msg])
    else:
        background.add_task(store_long_term, req.user_id, [user_msg])

    return RunOut(
        answer=answer,
        trace_id=trace_id,
        prompt={"name": prompt["name"], "version": prompt["version"], "label": req.prompt_label},
        memories_used=memories,
        tool_calls=tool_calls_out,
        llm_rounds=rounds,
    )
