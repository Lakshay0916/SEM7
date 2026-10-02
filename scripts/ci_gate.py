"""Risk-aware quality gate for a real repository checkout (used by GitHub Actions).

Steps: diff (base..head) -> ML risk score -> run the real test suite -> gate decision.
Policy: tests always run; FAIL/ERROR/TIMEOUT => BLOCK (exit 1); HIGH risk with passing
tests => REVIEW (exit 0 with a warning, or exit 1 with --fail-on-review); else PASS.

Usage:
    python scripts/ci_gate.py --base origin/main
    python scripts/ci_gate.py --base HEAD~1 --summary-file "$GITHUB_STEP_SUMMARY" --json gate.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ci.quality_gate import evaluate_gate  # noqa: E402
from app.ci.test_runner import run_tests  # noqa: E402
from app.git.diff import extract_diff  # noqa: E402
from app.git.repo import GitError, GitRepo  # noqa: E402
from app.models.schemas import GateStatus  # noqa: E402
from app.reporting.markdown import render_gate_summary  # noqa: E402
from app.risk.predictor import MODEL_PATH, RiskPredictor  # noqa: E402

ZERO_SHA = "0" * 40


def resolve_base(repo: GitRepo, base: str | None) -> str:
    """Pick a usable diff base: the given ref, else HEAD~1, else HEAD (first commit)."""
    for candidate in ([base] if base and base != ZERO_SHA else []) + ["HEAD~1"]:
        try:
            return repo.merge_base(candidate, "HEAD")
        except GitError:
            continue
    return repo.head_sha()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository to check (default: current directory)")
    ap.add_argument("--base", help="base ref/sha to diff against (e.g. origin/main, github.event.before)")
    ap.add_argument("--pull-request", action="store_true", help="score the change in pull-request context")
    ap.add_argument("--timeout", type=int, default=900, help="test timeout in seconds")
    ap.add_argument("--summary-file", help="append the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--json", help="write the full machine-readable result here")
    ap.add_argument("--fail-on-review", action="store_true", help="treat REVIEW as a failing gate")
    args = ap.parse_args()

    repo = GitRepo(args.repo)
    commit = repo.commit_info()
    base = resolve_base(repo, args.base)
    diff = extract_diff(repo, base, "HEAD")

    risk = None
    if MODEL_PATH.exists():
        risk = RiskPredictor.load().assess(diff, commit.message, commit_sha=commit.sha,
                                           is_pull_request=args.pull_request)
    report = run_tests(repo.path, timeout=args.timeout)
    gate = evaluate_gate(report, risk)

    summary = render_gate_summary(commit, diff, risk, report, gate)
    print(summary)
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as fh:
            fh.write(summary)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "commit": commit.model_dump(mode="json"),
            "base": base,
            "diff": diff.model_dump(mode="json", exclude={"patch"}),
            "risk": risk.model_dump(mode="json") if risk else None,
            "tests": report.model_dump(mode="json", exclude={"stdout", "stderr"}),
            "gate": gate.model_dump(mode="json"),
        }, indent=2), encoding="utf-8")

    # GitHub Actions annotations (plain text elsewhere)
    if gate.status == GateStatus.BLOCK:
        print(f"::error title=Quality gate BLOCK::{gate.reasons[0]}")
        return 1
    if gate.status == GateStatus.REVIEW:
        print(f"::warning title=Quality gate REVIEW::{gate.reasons[0]}")
        return 1 if args.fail_on_review else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
