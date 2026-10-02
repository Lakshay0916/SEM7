"""Typed data contracts passed between pipeline stages and agents.

Agents exchange these models instead of free text, so every hand-off is
validated and every claim (evidence, test result, retrieved document) has a
place where its source is recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Workflow
# --------------------------------------------------------------------------- #


class WorkflowState(str, Enum):
    RECEIVED = "RECEIVED"
    RISK_ANALYZED = "RISK_ANALYZED"
    TESTING = "TESTING"
    FAILED = "FAILED"
    INVESTIGATING = "INVESTIGATING"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    SOLUTION_PROPOSED = "SOLUTION_PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REPAIRING = "REPAIRING"
    TESTING_REPAIR = "TESTING_REPAIR"
    PASSED = "PASSED"
    REFLECTING = "REFLECTING"
    PR_CREATED = "PR_CREATED"
    ABORTED = "ABORTED"


# --------------------------------------------------------------------------- #
# Git / diff
# --------------------------------------------------------------------------- #


class FileChange(BaseModel):
    path: str
    change_type: str = Field(description="A (added), M (modified), D (deleted), R (renamed)")
    additions: int = 0
    deletions: int = 0
    functions_changed: list[str] = Field(default_factory=list)
    is_test: bool = False
    is_dependency: bool = False
    is_config: bool = False


class DiffSummary(BaseModel):
    base_sha: str
    head_sha: str
    files: list[FileChange] = Field(default_factory=list)
    patch: str = Field(default="", description="Raw unified diff text")

    @property
    def changed_files(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)


class CommitInfo(BaseModel):
    sha: str
    branch: str
    message: str
    author: str
    timestamp: datetime


# --------------------------------------------------------------------------- #
# Testing / CI
# --------------------------------------------------------------------------- #


class TestStatus(str, Enum):
    __test__ = False  # dunder names are not Enum members; stops pytest collection

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"  # tests could not be collected/run, or results unparseable
    TIMEOUT = "TIMEOUT"


class TestCaseResult(BaseModel):
    __test__ = False  # stop pytest trying to collect this class

    node_id: str
    outcome: str = Field(description="passed | failed | error | skipped")
    message: str = ""
    traceback: str = ""
    duration_seconds: float = 0.0


class TestReport(BaseModel):
    __test__ = False

    status: TestStatus
    command: str
    exit_code: int | None
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errored: int = 0
    tests_skipped: int = 0
    duration_seconds: float = 0.0
    failures: list[TestCaseResult] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == TestStatus.PASS


class FailureEvent(BaseModel):
    commit_sha: str
    branch: str
    pipeline_id: str
    status: str = "failed"
    failed_tests: list[str] = Field(default_factory=list)
    logs: str = ""
    changed_files: list[str] = Field(default_factory=list)
    test_report: TestReport
    diff: DiffSummary
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskAssessment(BaseModel):
    commit_sha: str
    risk_score: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    risk_factors: list[str] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)
    model_version: str


class PipelineRun(BaseModel):
    pipeline_id: str
    commit: CommitInfo
    diff: DiffSummary
    risk: RiskAssessment | None = None
    test_report: TestReport
    failure_event: FailureEvent | None = None


# --------------------------------------------------------------------------- #
# Agent outputs
# --------------------------------------------------------------------------- #


class InvestigationReport(BaseModel):
    failure_summary: str
    failed_tests: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    recent_changes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, description="Facts taken directly from logs/diff")
    initial_hypotheses: list[str] = Field(default_factory=list, description="Guesses, not facts")


class RetrievedChunk(BaseModel):
    source: str
    start_line: int | None = None
    end_line: int | None = None
    text: str
    score: float


class RAGContext(BaseModel):
    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    retrieval_succeeded: bool = True
    error: str | None = None


class RootCauseReport(BaseModel):
    summary: str
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    alternative_hypotheses: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""


class ProposedChange(BaseModel):
    file: str
    description: str
    original: str = Field(description="Exact text to replace (must exist in the file)")
    replacement: str


class RepairPlan(BaseModel):
    summary: str
    root_cause: str
    changes: list[ProposedChange] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    rollback_plan: str = ""
    strategy: str = Field(default="", description="Which RepairStrategy produced this plan")
    strategy_kind: str = Field(default="", description="rule-based | llm-agent")
    level: str = Field(default="", description="Strategy-specific escalation level, e.g. operator-restore")
    attempt: int = 1


# --------------------------------------------------------------------------- #
# Self-healing loop
# --------------------------------------------------------------------------- #


class ApprovalDecision(BaseModel):
    approved: bool
    reviewer: str
    comment: str = ""
    plan_attempt: int
    decided_at: datetime = Field(default_factory=utcnow)


class RepairAttempt(BaseModel):
    attempt: int
    branch: str
    base_commit: str = Field(description="Commit the attempt was applied on top of")
    commit_sha: str
    changed_files: list[str] = Field(default_factory=list)
    patch: str = ""


class Reflection(BaseModel):
    attempt: int
    still_failing: list[str] = Field(default_factory=list)
    newly_fixed: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list, description="Tests that passed before but fail now")
    summary: str
    will_retry: bool


class PullRequestRecord(BaseModel):
    provider: str = Field(description="local | github")
    title: str
    head_branch: str
    base_branch: str
    url: str = Field(description="Web URL, or path to the PR description for the local adapter")
    body: str = ""
    merged: bool = False  # never merged automatically


class GateStatus(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"  # tests passed but a human should look (e.g. HIGH risk)
    BLOCK = "BLOCK"  # tests failed / errored / timed out


class GateDecision(BaseModel):
    status: GateStatus
    reasons: list[str] = Field(default_factory=list)
    risk_level: RiskLevel | None = None
    test_status: TestStatus
