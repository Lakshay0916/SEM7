"""Run the local CI simulator for a demo scenario.

Usage:
    python scripts/run_ci.py --scenario simple_bug
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

    out = repo_dir.parent / "pipeline_run.json"
    out.write_text(run.model_dump_json(indent=2))
    print(f"Saved    : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
