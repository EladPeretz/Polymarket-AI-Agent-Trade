"""Transport between the agent's tool calls and the engine (ADR-0011).

The engine serves the bridge on a local Unix socket; the agent calls tools with
``perps-agent agent <tool> '<json>'``, a thin client that sends one request
and prints the answer. One JSON line each way:

* request: ``{"tool": "<name>", "args": {...}}``
* answer: ``{"ok": true, "result": {...}}`` or ``{"ok": false, "error": "..."}``

The socket file is created owner-only (0600) and nothing listens on the
network. Only the bridge's tools are reachable; there is no generic call.
"""

import asyncio
import json
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from perps_agent.agent.bridge import AgentBridge, ToolError

log = logging.getLogger(__name__)

# Requests are small JSON objects; this bounds a malformed or hostile one.
MAX_REQUEST_BYTES = 256 * 1024
# Answers can carry several symbols' snapshots.
_STREAM_LIMIT = 16 * 1024 * 1024


class TransportError(Exception):
    """The engine could not be reached, or its answer was not understood."""


class BridgeServer:
    def __init__(self, bridge: AgentBridge, path: Path) -> None:
        self._bridge = bridge
        self.path = path
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            self.path.unlink()  # a socket left by a previous run
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.path, limit=MAX_REQUEST_BYTES
            )
        finally:
            os.umask(old)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with suppress(FileNotFoundError):
            self.path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                answer: dict[str, Any] = {"ok": False, "error": "request too large"}
            else:
                answer = await self.answer(line)
            writer.write(json.dumps(answer, default=str).encode() + b"\n")
            await writer.drain()
        except ConnectionError:
            pass  # the client gave up (e.g. its own timeout)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def answer(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except ValueError:
            return {"ok": False, "error": "request is not valid JSON"}
        if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
            return {"ok": False, "error": 'request must be {"tool": "<name>", "args": {...}}'}
        args = request.get("args") or {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be a JSON object"}
        tool = request["tool"]
        if tool == "tools":
            return {"ok": True, "result": {"tools": self._bridge.describe_tools()}}
        try:
            result = await self._bridge.call(tool, args)
        except ToolError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:  # the agent gets an answer; the engine keeps running
            log.exception("tool failed", extra={"tool": tool})
            return {"ok": False, "error": f"internal error in {tool}: {error}"}
        return {"ok": True, "result": result}


async def request(path: Path, tool: str, args: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """Send one tool call to the engine and return its answer."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=_STREAM_LIMIT), timeout=5
        )
    except (OSError, TimeoutError) as error:
        raise TransportError(f"engine not reachable at {path}: {error}") from error
    try:
        writer.write(json.dumps({"tool": tool, "args": args}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except TimeoutError as error:
        raise TransportError(f"no answer from the engine within {timeout:.0f}s") from error
    except (OSError, ValueError) as error:
        raise TransportError(f"engine connection failed: {error}") from error
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
    try:
        answer = json.loads(line)
    except ValueError as error:
        raise TransportError("the engine's answer is not valid JSON") from error
    if not isinstance(answer, dict):
        raise TransportError("the engine's answer is not a JSON object")
    return answer
