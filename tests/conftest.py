import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.git.repo import GitRepo  # noqa: E402


@pytest.fixture
def make_project(tmp_path):
    """Create a tiny pytest project from {relative_path: content}."""

    def _make(files: dict[str, str], name: str = "proj") -> Path:
        root = tmp_path / name
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        (root / "pytest.ini").write_text("[pytest]\npythonpath = src\ntestpaths = tests\n")
        return root

    return _make


@pytest.fixture
def git_project(make_project):
    """A git repo with a healthy commit on main."""
    root = make_project({
        "src/mod.py": "def f():\n    return 1\n",
        "tests/test_mod.py": "from mod import f\n\ndef test_f():\n    assert f() == 1\n",
    })
    repo = GitRepo.init(root)
    repo.commit_all("init")
    return repo
