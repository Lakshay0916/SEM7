import pytest

from app.git.repo import GitError, GitRepo, ProtectedBranchError


def test_developer_can_commit_on_main(git_project):
    assert git_project.current_branch() == "main"
    assert len(git_project.head_sha()) == 40


def test_ai_cannot_commit_on_main(git_project):
    ai = GitRepo(git_project.path, actor="ai")
    (ai.path / "src/mod.py").write_text("def f():\n    return 2\n")
    with pytest.raises(ProtectedBranchError):
        ai.commit_all("sneaky")
    with pytest.raises(ProtectedBranchError):
        ai.commit_paths(["src/mod.py"], "sneaky")
    with pytest.raises(ProtectedBranchError):
        ai.reset_hard("HEAD")
    with pytest.raises(ProtectedBranchError):
        ai.push("origin", "main")


def test_main_unchanged_after_blocked_ai_commit(git_project):
    before = git_project.head_sha("main")
    ai = GitRepo(git_project.path, actor="ai")
    (ai.path / "src/mod.py").write_text("x = 1\n")
    with pytest.raises(ProtectedBranchError):
        ai.commit_all("sneaky")
    assert git_project.head_sha("main") == before


def test_ai_branch_requires_repair_prefix(git_project):
    ai = GitRepo(git_project.path, actor="ai")
    with pytest.raises(ProtectedBranchError):
        ai.create_branch("feature/whatever")
    ai.create_branch("ai/repair/issue-1")
    assert ai.current_branch() == "ai/repair/issue-1"


def test_ai_can_commit_on_repair_branch_only_listed_paths(git_project):
    ai = GitRepo(git_project.path, actor="ai")
    ai.create_branch("ai/repair/issue-2")
    (ai.path / "src/mod.py").write_text("def f():\n    return 1  # fixed\n")
    (ai.path / "unrelated.txt").write_text("should not be committed\n")
    ai.commit_paths(["src/mod.py"], "repair")
    committed = ai.diff_name_status("main", "HEAD").split()
    assert "src/mod.py" in committed
    assert "unrelated.txt" not in committed


def test_duplicate_branch_rejected(git_project):
    git_project.create_branch("feature/x")
    git_project.checkout("main")
    with pytest.raises(GitError):
        git_project.create_branch("feature/x")
