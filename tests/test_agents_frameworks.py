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
