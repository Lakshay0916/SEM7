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
from app.observability import tracing
from app.mcp_servers.tools import RepairScope

SERVERS = {"sem7-risk": "app.mcp_servers.risk_server", "sem7-ci": "app.mcp_servers.ci_server"}

AGENT_TOOLS: dict[str, dict[str, list[str]]] = {
    "investigator": {"sem7-risk": ["score_change_risk"], "sem7-ci": ["past_failures"]},
    # parallel pattern: each specialist gets the tools for its own angle
    "test_analyst": {"sem7-ci": ["read_source"]},
    "change_analyst": {"sem7-ci": ["read_source"]},
    "history_analyst": {"sem7-risk": ["score_change_risk"], "sem7-ci": ["past_failures"]},
    "root_cause_analyst": {"sem7-ci": ["read_source"]},
    "Repair Planner": {"sem7-ci": ["read_source", "run_tests_with_patch"]},
    "Code Reviewer": {"sem7-ci": ["run_tests_with_patch"]},
}

TOOL_HINTS = {
    "investigator": "\n\nTOOLS: call `score_change_risk` once to get the ML risk of the commit, and `past_failures` "
                    "with the changed function's name to find similar earlier failures. Report what they return as "
                    "FACT lines (e.g. \"FACT (risk model): HIGH, score 0.58\"). Do not invent tool results.",
    "test_analyst": "\n\nTOOLS: use `read_source` to read a failing test, or the function it calls, when the evidence "
                    "does not show enough of it.",
    "change_analyst": "\n\nTOOLS: use `read_source` (version=baseline and version=current, with the function name) "
                      "to compare each changed function before and after the commit.",
    "history_analyst": "\n\nTOOLS: call `score_change_risk` once for the ML risk of the commit, and `past_failures` "
                       "with the changed function's name. Report what they return as FACT lines; do not invent "
                       "tool results.",
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

    def span_key(tool, tool_context):
        return f"tool:{getattr(tool_context, 'function_call_id', '')}:{agent}:{tool.name}"

    def before(tool, args, tool_context):
        started[tool.name] = time.monotonic()
        if tool.name not in INTERNAL_TOOLS:
            tracing.begin(span_key(tool, tool_context), f"tool · {tool.name}", "tool", "mcp", agent=agent,
                          args=_summarise(args, 120))
        return None

    def after(tool, args, tool_context, tool_response):
        if tool.name in INTERNAL_TOOLS:
            return None
        tracing.finish(span_key(tool, tool_context), status="error" if _is_error(tool_response) else "ok",
                       result=_summarise(tool_response, 160))
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
    """CrewAI's event bus while the block runs: record every MCP tool call and every delegation a manager
    makes (monitoring), and - when a trace is active - turn agent runs, LLM calls, delegations and tool
    calls into spans.

    CrewAI runs handlers on a thread pool, so events can be handled out of order; the raw events are kept
    and the span tree is built from their timestamps once the crew is done."""
    from crewai.events.event_bus import crewai_event_bus
    from crewai.events.types.agent_events import (AgentExecutionCompletedEvent, AgentExecutionErrorEvent,
                                                  AgentExecutionStartedEvent)
    from crewai.events.types.llm_events import LLMCallCompletedEvent, LLMCallFailedEvent, LLMCallStartedEvent
    from crewai.events.types.mcp_events import MCPToolExecutionCompletedEvent, MCPToolExecutionFailedEvent
    from crewai.events.types.tool_usage_events import ToolUsageFinishedEvent, ToolUsageStartedEvent

    from app.agents.crew_planner import DELEGATION_TOOLS

    trace, parent = tracing.current()
    seen: list = []  # raw events, for the trace
    working: list[str] = []  # coworkers doing delegated work right now (CrewAI reports the manager as the caller)

    def is_delegation(event) -> bool:
        return (getattr(event, "tool_name", "") or "").lower().replace(" ", "_") in DELEGATION_TOOLS

    def keep(source, event):
        seen.append(event)

    def done(source, event):
        seen.append(event)
        failed = isinstance(event, MCPToolExecutionFailedEvent)
        result = event.error if failed else event.result
        caller = working[-1] if working else event.agent_role or getattr(event.from_agent, "role", None) or role
        records.append(AgentRecord(
            agent=f"{caller} → {event.tool_name}", framework="mcp",
            status="error" if failed or _is_error(result) else "ok",
            seconds=round((getattr(event, "execution_duration_ms", None) or 0) / 1000, 2),
            message=f"args={_summarise(event.tool_args or {}, 120)} result={_summarise(result)}"))

    def delegation_started(source, event):
        if is_delegation(event):
            seen.append(event)
            working.append(_args(event.tool_args).get("coworker", "coworker"))

    def delegated(source, event):
        if not is_delegation(event):
            return
        seen.append(event)
        if working:
            working.pop()
        args = event.tool_args if isinstance(event.tool_args, dict) else {}
        took = (event.finished_at - event.started_at).total_seconds() if event.finished_at and event.started_at else 0
        verb = "delegated" if event.tool_name.lower().startswith("delegate") else "asked"
        records.append(AgentRecord(
            agent=f"{event.agent_role or role} → {args.get('coworker', 'coworker')}", framework="crewai",
            status="ok", seconds=round(took, 2),
            message=f"{verb}: {_summarise(args.get('task') or args.get('question') or '', 110)} "
                    f"→ got: {_summarise(getattr(event, 'output', '') or '', 110)}"))

    handlers = [(MCPToolExecutionCompletedEvent, done), (MCPToolExecutionFailedEvent, done),
                (ToolUsageStartedEvent, delegation_started), (ToolUsageFinishedEvent, delegated)]
    if trace is not None:
        handlers += [(t, keep) for t in (AgentExecutionStartedEvent, AgentExecutionCompletedEvent,
                                         AgentExecutionErrorEvent, LLMCallStartedEvent, LLMCallCompletedEvent,
                                         LLMCallFailedEvent)]
    for event_type, handler in handlers:
        crewai_event_bus.register_handler(event_type, handler)
    try:
        yield
    finally:
        crewai_event_bus.flush(timeout=10)
        for event_type, handler in handlers:
            crewai_event_bus.off(event_type, handler)
        if trace is not None:
            crew_spans(trace, parent, list(seen), is_delegation)


def _args(raw) -> dict:
    """Tool arguments as a dict (CrewAI reports them as a JSON string when a tool call starts)."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ts(event) -> float:
    t = getattr(event, "timestamp", None)
    return t.timestamp() if t is not None else time.time()


def _role(event) -> str:
    return (getattr(event, "agent_role", None) or getattr(getattr(event, "agent", None), "role", None)
            or getattr(getattr(event, "from_agent", None), "role", None) or "agent")


def crew_spans(trace, parent: str | None, events: list, is_delegation) -> None:
    """Rebuild a crew run as spans: agent runs, delegations (with the coworker's run inside), LLM calls
    and MCP tool calls, each placed under the innermost agent or delegation open at that moment."""
    stack: list = []   # open agent / delegation spans
    calls: dict = {}   # LLM call_id -> span

    def under(role_name: str | None = None) -> str | None:
        for s in reversed(stack):
            if role_name is None or s.attributes.get("role") == role_name:
                return s.span_id
        return stack[-1].span_id if stack else parent

    def close(match, ts, status="ok"):
        for i in range(len(stack) - 1, -1, -1):
            if match(stack[i]):
                for s in stack[i:]:
                    if s.end is None:
                        s.end = ts
                stack[i].status = status
                del stack[i:]
                return

    for ev in sorted(events, key=lambda e: (_ts(e), getattr(e, "emission_sequence", 0) or 0)):
        name, ts = type(ev).__name__, _ts(ev)
        if name == "AgentExecutionStartedEvent":
            stack.append(trace.open(_role(ev), "agent", under(), "crewai", start=ts, role=_role(ev)))
        elif name in ("AgentExecutionCompletedEvent", "AgentExecutionErrorEvent"):
            role_name = _role(ev)
            close(lambda s: s.kind == "agent" and s.attributes.get("role") == role_name, ts,
                  "error" if name.endswith("ErrorEvent") else "ok")
        elif name == "ToolUsageStartedEvent" and is_delegation(ev):
            args = _args(ev.tool_args)
            stack.append(trace.open(f"delegate → {args.get('coworker', 'coworker')}", "delegation", under(),
                                    "crewai", start=ts, task=_summarise(args.get("task") or "", 140)))
        elif name == "ToolUsageFinishedEvent" and is_delegation(ev):
            coworker = _args(ev.tool_args).get("coworker")
            open_delegation = next((s for s in reversed(stack) if s.kind == "delegation"), None)
            if open_delegation is not None and coworker:  # the start event may not have had the parsed args yet
                open_delegation.name = f"delegate → {coworker}"
            close(lambda s: s.kind == "delegation", ts)
        elif name == "LLMCallStartedEvent":
            calls[ev.call_id] = trace.open(f"LLM call · {_role(ev)}", "llm", under(_role(ev)), "crewai",
                                           start=ts, model=getattr(ev, "model", None), agent=_role(ev))
        elif name in ("LLMCallCompletedEvent", "LLMCallFailedEvent"):
            s = calls.pop(getattr(ev, "call_id", None), None)
            if s is not None:
                usage = getattr(ev, "usage", None) or {}
                get = usage.get if isinstance(usage, dict) else (lambda k, d=None: getattr(usage, k, d))
                s.end, s.status = ts, "error" if name == "LLMCallFailedEvent" else "ok"
                if name == "LLMCallFailedEvent":
                    s.attributes["error"] = str(getattr(ev, "error", "") or "")[:200]
                s.attributes.update({k: v for k, v in {"prompt_tokens": get("prompt_tokens"),
                                                       "output_tokens": get("completion_tokens")}.items() if v})
        elif name in ("MCPToolExecutionCompletedEvent", "MCPToolExecutionFailedEvent"):
            took = (getattr(ev, "execution_duration_ms", None) or 0) / 1000
            failed = name.endswith("FailedEvent")
            trace.add(f"tool · {ev.tool_name}", "tool", ts - took, ts, under(), "mcp",
                      "error" if failed or _is_error(getattr(ev, "result", None)) else "ok",
                      args=_summarise(ev.tool_args or {}, 120))
    last = max((_ts(e) for e in events), default=time.time())
    for s in stack + list(calls.values()):
        if s.end is None:
            s.end, s.status = last, "open"
