import asyncio
import json
import stat
from pathlib import Path

import pytest

from perps_agent.agent import BridgeServer, TransportError, request
from perps_agent.journal import TriggerKind
from tests.unit.agent_factories import SYMBOL, AgentDesk


def _with_server(desk: AgentDesk, path: Path, body):
    async def scenario():
        server = BridgeServer(desk.bridge, path)
        await server.start()
        try:
            return await body(server)
        finally:
            await server.close()

    return desk.run(scenario())


def test_round_trip_over_the_socket(tmp_path):
    desk = AgentDesk()
    desk.market(0)
    path = tmp_path / "agent.sock"

    async def body(server):
        mode = stat.S_IMODE(path.stat().st_mode)
        snapshot = await request(path, "get_snapshot", {"symbol": SYMBOL}, timeout=5)
        error = await request(path, "get_snapshot", {"symbol": "X"}, timeout=5)
        tools = await request(path, "tools", {}, timeout=5)
        return mode, snapshot, error, tools

    mode, snapshot, error, tools = _with_server(desk, path, body)
    assert mode & 0o077 == 0  # owner only
    assert snapshot["ok"] is True and snapshot["result"]["symbol"] == SYMBOL
    assert error == {"ok": False, "error": "'X' is not traded; symbols: NVDA-USD"}
    assert any(t["name"] == "propose_trade" for t in tools["result"]["tools"])
    assert not path.exists()  # removed on close


def test_next_event_waits_on_the_engine(tmp_path):
    desk = AgentDesk()
    path = tmp_path / "agent.sock"

    async def body(server):
        waiting = asyncio.create_task(
            request(path, "next_event", {"timeout_seconds": 10}, timeout=15)
        )
        await asyncio.sleep(0.05)
        desk.bridge.notify(TriggerKind.CHECK_IN, "check_in", "scheduled check-in")
        return await waiting

    answer = _with_server(desk, path, body)
    assert answer["result"]["events"][0]["key"] == "check_in"


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (b"not json\n", "not valid JSON"),
        (b"[1]\n", "request must be"),
        (b'{"tool": "get_portfolio", "args": [1]}\n', "args must be"),
    ],
)
def test_malformed_requests_get_an_error(tmp_path, line, error):
    desk = AgentDesk()
    server = BridgeServer(desk.bridge, tmp_path / "agent.sock")
    answer = desk.run(server.answer(line))
    assert answer["ok"] is False and error in answer["error"]


def test_internal_errors_do_not_escape(tmp_path):
    desk = AgentDesk()
    server = BridgeServer(desk.bridge, tmp_path / "agent.sock")

    async def broken(_):
        raise RuntimeError("boom")

    desk.bridge.tools["get_portfolio"] = desk.bridge.tools["get_portfolio"].__class__(
        "get_portfolio", "x", desk.bridge.tools["get_portfolio"].schema, broken
    )
    answer = desk.run(server.answer(json.dumps({"tool": "get_portfolio"}).encode()))
    assert answer == {"ok": False, "error": "internal error in get_portfolio: boom"}


def test_client_reports_an_engine_that_is_not_running(tmp_path):
    with pytest.raises(TransportError, match="not reachable"):
        asyncio.run(request(tmp_path / "missing.sock", "get_portfolio", {}, timeout=1))
