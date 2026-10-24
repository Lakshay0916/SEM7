"""Structured outputs of the LLM agents (Pydantic), plus per-call monitoring records.

The analysts (ADK) return the pipeline's existing `InvestigationReport` / `RootCauseReport`.
The planner crew (CrewAI) returns a `PlanDraft`: it names whole functions and writes their new
source; the exact text being replaced is filled in by code from the repository, never typed by
the model, so a patch can't fail on a miscopied character.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FunctionEdit(BaseModel):
    file: str = Field(description="Repository-relative path of the source file, e.g. src/calculator.py")
    function: str = Field(description="Name of the function to replace, e.g. divide")
    new_source: str = Field(description="Complete new source of that function, from its `def` line, "
                                        "with the original indentation")
    description: str = Field(description="One sentence: what the edit changes and why")


class PlanDraft(BaseModel):
    summary: str = Field(description="One-line summary of the fix")
    root_cause: str = Field(description="The root cause this plan addresses")
    edits: list[FunctionEdit] = Field(description="Smallest set of function replacements that fixes the failures")
    risks: list[str] = Field(default_factory=list, description="What could go wrong with this fix")


class PlanReview(BaseModel):
    approved: bool = Field(description="True if the draft is minimal, grounded in the evidence and complete")
    issues: list[str] = Field(default_factory=list, description="Problems found in the draft (empty if none)")
    final_plan: PlanDraft = Field(description="The draft itself if approved, otherwise a corrected plan")


class AgentRecord(BaseModel):
    """One agent step, for monitoring/logging (written to the workflow's event log)."""

    agent: str
    framework: str  # adk | crewai | system
    model: str = ""
    status: str = "ok"  # ok | retry | error | fallback | blocked
    seconds: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    message: str = ""
