"""Telegram notifications: which events send, what the message says, and that sending never breaks the loop."""

import json
from dataclasses import replace

import httpx
import pytest

import app.ci.simulator as sim_mod
import app.notifications.notifier as notifier
import app.orchestration.store as store_mod
from app.healing.loop import abort, apply_and_verify, decide, propose_fix
from app.healing.rules import RuleBasedStrategy
from app.models.schemas import WorkflowState as S
from app.notifications.telegram import TelegramChannel, TelegramError
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store


class FakeChannel:
    name = "telegram"

    def __init__(self, fail: Exception | None = None):
        self.sent, self.fail = [], fail

    def send(self, text):
        if self.fail:
            raise self.fail
        self.sent.append(text)


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'n.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


@pytest.fixture
def fake(monkeypatch):
    ch = FakeChannel()
    monkeypatch.setattr(notifier, "channel", lambda: ch)
    monkeypatch.setattr(notifier, "settings", replace(notifier.settings, public_url="https://demo.sslip.io"))
    return ch


def test_the_whole_loop_sends_one_message_per_event(store, fake):
    strategy = RuleBasedStrategy()
    wf = run_ci_workflow("failed_first_repair", store, None, strategy=strategy).workflow_id
    assert len(fake.sent) == 1 and "waiting for your approval" in fake.sent[0]
    decide(store, wf, True, "alice")
    assert apply_and_verify(store, wf, strategy=strategy) == S.WAITING_APPROVAL   # reflection: a 2nd plan
    decide(store, wf, True, "alice")
    assert apply_and_verify(store, wf, strategy=strategy) == S.PR_CREATED
    first, second, done = fake.sent
    assert "attempt 1" in first and "attempt 2" in second and "no code has been changed yet" in first
    assert "pull request created" in done and "9/9 passed" in done and "Approved by</b> alice" in done
    assert "Simplify divide()" in done and "divide" in done and "Open SEM7" in done and wf in done
    events = [e for e in store.events(wf) if e["agent"] == "notify:telegram"]
    assert [e["status"] for e in events] == ["ok", "ok", "ok"]


def test_a_stopped_workflow_says_why(store, fake):
    wf = run_ci_workflow("simple_bug", store, None, strategy=RuleBasedStrategy()).workflow_id
    abort(store, wf, "Stopped by reviewer: not now")
    assert "a human is needed" in fake.sent[-1] and "Stopped by reviewer: not now" in fake.sent[-1]


def test_a_failed_notification_never_breaks_the_loop_or_leaks_the_token(store, monkeypatch):
    token = "123456:secret-token-value"
    monkeypatch.setattr(notifier, "settings", replace(notifier.settings, telegram_bot_token=token))
    monkeypatch.setattr(notifier, "channel", lambda: FakeChannel(fail=httpx.ConnectError(
        f"cannot reach https://api.telegram.org/bot{token}/sendMessage")))
    strategy = RuleBasedStrategy()
    wf = run_ci_workflow("simple_bug", store, None, strategy=strategy).workflow_id
    decide(store, wf, True, "alice")
    assert apply_and_verify(store, wf, strategy=strategy) == S.PR_CREATED          # the fix still went through
    errors = [e for e in store.events(wf) if e["agent"] == "notify:telegram"]
    assert errors and all(e["status"] == "error" for e in errors)
    assert not any(token in (e["message"] or "") for e in errors) and "[REDACTED]" in errors[0]["message"]


def test_messages_escape_whatever_the_commit_says(store, fake):
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    run = store.latest(wf, "pipeline_run")
    run["commit"]["message"] = "<b>sneaky</b> & <script>"
    store.save_artifact(wf, "pipeline_run", run)
    text = notifier.build_message(store, wf, "aborted", "x")
    assert "&lt;b&gt;sneaky&lt;/b&gt; &amp; &lt;script&gt;" in text


def test_no_channel_without_a_token_chat_or_with_notify_off(monkeypatch):
    base = replace(notifier.settings, telegram_bot_token="t", telegram_chat_id="42", notify=True)
    monkeypatch.setattr(notifier, "settings", base)
    assert notifier.channel() is not None
    for change in ({"telegram_bot_token": ""}, {"telegram_chat_id": ""}, {"notify": False}):
        monkeypatch.setattr(notifier, "settings", replace(base, **change))
        assert notifier.channel() is None


def test_telegram_channel_posts_html_and_retries_a_rate_limit(monkeypatch):
    import app.notifications.telegram as tg

    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 3}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    ch = TelegramChannel("123:abc", "42", client=httpx.Client(transport=httpx.MockTransport(handler)))
    ch.send("<b>hi</b>")
    assert len(calls) == 2 and calls[-1].url.path == "/bot123:abc/sendMessage"
    body = json.loads(calls[-1].content)
    assert body == {"chat_id": "42", "text": "<b>hi</b>", "parse_mode": "HTML", "disable_web_page_preview": True}


def test_telegram_errors_name_the_problem_not_the_token():
    ch = TelegramChannel("123:abc", "42", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"}))))
    with pytest.raises(TelegramError, match="chat not found") as err:
        ch.send("x")
    assert "123:abc" not in str(err.value)


def test_test_endpoint_reports_the_setup(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.main as api

    monkeypatch.setattr(notifier, "channel", lambda: None)
    assert TestClient(api.app).post("/notifications/test").json()["configured"] is False
    ch = FakeChannel()
    monkeypatch.setattr(notifier, "channel", lambda: ch)
    assert TestClient(api.app).post("/notifications/test").json()["sent"] is True and "test message" in ch.sent[0]
