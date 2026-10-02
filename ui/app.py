"""SEM7 dashboard. Run with:  streamlit run ui/app.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from ui.style import inject_css  # noqa: E402
from ui.views import dashboard, how_it_works, risk_model, run_pipeline, workflows  # noqa: E402

st.set_page_config(page_title="SEM7 Self-Healing CI/CD", page_icon="🛠️", layout="wide")
inject_css()

pages = {
    "dashboard": st.Page(dashboard.render, title="Dashboard", icon="📊", url_path="dashboard", default=True),
    "how-it-works": st.Page(how_it_works.render, title="How it works", icon="📘", url_path="how-it-works"),
    "run": st.Page(run_pipeline.render, title="Run Pipeline", icon="▶️", url_path="run"),
    "workflows": st.Page(workflows.render, title="Workflow History", icon="🧭", url_path="workflows"),
    "risk": st.Page(risk_model.render, title="Risk Model", icon="📈", url_path="risk"),
}
st.session_state["_pages"] = pages  # lets pages link to each other

nav = st.navigation({
    "Overview": [pages["dashboard"], pages["how-it-works"]],
    "Pipeline": [pages["run"], pages["workflows"]],
    "Models": [pages["risk"]],
})
with st.sidebar:
    st.markdown("**SEM7 · Self-Healing CI/CD**")
    st.caption("ML predicts · RAG retrieves · LLM reasons · agents act · tests verify")
nav.run()
