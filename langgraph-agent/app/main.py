"""Minimal LangGraph agent: one LLM node that calls the LiteLLM proxy.

POST /run {"message": "..."} → {"answer": "...", "model": "..."}
GET /healthz → the agent process is up
GET /readyz  → the agent can reach the LiteLLM proxy (503 otherwise), with its model list
"""

import os

import httpx
from fastapi import FastAPI, Response
from langchain_openai import ChatOpenAI
from langgraph.graph import START, END, MessagesState, StateGraph
from pydantic import BaseModel

LLM_PROXY_URL = os.getenv("LLM_PROXY_URL", "http://llm-proxy:4000").rstrip("/")
MODEL = os.getenv("AGENT_MODEL", "agent-model")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a concise, helpful assistant.")

# LiteLLM speaks the OpenAI API. It has no master key configured, so any api_key works.
llm = ChatOpenAI(base_url=f"{LLM_PROXY_URL}/v1", api_key="unused", model=MODEL, timeout=180, max_retries=1)


def call_llm(state: MessagesState) -> dict:
    messages = [("system", SYSTEM_PROMPT), *state["messages"]]
    return {"messages": [llm.invoke(messages)]}


builder = StateGraph(MessagesState)
builder.add_node("llm", call_llm)
builder.add_edge(START, "llm")
builder.add_edge("llm", END)
graph = builder.compile()

app = FastAPI(title="langgraph-agent")


class RunRequest(BaseModel):
    message: str


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response) -> dict:
    try:
        resp = httpx.get(f"{LLM_PROXY_URL}/v1/models", timeout=5)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        response.status_code = 503
        return {"status": "proxy unreachable", "llm_proxy": LLM_PROXY_URL, "error": repr(e)}
    return {"status": "ok", "llm_proxy": LLM_PROXY_URL, "models": [m["id"] for m in resp.json()["data"]]}


@app.post("/run")
def run(req: RunRequest) -> dict:
    result = graph.invoke({"messages": [("user", req.message)]})
    reply = result["messages"][-1]
    return {"answer": reply.content, "model": reply.response_metadata.get("model_name", MODEL)}
