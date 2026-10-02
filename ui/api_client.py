"""HTTP client for the SEM7 backend API - the UI's only way to read or change anything.

Base URL comes from SEM7_API_URL (default http://localhost:8000). With
SEM7_API_INPROCESS=1 (tests) the FastAPI app is called in-process instead.
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

DEFAULT_URL = "http://localhost:8000"


class APIError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"API {status}: {detail}")
        self.status, self.detail = status, detail


class ApiClient:
    def __init__(self, http: httpx.Client, base_url: str):
        self.http, self.base_url = http, base_url

    def _req(self, method: str, path: str, json: dict | None = None):
        try:
            r = self.http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise APIError(0, f"cannot reach the backend at {self.base_url} ({type(exc).__name__})") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise APIError(r.status_code, str(detail))
        return r.json()

    # meta
    def health(self) -> dict: return self._req("GET", "/health")
    def scenarios(self) -> list[dict]: return self._req("GET", "/scenarios")

    # workflows
    def run(self, scenario: str, heal: bool = True) -> dict:
        return self._req("POST", "/workflows", {"scenario": scenario, "heal": heal})

    def workflows(self, limit: int = 200) -> list[dict]: return self._req("GET", f"/workflows?limit={limit}")
    def trace(self, wf_id: str) -> dict: return self._req("GET", f"/workflows/{wf_id}")
    def propose(self, wf_id: str) -> dict: return self._req("POST", f"/workflows/{wf_id}/propose")

    def approve(self, wf_id: str, reviewer: str, comment: str = "") -> dict:
        return self._req("POST", f"/workflows/{wf_id}/approve", {"reviewer": reviewer, "comment": comment})

    def reject(self, wf_id: str, reviewer: str, comment: str = "") -> dict:
        return self._req("POST", f"/workflows/{wf_id}/reject", {"reviewer": reviewer, "comment": comment})

    def abort(self, wf_id: str, reason: str) -> dict:
        return self._req("POST", f"/workflows/{wf_id}/abort", {"reason": reason})

    def safety_check(self, wf_id: str) -> dict: return self._req("POST", f"/workflows/{wf_id}/safety-check")

    # risk
    def risk_model(self) -> dict | None:
        try:
            return self._req("GET", "/risk/model")
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    def risk_evaluation(self) -> dict: return self._req("GET", "/risk/evaluation")


@st.cache_resource
def get_api() -> ApiClient:
    if os.getenv("SEM7_API_INPROCESS") == "1":
        from fastapi.testclient import TestClient  # only needed in tests

        from app.api.main import app

        return ApiClient(TestClient(app), "in-process")
    url = os.getenv("SEM7_API_URL", DEFAULT_URL).rstrip("/")
    # Pipeline runs and repairs execute real test suites, so allow generous request times.
    return ApiClient(httpx.Client(base_url=url, timeout=httpx.Timeout(300, connect=5)), url)


def require_api() -> dict:
    """Stop the page with clear instructions if the backend is unreachable."""
    api = get_api()
    try:
        return api.health()
    except APIError as exc:
        st.error(f"**Backend API unavailable** — {exc.detail}")
        st.markdown(
            "Start the backend, then refresh:\n"
            "- Docker: `docker compose up`\n"
            "- Local: `uvicorn app.api.main:app --port 8000` (and set `SEM7_API_URL` if not on localhost:8000)"
        )
        st.stop()


class TraceView:
    """Read-only view over GET /workflows/{id} with the same accessors the UI used on the Store."""

    def __init__(self, trace: dict):
        self._t = trace

    def get_workflow(self) -> dict: return self._t["workflow"]
    def state(self) -> str: return self._t["workflow"]["state"]
    def events(self) -> list[dict]: return self._t["events"]

    def artifacts(self, kind: str | None = None) -> list[dict]:
        return [a for a in self._t["artifacts"] if kind is None or a["kind"] == kind]

    def latest(self, kind: str) -> dict | None:
        items = self.artifacts(kind)
        return items[-1]["payload"] if items else None
