"""Run the local CI simulator for a demo scenario, optionally with self-healing.

Usage:
    python scripts/run_ci.py --scenario simple_bug
    python scripts/run_ci.py --scenario failed_first_repair --heal            # stops at approval
    python scripts/run_ci.py --scenario failed_first_repair --heal --approve  # you approve every plan
    python scripts/run_ci.py --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ci.scenarios import SCENARIOS, get_scenario  # noqa: E402
from app.ci.simulator import create_sandbox, run_pipeline  # noqa: E402
from app.risk.predictor import MODEL_PATH, RiskPredictor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="simple_bug")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--heal", action="store_true", help="on failure, investigate and propose a fix")
    parser.add_argument("--approve", action="store_true",
                        help="with --heal: you approve every proposed plan up front (recorded as 'cli --approve')")
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS.values():
            print(f"{s.id:22} {s.title}\n{'':22} {s.description}")
        return 0

    scenario = get_scenario(args.scenario)
    repo_dir = create_sandbox(scenario)
    predictor = RiskPredictor.load() if MODEL_PATH.exists() else None
    run = run_pipeline(repo_dir, predictor=predictor)

    r = run.test_report
    print(f"Scenario : {scenario.title}")
    print(f"Sandbox  : {repo_dir}")
    print(f"Commit   : {run.commit.sha[:10]} on {run.commit.branch} - {run.commit.message}")
    print(f"Changed  : {', '.join(run.diff.changed_files)} "
          f"(+{run.diff.total_additions}/-{run.diff.total_deletions})")
    for f in run.diff.files:
        print(f"           {f.path}: functions={f.functions_changed} test={f.is_test}")
    if run.risk:
        print(f"Risk     : {run.risk.risk_level.value} (score {run.risk.risk_score:.3f}, {run.risk.model_version})")
        for f in run.risk.risk_factors:
            print(f"           - {f}")
    else:
        print("Risk     : skipped (no trained model; run scripts/train_risk_model.py)")
    print(f"Tests    : {r.status.value}  run={r.tests_run} passed={r.tests_passed} "
          f"failed={r.tests_failed} errors={r.tests_errored} exit={r.exit_code} ({r.duration_seconds}s)")
    for f in r.failures:
        print(f"  FAILED {f.node_id}: {f.message}")
    for n in r.notes:
        print(f"  note: {n}")

    if args.heal and run.failure_event is not None:
        _heal(repo_dir, run, args.approve)

    out = repo_dir.parent / "pipeline_run.json"
    out.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    print(f"Saved    : {out}")
    return 0


def _heal(repo_dir, run, approve: bool) -> None:
    from app.healing.loop import apply_and_verify, decide, propose_fix
    from app.healing.rules import RuleBasedStrategy
    from app.models.schemas import WorkflowState as S
    from app.orchestration.store import Store

    store, strategy = Store(), RuleBasedStrategy()
    wf = store.create_workflow(repo_dir, "cli", run.commit.sha, run.commit.branch)
    if run.risk:
        store.save_artifact(wf, "risk_assessment", run.risk)
        store.transition(wf, S.RISK_ANALYZED, f"Risk = {run.risk.risk_level.value}", agent="risk_model")
    store.save_artifact(wf, "pipeline_run", run)
    store.save_artifact(wf, "failure_event", run.failure_event)
    for state in (S.TESTING, S.FAILED):
        store.transition(wf, state, agent="ci")
    print(f"\nSelf-healing ({strategy.kind}) - workflow {wf}")
    propose_fix(store, wf, strategy)
    while store.state(wf) == S.WAITING_APPROVAL:
        plan = store.latest(wf, "repair_plan")
        print(f"  Plan attempt {plan['attempt']} [{plan['level']}]: {plan['summary']}")
        for c in plan["changes"]:
            print(f"    - {c['file']}: {c['description']}")
        if not approve:
            print("  Waiting for approval. Re-run with --approve, or approve it in the dashboard.")
            return
        decide(store, wf, True, "cli --approve")
        apply_and_verify(store, wf, strategy=strategy)
        last = store.latest(wf, "test_run")
        print(f"  Re-test after attempt {plan['attempt']}: {last['status']} "
              f"({last['tests_passed']} passed, {last['tests_failed']} failed)")
    print(f"  Final state: {store.state(wf).value}")
    if pr := store.latest(wf, "pull_request"):
        print(f"  PR (local, not merged): {pr['url']}")


if __name__ == "__main__":
    raise SystemExit(main())
