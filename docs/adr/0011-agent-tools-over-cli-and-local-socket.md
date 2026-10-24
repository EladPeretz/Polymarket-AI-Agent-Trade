# ADR-0011: Agent tools as a CLI over a local socket, not an MCP server

Status: Proposed · Date: 2026-10-21

## Context

The agent is Claude in a Claude Code session; the engine is a separate,
long-running process (overview §3.5). Overview §3.5 left open whether the
agent's tools are exposed as an MCP server or as a thin CLI, to be settled by a
Phase 2 spike (WP10). The constraints:

* The daily paper session is a **scheduled cloud session** with nobody at the
  keyboard. Project MCP servers declared in `.mcp.json` need the session to
  trust and start them; a server that fails to start leaves the agent without
  tools, and that failure is hard to see from inside the session.
* Bash is always available. A single foreground command is capped at 10
  minutes, so `next_event` waits at most 9 (540 s); the 15-minute check-in is
  an engine-side timer, not one long wait.
* The engine must keep running between tool calls (feeds, simulator, exit
  plans, triggers), so whatever serves the tools has to reach the engine's
  in-memory state; a per-call process cannot hold it.
* The same mechanism should run headless on the VPS in live mode.

## Decision

* The engine (`perps-agent run`) serves the bridge on a **Unix socket**
  (`agent.socket_path`, default `data/agent.sock`), created owner-only (0600).
  Nothing listens on the network.
* The agent calls tools with **`perps-agent agent <tool> '<json>'`**, a thin
  client that sends one JSON line (`{"tool", "args"}`) and prints the JSON
  answer (`{"ok": true, "result"}` or `{"ok": false, "error"}`).
  `perps-agent agent tools` lists the tools with their JSON schemas.
* Arguments are validated by pydantic schemas in `perps_agent.agent.schemas`
  before anything runs; errors come back as messages for the agent.
* The bridge (`AgentBridge`) is transport-independent: an MCP adapter can be
  added later over the same `call(tool, args)` and `describe_tools()` without
  changing tools, journaling or risk.

## Consequences

* No MCP configuration, trust prompt or server lifecycle in the scheduled
  session: if the engine is up, the tools work, and if it is not, the agent
  gets a clear "engine not reachable" answer.
* Tool calls appear as Bash commands in the session transcript, which is
  easy to audit.
* Each call starts a Python process (a fraction of a second). Acceptable for an
  agent that acts a few times a minute at most.
* No typed tool schemas in the model's tool list; the system prompt documents
  the tools and `tools` returns their schemas. Revisit with an MCP adapter if
  malformed calls turn out to be common in paper trading.
