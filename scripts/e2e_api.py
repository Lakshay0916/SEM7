"""End-to-end check of a running stack, through the HTTP API only (stdlib, no extra deps).

Runs scenario 3 and approves each proposed plan until a PR is opened:
break → investigate → approve → repair → re-test FAIL → reflect → approve → re-test PASS → PR.

Usage:
    python scripts/e2e_api.py --url http://localhost:8000 [--expect-db postgresql] [--ui http://localhost:8502]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, body: dict | None = None, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_healthy(url: str, seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)
            return
        except (urllib.error.URLError, ConnectionError):
            if time.time() > deadline:
                raise SystemExit(f"FAIL: {url} not healthy after {seconds}s")
            time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--ui", help="also check the UI health endpoint at this base URL")
    ap.add_argument("--expect-db", help="fail unless the API reports this database backend")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    wait_healthy(base + "/health")
    health = call(base, "GET", "/health")
    print(f"API healthy: database={health['database']} risk_model={health['risk_model']}")
    if args.expect_db and health["database"] != args.expect_db:
        print(f"FAIL: expected database {args.expect_db}, got {health['database']}")
        return 1
    if args.ui:
        wait_healthy(args.ui.rstrip("/") + "/_stcore/health")
        print("UI healthy")

    wf = call(base, "POST", "/workflows", {"scenario": "failed_first_repair"})
    wf_id = wf["workflow_id"]
    print(f"Workflow {wf_id}: {wf['state']}")
    for _ in range(3):
        if wf["state"] != "WAITING_APPROVAL":
            break
        wf = call(base, "POST", f"/workflows/{wf_id}/approve", {"reviewer": "e2e-check"})
        print(f"  approved → {wf['state']} (attempt {wf['attempt']})")

    trace = call(base, "GET", f"/workflows/{wf_id}")
    tests = [a["payload"]["status"] for a in trace["artifacts"] if a["kind"] == "test_run"]
    print(f"Re-test results per attempt: {tests}")
    if wf["state"] != "PR_CREATED" or tests[-1:] != ["PASS"]:
        print(f"FAIL: expected PR_CREATED with a passing re-test, got {wf['state']} {tests}")
        return 1
    blocked = call(base, "POST", f"/workflows/{wf_id}/safety-check")
    if not (blocked["blocked"] and blocked["main_unchanged"]):
        print("FAIL: AI commit to main was not blocked")
        return 1
    print("OK: self-healing loop completed end-to-end; AI write to main blocked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
