"""Agent-to-Agent (A2A) plumbing shared by the agent services and their callers (official a2a-sdk).

Wire format, JSON-RPC over HTTP (A2A protocol 1.0):
    request : Message(role=user, parts=[DataPart {"skill": "<skill id>", "payload": {...}}])
    response: Task(state=COMPLETED, artifacts=[DataPart {"result": ..., "records": [...]}])
              or Task(state=FAILED, status message = error text)

Each service publishes an Agent Card at /.well-known/agent-card.json listing its skills, so a caller
(the orchestrator, or another agent) can discover what it offers before sending work.

Tracing crosses the boundary too: a service traces its skill run and returns the spans with the result
("spans"), and `call_skill_traced` attaches them under the caller's A2A span, so one trace covers the
orchestrator and both agent containers.
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable

import httpx
from a2a.helpers.proto_helpers import (
    get_data_parts,
    get_text_parts,
    new_data_message,
    new_data_part,
    new_task,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, SendMessageRequest, TaskState

from app.observability import otel, tracing

Skill = Callable[[dict], dict]  # payload -> {"result": ..., "records": [...]}


class A2ACallError(RuntimeError):
    pass


# --------------------------------------------------------------------------- server side


class SkillExecutor(AgentExecutor):
    """Runs the requested skill (a blocking Python function) in a worker thread and returns its data."""

    def __init__(self, skills: dict[str, Skill], service: str = "agent") -> None:
        self.skills, self.service = skills, service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:  # A2A 1.x: the Task must exist before any status update
            await event_queue.enqueue_event(new_task(context.task_id, context.context_id,
                                                     TaskState.TASK_STATE_SUBMITTED, history=[context.message]))
        await updater.start_work()
        try:
            request = next(d for d in get_data_parts(context.message.parts) if isinstance(d, dict) and "skill" in d)
            skill = self.skills.get(request["skill"])
            if skill is None:
                raise A2ACallError(f"unknown skill {request['skill']!r}; offered: {', '.join(self.skills)}")
            output = await asyncio.to_thread(run_traced, skill, dict(request.get("payload") or {}),
                                             f"{self.service}/{request['skill']}")
            await updater.add_artifact([new_data_part(output)], name=f"{request['skill']}-result")
            await updater.complete()
        except Exception as exc:  # reported to the caller as a FAILED task, never swallowed
            await updater.failed(updater.new_agent_message([new_text_part(f"{type(exc).__name__}: {exc}"[:2000])]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def run_traced(skill: Skill, payload: dict, label: str) -> dict:
    """Run a skill under its own trace and return its spans with the output (merged by the caller)."""
    trace = tracing.Trace()
    with tracing.activate(trace), tracing.span(f"{label} (served)", "a2a", "a2a", side="server"):
        output = skill(payload)
    return {**output, "spans": trace.drain()}


def agent_card(name: str, description: str, skills: list[tuple[str, str, str]], url: str) -> AgentCard:
    return AgentCard(
        name=name, description=description, version="1.0.0",
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        skills=[AgentSkill(id=sid, name=sname, description=sdesc, tags=["sem7", "self-healing"])
                for sid, sname, sdesc in skills],
    )


def build_app(card: AgentCard, skills: dict[str, Skill]):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    handler = DefaultRequestHandler(agent_executor=SkillExecutor(skills, card.name), task_store=InMemoryTaskStore(),
                                    agent_card=card)

    async def health(_):
        return JSONResponse({"status": "ok", "agent": card.name, "skills": list(skills)})

    app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, rpc_url="/"),
                            Route("/health", health)])
    if otel.setup(card.name):  # server spans joined to the caller's trace (W3C trace context from httpx)
        otel.instrument_app(app)
    return app


def public_url(default_port: int) -> str:
    return os.getenv("A2A_PUBLIC_URL") or f"http://localhost:{default_port}/"


# --------------------------------------------------------------------------- client side


async def call_skill_async(url: str, skill: str, payload: dict, timeout: float = 600.0) -> dict:
    from a2a.client import ClientConfig, create_client

    async with httpx.AsyncClient(timeout=timeout) as http:
        client = await create_client(url, client_config=ClientConfig(streaming=False, httpx_client=http))
        try:
            request = SendMessageRequest(message=new_data_message({"skill": skill, "payload": payload}))
            task = None
            async for response in client.send_message(request):
                if response.HasField("task"):
                    task = response.task
        finally:
            await client.close()
    if task is None:
        raise A2ACallError(f"{url} skill {skill}: no task returned")
    if task.status.state != TaskState.TASK_STATE_COMPLETED:
        detail = " ".join(get_text_parts(task.status.message.parts)) if task.status.HasField("message") else ""
        raise A2ACallError(f"{url} skill {skill}: {TaskState.Name(task.status.state)} {detail}".strip())
    data = [d for a in task.artifacts for d in get_data_parts(a.parts)]
    if not data:
        raise A2ACallError(f"{url} skill {skill}: completed without a result")
    return data[0]


def call_skill(url: str, skill: str, payload: dict, timeout: float = 600.0) -> dict:
    """Blocking A2A call (the strategy and the skills themselves are synchronous code)."""
    return asyncio.run(call_skill_async(url, skill, payload, timeout))


def call_skill_traced(url: str, skill: str, payload: dict, label: str, timeout: float = 600.0) -> dict:
    """`call_skill` inside an A2A span; the remote service's own spans are attached under it."""
    with tracing.span(f"A2A → {label}", "a2a", "a2a", url=url, side="client") as s:
        out = call_skill(url, skill, payload, timeout)
        trace, _ = tracing.current()
        if s is not None and trace is not None and out.get("spans"):
            trace.adopt(out["spans"], s.span_id)
    return out


def fetch_card(url: str, timeout: float = 3.0) -> dict | None:
    """The agent card as JSON, or None if the service is not reachable."""
    try:
        r = httpx.get(url.rstrip("/") + "/.well-known/agent-card.json", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None
