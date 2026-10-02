"""Mine a labelled commit-risk dataset from public GitHub repositories.

Label  : outcome of the repo's *test* workflow for that commit, judged per job:
         1 = a regular job failed in a non-infrastructure step,
         0 = every regular job passed.
         Jobs that test against unreleased/dev dependencies are ignored (they
         fail independently of the commit); runs that failed only in infra
         steps (checkout, setup, cache, upload) are dropped as ambiguous;
         cancelled / skipped / timed-out / action_required runs are ignored.
Features: computed from the commit's diff against its first parent, plus
         history features built only from CI results that were already known
         (run completed) before this commit's first CI run was created.
"""

from __future__ import annotations

import heapq
import json
import logging
import re
import shutil
import subprocess
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.git.diff import extract_diff
from app.git.repo import GitRepo
from app.risk.features import HistoryContext, build_features
from app.risk.github_api import GitHubClient

log = logging.getLogger(__name__)

LABEL_EVENTS = {"push", "pull_request"}
RECENT_WINDOW = 20
NOISE_JOB_RE = re.compile(
    r"(development versions?|\bdev(el)? deps|nightly|pre-?release|upstream|experimental|allow[- ]?fail)",
    re.I,
)
INFRA_STEP_RE = re.compile(
    r"^(set ?up|checkout|run actions/checkout|cache|restore cache|save cache|upload|download|"
    r"post |complete job|codecov|.*artifact)",
    re.I,
)


@dataclass
class RepoSpec:
    name: str  # owner/repo
    workflows: list[str]  # workflow file names, e.g. ["tests.yaml"]

    @property
    def slug(self) -> str:
        return self.name.replace("/", "__")


@dataclass
class CommitLabel:
    sha: str
    label: int
    created_at: datetime  # first relevant CI run created
    known_at: datetime  # last relevant CI run completed
    is_pr: bool
    n_runs: int


@dataclass
class _Extracted:
    label: CommitLabel
    parent: str
    author: str
    message: str
    is_merge: bool
    files: list[str] = field(default_factory=list)
    diff_feats: dict | None = None
    diff: object | None = None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# 1. CI runs -> labels
# --------------------------------------------------------------------------- #


def fetch_runs(client: GitHubClient, spec: RepoSpec, max_runs: int, cache_dir: Path,
               refresh: bool = False) -> list[dict]:
    cache = cache_dir / f"{spec.slug}_runs.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    keep = ("id", "name", "head_sha", "head_branch", "event", "status", "conclusion",
            "created_at", "updated_at", "run_attempt")
    runs: list[dict] = []
    for wf in spec.workflows:
        raw = client.paginate(f"repos/{spec.name}/actions/workflows/{wf}/runs", "workflow_runs", max_runs)
        runs += [{**{k: r.get(k) for k in keep}, "workflow": wf} for r in raw]
        log.info("%s: fetched %d runs of %s", spec.name, len(raw), wf)

    # Job/step detail is only needed for failed runs (a successful run has no failed jobs).
    failed = [r for r in runs if r["event"] in LABEL_EVENTS and r["conclusion"] == "failure"]
    log.info("%s: fetching job details for %d failed runs", spec.name, len(failed))

    def jobs_for(run: dict) -> None:
        try:
            jobs = client.paginate(f"repos/{spec.name}/actions/runs/{run['id']}/jobs", "jobs", 1000)
        except Exception as exc:
            log.warning("jobs for run %s unavailable: %s", run["id"], exc)
            run["jobs"] = None
            return
        run["jobs"] = [
            {
                "name": j["name"],
                "conclusion": j["conclusion"],
                "failed_steps": [st["name"] for st in j.get("steps") or [] if st.get("conclusion") == "failure"],
            }
            for j in jobs
        ]

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(jobs_for, failed))
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(runs))
    return runs


def run_label(run: dict) -> int | None:
    """1 = genuine failure, 0 = pass, None = unusable/ambiguous."""
    if run["event"] not in LABEL_EVENTS or run["status"] != "completed":
        return None
    if run["conclusion"] == "success":
        return 0
    if run["conclusion"] != "failure" or not run.get("jobs"):
        return None
    failed = [j for j in run["jobs"] if j["conclusion"] == "failure" and not NOISE_JOB_RE.search(j["name"])]
    if not failed:
        return 0  # only allowed-to-fail (dev/nightly) jobs failed; regular jobs passed
    if any(any(not INFRA_STEP_RE.match(st) for st in j["failed_steps"]) for j in failed):
        return 1
    return None  # failed only in infrastructure steps (or no step info)


def aggregate_labels(runs: list[dict]) -> dict[str, CommitLabel]:
    by_sha: dict[str, list[tuple[dict, int]]] = defaultdict(list)
    for r in runs:
        y = run_label(r)
        if y is not None:
            by_sha[r["head_sha"]].append((r, y))
    labels = {}
    for sha, pairs in by_sha.items():
        rs = [r for r, _ in pairs]
        labels[sha] = CommitLabel(
            sha=sha,
            label=int(any(y for _, y in pairs)),
            created_at=min(_ts(r["created_at"]) for r in rs),
            known_at=max(_ts(r["updated_at"]) for r in rs),
            is_pr=any(r["event"] == "pull_request" for r in rs),
            n_runs=len(rs),
        )
    return labels


# --------------------------------------------------------------------------- #
# 2. Local clone with all needed commits
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str, check: bool = True, input: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=input)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _missing(repo_dir: Path, shas: list[str]) -> list[str]:
    out = _git(repo_dir, "cat-file", "--batch-check", input="\n".join(f"{s}^{{commit}}" for s in shas) + "\n")
    return [s for s, line in zip(shas, out.splitlines()) if line.endswith("missing")]


def ensure_clone(spec: RepoSpec, clones_dir: Path, shas: list[str]) -> tuple[Path, list[str]]:
    """Clone the repo (once) and fetch PR heads / specific SHAs. Returns (path, still-missing SHAs)."""
    repo_dir = clones_dir / spec.slug
    if not (repo_dir / ".git").exists():
        clones_dir.mkdir(parents=True, exist_ok=True)
        log.info("%s: cloning", spec.name)
        url = f"https://github.com/{spec.name}.git"
        try:
            _git(clones_dir, "clone", "-q", "--no-tags", url, spec.slug)
        except RuntimeError as exc:  # flaky HTTP/2 streams on large clones: retry over HTTP/1.1
            log.warning("%s: clone failed (%s); retrying over HTTP/1.1", spec.name, exc)
            shutil.rmtree(repo_dir, ignore_errors=True)
            _git(clones_dir, "-c", "http.version=HTTP/1.1", "clone", "-q", "--no-tags", url, spec.slug)
    missing = _missing(repo_dir, shas)
    if missing:
        log.info("%s: %d SHAs missing locally; fetching PR heads", spec.name, len(missing))
        _git(repo_dir, "fetch", "-q", "origin", "+refs/pull/*/head:refs/pull/*/head", check=False)
        missing = _missing(repo_dir, missing)
    for i in range(0, len(missing), 100):  # direct SHA fetch for anything still absent
        _git(repo_dir, "fetch", "-q", "origin", *missing[i : i + 100], check=False)
    return repo_dir, _missing(repo_dir, missing) if missing else []


# --------------------------------------------------------------------------- #
# 3. Features (diff in parallel, history sequentially in time order)
# --------------------------------------------------------------------------- #


def _extract(repo: GitRepo, lab: CommitLabel) -> _Extracted | None:
    try:
        parents, author, subject = repo.log_format(lab.sha, "%P%x1f%ae%x1f%s").split("\x1f", 2)
        parent_list = parents.split()
        if not parent_list:
            return None  # root commit: no diff base
        diff = extract_diff(repo, parent_list[0], lab.sha)
    except Exception as exc:  # corrupt/unreachable objects: skip, but say so
        log.debug("skip %s: %s", lab.sha[:10], exc)
        return None
    return _Extracted(
        label=lab, parent=parent_list[0], author=author.lower(), message=subject,
        is_merge=len(parent_list) > 1, files=diff.changed_files, diff=diff,
    )


def build_rows(spec: RepoSpec, repo_dir: Path, labels: list[CommitLabel], workers: int = 8) -> list[dict]:
    repo = GitRepo(repo_dir)
    with ThreadPoolExecutor(workers) as pool:
        extracted = [e for e in pool.map(lambda l: _extract(repo, l), labels) if e is not None]
    extracted.sort(key=lambda e: e.label.created_at)

    file_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # path -> [changes, failures]
    author_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    known_label: dict[str, int] = {}
    recent: deque[int] = deque(maxlen=RECENT_WINDOW)
    pending: list[tuple[datetime, int, _Extracted]] = []

    rows = []
    for seq, e in enumerate(extracted):
        # Apply every CI result that had completed before this commit's CI started.
        while pending and pending[0][0] <= e.label.created_at:
            _, _, done = heapq.heappop(pending)
            y = done.label.label
            for f in done.files:
                file_stats[f][0] += 1
                file_stats[f][1] += y
            author_stats[done.author][0] += 1
            author_stats[done.author][1] += y
            known_label[done.label.sha] = y
            recent.append(y)

        seen = [file_stats[f] for f in e.files if f in file_stats and file_stats[f][0] > 0]
        rates = [fails / ch for ch, fails in seen]
        a_commits, a_fails = author_stats.get(e.author, (0, 0))
        history = HistoryContext(
            hist_file_max_failure_rate=max(rates, default=0.0),
            hist_file_mean_failure_rate=sum(rates) / len(rates) if rates else 0.0,
            hist_file_mean_prior_changes=(
                sum(file_stats[f][0] if f in file_stats else 0 for f in e.files) / len(e.files)
                if e.files else 0.0
            ),
            author_prior_commits=a_commits,
            author_failure_rate=a_fails / a_commits if a_commits else 0.0,
            parent_known=float(e.parent in known_label),
            parent_failed=float(known_label.get(e.parent, 0)),
            repo_recent_failure_rate=sum(recent) / len(recent) if recent else 0.0,
        )
        feats = build_features(e.diff, e.message, history, is_pull_request=e.label.is_pr, is_merge=e.is_merge)
        rows.append({
            "repo": spec.name,
            "sha": e.label.sha,
            "created_at": e.label.created_at.isoformat(),
            "label": e.label.label,
            "n_runs": e.label.n_runs,
            **feats,
        })
        heapq.heappush(pending, (e.label.known_at, seq, e))
    return rows


def mine_repo(spec: RepoSpec, client: GitHubClient, raw_dir: Path, clones_dir: Path,
              max_runs: int, workers: int = 8, refresh: bool = False) -> tuple[list[dict], dict]:
    runs = fetch_runs(client, spec, max_runs, raw_dir, refresh)
    labels = aggregate_labels(runs)
    repo_dir, missing = ensure_clone(spec, clones_dir, list(labels))
    available = [l for s, l in labels.items() if s not in set(missing)]
    rows = build_rows(spec, repo_dir, available, workers)
    summary = {
        "repo": spec.name,
        "workflows": spec.workflows,
        "runs_fetched": len(runs),
        "labelled_commits": len(labels),
        "missing_locally": len(missing),
        "rows": len(rows),
        "failures": sum(r["label"] for r in rows),
        "failure_rate": round(sum(r["label"] for r in rows) / len(rows), 4) if rows else None,
        "date_range": [rows[0]["created_at"], rows[-1]["created_at"]] if rows else None,
    }
    return rows, summary
