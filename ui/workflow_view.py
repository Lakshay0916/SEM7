"""Step-by-step rendering of one workflow: each step explains itself, then shows its real output."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.models.schemas import PipelineRun, TestStatus
from ui.common import RISK_BADGE, TEST_BADGE, get_predictor, get_store, try_ai_commit_to_main
from ui.content import PROGRESS, STEPS, UPCOMING
from ui.style import kv_tiles, progress_bar, risk_gauge, step_header, upcoming_cards


def _progress(run: PipelineRun) -> None:
    failed = run.test_report.status != TestStatus.PASS
    items = []
    for step, label, _phase in PROGRESS:
        if step == "risk" and run.risk is None:
            items.append((label + " (skipped)", "plan"))
        elif step == "tests":
            items.append((label, "fail" if failed else "done"))
        elif step:
            items.append((label, "done"))
        else:
            items.append((label, "plan"))
    progress_bar(items)
    st.caption(
        "✓ done · ✗ tests failed (hand-off point to the agents) · dashed = later phase, not built yet"
        if failed else "✓ done · tests passed, so no investigation or repair is needed · dashed = later phase"
    )


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
    _progress(run)

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

    # 4. Tests ---------------------------------------------------------------------
    t = run.test_report
    st_status = "done" if t.status == TestStatus.PASS else "fail"
    with st.container(border=True, key=f"step-{st_status}-4"):
        s = STEPS["tests"]
        step_header(4, s["title"], st_status, TEST_BADGE[t.status.value], s["what"], s["why"])
        cols = st.columns(5)
        cols[0].metric("Verdict", TEST_BADGE[t.status.value])
        cols[1].metric("Tests run", t.tests_run)
        cols[2].metric("Passed", t.tests_passed)
        cols[3].metric("Failed", t.tests_failed)
        cols[4].metric("Duration", f"{t.duration_seconds:.2f}s")
        st.caption(f"Command `{t.command}` · pytest exit code `{t.exit_code}`")
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
            with st.expander("FailureEvent JSON (exact input for the Investigation Agent)"):
                st.json(ev.model_dump(mode="json", exclude={"test_report", "diff"}))
        else:
            step_header(5, s["title"], "done", "Not needed - tests passed", s["what"], s["why"])
            st.success("All tests passed, so there is nothing to investigate or repair. The workflow ends here.")

    # 6. Recorded -------------------------------------------------------------------
    with st.container(border=True, key="step-done-6"):
        s = STEPS["record"]
        step_header(6, s["title"], "done", f"✓ Recorded · {len(store.events(wf_id))} events", s["what"], s["why"])
        ev = pd.DataFrame(store.events(wf_id))
        ev["ts"] = ev["ts"].str[11:19]
        ev["state"] = ev["state"].fillna("(log only)")
        st.dataframe(ev[["ts", "state", "agent", "status", "message"]].rename(columns={"ts": "time (UTC)"}),
                     hide_index=True, width="stretch")
        with st.expander("Stored artifacts (raw JSON)"):
            for a in store.artifacts(wf_id):
                st.markdown(f"**{a['kind']}** · {a['created_at'][:19]}")
                st.json(a["payload"], expanded=False)

    # Safety + what's next ---------------------------------------------------------
    with st.container(border=True, key="safety-card"):
        st.markdown("#### 🛡️ Safety check — can the AI write to `main`?")
        st.write("This tries an **AI-actor** commit on `main` in this workflow's sandbox. The Git guard "
                 "must refuse it and `main` must stay unchanged. Repairs are only ever allowed on "
                 "`ai/repair/*` branches.")
        if st.button("Attempt AI commit to main", key=f"guard-{wf_id}"):
            st.code(try_ai_commit_to_main(wf["repo_path"]), language="text")

    st.markdown("#### What happens next" + (" (after a failure)" if failed else ""))
    st.caption("These stages pick up the FailureEvent above. They are planned for later phases and not built yet.")
    upcoming_cards(UPCOMING)
