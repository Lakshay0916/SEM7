"""Run the agent evaluation: ~40 full self-healing runs, measured from their own records.

Usage (needs the SEM7-agents environment and a Gemini key in .env):
    python scripts/run_evaluation.py                     # the whole matrix, resumes where it stopped
    python scripts/run_evaluation.py --limit 2           # a quick smoke run
    python scripts/run_evaluation.py --report-only       # rebuild summary.json / EVALUATION.md
    python scripts/run_evaluation.py --pause 45          # slower pacing for a tight free tier

Results go to data/eval/: runs.jsonl (one line per run, appended as each finishes), summary.json and
EVALUATION.md. Runs already in runs.jsonl are skipped, so an interrupted session simply continues.
If Gemini reports a DAILY quota error the run is not recorded and the script stops - run it again
after the quota resets (midnight Pacific).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "eval"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{OUT / 'eval.sqlite3'}")  # its own database
os.environ.setdefault("WORKSPACE_DIR", str(OUT / "workspace"))
sys.path.insert(0, str(ROOT))

from app.evaluation.runner import matrix, run_one  # noqa: E402
from app.evaluation.summary import report, summarize  # noqa: E402
from app.orchestration.store import Store  # noqa: E402


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def write_reports(rows: list[dict]) -> None:
    summary = summarize(rows)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    (OUT / "EVALUATION.md").write_text(report(summary, rows))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeats", type=int, default=3, help="repeats per agent configuration (default 3)")
    parser.add_argument("--pause", type=float, default=30, help="seconds between runs, for the free tier's "
                                                                 "per-minute limit (default 30)")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many new runs")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    runs_file = OUT / "runs.jsonl"
    rows = load(runs_file)
    if args.report_only:
        write_reports(rows)
        print(f"report rebuilt from {len(rows)} runs")
        return 0

    done = {r["run_id"] for r in rows}
    todo = [s for s in matrix(args.repeats) if s.run_id not in done]
    print(f"{len(done)} runs done, {len(todo)} to go", flush=True)
    store = Store()
    for i, spec in enumerate(todo[: args.limit or None], 1):
        t = time.monotonic()
        row = run_one(store, spec)
        if row["daily_quota_hit"]:
            print(f"daily Gemini quota reached during {spec.run_id} - not recorded; run again after the reset",
                  flush=True)
            return 2
        with runs_file.open("a") as f:
            f.write(json.dumps(row) + "\n")
        rows.append(row)
        write_reports(rows)
        print(f"[{len(rows)}] {spec.run_id}: {row['final_state']} attempts={row['attempts']} "
              f"agents={row['plans_by_agents']} fallbacks={row['fallbacks']} llm={row['llm_calls']} "
              f"tokens={row['tokens']} ${row['cost_usd']:.4f} {time.monotonic() - t:.0f}s", flush=True)
        if i < len(todo) and spec.group != "rules":
            time.sleep(args.pause)
    print(f"done: {len(rows)} runs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
