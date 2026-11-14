"""Notifications for the moments a person should know about, sent automatically by the self-healing loop:

    waiting_approval   a fix is proposed and waits for a human - nothing has changed yet (every attempt)
    pr_created         the fix passed the real tests and a pull request was opened (never merged)
    aborted            the workflow stopped and needs a human (attempt limit, unsafe plan, no fix, or a person)

Every message is built only from what the workflow recorded (risk, quality gate, root cause, plan, test runs,
approval, PR, reflection, and tokens from the agent traces) - nothing is inferred. Sending never breaks the
pipeline: failures are retried once, then recorded as an event and a metric while the workflow carries on.
"""

from __future__ import annotations

import html
import logging

from app.config import settings
from app.observability import metrics
from app.observability.metrics import PRICE_PER_MTOK
from app.orchestration.store import Store

log = logging.getLogger(__name__)
EVENTS = ("waiting_approval", "pr_created", "aborted")
TITLE = {"waiting_approval": "⏸ <b>Fix ready - waiting for your approval</b>",
         "pr_created": "✅ <b>Bug fixed - pull request created</b> (not merged)",
         "aborted": "🛑 <b>Stopped - a human is needed</b>"}


def channel():
    """The configured channel, or None (no token / chat id, or NOTIFY=0)."""
    if not (settings.notify and settings.telegram_bot_token and settings.telegram_chat_id):
        return None
    from app.notifications.telegram import TelegramChannel

    return TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)


def _e(text) -> str:
    return html.escape(str(text), quote=False)


def _tokens(store: Store, wf_id: str) -> tuple[int, int]:
    prompt = output = 0
    for art in store.artifacts(wf_id, "trace"):
        for s in art["payload"]["spans"]:
            if s["kind"] == "llm":
                prompt += s["attributes"].get("prompt_tokens") or 0
                output += s["attributes"].get("output_tokens") or 0
    return prompt, output


def build_message(store: Store, wf_id: str, event: str, reason: str | None = None) -> str:
    wf = store.get_workflow(wf_id)
    run = store.latest(wf_id, "pipeline_run") or {}
    commit = run.get("commit") or {}
    risk, gate = store.latest(wf_id, "risk_assessment"), store.latest(wf_id, "quality_gate")
    rc, plan = store.latest(wf_id, "root_cause"), store.latest(wf_id, "repair_plan")
    choice = store.latest(wf_id, "strategy_choice") or {}
    failure = store.latest(wf_id, "failure_event") or {}
    attempts = len(store.artifacts(wf_id, "repair_plan"))

    lines = [TITLE[event], "",
             f"<b>Workflow</b> <code>{_e(wf_id)}</code> · scenario {_e(wf.get('scenario') or '-')}"]
    if commit:
        lines.append(f"<b>Commit</b> \"{_e(commit.get('message', ''))}\" on <code>{_e(commit.get('branch', ''))}</code>")
    if risk or gate:
        bits = []
        if risk:
            bits.append(f"risk {_e(risk['risk_level'])} ({risk['risk_score']:.2f})")
        if gate:
            bits.append(f"gate {_e(gate['status'])}")
        report = failure.get("test_report") or {}
        if report:
            bits.append(f"{report.get('tests_failed', 0)} of {report.get('tests_run', 0)} tests failed")
        lines.append("<b>CI</b> " + " · ".join(bits))
    if rc:
        lines.append(f"<b>Cause</b> {_e(rc['root_cause'])} (confidence {rc['confidence']:.0%})")
    if plan:
        files = ", ".join(sorted({c["file"] for c in plan.get("changes", [])})) or "-"
        lines.append(f"<b>Fix</b> {_e(plan['summary'])} - {_e(files)}")
    engine = "AI agents" if choice.get("strategy") == "agents" else "rule-based"
    if choice.get("strategy") == "agents":
        engine += f" · {choice.get('pattern') or 'sequential'} · {choice.get('messaging') or 'blackboard'}"
    lines.append(f"<b>Analysis</b> {_e(engine)} · attempt {attempts or 1}")

    if event == "pr_created":
        test = store.latest(wf_id, "test_run") or {}
        approval = store.latest(wf_id, "approval") or {}
        pr = store.latest(wf_id, "pull_request") or {}
        lines.append(f"<b>Tests</b> {test.get('tests_passed', 0)}/{test.get('tests_run', 0)} passed on "
                     f"<code>{_e(pr.get('head_branch', ''))}</code>")
        if approval:
            lines.append(f"<b>Approved by</b> {_e(approval['reviewer'])}")
        if pr:
            lines.append(f"<b>PR</b> {_e(pr['title'])} → <code>{_e(pr.get('base_branch', ''))}</code>")
    elif event == "aborted":
        lines.append(f"<b>Why</b> {_e(reason or 'stopped')}")
        refl = store.latest(wf_id, "reflection")
        if refl and refl.get("still_failing"):
            lines.append("<b>Still failing</b> " + _e(", ".join(t.split("::")[-1] for t in refl["still_failing"])))
    else:
        lines.append("<b>Next</b> review the diff and approve or reject it - no code has been changed yet")

    prompt, output = _tokens(store, wf_id)
    if prompt or output:
        cost = prompt / 1e6 * PRICE_PER_MTOK["input"] + output / 1e6 * PRICE_PER_MTOK["output"]
        lines.append(f"<b>LLM</b> {prompt + output:,} tokens ≈ ${cost:.4f}")
    if settings.public_url:
        lines.append(f"\n<a href=\"{_e(settings.public_url)}\">Open SEM7</a> → Workflow History → {_e(wf_id)}")
    return "\n".join(lines)


def notify(store: Store, wf_id: str, event: str, reason: str | None = None, via=None) -> bool:
    """Send the event's message if a channel is configured. Never raises; returns whether it was sent."""
    via = via or channel()
    if via is None:
        return False
    try:
        via.send(build_message(store, wf_id, event, reason))
    except Exception as exc:  # a notification must never break the self-healing loop
        detail = f"{type(exc).__name__}: {exc}"
        for secret in settings.secret_values():  # an HTTP error could quote the request URL, which holds the token
            detail = detail.replace(secret, "[REDACTED]")
        log.warning("notification %s for %s failed: %s", event, wf_id, detail)
        store.log_event(wf_id, f"notify:{via.name}", "error", f"{event} not sent: {detail}"[:300])
        metrics.record_notification(via.name, event, "error")
        return False
    store.log_event(wf_id, f"notify:{via.name}", "ok", f"{event} message sent")
    metrics.record_notification(via.name, event, "ok")
    return True
