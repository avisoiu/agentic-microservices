"""Minimal MCP client over streamable HTTP (JSON-RPC), just enough for tools.

The official SDK would work too, but mcp-toolbox only continues a trace when the
W3C trace context is sent in `params._meta.traceparent` (the MCP convention) rather
than the HTTP header, so we build the requests ourselves and inject it there.
"""

import itertools
import json

import httpx
from opentelemetry import propagate

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, http: httpx.AsyncClient, url: str):
        self.http = http
        self.url = url
        self.session_id: str | None = None
        self._ids = itertools.count(1)

    async def _post(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        params = dict(params or {})
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        if carrier:
            params["_meta"] = {**params.get("_meta", {}), **carrier}
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            body["id"] = next(self._ids)
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = await self.http.post(self.url, json=body, headers=headers)
        resp.raise_for_status()
        self.session_id = resp.headers.get("Mcp-Session-Id", self.session_id)
        if notify:
            return {}
        msg = self._parse(resp)
        if "error" in msg:
            raise MCPError(f"{method}: {msg['error']}")
        return msg["result"]

    @staticmethod
    def _parse(resp: httpx.Response) -> dict:
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            # Take the last JSON-RPC response carried in an SSE `data:` line.
            data = [
                line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")
            ]
            return json.loads(data[-1])
        return resp.json()

    async def initialize(self) -> dict:
        result = await self._post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "demo-agent", "version": "0.1.0"},
            },
        )
        await self._post("notifications/initialized", notify=True)
        return result

    async def list_tools(self) -> list[dict]:
        return (await self._post("tools/list"))["tools"]

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Returns (text content, is_error)."""
        result = await self._post("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return text, bool(result.get("isError"))
