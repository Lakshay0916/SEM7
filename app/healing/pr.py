"""Pull-request adapters. PRs are opened only after a validated repair and never merged.

`LocalPRAdapter` writes the PR description to disk - used for the sandbox demo,
whose repositories have no remote. A GitHub adapter (same interface) is planned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.models.schemas import PullRequestRecord


class PRAdapter(Protocol):
    provider: str

    def open(self, repo_dir: Path, head: str, base: str, title: str, body: str) -> PullRequestRecord: ...


class LocalPRAdapter:
    provider = "local"

    def open(self, repo_dir: Path, head: str, base: str, title: str, body: str) -> PullRequestRecord:
        out = Path(repo_dir).parent / f"PR_{head.replace('/', '_')}.md"
        out.write_text(f"# {title}\n\n**{head} → {base}** (not merged)\n\n{body}\n", encoding="utf-8")
        return PullRequestRecord(provider=self.provider, title=title, head_branch=head, base_branch=base,
                                 url=str(out), body=body, merged=False)
