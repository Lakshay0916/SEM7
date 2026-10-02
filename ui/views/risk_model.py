import pandas as pd
import streamlit as st

from ui.api_client import get_api, require_api
from ui.common import hbar
from ui.content import GLOSSARY
from ui.style import hero

METRICS = ["roc_auc", "pr_auc", "precision", "recall", "f1", "accuracy"]


def _metric_rows(variant: dict) -> pd.DataFrame:
    rows = []
    for name, c in variant["candidates"].items():
        mark = " ✅ chosen" if name == variant["chosen_model"] else ""
        rows.append({"Model": name + mark, **{m: c["test"][m] for m in METRICS},
                     "TN/FP/FN/TP": "/".join(str(v) for v in c["test"]["confusion_matrix"].values())})
    for name, m in variant["baselines_test"].items():
        rows.append({"Model": f"baseline: {name}", **{k: m[k] for k in METRICS},
                     "TN/FP/FN/TP": "/".join(str(v) for v in m["confusion_matrix"].values())})
    return pd.DataFrame(rows)


def render() -> None:
    st.title("ML Risk Model")
    require_api()
    data = get_api().risk_evaluation()
    evaluation, mining = data["evaluation"], data["mining_summary"]
    if not evaluation:
        st.warning("No evaluation found. Run `python scripts/mine_ci_data.py` then `python scripts/train_risk_model.py`.")
        return
    d = evaluation["dataset"]
    hero(
        "Predicting how likely a commit is to break CI",
        f"The model learned from {d['rows']:,} real commits in {len(d['repos'])} open-source Python projects, "
        f"each labelled with whether that project's real GitHub Actions tests passed or failed "
        f"({d['failure_rate']:.1%} failed). It answers 'how risky is this change?' — it does not find the bug.",
        chips=[f"📦 {d['rows']:,} commits", f"🗂️ {len(d['repos'])} projects", "⏱️ Temporal split",
               "⚖️ Compared with baselines"],
    )

    with st.expander("📚 How the dataset and model were built (4 steps)", expanded=False):
        st.markdown(
            "1. **Mine CI history** - for each project, download ~1 year of GitHub Actions results of its "
            "*test* workflow and clone the repository.\n"
            "2. **Clean the labels** - a commit is labelled *failed* only if a regular test job failed in a "
            "real step. Jobs testing against unreleased dependencies are ignored (they fail regardless of the "
            "commit), and runs that failed before any step executed are dropped as ambiguous.\n"
            "3. **Compute features** - 19 from the diff (size, files, tests, dependencies, ...) and 8 from CI "
            "history (did the parent fail, recent failure rate, ...). History only uses results that were "
            "already known when the commit was tested, so no future information leaks in.\n"
            "4. **Train & evaluate honestly** - per project, oldest 70% train, next 10% validation (to pick the "
            "model and threshold), newest 20% test. Three model types are compared with simple baselines."
        )

    st.markdown("#### Dataset")
    if mining:
        md = pd.DataFrame([m for m in mining if "rows" in m])[["repo", "runs_fetched", "rows", "failures", "failure_rate"]]
        left, right = st.columns([3, 2])
        left.dataframe(md.rename(columns={"rows": "commits", "runs_fetched": "CI runs fetched"}),
                       hide_index=True, width="stretch",
                       column_config={"failure_rate": st.column_config.NumberColumn("failure rate", format="percent")})
        right.altair_chart(hbar(md, "repo", "failure_rate", ".0%", "Share of commits whose CI failed"),
                           width="stretch")

    st.markdown("#### Results")
    variant_name = st.radio(
        "Model variant", list(evaluation["variants"]), horizontal=True,
        format_func=lambda v: {"full": "full - diff + CI history",
                               "diff_only": "diff_only - no history (used for demo commits)"}.get(v, v),
        help="A brand-new commit in the demo sandbox has no CI history, so the pipeline uses diff_only.",
    )
    v = evaluation["variants"][variant_name]
    best = v["candidates"][v["chosen_model"]]["test"]
    base_pr = v["baselines_test"]["majority_class (always pass)"]["pr_auc"]

    cols = st.columns(4)
    cols[0].metric("Chosen model", v["chosen_model"].replace("_", " "))
    cols[1].metric("Test ROC-AUC", f"{best['roc_auc']:.3f}", help=GLOSSARY["ROC-AUC"])
    cols[2].metric("Test PR-AUC", f"{best['pr_auc']:.3f}", f"{best['pr_auc'] - base_pr:+.3f} vs. blind guess",
                   help=GLOSSARY["PR-AUC"])
    lv = v["test_failure_rate_by_risk_level"]
    cols[3].metric("HIGH vs LOW failure rate",
                   f"{(lv['HIGH']['observed_failure_rate'] or 0):.0%} vs {(lv['LOW']['observed_failure_rate'] or 0):.0%}",
                   help="Of held-out commits rated HIGH / LOW, the share whose CI really failed")

    st.dataframe(_metric_rows(v), hide_index=True, width="stretch")
    st.caption("Rows marked *baseline* are trivial rules; the model is only useful where it beats them. "
               "Hover the metric tiles above or open the glossary below for definitions.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Does a higher risk level really mean more failures?**")
        lvdf = pd.DataFrame([{"Level": k, "failure_rate": x["observed_failure_rate"] or 0, "commits": x["commits"]}
                             for k, x in lv.items()])
        st.altair_chart(hbar(lvdf, "Level", "failure_rate", ".0%", "Observed failure rate on test commits",
                             sort=["LOW", "MEDIUM", "HIGH"]), width="stretch")
        st.caption("Levels: LOW = bottom half of validation scores, MEDIUM = 50th-85th percentile, HIGH = top 15%. "
                   + " · ".join(f"{r.Level}: {r.commits} commits" for r in lvdf.itertuples()))
    with right:
        st.markdown("**Which features does the model rely on most?**")
        imp = pd.DataFrame(list(v["permutation_importance_val"].items())[:10], columns=["feature", "importance"])
        st.altair_chart(hbar(imp, "feature", "importance", ".3f", "Drop in validation PR-AUC when shuffled"),
                        width="stretch")
        st.caption(GLOSSARY["Permutation importance"])

    st.markdown("**Does it work on a project it has never seen?** (leave-one-repo-out)")
    st.dataframe(pd.DataFrame([{"held-out repo": r, **x} for r, x in v["leave_one_repo_out"].items()]),
                 hide_index=True, width="stretch",
                 column_config={"failure_rate": st.column_config.NumberColumn("failure rate", format="percent"),
                                "roc_auc": st.column_config.NumberColumn("ROC-AUC", format="%.3f"),
                                "pr_auc": st.column_config.NumberColumn("PR-AUC", format="%.3f"),
                                "n": st.column_config.NumberColumn("commits")})

    with st.expander("📖 Glossary of metrics"):
        for term, text in GLOSSARY.items():
            st.markdown(f"**{term}** — {text}")

    if data["caveats"]:
        with st.expander("⚠️ Caveats and limitations"):
            st.markdown(data["caveats"])
