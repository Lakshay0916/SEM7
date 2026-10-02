"""Thin, safe wrapper around the git CLI.

Every command is executed with an argument list (never a shell string).
A repo opened with ``actor="ai"`` refuses to commit on, reset, or push
protected branches (main/master) -- this is the enforcement point for
"AI never modifies main".
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.config import settings
from app.models.schemas import CommitInfo

Actor = Literal["developer", "ai"]


class GitError(RuntimeError):
    pass


class ProtectedBranchError(GitError):
    """Raised when an AI actor tries to write to a protected branch."""


class GitRepo:
    def __init__(self, path: str | Path, actor: Actor = "developer") -> None:
        self.path = Path(path).resolve()
        self.actor = actor

    # ------------------------------------------------------------------ core

    def _run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    @classmethod
    def init(cls, path: str | Path, actor: Actor = "developer", branch: str = "main") -> "GitRepo":
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        repo = cls(path, actor)
        repo._run("init", "-q", "-b", branch)
        # Local identity so commits work in clean environments / CI.
        repo._run("config", "user.name", "SEM7 Developer" if actor == "developer" else "SEM7 AI Agent")
        repo._run("config", "user.email", "sem7@example.local")
        repo._run("config", "commit.gpgsign", "false")
        return repo

    # ---------------------------------------------------------------- queries

    def current_branch(self) -> str:
        # symbolic-ref works on an unborn branch (before the first commit);
        # fall back to rev-parse for a detached HEAD.
        name = self._run("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
        return name or self._run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head_sha(self, ref: str = "HEAD") -> str:
        return self._run("rev-parse", ref).strip()

    def branch_exists(self, name: str) -> bool:
        return self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False) != ""

    def is_clean(self) -> bool:
        return self._run("status", "--porcelain").strip() == ""

    def commit_info(self, ref: str = "HEAD") -> CommitInfo:
        sha, author, ts, message = self._run(
            "log", "-1", "--format=%H%x1f%an%x1f%aI%x1f%B", ref
        ).split("\x1f", 3)
        branch = self.current_branch()
        return CommitInfo(
            sha=sha,
            branch=branch,
            message=message.strip(),
            author=author,
            timestamp=datetime.fromisoformat(ts),
        )

    def log_format(self, ref: str, fmt: str) -> str:
        """`git log -1 --format=<fmt> <ref>` (read-only)."""
        return self._run("log", "-1", f"--format={fmt}", ref).rstrip("\n")

    def merge_base(self, a: str, b: str) -> str:
        return self._run("merge-base", a, b).strip()

    def diff_text(self, base: str, head: str = "HEAD", unified: int = 3) -> str:
        return self._run("diff", f"-U{unified}", "--no-color", "--no-renames", base, head)

    def diff_numstat(self, base: str, head: str = "HEAD") -> str:
        return self._run("diff", "--numstat", "--no-color", "--no-renames", base, head)

    def diff_name_status(self, base: str, head: str = "HEAD") -> str:
        return self._run("diff", "--name-status", "--no-color", "--no-renames", base, head)

    def show_file(self, ref: str, path: str) -> str:
        return self._run("show", f"{ref}:{path}")

    # --------------------------------------------------------------- mutation

    def _guard(self, branch: str | None = None) -> None:
        branch = branch or self.current_branch()
        if self.actor == "ai" and branch in settings.protected_branches:
            raise ProtectedBranchError(f"AI actor may not modify protected branch '{branch}'")

    def create_branch(self, name: str, start_point: str = "HEAD", checkout: bool = True) -> None:
        if self.actor == "ai" and not name.startswith(settings.repair_branch_prefix):
            raise ProtectedBranchError(
                f"AI branches must start with '{settings.repair_branch_prefix}', got '{name}'"
            )
        if self.branch_exists(name):
            raise GitError(f"branch '{name}' already exists")
        self._run("branch", name, start_point)
        if checkout:
            self.checkout(name)

    def checkout(self, name: str) -> None:
        self._run("checkout", "-q", name)

    def commit_all(self, message: str) -> str:
        """Stage all changes and commit. Returns the new commit SHA."""
        self._guard()
        self._run("add", "-A")
        if self.is_clean():
            raise GitError("nothing to commit")
        self._run("commit", "-q", "-m", message)
        return self.head_sha()

    def commit_paths(self, paths: list[str], message: str) -> str:
        """Commit only the given paths (used by the repair agent to avoid touching unrelated files)."""
        self._guard()
        self._run("add", "--", *paths)
        if self._run("diff", "--cached", "--name-only").strip() == "":
            raise GitError("nothing staged to commit")
        self._run("commit", "-q", "-m", message)
        return self.head_sha()

    def reset_hard(self, ref: str) -> None:
        self._guard()
        self._run("reset", "-q", "--hard", ref)

    def push(self, remote: str, branch: str) -> None:
        self._guard(branch)
        self._run("push", remote, branch)
