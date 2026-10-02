"""Risk-aware quality gate.

Policy (tests are always run; risk never replaces them):
* tests not PASS (FAIL / ERROR / TIMEOUT)  -> BLOCK
* tests PASS and risk HIGH                 -> REVIEW  (merge allowed only after human review)
* otherwise                                -> PASS

Risk can only *add* scrutiny. It never skips tests and never turns a failing
build into a passing one: the diff-only risk model is a weak signal.
"""

from __future__ import annotations

from app.models.schemas import GateDecision, GateStatus, RiskAssessment, RiskLevel, TestReport, TestStatus


def evaluate_gate(report: TestReport, risk: RiskAssessment | None = None) -> GateDecision:
    reasons: list[str] = []
    if report.status != TestStatus.PASS:
        reasons.append(
            f"Tests {report.status.value}: {report.tests_failed} failed, {report.tests_errored} errored "
            f"of {report.tests_run} run"
        )
        reasons += report.notes
        status = GateStatus.BLOCK
    elif risk is not None and risk.risk_level == RiskLevel.HIGH:
        reasons.append(f"All {report.tests_passed} tests passed, but the change is HIGH risk "
                       f"(score {risk.risk_score:.2f}) - human review required")
        reasons += [f"risk factor: {f}" for f in risk.risk_factors]
        status = GateStatus.REVIEW
    else:
        reasons.append(f"All {report.tests_passed} tests passed")
        if risk is not None:
            reasons.append(f"Risk {risk.risk_level.value} (score {risk.risk_score:.2f})")
        else:
            reasons.append("Risk not assessed (no model)")
        status = GateStatus.PASS
    return GateDecision(
        status=status,
        reasons=reasons,
        risk_level=risk.risk_level if risk else None,
        test_status=report.status,
    )
