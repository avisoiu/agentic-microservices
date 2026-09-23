"""Memory service.

Short-term: per-session message history in Redis lists that expire after
SESSION_TTL_SECONDS of inactivity.
Long-term: per-user facts extracted and searched by mem0, stored in Qdrant. mem0's
LLM and embedding calls go through the LiteLLM proxy, so they show up in traces.
"""

import json
import os
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, HTTPException, Response
from mem0 import Memory
from pydantic import BaseModel

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
SESSION_MAX_MESSAGES = int(os.getenv("SESSION_MAX_MESSAGES", "50"))
LLM_PROXY_URL = os.getenv("LLM_PROXY_URL", "http://localhost:4000")

MEM0_CONFIG = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host": os.getenv("QDRANT_HOST", "localhost"),
            "port": int(os.getenv("QDRANT_PORT", "6333")),
            "collection_name": os.getenv("MEM0_COLLECTION", "memories"),
            "embedding_model_dims": int(os.getenv("EMBED_DIMS", "768")),
        },
    },
    "llm": {
        "provider": "openai",
        "config": {
            "model": os.getenv("MEM0_LLM_MODEL", "agent-model"),
            "openai_base_url": f"{LLM_PROXY_URL}/v1",
            "api_key": os.getenv("LLM_PROXY_KEY", "unused"),
            "temperature": 0,
        },
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": os.getenv("MEM0_EMBED_MODEL", "embed-model"),
            "openai_base_url": f"{LLM_PROXY_URL}/v1",
            "api_key": os.getenv("LLM_PROXY_KEY", "unused"),
            "embedding_dims": int(os.getenv("EMBED_DIMS", "768")),
        },
    },
    # mem0's change history is an audit log only; keep it off the image filesystem root.
    "history_db_path": os.getenv("MEM0_HISTORY_DB", "/tmp/mem0-history.db"),
}

rdb = redis.Redis.from_url(REDIS_URL, decode_responses=True)
state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["mem0"] = Memory.from_config(MEM0_CONFIG)
    yield


app = FastAPI(title="memory", lifespan=lifespan)


def session_key(session_id: str) -> str:
    return f"session:{session_id}:messages"


class Message(BaseModel):
    role: str
    content: str


class MessagesIn(BaseModel):
    messages: list[Message]


class FactsIn(BaseModel):
    messages: list[Message]
    # infer=True lets mem0 extract/merge facts with the LLM; False stores messages verbatim.
    infer: bool = True
    metadata: dict | None = None


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    try:
        rdb.ping()
    except redis.RedisError as e:
        raise HTTPException(503, f"redis: {e}")
    if "mem0" not in state:
        raise HTTPException(503, "mem0 not initialised")
    return {"status": "ready"}


# --- short-term memory -------------------------------------------------------


@app.post("/sessions/{session_id}/messages", status_code=201)
def append_messages(session_id: str, body: MessagesIn) -> dict:
    key = session_key(session_id)
    pipe = rdb.pipeline()
    pipe.rpush(key, *(m.model_dump_json() for m in body.messages))
    pipe.ltrim(key, -SESSION_MAX_MESSAGES, -1)
    pipe.expire(key, SESSION_TTL_SECONDS)
    pipe.llen(key)
    *_, length = pipe.execute()
    return {"session_id": session_id, "length": length, "ttl_seconds": SESSION_TTL_SECONDS}


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, limit: int = 20) -> dict:
    key = session_key(session_id)
    raw = rdb.lrange(key, -limit, -1) if limit > 0 else []
    return {
        "session_id": session_id,
        "messages": [json.loads(m) for m in raw],
        "ttl_seconds": rdb.ttl(key),
    }


@app.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    rdb.delete(session_key(session_id))
    return Response(status_code=204)


# --- long-term memory ---------------------------------------------------------


@app.post("/users/{user_id}/memories", status_code=201)
def add_memories(user_id: str, body: FactsIn) -> dict:
    result = state["mem0"].add(
        [m.model_dump() for m in body.messages],
        user_id=user_id,
        infer=body.infer,
        metadata=body.metadata,
    )
    return {"user_id": user_id, "results": result.get("results", [])}


@app.get("/users/{user_id}/memories")
def list_memories(user_id: str, limit: int = 50) -> dict:
    result = state["mem0"].get_all(filters={"user_id": user_id}, top_k=limit)
    return {"user_id": user_id, "results": result.get("results", [])}


@app.get("/users/{user_id}/memories/search")
def search_memories(user_id: str, q: str, limit: int = 5) -> dict:
    result = state["mem0"].search(q, filters={"user_id": user_id}, top_k=limit)
    return {"user_id": user_id, "query": q, "results": result.get("results", [])}


@app.delete("/users/{user_id}/memories", status_code=204)
def delete_memories(user_id: str) -> Response:
    state["mem0"].delete_all(user_id=user_id)
    return Response(status_code=204)
