"""Live demonstration that the protected-branch guard holds."""

from __future__ import annotations

import subprocess

from app.git.repo import GitRepo, ProtectedBranchError


def try_ai_commit_to_main(repo_path: str) -> dict:
    """Attempt an AI-actor commit on main in a sandbox; report whether it was blocked."""
    ai = GitRepo(repo_path, actor="ai")
    original = ai.current_branch()
    probe = ai.path / "AI_WAS_HERE.txt"
    main_before = ai.head_sha("main")
    blocked, message = False, "commit succeeded"
    try:
        ai.checkout("main")
        probe.write_text("unauthorised change\n", encoding="utf-8")
        ai.commit_all("AI tries to modify main")
    except ProtectedBranchError as exc:
        blocked, message = True, f"ProtectedBranchError: {exc}"
    finally:
        probe.unlink(missing_ok=True)
        subprocess.run(["git", "checkout", "-q", original], cwd=ai.path, check=False)
    return {
        "blocked": blocked,
        "message": message,
        "main_unchanged": ai.head_sha("main") == main_before,
        "main_sha": main_before,
    }
