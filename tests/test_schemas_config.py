import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models.schemas import RiskAssessment, RiskLevel, RootCauseReport


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        RootCauseReport(summary="s", root_cause="r", confidence=1.5)


def test_risk_score_bounds():
    with pytest.raises(ValidationError):
        RiskAssessment(commit_sha="x", risk_score=-0.1, risk_level=RiskLevel.LOW, model_version="v1")


def test_secrets_not_in_repr(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret")
    s = Settings()
    assert "sk-super-secret" not in repr(s)
    assert "ghp_super_secret" not in repr(s)
    assert set(s.secret_values()) == {"sk-super-secret", "ghp_super_secret"}
