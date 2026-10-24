"""Output guardrails for agent plans, and conversion to the pipeline's `RepairPlan`.

`check_draft` runs as a CrewAI task guardrail (the crew retries with the problems as feedback)
and again before the plan leaves the strategy. The pipeline's own `validate_plan` and the
protected-branch guard still apply afterwards, so a bad plan has to pass three layers.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.agents.models import PlanDraft
from app.git.diff import is_config_file, is_dependency_file, is_test_file
from app.healing.code_utils import function_source
from app.healing.strategy import RepairContext
from app.models.schemas import ProposedChange, RepairPlan, RootCauseReport


def check_draft(draft: PlanDraft, repo_dir: Path) -> list[str]:
    """Problems that make the draft unusable (empty list = OK)."""
    problems: list[str] = []
    if not draft.edits:
        return ["the plan has no edits"]
    root = repo_dir.resolve()
    for e in draft.edits:
        target = (root / e.file).resolve()
        where = f"{e.file}::{e.function}"
        if not target.is_relative_to(root) or not target.is_file():
            problems.append(f"{e.file}: file does not exist in the repository")
            continue
        if is_test_file(e.file) or is_config_file(e.file) or is_dependency_file(e.file):
            problems.append(f"{e.file}: fixes may only edit source code, not tests/config/dependencies")
            continue
        current = function_source(target.read_text(encoding="utf-8"), e.function)
        if current is None:
            problems.append(f"{where}: function not found in the current file")
            continue
        try:
            tree = ast.parse(_dedent(e.new_source))
        except SyntaxError as exc:
            problems.append(f"{where}: new_source is not valid Python ({exc.msg}, line {exc.lineno})")
            continue
        defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(defs) != 1 or defs[0].name != e.function.split(".")[-1]:
            problems.append(f"{where}: new_source must define exactly the function `{e.function}`")
        elif _normalise(e.new_source) == _normalise(current):
            problems.append(f"{where}: new_source is identical to the current code")
    return problems


def to_repair_plan(draft: PlanDraft, ctx: RepairContext, root_cause: RootCauseReport,
                   strategy: str, kind: str) -> RepairPlan:
    changes = []
    for e in draft.edits:
        current = function_source((ctx.repo_dir / e.file).read_text(encoding="utf-8"), e.function)
        changes.append(ProposedChange(file=e.file, description=e.description, original=current,
                                      replacement=_reindent(e.new_source, current)))
    return RepairPlan(
        summary=draft.summary,
        root_cause=draft.root_cause or root_cause.root_cause,
        changes=changes,
        tests=[*ctx.failing_tests, "full test suite (regression check)"],
        risks=draft.risks or ["LLM-written change: review the diff before approving"],
        rollback_plan="Delete the ai/repair branch; main and the developer's branch are untouched.",
        strategy=strategy,
        strategy_kind=kind,
        level=f"llm-attempt-{ctx.attempt}",
        attempt=ctx.attempt,
    )


# --------------------------------------------------------------------------- helpers


def _dedent(src: str) -> str:
    lines = src.strip("\n").splitlines()
    indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return "\n".join(line[indent:] for line in lines) + "\n"


def _normalise(src: str) -> str:
    return "\n".join(line.rstrip() for line in _dedent(src).splitlines())


def _reindent(new_src: str, current: str) -> str:
    """Give the model's function the same indentation and trailing newline as the code it replaces."""
    indent = current[: len(current) - len(current.lstrip())]
    body = "".join(f"{indent}{line}\n" if line.strip() else "\n" for line in _dedent(new_src).splitlines())
    return body if current.endswith("\n") else body.rstrip("\n")
