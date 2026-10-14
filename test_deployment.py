#!/usr/bin/env python3
"""Smoke test for the Oakestra agentstack deployment (agent -> LiteLLM proxy -> Ollama).

Usage:
  python3 test_deployment.py [agent_url]          test through the agent (default)
  python3 test_deployment.py --proxy <proxy_url>  test the LiteLLM proxy directly

Default agent_url is $AGENT_URL or http://192.168.0.108:8000. Only the agent publishes a
port: services with a published `port` can't be reached over the Oakestra overlay, so the
proxy is internal-only (10.30.10.2:4000). The port is published on the worker's IP, not
on localhost.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

EMBED_DIMS = 768
EXPECTED_MODELS = {"agent-model", "embed-model"}


def call(base, path, body=None, timeout=180):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# --- through the agent ---------------------------------------------------------------

def agent_health(base):
    return f"healthz: {call(base, '/healthz', timeout=10)['status']}"


def agent_proxy_reachable(base):
    try:
        resp = call(base, "/readyz", timeout=15)
    except urllib.error.HTTPError as e:  # 503: agent is up but can't reach the proxy
        raise AssertionError(f"agent can't reach proxy: {e.read().decode()[:200]}") from e
    missing = EXPECTED_MODELS - set(resp["models"])
    assert not missing, f"proxy is missing models: {missing}"
    return f"{resp['llm_proxy']} models: {sorted(resp['models'])}"


def agent_run(base):
    resp = call(base, "/run", {"message": "Reply with exactly one word: pong"})
    assert resp["answer"].strip(), "empty answer"
    return f"answer: {resp['answer'].strip()[:80]!r} ({resp['model']})"


# --- the proxy directly ---------------------------------------------------------------

def proxy_health(base):
    return f"liveliness: {call(base, '/health/liveliness', timeout=10)}"


def proxy_models(base):
    ids = {m["id"] for m in call(base, "/v1/models", timeout=10)["data"]}
    missing = EXPECTED_MODELS - ids
    assert not missing, f"missing models: {missing} (got {sorted(ids)})"
    return f"models: {sorted(ids)}"


def proxy_chat(base):
    resp = call(base, "/v1/chat/completions", {
        "model": "agent-model",
        "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
    })
    text = resp["choices"][0]["message"]["content"]
    assert text and text.strip(), "empty completion"
    return f"reply: {text.strip()[:80]!r}"


def proxy_embeddings(base):
    vec = call(base, "/v1/embeddings", {"model": "embed-model", "input": "hello"})["data"][0]["embedding"]
    assert len(vec) == EMBED_DIMS, f"expected {EMBED_DIMS} dims, got {len(vec)}"
    return f"dims: {len(vec)}"


def proxy_tool_call(base):
    resp = call(base, "/v1/chat/completions", {
        "model": "agent-model",
        "messages": [{"role": "user", "content": "What's the weather in Munich? Use the tool."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }],
    })
    calls = resp["choices"][0]["message"].get("tool_calls") or []
    assert calls, "model answered without calling the tool"
    fn = calls[0]["function"]
    return f"{fn['name']}({fn['arguments']})"


# (name, function, required). A failed optional check is reported as WARN and
# doesn't fail the run. The 0.5B model can be flaky at tool calling.
AGENT_CHECKS = [
    ("health", agent_health, True),
    ("proxy", agent_proxy_reachable, True),
    ("run", agent_run, True),
]
PROXY_CHECKS = [
    ("health", proxy_health, True),
    ("models", proxy_models, True),
    ("chat", proxy_chat, True),
    ("embeddings", proxy_embeddings, True),
    ("tool call", proxy_tool_call, False),
]


def main():
    args = sys.argv[1:]
    if args[:1] == ["--proxy"]:
        if len(args) < 2:
            sys.exit("usage: test_deployment.py --proxy <proxy_url>")
        base, checks, target = args[1], PROXY_CHECKS, "LiteLLM proxy"
    else:
        base = args[0] if args else os.getenv("AGENT_URL", "http://192.168.0.108:8000")
        checks, target = AGENT_CHECKS, "agent"
    base = base.rstrip("/")

    print(f"Testing {target} at {base}\n")
    failed = 0
    for name, fn, required in checks:
        start = time.monotonic()
        try:
            detail, status = fn(base), "PASS"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            detail, status = f"request failed: {e}", "FAIL" if required else "WARN"
        except (AssertionError, KeyError, IndexError, TypeError) as e:
            detail, status = str(e) or type(e).__name__, "FAIL" if required else "WARN"
        failed += status == "FAIL"
        print(f"[{status}] {name:<11} {time.monotonic() - start:5.1f}s  {detail}")
    print(f"\n{'All required checks passed.' if not failed else f'{failed} required check(s) failed.'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
