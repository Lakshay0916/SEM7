"""Project assistant: knowledge retrieval, live-data answers, Claude path (faked) and the /chat API."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.main as api
import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod
from app.assistant.bot import FALLBACK_BETA, Assistant
from app.assistant.knowledge import KnowledgeBase, analyze, split_sections
from app.config import settings
from app.healing.rules import RuleBasedStrategy
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'chat.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase.load()


@pytest.fixture
def failed_wf(store):
    return run_ci_workflow("simple_bug", store, None, strategy=RuleBasedStrategy()).workflow_id


# --------------------------------------------------------------------------- knowledge base


def test_split_sections_uses_headings_and_skips_empty():
    text = "# Title\nintro\n## A\nbody a\n### B\nbody b\n## Empty\n"
    assert [(s.title, s.body) for s in split_sections("X", text)] == [("A", "body a"), ("B", "body b")]


def test_analyzer_stems_and_keeps_digits():
    assert analyze("approving the 9 tests") == ["approv", "9", "test", "approv 9", "9 test"]


def test_knowledge_base_loads_faq_and_docs(kb):
    sources = {s.source for s in kb.sections}
    assert {"FAQ", "README", "Risk model evaluation"} <= sources
    assert "<" not in kb.render()[:3] and "### " in kb.render()


@pytest.mark.parametrize("question, expected_title", [
    ("How does the quality gate decide?", "What is the quality gate and how does it decide?"),
    ("what happens when I approve", "What happens when I approve a fix? Is a real PR created?"),
    ("what are the 9 tests", "What are the 9 tests? Are they different from the GitHub Actions tests?"),
    ("why random forest", "Why Logistic Regression for one variant and Random Forest for the other?"),
    ("how is the risk level decided", "How is the risk level (LOW, MEDIUM, HIGH) of a commit decided?"),
    ("can the AI push to main", "Can the AI modify the main branch?"),
])
def test_retrieval_finds_the_right_faq_entry(kb, question, expected_title):
    assert kb.search(question)[0].section.title == expected_title


# --------------------------------------------------------------------------- offline answers


def test_offline_answer_is_grounded_with_sources(store, kb):
    r = Assistant(store, kb=kb).reply("How does the quality gate decide?")
    assert r.provider == "heuristic" and "BLOCK" in r.answer and "REVIEW" in r.answer
    assert r.sources[0] == "What is the quality gate and how does it decide?"


def test_off_topic_question_is_not_answered_from_a_weak_match(store, kb):
    r = Assistant(store, kb=kb).reply("Who won the world cup?")
    assert "couldn't find that" in r.answer and r.sources == []


def test_greeting_lists_suggestions(store, kb):
    assert "Try one of these" in Assistant(store, kb=kb).reply("hello").answer


def test_stats_question_reads_live_database(store, kb, failed_wf):
    r = Assistant(store, kb=kb).reply("How many workflows have been run?")
    assert "Workflows recorded: 1" in r.answer and failed_wf in r.answer
    assert r.sources == ["Live data › workflow history"]


def test_workflow_question_uses_the_runs_trace(store, kb, failed_wf):
    r = Assistant(store, kb=kb).reply("Why did this run fail?", workflow_id=failed_wf)
    assert r.workflow_id == failed_wf
    assert "WAITING_APPROVAL" in r.answer and "add()" in r.answer and "BLOCK" in r.answer
    assert "Next step:" in r.answer


def test_workflow_id_in_the_message_is_picked_up(store, kb, failed_wf):
    r = Assistant(store, kb=kb).reply(f"tell me about {failed_wf}")
    assert r.workflow_id == failed_wf and "Root cause" in r.answer


def test_unknown_workflow_raises_key_error(store, kb):
    with pytest.raises(KeyError):
        Assistant(store, kb=kb).reply("status?", workflow_id="wf-000000")


# --------------------------------------------------------------------------- Claude path (no network)


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request, returns a canned response or raises."""

    def __init__(self, response=None, error: Exception | None = None):
        self.calls = []
        self.response, self.error = response, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return self.response


def _response(text="Grounded answer.", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop_reason, model=settings.llm_model)


@pytest.fixture
def claude_cfg():
    return replace(settings, llm_provider="anthropic", llm_model="claude-opus-5-5", llm_api_key="")


def test_claude_request_is_cached_grounded_and_multi_turn(store, kb, failed_wf, claude_cfg):
    client = FakeClient(_response())
    history = [{"role": "assistant", "content": "orphan"}, {"role": "user", "content": "hi"},
               {"role": "assistant", "content": "Hello!"}]
    r = Assistant(store, kb=kb, cfg=claude_cfg, client=client).reply("Why did it fail?", history, failed_wf)

    assert r.provider == "anthropic" and r.answer == "Grounded answer."
    assert r.sources[0] == f"Live data › workflow {failed_wf}"
    params = client.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["fallbacks"] == "default" and params["betas"] == [FALLBACK_BETA]
    assert params["output_config"] == {"effort": claude_cfg.llm_effort}
    system = params["system"]
    assert "cache_control" not in system[0] and system[1]["cache_control"] == {"type": "ephemeral"}
    assert "Can the AI modify the main branch?" in system[1]["text"]  # whole knowledge base is in the prefix
    msgs = params["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]  # leading assistant turn dropped
    assert "<live_data>" in msgs[-1]["content"] and failed_wf in msgs[-1]["content"]
    assert "<question>\nWhy did it fail?\n</question>" in msgs[-1]["content"]


def test_fallbacks_only_sent_for_models_that_support_them(store, kb, claude_cfg):
    client = FakeClient(_response())
    Assistant(store, kb=kb, cfg=replace(claude_cfg, llm_model="claude-haiku-4-5"), client=client).reply("hi there?")
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]


def test_api_error_falls_back_to_offline_answer(store, kb, claude_cfg):
    import anthropic
    import httpx2

    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    r = Assistant(store, kb=kb, cfg=claude_cfg, client=FakeClient(error=err)).reply("How does the quality gate decide?")
    assert r.provider == "heuristic" and "BLOCK" in r.answer
    assert "APIConnectionError" in r.note


def test_refusal_falls_back_to_offline_answer(store, kb, claude_cfg):
    client = FakeClient(_response(text="", stop_reason="refusal"))
    r = Assistant(store, kb=kb, cfg=claude_cfg, client=client).reply("How does the quality gate decide?")
    assert r.provider == "heuristic" and r.note


# --------------------------------------------------------------------------- HTTP API


@pytest.fixture
def client(store):
    api.get_store.cache_clear()
    yield TestClient(api.app)
    api.get_store.cache_clear()


def test_assistant_info_endpoint(client):
    info = client.get("/assistant").json()
    assert info["provider"] in {"heuristic", "anthropic"} and info["suggestions"]


def test_chat_endpoint_answers_with_history(client):
    r = client.post("/chat", json={"message": "What does this project do?",
                                   "history": [{"role": "user", "content": "hi"},
                                               {"role": "assistant", "content": "Hello!"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] and body["sources"] and body["provider"] == "heuristic"


def test_chat_endpoint_validates_input(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422
    assert client.post("/chat", json={"message": "x", "history": [{"role": "system", "content": "y"}]}).status_code == 422
    assert client.post("/chat", json={"message": "status?", "workflow_id": "wf-000000"}).status_code == 404
