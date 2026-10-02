"""Step-by-step rendering of one workflow: each step explains itself, then shows its real output.

Steps 1-5 are the CI run; step 6 is the interactive, human-approved self-healing loop;
step 7 is the full audit trail.
"""

from __future__ import annotations

import difflib

import pandas as pd
import streamlit as st

from app.ci.quality_gate import evaluate_gate
from app.healing.loop import abort, apply_and_verify, decide, propose_fix, repair_branch
from app.healing.rules import RuleBasedStrategy
from app.models.schemas import PipelineRun, TestStatus
from app.models.schemas import WorkflowState as S
from ui.common import RISK_BADGE, TEST_BADGE, get_predictor, get_store, try_ai_commit_to_main
from ui.content import PROGRESS, STEPS, UPCOMING
from ui.style import kv_tiles, progress_bar, risk_gauge, step_header, upcoming_cards

GATE_BADGE = {"PASS": "✅ PASS", "REVIEW": "🟠 REVIEW", "BLOCK": "⛔ BLOCK"}


def strategy() -> RuleBasedStrategy:
    return RuleBasedStrategy()


# --------------------------------------------------------------------------- progress strip


def _progress(store, wf_id: str, run: PipelineRun) -> None:
    reached = {e["state"] for e in store.events(wf_id) if e["state"]}
    state = store.get_workflow(wf_id)["state"]
    failed = run.test_report.status != TestStatus.PASS
    items = []
    for key, label, status, marker in PROGRESS:
        if status == "planned":
            items.append((label, "plan"))
        elif key == "risk" and run.risk is None:
            items.append((label + " (skipped)", "plan"))
        elif key == "tests":
            items.append((label, "fail" if failed else "done"))
        elif key == "approval" and state == "WAITING_APPROVAL":
            items.append((label, "wait"))
        elif key in ("sandbox", "diff") or marker in reached:
            items.append((label, "done"))
        else:
            items.append((label, "todo"))
    progress_bar(items)
    st.caption("✓ reached · ✗ tests failed · ⏸ waiting for you · plain = not reached in this run · "
               "dashed = planned (Agentic AI phase)")


# --------------------------------------------------------------------------- self-healing


def _diff(original: str, replacement: str, path: str) -> str:
    return "".join(difflib.unified_diff(original.splitlines(keepends=True), replacement.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


def _by_attempt(store, wf_id: str, kind: str) -> dict[int, dict]:
    return {a["attempt"]: a["payload"] for a in store.artifacts(wf_id, kind)}


def _attempt_block(store, wf_id: str, n: int) -> None:
    inv = _by_attempt(store, wf_id, "investigation").get(n)
    rc = _by_attempt(store, wf_id, "root_cause").get(n)
    plan = _by_attempt(store, wf_id, "repair_plan").get(n)
    approval = _by_attempt(store, wf_id, "approval").get(n)
    attempt = _by_attempt(store, wf_id, "repair_attempt").get(n)
    test = _by_attempt(store, wf_id, "test_run").get(n)
    refl = _by_attempt(store, wf_id, "reflection").get(n)

    with st.container(border=True, key=f"attempt-{n}"):
        st.markdown(f"##### 🔁 Attempt {n}" + (f" · `{plan['level']}`" if plan else ""))
        a, b = st.columns(2)
        with a:
            st.markdown("**🔎 Investigation** — what failed")
            if inv:
                st.markdown(inv["failure_summary"])
                st.markdown("\n".join(f"- {e.removeprefix('FACT: ')}" for e in inv["evidence"]))
                if inv["initial_hypotheses"]:
                    st.caption("Hypotheses (not facts): " + " · ".join(
                        h.removeprefix("HYPOTHESIS: ") for h in inv["initial_hypotheses"]))
        with b:
            st.markdown("**🧠 Root cause** — why")
            if rc:
                st.markdown(rc["root_cause"])
                st.progress(rc["confidence"], text=f"Confidence {rc['confidence']:.0%}")
                for e in [x for x in rc["evidence"] if not x.startswith("FACT:")][:4]:
                    st.markdown(f"- {e}")
                for alt in rc["alternative_hypotheses"]:
                    st.caption(f"Also considered: {alt}")

        if plan:
            st.markdown(f"**🛠️ Proposed fix:** {plan['summary']}  ·  _strategy `{plan['strategy']}` "
                        f"({plan['strategy_kind']})_")
            for ch in plan["changes"]:
                st.caption(f"`{ch['file']}` — {ch['description']}")
                st.code(_diff(ch["original"], ch["replacement"], ch["file"]), language="diff")
            c1, c2 = st.columns(2)
            c1.markdown("**Risks of this fix**\n" + "\n".join(f"- {r}" for r in plan["risks"]))
            c2.markdown(f"**Tests to run**\n" + "\n".join(f"- `{t}`" for t in plan["tests"])
                        + f"\n\n**Rollback:** {plan['rollback_plan']}")

        if approval:
            verdict = "✅ Approved" if approval["approved"] else "🚫 Rejected"
            st.markdown(f"**👤 Human decision:** {verdict} by **{approval['reviewer']}**"
                        + (f" — _{approval['comment']}_" if approval["comment"] else ""))
        if attempt:
            with st.expander(f"🌿 Repair commit `{attempt['commit_sha'][:10]}` on `{attempt['branch']}`"):
                st.code(attempt["patch"], language="diff")
        if test:
            badge = TEST_BADGE[test["status"]]
            st.markdown(f"**🧪 Re-test (real pytest):** {badge} — {test['tests_passed']} passed, "
                        f"{test['tests_failed']} failed, {test['tests_errored']} errors")
        if refl:
            st.info(f"🪞 **Reflection:** {refl['summary']}"
                    + (f"  \nStill failing: {', '.join(f'`{t}`' for t in refl['still_failing'])}"
                       if refl["still_failing"] else "")
                    + (f"  \n⚠️ Regressions: {', '.join(f'`{t}`' for t in refl['regressions'])}"
                       if refl["regressions"] else ""))


def _healing(store, wf_id: str) -> None:
    wf = store.get_workflow(wf_id)
    state = S(wf["state"])
    attempts = sorted(_by_attempt(store, wf_id, "investigation"))
    status, label = {
        S.PR_CREATED: ("done", "✓ Repaired · PR opened"),
        S.ABORTED: ("fail", "✗ Stopped - human needed"),
        S.WAITING_APPROVAL: ("warn", "⏸ Waiting for your approval"),
        S.REJECTED: ("warn", "Plan rejected"),
        S.FAILED: ("warn", "Ready to investigate"),
    }.get(state, ("warn", state.value))

    with st.container(border=True, key=f"step-{status}-6"):
        s = STEPS["heal"]
        step_header(6, s["title"], status, label, s["what"], s["why"])

        if state == S.FAILED and not attempts:
            st.write("No analysis yet for this failure.")
            if st.button("🩺 Investigate & propose a fix", type="primary", key=f"start-{wf_id}"):
                propose_fix(store, wf_id, strategy())
                st.rerun()

        for n in attempts:
            _attempt_block(store, wf_id, n)

        if state == S.WAITING_APPROVAL:
            with st.container(border=True, key="approval-box"):
                st.markdown("#### ⏸ Your decision")
                st.write("Nothing has been changed yet. Approving applies **only** the diff above, on branch "
                         f"`{repair_branch(wf_id)}` (never `main`), then re-runs the full test suite.")
                c1, c2 = st.columns([1, 2])
                reviewer = c1.text_input("Reviewer name", value="reviewer", key=f"rev-{wf_id}")
                comment = c2.text_input("Comment (optional)", key=f"com-{wf_id}")
                b1, b2, _ = st.columns([1, 1, 2])
                if b1.button("✅ Approve & apply", type="primary", key=f"approve-{wf_id}", width="stretch"):
                    decide(store, wf_id, True, reviewer or "reviewer", comment)
                    with st.spinner("Applying on the repair branch and running the real tests…"):
                        apply_and_verify(store, wf_id, strategy=strategy())
                    st.rerun()
                if b2.button("🚫 Reject", key=f"reject-{wf_id}", width="stretch"):
                    decide(store, wf_id, False, reviewer or "reviewer", comment)
                    st.rerun()
        elif state == S.REJECTED:
            st.warning("You rejected the proposed fix. No code was changed.")
            b1, b2, _ = st.columns([1, 1, 2])
            if b1.button("🔁 Re-analyse", key=f"reanalyse-{wf_id}", width="stretch"):
                propose_fix(store, wf_id, strategy())
                st.rerun()
            if b2.button("⏹ Stop here", key=f"abort-{wf_id}", width="stretch"):
                abort(store, wf_id, "Stopped by reviewer after rejection")
                st.rerun()
        elif state == S.PR_CREATED:
            pr = store.latest(wf_id, "pull_request")
            st.success(f"🎉 Fix validated by real tests. Pull request opened ({pr['provider']}, **not merged**): "
                       f"`{pr['head_branch']}` → `{pr['base_branch']}`")
            with st.expander("📄 Pull request description"):
                st.markdown(pr["body"])
            st.caption(f"Saved at `{pr['url']}`")
        elif state == S.ABORTED:
            last = [e for e in store.events(wf_id) if e["state"] == "ABORTED"]
            st.error(f"⏹ {last[-1]['message'] if last else 'Workflow stopped.'}")


# --------------------------------------------------------------------------- main view


def render_workflow(wf_id: str) -> None:
    store = get_store()
    wf = store.get_workflow(wf_id)
    payload = store.latest(wf_id, "pipeline_run")
    if payload is None:
        st.warning("This workflow has no recorded pipeline run.")
        return
    run = PipelineRun.model_validate(payload)
    failed = run.failure_event is not None

    st.markdown(f"### Workflow `{wf_id}`")
    st.caption(f"Scenario `{wf['scenario']}` · final state **{wf['state']}** · sandbox `{wf['repo_path']}`")
    _progress(store, wf_id, run)

    # 1. Sandbox + commit ------------------------------------------------------
    with st.container(border=True, key="step-done-1"):
        s = STEPS["sandbox"]
        step_header(1, s["title"], "done", "✓ Done", s["what"], s["why"])
        c = run.commit
        kv_tiles({"Base branch (healthy)": "main", "Feature branch": c.branch,
                  "Commit": c.sha[:10], "Author": c.author})
        st.markdown(f"**Commit message:** _{c.message}_")

    # 2. Diff -----------------------------------------------------------------------
    with st.container(border=True, key="step-done-2"):
        s = STEPS["diff"]
        d = run.diff
        step_header(2, s["title"], "done",
                    f"✓ {len(d.files)} file(s) · +{d.total_additions}/-{d.total_deletions}", s["what"], s["why"])
        st.dataframe(
            pd.DataFrame([
                {"File": f.path,
                 "Change": {"A": "added", "M": "modified", "D": "deleted"}.get(f.change_type, f.change_type),
                 "Lines +": f.additions, "Lines -": f.deletions,
                 "Functions changed": ", ".join(f.functions_changed) or "-",
                 "Kind": "test" if f.is_test else "dependency" if f.is_dependency
                 else "config" if f.is_config else "source"}
                for f in d.files
            ]),
            hide_index=True, width="stretch",
        )
        with st.expander("Show the raw diff (red = removed, green = added)"):
            st.code(d.patch or "(empty diff)", language="diff")

    # 3. Risk ----------------------------------------------------------------------
    r = run.risk
    risk_status = "plan" if r is None else {"LOW": "done", "MEDIUM": "warn", "HIGH": "fail"}[r.risk_level.value]
    with st.container(border=True, key=f"step-{risk_status}-3"):
        s = STEPS["risk"]
        if r is None:
            step_header(3, s["title"], "plan", "Skipped - no model", s["what"], s["why"])
            st.info("Train the model with `python scripts/train_risk_model.py` to enable this step.")
        else:
            step_header(3, s["title"], risk_status, RISK_BADGE[r.risk_level.value], s["what"], s["why"])
            left, right = st.columns([3, 2])
            with left:
                predictor = get_predictor()
                variant = r.model_version.split(":")[1]
                if predictor is not None and variant in predictor.bundle["variants"]:
                    lv = predictor.bundle["variants"][variant]["risk_levels"]
                    risk_gauge(r.risk_score, lv["medium"], lv["high"])
                    st.caption(
                        "Bands come from the validation set: MEDIUM = above the median score, "
                        "HIGH = top 15%. On held-out commits, a higher band really did fail more often "
                        "(see the Risk Model page)."
                    )
                st.markdown("**Why this score - main risk factors:**")
                st.markdown("\n".join(f"- {f}" for f in r.risk_factors) or "_Nothing stands out vs. typical commits._")
            with right:
                st.metric("Risk score", f"{r.risk_score:.3f}", help="Model's estimated probability-like score")
                st.metric("Model", r.model_version.split(":", 1)[1],
                          help="diff_only = no CI history available for this commit")
                with st.expander("All 27 feature values"):
                    st.dataframe(pd.DataFrame(r.features.items(), columns=["Feature", "Value"]),
                                 hide_index=True, width="stretch")

    # 4. Tests + quality gate --------------------------------------------------------
    t = run.test_report
    gate = evaluate_gate(t, r)
    st_status = "done" if t.status == TestStatus.PASS else "fail"
    with st.container(border=True, key=f"step-{st_status}-4"):
        s = STEPS["tests"]
        step_header(4, s["title"], st_status, TEST_BADGE[t.status.value], s["what"], s["why"])
        cols = st.columns(5)
        cols[0].metric("Verdict", TEST_BADGE[t.status.value])
        cols[1].metric("Tests run", t.tests_run)
        cols[2].metric("Passed", t.tests_passed)
        cols[3].metric("Failed", t.tests_failed)
        cols[4].metric("Quality gate", GATE_BADGE[gate.status.value],
                       help="Tests always run. FAIL → BLOCK; tests pass + HIGH risk → REVIEW; else PASS")
        st.caption(f"Command `{t.command}` · pytest exit code `{t.exit_code}` · {t.duration_seconds:.2f}s · "
                   f"gate: {gate.reasons[0]}")
        for n in t.notes:
            st.warning(n)
        for f in t.failures:
            first = f.message.splitlines()[0] if f.message else ""
            with st.expander(f"❌ {f.node_id} — {first}"):
                st.code(f.traceback or f.message, language="python")
        with st.expander("Raw pytest output"):
            st.code((t.stdout + "\n" + t.stderr).strip() or "(no output)", language="text")

    # 5. Failure event ------------------------------------------------------------
    with st.container(border=True, key=f"step-{'fail' if failed else 'done'}-5"):
        s = STEPS["failure"]
        if failed:
            ev = run.failure_event
            step_header(5, s["title"], "fail", f"✗ {len(ev.failed_tests)} failing test(s)", s["what"], s["why"])
            st.markdown("**Failing tests:** " + ", ".join(f"`{x}`" for x in ev.failed_tests))
            st.markdown("**Changed files:** " + ", ".join(f"`{x}`" for x in ev.changed_files))
            with st.expander("FailureEvent JSON (exact input to the self-healing loop)"):
                st.json(ev.model_dump(mode="json", exclude={"test_report", "diff"}))
        else:
            step_header(5, s["title"], "done", "Not needed - tests passed", s["what"], s["why"])
            st.success("All tests passed, so there is nothing to investigate or repair. The workflow ends here.")

    # 6. Self-healing -------------------------------------------------------------
    if failed:
        _healing(store, wf_id)

    # 7. Recorded -------------------------------------------------------------------
    with st.container(border=True, key="step-done-7"):
        s = STEPS["record"]
        step_header(7 if failed else 6, s["title"], "done", f"✓ Recorded · {len(store.events(wf_id))} events",
                    s["what"], s["why"])
        ev = pd.DataFrame(store.events(wf_id))
        ev["ts"] = ev["ts"].str[11:19]
        ev["state"] = ev["state"].fillna("(log only)")
        st.dataframe(ev[["ts", "state", "agent", "status", "message"]].rename(columns={"ts": "time (UTC)"}),
                     hide_index=True, width="stretch")
        with st.expander("Stored artifacts (raw JSON)"):
            for a in store.artifacts(wf_id):
                st.markdown(f"**{a['kind']}** (attempt {a['attempt']}) · {a['created_at'][:19]}")
                st.json(a["payload"], expanded=False)

    # Safety + roadmap ---------------------------------------------------------------
    with st.container(border=True, key="safety-card"):
        st.markdown("#### 🛡️ Safety check — can the AI write to `main`?")
        st.write("This tries an **AI-actor** commit on `main` in this workflow's sandbox. The Git guard "
                 "must refuse it and `main` must stay unchanged. Repairs are only ever allowed on "
                 "`ai/repair/*` branches.")
        if st.button("Attempt AI commit to main", key=f"guard-{wf_id}"):
            st.code(try_ai_commit_to_main(wf["repo_path"]), language="text")

    st.markdown("#### Roadmap")
    st.caption("Still to come. The Agentic AI items plug into the same loop shown above.")
    upcoming_cards(UPCOMING)
