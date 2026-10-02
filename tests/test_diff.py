from app.git.diff import (
    changed_line_numbers,
    extract_diff,
    functions_changed,
    is_config_file,
    is_dependency_file,
    is_test_file,
)


def test_file_classification():
    assert is_test_file("tests/test_auth.py")
    assert is_test_file("pkg/foo_test.py")
    assert not is_test_file("src/auth.py")
    assert is_dependency_file("requirements.txt")
    assert is_dependency_file("frontend/package.json")
    assert is_config_file(".github/workflows/ci.yml")
    assert is_config_file("Dockerfile")
    assert not is_config_file("requirements.txt")  # dependency, not config


def test_changed_line_numbers():
    patch = [
        "@@ -5,3 +5,3 @@",
        " a",
        "-b",
        "+B",
        " c",
    ]
    assert changed_line_numbers(patch) == ({6}, {6})


def test_functions_changed_uses_ast():
    old = "class C:\n    def m(self):\n        return 1\n\ndef g():\n    return 2\n"
    new = "class C:\n    def m(self):\n        return 10\n\ndef g():\n    return 2\n"
    patch = ["@@ -3 +3 @@", "-        return 1", "+        return 10"]
    assert functions_changed(patch, old, new) == ["C.m"]


def test_extract_diff_on_real_commit(git_project):
    repo = git_project
    repo.create_branch("feature/x")
    (repo.path / "src/mod.py").write_text("def f():\n    return 2\n\ndef h():\n    return 3\n")
    (repo.path / "requirements.txt").write_text("requests\n")
    (repo.path / "tests/test_mod.py").write_text(
        "from mod import f\n\ndef test_f():\n    assert f() == 2\n"
    )
    repo.commit_all("change")

    diff = extract_diff(repo, "main", "HEAD")
    by_path = {f.path: f for f in diff.files}
    assert set(by_path) == {"src/mod.py", "requirements.txt", "tests/test_mod.py"}
    assert by_path["src/mod.py"].functions_changed == ["f", "h"]
    assert by_path["src/mod.py"].additions == 4 and by_path["src/mod.py"].deletions == 1
    assert by_path["requirements.txt"].change_type == "A"
    assert by_path["requirements.txt"].is_dependency
    assert by_path["tests/test_mod.py"].is_test
    assert "return 2" in diff.patch
