"""Export a Markdown report of a recorded workflow.

Usage:
    python scripts/export_report.py --latest
    python scripts/export_report.py --workflow wf-1234abcd [--out report.md]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.orchestration.store import Store  # noqa: E402
from app.reporting.markdown import render_workflow_report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--workflow")
    g.add_argument("--latest", action="store_true")
    ap.add_argument("--out", help="output path (default: workspace/reports/<id>.md)")
    args = ap.parse_args()

    store = Store()
    if args.latest:
        wfs = store.list_workflows(limit=1)
        if not wfs:
            print("No workflows recorded yet.")
            return 1
        wf_id = wfs[0]["id"]
    else:
        wf_id = args.workflow
    md = render_workflow_report(store.trace(wf_id))
    out = Path(args.out) if args.out else settings.workspace_dir / "reports" / f"{wf_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"Saved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
