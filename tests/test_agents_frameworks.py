"""Real ADK and CrewAI wiring, driven by fake LLMs (no network). Skipped without the agent packages."""

import json

import pytest

pytest.importorskip("google.adk")
pytest.importorskip("crewai")

from crewai.llms.base_llm import BaseLLM  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402

from app.agents.adk_analysts import run_analysts  # noqa: E402
from app.agents.crew_planner import run_planner  # noqa: E402
from app.agents.models import PlanDraft, PlanReview  # noqa: E402
from app.models.schemas import InvestigationReport, RootCauseReport  # noqa: E402

INVESTIGATION = {"failure_summary": "2 tests fail", "failed_tests": ["tests/test_calculator.py::test_add"],
                 "affected_files": ["src/calculator.py"], "evidence": ["FACT: test_add failed"],
                 "initial_hypotheses": ["HYPOTHESIS: add() changed"]}
ROOT_CAUSE = {"summary": "add subtracts", "root_cause": "add() uses - instead of +", "confidence": 0.9,
              "evidence": ["diff: return a - b"], "affected_files": ["src/calculator.py"]}
GOOD_ADD = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


class FakeGemini(BaseLlm):
    """Answers like Gemini would: the investigator's or the root-cause agent's JSON, with usage."""

    model: str = "fake-gemini"
    requests: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        instruction = str(llm_request.config.system_instruction)
        payload = ROOT_CAUSE if "Root Cause Agent" in instruction else INVESTIGATION
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]),
                          usage_metadata=types.GenerateContentResponseUsageMetadata(
                              prompt_token_count=100, candidates_token_count=20, total_token_count=120))


class FakeCrewLLM(BaseLLM):
    """Planner answers with a draft (first a bad one, to exercise the guardrail retry); reviewer approves."""

    calls: list = []

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
             from_agent=None, response_model=None):
        text = json.dumps(messages) if not isinstance(messages, str) else messages
        self.calls.append(text)
        draft = {"summary": "Restore addition", "root_cause": "add() uses -",
                 "edits": [{"file": "src/calculator.py", "function": "add", "new_source": GOOD_ADD,
                            "description": "use + again"}]}
        if "Review the repair planner" in text:
            return json.dumps({"approved": True, "issues": [], "final_plan": draft})
        planner_calls = [c for c in self.calls if "Review the repair planner" not in c]
        if len(planner_calls) == 1:  # first attempt edits a test - the guardrail must reject it
            return json.dumps({**draft, "edits": [{**draft["edits"][0], "file": "tests/test_calculator.py"}]})
        return json.dumps(draft)


def test_adk_analysts_hand_off_through_session_state():
    records = []
    model = FakeGemini()
    model.requests.clear()
    inv, rc = run_analysts("EVIDENCE: test_add fails, diff shows return a - b", model, records)
    assert isinstance(inv, InvestigationReport) and isinstance(rc, RootCauseReport)
    assert rc.root_cause == "add() uses - instead of +"
    assert [r.agent for r in records] == ["investigator", "root_cause_analyst"]
    assert records[0].prompt_tokens == 100 and records[1].output_tokens == 20
    # the evidence reached the investigator, and its report reached the root-cause agent via state
    instructions = [str(r.config.system_instruction) for r in model.requests]
    assert "test_add fails" in instructions[0]
    assert "2 tests fail" in instructions[1]


def test_crewai_planner_guardrail_retries_then_reviewer_approves(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calculator.py").write_text('def add(a, b):\n    """Return the sum of a and b."""\n'
                                                    "    return a - b\n")
    (tmp_path / "tests" / "test_calculator.py").write_text("def test_add():\n    assert True\n")
    llm = FakeCrewLLM(model="fake-crew")
    llm.calls.clear()
    records = []
    review = run_planner("EVIDENCE", InvestigationReport(**INVESTIGATION), RootCauseReport(**ROOT_CAUSE),
                         tmp_path, llm, records)
    assert isinstance(review, PlanReview) and review.approved
    assert isinstance(review.final_plan, PlanDraft) and review.final_plan.edits[0].file == "src/calculator.py"
    assert any("not tests" in c or "Fix these problems" in c for c in llm.calls)  # guardrail feedback was sent
    assert [r.agent for r in records if r.framework == "crewai"] == ["Repair Planner", "Code Reviewer"]


def test_blackboard_mode_runs_each_framework_agent_on_its_own(tmp_path, monkeypatch):
    """Full blackboard run with the real ADK + CrewAI runners: 4 separate agent calls, board in order."""
    import app.agents.strategy as strat
    from app.agents.blackboard import Blackboard, BlackboardController

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calculator.py").write_text('def add(a, b):\n    """Return the sum of a and b."""\n'
                                                    "    return a - b\n")
    (tmp_path / "tests" / "test_calculator.py").write_text("def test_add():\n    assert True\n")
    gemini, crew = FakeGemini(), FakeCrewLLM(model="fake-crew")
    gemini.requests.clear()
    crew.calls.clear()
    monkeypatch.setattr("app.agents.crew_planner._llm", lambda llm: (crew, "fake-crew"))
    sources = strat.default_sources(model=gemini)

    bb, records = Blackboard(), []
    bb.post("evidence", "EVIDENCE: test_add fails, diff shows return a - b", "orchestrator", "system")
    review = BlackboardController(sources, strat.plan_guards(tmp_path)).run_until(bb, "review", records)

    assert review["approved"] and review["final_plan"]["edits"][0]["file"] == "src/calculator.py"
    assert [(e.key, e.framework, e.retracted) for e in bb.entries] == [
        ("evidence", "system", False), ("investigation", "adk", False), ("root_cause", "adk", False),
        ("plan_draft", "crewai", True),            # fake planner edits a test first -> guard rejects
        ("feedback:plan_draft", "guardrail", False), ("plan_draft", "crewai", False), ("review", "crewai", False)]
    assert len(gemini.requests) == 2                 # one ADK call per analyst
    assert any("GUARDRAIL FEEDBACK" in c for c in crew.calls)        # planner's retry saw the board's feedback
    assert any("DRAFT TO REVIEW" in c for c in crew.calls)           # reviewer read the draft from the board
    assert {r.agent for r in records if r.status == "ok"} == {"investigator", "root_cause_analyst",
                                                              "Repair Planner", "Code Reviewer"}


# --------------------------------------------------------------------------- MCP (real stdio servers)


@pytest.fixture
def mcp_scope(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from dataclasses import replace

    import app.ci.simulator as sim_mod
    import app.orchestration.store as store_mod
    from app.ci.scenarios import get_scenario
    from app.ci.simulator import create_sandbox
    from app.git.repo import GitRepo
    from app.mcp_servers.tools import RepairScope

    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'm.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    monkeypatch.setenv("DATABASE_URL", patched.database_url)
    repo = create_sandbox(get_scenario("simple_bug"))
    g = GitRepo(repo)
    return RepairScope(repo, g._run("rev-parse", "main").strip(), g.head_sha())


def test_mcp_servers_speak_the_protocol(mcp_scope):
    import asyncio
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from app.agents.mcp_tools import SERVERS, server_env
    from app.config import PROJECT_ROOT

    async def call(server, tool, args):
        params = StdioServerParameters(command=sys.executable, args=["-m", SERVERS[server]],
                                       env=server_env(mcp_scope), cwd=str(PROJECT_ROOT))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            res = await s.call_tool(tool, args)
            return names, json.loads(res.content[0].text)

    names, risk = asyncio.run(call("sem7-risk", "score_change_risk", {}))
    assert names == ["score_change_risk", "risk_model_card"] and risk["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
    names, dry = asyncio.run(call("sem7-ci", "run_tests_with_patch",
                                  {"path": "src/calculator.py", "function": "add", "new_source": GOOD_ADD}))
    assert names == ["read_source", "run_tests_with_patch", "past_failures"] and dry["status"] == "PASS"


class ToolCallingGemini(FakeGemini):
    """First turn: call the MCP tool `score_change_risk`; after the tool answers: return the report."""

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        answered = any(p.function_response for c in llm_request.contents for p in (c.parts or []))
        if not answered:
            part = types.Part(function_call=types.FunctionCall(name="score_change_risk", args={}))
        else:
            part = types.Part(text=json.dumps(INVESTIGATION))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def test_adk_agent_calls_an_mcp_tool_and_the_call_is_recorded(mcp_scope):
    from app.agents.adk_analysts import run_agent

    model, records = ToolCallingGemini(), []
    model.requests.clear()
    inv = run_agent("investigator", {"evidence": "EVIDENCE"}, model, records, scope=mcp_scope)
    assert isinstance(inv, InvestigationReport)
    tool_calls = [r for r in records if r.framework == "mcp"]
    assert [r.agent for r in tool_calls] == ["investigator → score_change_risk"]
    assert tool_calls[0].status == "ok" and '"risk_level"' in tool_calls[0].message
    tools_offered = {d.name for t in model.requests[0].config.tools for d in (t.function_declarations or [])}
    assert {"score_change_risk", "past_failures"} <= tools_offered and "read_source" not in tools_offered
