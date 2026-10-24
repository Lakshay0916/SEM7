"""A2A protocol: real HTTP agent services (fake skills, no LLM) called by the orchestrator and by each other."""

import socket
import threading
import time
from dataclasses import replace

import pytest

pytest.importorskip("a2a")

import uvicorn  # noqa: E402

import app.ci.simulator as sim_mod  # noqa: E402
import app.orchestration.store as store_mod  # noqa: E402
from app.a2a_agents import remote  # noqa: E402
from app.a2a_agents.protocol import A2ACallError, agent_card, build_app, call_skill, fetch_card  # noqa: E402
from app.agents.models import AgentRecord, FunctionEdit, PlanDraft, PlanReview  # noqa: E402
from app.agents.strategy import AgenticStrategy  # noqa: E402
from app.healing.loop import apply_and_verify, decide, propose_fix  # noqa: E402
from app.models.schemas import InvestigationReport, RootCauseReport  # noqa: E402
from app.models.schemas import WorkflowState as S  # noqa: E402
from app.orchestration.pipeline import run_ci_workflow  # noqa: E402
from app.orchestration.store import Store  # noqa: E402

GOOD_ADD = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
DRAFT = PlanDraft(summary="restore +", root_cause="add() uses -",
                  edits=[FunctionEdit(file="src/calculator.py", function="add", new_source=GOOD_ADD, description="d")])
INV = InvestigationReport(failure_summary="2 tests fail in add()", affected_files=["src/calculator.py"])
RC = RootCauseReport(summary="s", root_cause="add() uses - instead of +", confidence=0.9)


def _rec(agent, framework):
    return [AgentRecord(agent=agent, framework=framework, seconds=0.1, prompt_tokens=10, output_tokens=2).model_dump()]


def serve(name, skills):
    """Run an A2A service on a free port in a background thread; return its URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(build_app(agent_card(name, "test agent", [(k, k, k) for k in skills], url),
                                                     skills), port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if fetch_card(url, timeout=0.5):
            return url
        time.sleep(0.05)
    raise RuntimeError("test A2A server did not start")


@pytest.fixture(scope="module")
def services():
    seen = {"crew": [], "adk": []}

    def crew_plan(p):
        seen["crew"].append(("plan", p))
        bad = len([s for s in seen["crew"] if s[0] == "plan"]) == 1  # first draft edits a test -> guard rejects
        draft = DRAFT.model_copy(update={"edits": [FunctionEdit(file="tests/test_calculator.py", function="test_add",
                                                                new_source="def test_add():\n    pass\n",
                                                                description="cheat")]}) if bad else DRAFT
        return {"result": draft.model_dump(mode="json"), "records": _rec("Repair Planner", "crewai")}

    def crew_review(p):
        seen["crew"].append(("review", p))
        return {"result": PlanReview(approved=True, final_plan=PlanDraft.model_validate_json(p["draft"])).model_dump(
            mode="json"), "records": _rec("Code Reviewer", "crewai")}

    def crew_plan_and_review(p):
        seen["crew"].append(("plan_and_review", p))
        return {"result": PlanReview(approved=True, final_plan=DRAFT).model_dump(mode="json"),
                "records": _rec("Repair Planner", "crewai")}

    def boom(p):
        raise ValueError("planner exploded")

    crew_url = serve("fake-crew", {"plan": crew_plan, "review": crew_review, "plan_and_review": crew_plan_and_review,
                                   "boom": boom})

    def adk_investigator(p):
        seen["adk"].append(("investigator", p))
        return {"result": INV.model_dump(mode="json"), "records": _rec("investigator", "adk")}

    def adk_root_cause(p):
        seen["adk"].append(("root_cause_analyst", p))
        assert p["investigation"]["failure_summary"] == INV.failure_summary  # read from the board, sent over A2A
        return {"result": RC.model_dump(mode="json"), "records": _rec("root_cause_analyst", "adk")}

    def adk_analyse_and_plan(p):  # the real service hands off to CrewAI over A2A; so does this one
        seen["adk"].append(("analyse_and_plan", p))
        crew = call_skill(p["crew_url"], "plan_and_review", {"root_cause": RC.model_dump(mode="json")})
        return {"result": {"investigation": INV.model_dump(mode="json"), "root_cause": RC.model_dump(mode="json"),
                           "review": crew["result"]},
                "records": _rec("investigator", "adk") + crew["records"]}

    adk_url = serve("fake-adk", {"investigator": adk_investigator, "root_cause_analyst": adk_root_cause,
                                 "analyse_and_plan": adk_analyse_and_plan})
    return {"adk": adk_url, "crew": crew_url, "seen": seen}


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'a2a.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


# --------------------------------------------------------------------------- protocol


def test_agent_card_is_published_with_skills(services):
    card = fetch_card(services["crew"])
    assert card["name"] == "fake-crew" and {s["id"] for s in card["skills"]} >= {"plan", "review"}
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert fetch_card("http://127.0.0.1:9/") is None  # unreachable service -> None, not an exception


def test_skill_call_round_trip_and_errors(services):
    out = call_skill(services["crew"], "plan_and_review", {"x": 1})
    assert PlanReview.model_validate(out["result"]).approved and out["records"][0]["agent"] == "Repair Planner"
    with pytest.raises(A2ACallError, match="TASK_STATE_FAILED.*planner exploded"):
        call_skill(services["crew"], "boom", {})
    with pytest.raises(A2ACallError, match="unknown skill"):
        call_skill(services["crew"], "nope", {})


# --------------------------------------------------------------------------- strategy over A2A


def test_blackboard_over_a2a_heals_with_remote_agents(store, services, monkeypatch):
    monkeypatch.setattr(remote, "settings", replace(remote.settings, adk_agent_url=services["adk"],
                                                    crew_agent_url=services["crew"]))
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    strategy = AgenticStrategy(messaging="blackboard", transport="a2a", fallback=False)
    plan = propose_fix(store, wf, strategy)
    assert plan.strategy == "adk+crewai" and plan.changes[0].file == "src/calculator.py"
    decide(store, wf, True, "alice")
    assert apply_and_verify(store, wf, strategy=strategy) == S.PR_CREATED

    events = [e["agent"] for e in store.events(wf)]
    hops = [e for e in events if e.startswith("a2a:")]
    assert hops == ["a2a:blackboard → sem7-adk/investigator", "a2a:blackboard → sem7-adk/root_cause_analyst",
                    "a2a:blackboard → sem7-crewai/plan", "a2a:blackboard → sem7-crewai/plan",
                    "a2a:blackboard → sem7-crewai/review"]   # planner ran twice: guardrail rejected the first draft
    assert "adk:investigator" in events and "crewai:Code Reviewer" in events  # remote agents' records merged
    second_plan = [p for kind, p in services["seen"]["crew"] if kind == "plan"][-1]
    assert second_plan["feedback"] and "not tests" in second_plan["feedback"][0]  # board feedback travelled over A2A
    board = [a["payload"]["key"] for a in store.artifacts(wf, "blackboard")]
    assert board[:4] == ["evidence", "a2a:sem7-adk/investigator", "investigation", "a2a:sem7-adk/root_cause_analyst"]
    assert board.count("a2a:sem7-crewai/plan") == 2                     # every A2A hop is on the board
    assert board[-2:] == ["a2a:sem7-crewai/review", "review"]


def test_direct_over_a2a_adk_service_hands_off_to_crewai(store, services, monkeypatch):
    monkeypatch.setattr(remote, "settings", replace(remote.settings, adk_agent_url=services["adk"],
                                                    crew_agent_url=services["crew"]))
    services["seen"]["crew"].clear()
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    plan = propose_fix(store, wf, AgenticStrategy(messaging="direct", transport="a2a", fallback=False))
    assert plan.changes[0].replacement == GOOD_ADD
    assert [kind for kind, _ in services["seen"]["crew"]] == ["plan_and_review"]  # called by the ADK service
    hops = [e["agent"] for e in store.events(wf) if e["agent"].startswith("a2a:")]
    assert hops == ["a2a:orchestrator → sem7-adk/analyse_and_plan"]


def test_unreachable_agents_fall_back_and_strategies_report_it(store, monkeypatch):
    from app.healing import factory

    dead = replace(remote.settings, adk_agent_url="http://127.0.0.1:9/", crew_agent_url="http://127.0.0.1:9/",
                   agent_transport="a2a")
    monkeypatch.setattr(remote, "settings", dead)
    monkeypatch.setattr(factory, "settings", dead)
    agents = next(o for o in factory.strategy_options() if o["id"] == "agents")
    assert not agents["available"] and "not reachable over A2A" in agents["reason"]

    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    plan = propose_fix(store, wf, AgenticStrategy(messaging="blackboard", transport="a2a", fallback=True))
    assert plan.strategy == "rule-based" and any(e["status"] == "fallback" for e in store.events(wf))
