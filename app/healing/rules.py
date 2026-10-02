"""Deterministic, rule-based repair strategy (no LLM).

Evidence used - all of it real: the developer's diff, the failing tests and
their messages, and the healthy baseline on `main`.

Escalation (least invasive first; each level is tried at most once):
  1. operator-restore   - a changed line differs from its original only by an
                          operator (e.g. `+` -> `-`, `/` -> `//`): restore it.
  2. restore-function   - restore each implicated function to its version on
                          the baseline branch (discards the developer's edits there).
If neither applies, the strategy returns no plan and the loop hands over to a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from app.git.repo import GitRepo
from app.healing.code_utils import file_hunks, function_source, function_span, operator_swap
from app.healing.strategy import RepairContext
from app.models.schemas import InvestigationReport, ProposedChange, RepairPlan, RootCauseReport

LEVEL_OPERATOR = "operator-restore"
LEVEL_FUNCTION = "restore-function"


@dataclass
class _Swap:
    file: str
    function: str
    line_no: int
    old: str  # original (baseline) line
    new: str  # line introduced by the commit
    ops: list[tuple[str, str]]


def _short(name: str) -> str:
    return name.split(".")[-1]


class RuleBasedStrategy:
    name = "rule-based"
    kind = "rule-based"

    # ------------------------------------------------------------------ helpers

    def _source_files(self, ctx: RepairContext):
        return [f for f in ctx.failure.diff.files
                if f.path.endswith(".py") and not (f.is_test or f.is_config or f.is_dependency)]

    def _implicated(self, ctx: RepairContext) -> tuple[list[tuple[str, str]], bool]:
        """(file, function) pairs changed by the commit and referenced by failing tests.

        Returns (pairs, matched) where matched=False means no direct reference was found
        and all changed functions are returned as a weaker fallback.
        """
        text = " ".join(
            f"{f.node_id} {f.message} {f.traceback}" for f in ctx.current_report.failures
        ).lower()
        changed = [(f.path, fn) for f in self._source_files(ctx) for fn in f.functions_changed]
        hits = [(p, fn) for p, fn in changed if _short(fn).lower() in text]
        return (hits, True) if hits else (changed, False)

    def _swaps(self, ctx: RepairContext, implicated: list[tuple[str, str]]) -> list[_Swap]:
        repo = GitRepo(ctx.repo_dir)
        out: list[_Swap] = []
        for path in sorted({p for p, _ in implicated}):
            try:
                head_src = repo.show_file(ctx.failure.diff.head_sha, path)
            except Exception:
                continue
            spans = {fn: function_span(head_src, fn) for p, fn in implicated if p == path}
            for hunk in file_hunks(ctx.failure.diff.patch, path):
                for new_no, new_text in hunk.added:
                    fn = next((f for f, sp in spans.items() if sp and sp[0] <= new_no <= sp[1]), None)
                    if fn is None:
                        continue
                    for _, old_text in hunk.removed:
                        ops = operator_swap(old_text, new_text)
                        if ops and len(old_text) - len(old_text.lstrip()) == len(new_text) - len(new_text.lstrip()):
                            out.append(_Swap(path, fn, new_no, old_text, new_text, ops))
                            break
        return out

    def _removed_lines(self, ctx: RepairContext, implicated) -> list[str]:
        """Baseline lines deleted from implicated files that were not simply rewritten."""
        out = []
        for path in sorted({p for p, _ in implicated}):
            for h in file_hunks(ctx.failure.diff.patch, path):
                if len(h.removed) > len(h.added):
                    out += [t.strip() for _, t in h.removed if t.strip()]
        return out

    # ---------------------------------------------------------------- interface

    def investigate(self, ctx: RepairContext) -> InvestigationReport:
        failures = ctx.current_report.failures
        src_files = self._source_files(ctx)
        implicated, matched = self._implicated(ctx)
        when = "after commit" if ctx.attempt == 1 else f"after repair attempt {ctx.attempt - 1} of commit"
        evidence = [f"FACT: test `{f.node_id}` failed: {f.message.splitlines()[0] if f.message else 'no message'}"
                    for f in failures]
        evidence += [f"FACT: commit {ctx.failure.commit_sha[:10]} modified "
                     f"{', '.join(f'`{fn}()`' for fn in f.functions_changed) or 'no functions'} in `{f.path}`"
                     for f in src_files]
        hypotheses = (
            [f"HYPOTHESIS: the change to `{fn}()` in `{p}` caused the failures (its name appears in the failing "
             f"tests/tracebacks)" for p, fn in implicated]
            if matched else
            ["HYPOTHESIS: one of the changed functions caused the failure (no failing test references them by name)"]
        )
        return InvestigationReport(
            failure_summary=f"{len(failures)} test(s) failing {when} {ctx.failure.commit_sha[:10]} "
                            f"on `{ctx.failure.branch}`",
            failed_tests=[f.node_id for f in failures],
            error_messages=[f.message.splitlines()[0] for f in failures if f.message],
            affected_files=[f.path for f in src_files],
            affected_modules=sorted({str(PurePosixPath(f.path).with_suffix("")).replace("/", ".") for f in src_files}),
            recent_changes=[f"{f.path}: +{f.additions}/-{f.deletions}, functions: {', '.join(f.functions_changed) or '-'}"
                            for f in src_files],
            evidence=evidence,
            initial_hypotheses=hypotheses,
        )

    def root_cause(self, ctx: RepairContext, investigation: InvestigationReport) -> RootCauseReport:
        implicated, matched = self._implicated(ctx)
        swaps = self._swaps(ctx, implicated)
        rewritten = {sw.old.strip() for sw in swaps}  # swapped lines were rewritten, not deleted
        removed = [r for r in self._removed_lines(ctx, implicated) if r not in rewritten]
        evidence = [
            f"`{s.file}` line {s.line_no} in `{s.function}()`: `{s.old.strip()}` → `{s.new.strip()}` "
            f"(operator {', '.join(f'{a!r}→{b!r}' for a, b in s.ops)})"
            for s in swaps
        ]
        alternatives = []
        if removed:
            alternatives.append("The commit also deleted line(s) from the implicated code: "
                                + "; ".join(f"`{r}`" for r in removed[:5]))
        if ctx.reflections:
            last = ctx.reflections[-1]
            evidence.append(f"Previous attempt left {len(last.still_failing)} test(s) failing: "
                            + ", ".join(f"`{t}`" for t in last.still_failing))

        funcs = ", ".join(f"`{fn}()`" for _, fn in implicated) or "the changed code"
        if swaps and not ctx.reflections:
            cause = f"The commit changed operator(s) in {funcs}: " + "; ".join(
                f"{a!r}→{b!r}" for s in swaps for a, b in s.ops)
            confidence = 0.8 if matched else 0.5
        elif implicated:
            cause = f"The commit's modifications to {funcs} broke behaviour the tests rely on"
            if removed:
                cause += " (including deleted lines)"
            confidence = 0.65 if matched else 0.35
        else:
            cause, confidence = "Cause could not be determined from the diff and test output", 0.15
        return RootCauseReport(
            summary=investigation.failure_summary,
            root_cause=cause,
            confidence=confidence,
            evidence=evidence + investigation.evidence,
            affected_files=sorted({p for p, _ in implicated}),
            alternative_hypotheses=alternatives,
            reasoning_summary=(
                "Rule-based analysis: failing tests were matched to functions changed by the commit; "
                "the commit's hunks in those functions were compared with the baseline."
            ),
        )

    def plan(self, ctx: RepairContext, investigation: InvestigationReport,
             root_cause: RootCauseReport) -> RepairPlan | None:
        tried = {p.level for p in ctx.previous_plans}
        implicated, _ = self._implicated(ctx)
        branch_note = "Delete the ai/repair branch; main and the developer's branch are untouched."

        if LEVEL_OPERATOR not in tried:
            changes = []
            by_func: dict[tuple[str, str], list[_Swap]] = {}
            for sw in self._swaps(ctx, implicated):
                by_func.setdefault((sw.file, sw.function), []).append(sw)
            for (path, fn), swaps in by_func.items():
                # Edit the whole function text so the snippet is unambiguous even when an
                # identical line exists elsewhere in the file.
                cur_src = (ctx.repo_dir / path).read_text(encoding="utf-8")
                cur_fn = function_source(cur_src, fn)
                if not cur_fn or cur_src.count(cur_fn) != 1:
                    continue
                lines = cur_fn.splitlines(keepends=True)
                for sw in swaps:
                    idx = next((i for i, l in enumerate(lines) if l.rstrip("\n") == sw.new), None)
                    if idx is not None:
                        lines[idx] = sw.old + "\n"
                new_fn = "".join(lines)
                if new_fn != cur_fn:
                    ops = ", ".join(f"{b!r} back to {a!r}" for sw in swaps for a, b in sw.ops)
                    changes.append(ProposedChange(file=path, description=f"Restore operator in `{fn}()`: {ops}",
                                                  original=cur_fn, replacement=new_fn))
            if changes:
                return self._plan(ctx, root_cause, changes, LEVEL_OPERATOR,
                                  "Restore the operator(s) the commit changed (smallest possible fix)",
                                  ["Only operators are restored; any other edit in the commit remains"],
                                  branch_note)

        if LEVEL_FUNCTION not in tried:
            repo = GitRepo(ctx.repo_dir)
            changes = []
            for path, fn in implicated:
                try:
                    base_src = repo.show_file(ctx.failure.diff.base_sha, path)
                except Exception:
                    continue
                cur_src = (ctx.repo_dir / path).read_text(encoding="utf-8")
                base_fn, cur_fn = function_source(base_src, fn), function_source(cur_src, fn)
                if base_fn and cur_fn and base_fn != cur_fn and cur_src.count(cur_fn) == 1:
                    changes.append(ProposedChange(
                        file=path,
                        description=f"Restore `{fn}()` to its version on `{ctx.base_branch}`",
                        original=cur_fn,
                        replacement=base_fn,
                    ))
            if changes:
                return self._plan(ctx, root_cause, changes, LEVEL_FUNCTION,
                                  "Restore the implicated function(s) to the healthy baseline",
                                  ["Discards all of the developer's edits inside the restored function(s)",
                                   "Intended behaviour change in the commit (if any) is lost and must be redone"],
                                  branch_note)
        return None

    def _plan(self, ctx, root_cause, changes, level, summary, risks, rollback) -> RepairPlan:
        return RepairPlan(
            summary=summary,
            root_cause=root_cause.root_cause,
            changes=changes,
            tests=[*ctx.failing_tests, "full test suite (regression check)"],
            risks=risks,
            rollback_plan=rollback,
            strategy=self.name,
            strategy_kind=self.kind,
            level=level,
            attempt=ctx.attempt,
        )
