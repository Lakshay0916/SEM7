"""Mine a labelled commit-risk dataset from public GitHub repos' CI history.

Usage:
    python scripts/mine_ci_data.py                       # all repos in data/risk/repos.json
    python scripts/mine_ci_data.py --only pallets/flask --max-runs 300

Outputs:
    data/risk/raw/<owner>__<repo>_runs.json   cached Actions run metadata
    data/risk/rows/<owner>__<repo>.csv        per-repo rows (re-mining one repo keeps the others)
    data/risk/commits.csv                     all per-repo rows combined
    data/risk/mining_summary.json             per-repo counts
Clones go to workspace/mining/ (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.risk.github_api import GitHubClient  # noqa: E402
from app.risk.mining import RepoSpec, mine_repo  # noqa: E402

DATA = PROJECT_ROOT / "data" / "risk"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", default=str(DATA / "repos.json"))
    ap.add_argument("--only", nargs="*", help="subset of owner/repo names")
    ap.add_argument("--max-runs", type=int, default=1500, help="max runs per workflow")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--refresh", action="store_true", help="re-download run metadata")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    specs = [RepoSpec(**r) for r in json.loads(Path(args.repos).read_text())]
    if args.only:
        specs = [s for s in specs if s.name in args.only]
    client = GitHubClient()
    logging.info("GitHub client: %r", client)

    summaries = []
    for spec in specs:
        t0 = time.time()
        try:
            rows, summary = mine_repo(spec, client, DATA / "raw", settings.workspace_dir / "mining",
                                      args.max_runs, args.workers, args.refresh)
        except Exception as exc:
            logging.error("%s failed: %s", spec.name, exc)
            summaries.append({"repo": spec.name, "error": str(exc)})
            continue
        summary["seconds"] = round(time.time() - t0, 1)
        logging.info("%s", summary)
        (DATA / "rows").mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(DATA / "rows" / f"{spec.slug}.csv", index=False)
        summaries.append(summary)

    # Merge with summaries of repos not mined in this invocation.
    summary_path = DATA / "mining_summary.json"
    previous = json.loads(summary_path.read_text()) if summary_path.exists() else []
    mined = {s["repo"] for s in summaries}
    summaries = [s for s in previous if s["repo"] not in mined] + summaries
    summary_path.write_text(json.dumps(sorted(summaries, key=lambda s: s["repo"]), indent=2))

    parts = [pd.read_csv(f) for f in sorted((DATA / "rows").glob("*.csv"))]
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df.to_csv(DATA / "commits.csv", index=False)
    pos = int(df["label"].sum()) if len(df) else 0
    print(f"\nRows: {len(df)}  failures: {pos}  ({pos / max(len(df), 1):.1%})  -> {DATA / 'commits.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
