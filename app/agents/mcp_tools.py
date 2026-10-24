"""Which MCP tools each agent may use, and how ADK / CrewAI connect to the MCP servers.

    sem7-risk (ML)               score_change_risk, risk_model_card
    sem7-ci   (code/tests/DB)    read_source, run_tests_with_patch, past_failures

Servers run as stdio subprocesses scoped to one repair (SEM7_* env vars), so tools only ever see the
sandbox being repaired. Each tool call is recorded (agent, tool, time, outcome) for monitoring and is
posted to the blackboard.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager

from app.agents.models import AgentRecord
from app.config import PROJECT_ROOT
from app.mcp_servers.tools import RepairScope

SERVERS = {"sem7-risk": "app.mcp_servers.risk_server", "sem7-ci": "app.mcp_servers.ci_server"}

AGENT_TOOLS: dict[str, dict[str, list[str]]] = {
    "investigator": {"sem7-risk": ["score_change_risk"], "sem7-ci": ["past_failures"]},
    "root_cause_analyst": {"sem7-ci": ["read_source"]},
    "Repair Planner": {"sem7-ci": ["read_source", "run_tests_with_patch"]},
    "Code Reviewer": {"sem7-ci": ["run_tests_with_patch"]},
}

TOOL_HINTS = {
    "investigator": "\n\nTOOLS: call `score_change_risk` once to get the ML risk of the commit, and `past_failures` "
                    "with the changed function's name to find similar earlier failures. Report what they return as "
                    "FACT lines (e.g. \"FACT (risk model): HIGH, score 0.58\"). Do not invent tool results.",
    "root_cause_analyst": "\n\nTOOLS: use `read_source` (version=baseline and version=current, with the function "
                          "name) to compare the suspect function before and after the commit.",
    "Repair Planner": "\n\nTOOLS: you MUST call `run_tests_with_patch` (file, function, complete new source) at least "
                      "once before your final answer, to dry-run your candidate against the real tests. Only propose "
                      "a fix whose dry run returns status PASS; if you cannot reach PASS, propose your best fix and "
                      "say so in `risks`.",
    "Code Reviewer": "\n\nTOOLS: verify each edit of the draft with `run_tests_with_patch` before approving. Reject a "
                     "draft whose dry run does not PASS.",
}


def server_env(scope: RepairScope) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DATABASE_URL", "WORKSPACE_DIR",
                                                         "TEST_TIMEOUT_SECONDS", "SYSTEMROOT", "TMPDIR")}
    return {**env, **scope.env(), "PYTHONPATH": str(PROJECT_ROOT)}


def hint(agent: str, scope: RepairScope | None) -> str:
    return TOOL_HINTS.get(agent, "") if scope is not None else ""


INTERNAL_TOOLS = {"set_model_response"}  # ADK's own structured-output step, not an MCP tool


def _unwrap(value):
    """MCP results arrive as {"content": [{"type": "text", "text": "<json>"}]}; return the payload."""
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        texts = [c.get("text", "") for c in value["content"] if isinstance(c, dict)]
        value = "\n".join(texts)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _summarise(value, limit: int = 160) -> str:
    value = _unwrap(value)
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _is_error(value) -> bool:
    value = _unwrap(value)
    return (isinstance(value, dict) and ("error" in value or value.get("isError") is True)) or \
        (isinstance(value, str) and value.lower().startswith(("error", "tool execution failed")))


# --------------------------------------------------------------------------- Google ADK


def adk_toolsets(agent: str, scope: RepairScope | None) -> list:
    if scope is None:
        return []
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
    from mcp import StdioServerParameters

    return [McpToolset(connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(command=sys.executable, args=["-m", SERVERS[server]],
                                                    env=server_env(scope), cwd=str(PROJECT_ROOT)),
                timeout=120),
            tool_filter=names)
            for server, names in AGENT_TOOLS.get(agent, {}).items()]


def adk_tool_callbacks(agent: str, records: list[AgentRecord]):
    """before/after tool callbacks that time each MCP call and record it."""
    started: dict[str, float] = {}

    def before(tool, args, tool_context):
        started[tool.name] = time.monotonic()
        return None

    def after(tool, args, tool_context, tool_response):
        if tool.name in INTERNAL_TOOLS:
            return None
        records.append(AgentRecord(
            agent=f"{agent} → {tool.name}", framework="mcp", status="error" if _is_error(tool_response) else "ok",
            seconds=round(time.monotonic() - started.pop(tool.name, time.monotonic()), 2),
            message=f"args={_summarise(args, 120)} result={_summarise(tool_response)}"))
        return None

    return before, after


# --------------------------------------------------------------------------- CrewAI


def crew_mcps(agent: str, scope: RepairScope | None) -> list:
    if scope is None:
        return []
    from crewai.mcp import MCPServerStdio, create_static_tool_filter

    return [MCPServerStdio(command=sys.executable, args=["-m", SERVERS[server]], env=server_env(scope),
                           tool_filter=create_static_tool_filter(allowed_tool_names=names))
            for server, names in AGENT_TOOLS.get(agent, {}).items()]


@contextmanager
def crew_tool_events(records: list[AgentRecord], role: str = "crew"):
    """Record every MCP tool call CrewAI agents make (from CrewAI's event bus) while the block runs."""
    from crewai.events.event_bus import crewai_event_bus
    from crewai.events.types.mcp_events import MCPToolExecutionCompletedEvent, MCPToolExecutionFailedEvent

    def done(source, event):
        failed = isinstance(event, MCPToolExecutionFailedEvent)
        result = event.error if failed else event.result
        records.append(AgentRecord(
            agent=f"{event.agent_role or getattr(event.from_agent, 'role', None) or role} → {event.tool_name}",
            framework="mcp",
            status="error" if failed or _is_error(result) else "ok",
            seconds=round((getattr(event, "execution_duration_ms", None) or 0) / 1000, 2),
            message=f"args={_summarise(event.tool_args or {}, 120)} result={_summarise(result)}"))

    handlers = [(MCPToolExecutionCompletedEvent, done), (MCPToolExecutionFailedEvent, done)]
    for event_type, handler in handlers:
        crewai_event_bus.register_handler(event_type, handler)
    try:
        yield
    finally:
        crewai_event_bus.flush(timeout=10)
        for event_type, handler in handlers:
            crewai_event_bus.off(event_type, handler)
