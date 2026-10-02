"""Turn a git diff into a structured DiffSummary (input to risk analysis and investigation)."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from app.git.repo import GitRepo
from app.models.schemas import DiffSummary, FileChange

DEPENDENCY_FILES = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "pipfile", "pipfile.lock", "poetry.lock", "environment.yml",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock", "pom.xml", "build.gradle",
}
CONFIG_SUFFIXES = {".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".json"}
CONFIG_NAMES = {"dockerfile", "docker-compose.yml", "makefile", ".gitignore", "pytest.ini", "tox.ini"}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    return (
        "tests" in p.parts
        or "test" in p.parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def is_dependency_file(path: str) -> bool:
    return PurePosixPath(path).name.lower() in DEPENDENCY_FILES


def is_config_file(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    if is_dependency_file(path):
        return False
    return name in CONFIG_NAMES or p.suffix.lower() in CONFIG_SUFFIXES or ".github" in p.parts


def _split_patch_by_file(patch: str) -> dict[str, list[str]]:
    """Map new-path -> lines of that file's section in a unified diff."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            # "diff --git a/x b/y" -> y
            path = line.split(" b/", 1)[-1]
            current = sections.setdefault(path, [])
        if current is not None:
            current.append(line)
    return sections


def changed_line_numbers(file_patch_lines: list[str]) -> tuple[set[int], set[int]]:
    """(old-file line numbers removed, new-file line numbers added) for one file's hunks."""
    old_lines: set[int] = set()
    new_lines: set[int] = set()
    old_no = new_no = 0
    in_hunk = False
    for line in file_patch_lines:
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("-"):
            old_lines.add(old_no)
            old_no += 1
        elif line.startswith("+"):
            new_lines.add(new_no)
            new_no += 1
        else:
            old_no += 1
            new_no += 1
    return old_lines, new_lines


def _enclosing_defs(source: str, lines: set[int]) -> list[str]:
    """Qualified names (Class.method) of the innermost defs containing any of `lines`."""
    if not lines or not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[str] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                span = set(range(start, (child.end_lineno or child.lineno) + 1))
                if span & lines:
                    before = len(found)
                    visit(child, name + ".")
                    if len(found) == before:  # no nested def matched -> this is the innermost
                        found.append(name)

    visit(tree, "")
    return found


def functions_changed(
    file_patch_lines: list[str], old_source: str = "", new_source: str = ""
) -> list[str]:
    """Names of functions/classes touched by a file's hunks.

    For Python sources, changed line numbers are mapped onto the AST of the old
    and new file versions. Otherwise falls back to git's hunk-header context and
    def/class lines that were themselves added or removed.
    """
    names: list[str] = []
    if old_source or new_source:
        old_lines, new_lines = changed_line_numbers(file_patch_lines)
        names += _enclosing_defs(old_source, old_lines)
        names += _enclosing_defs(new_source, new_lines)
    else:
        for line in file_patch_lines:
            m = _HUNK_RE.match(line)
            if m:
                ctx = _DEF_RE.match(m.group(1))
                if ctx:
                    names.append(ctx.group(1))
                continue
            if line[:1] in "+-" and not line.startswith(("+++", "---")):
                d = _DEF_RE.match(line[1:])
                if d:
                    names.append(d.group(1))
    return list(dict.fromkeys(names))


def _safe_show(repo: GitRepo, ref: str, path: str) -> str:
    try:
        return repo.show_file(ref, path)
    except Exception:
        return ""


def extract_diff(
    repo: GitRepo, base: str, head: str = "HEAD", max_ast_files: int = 50
) -> DiffSummary:
    """Structured diff between two refs. Renames are reported as delete + add.

    AST-based function detection is applied to at most `max_ast_files` Python
    files (keeps very large commits cheap); others use the hunk-header fallback.
    """
    base_sha = repo.head_sha(base)
    head_sha = repo.head_sha(head)
    patch = repo.diff_text(base_sha, head_sha)

    change_types: dict[str, str] = {}
    for line in repo.diff_name_status(base_sha, head_sha).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            change_types[parts[-1]] = parts[0][0]

    per_file = _split_patch_by_file(patch)
    files: list[FileChange] = []
    ast_budget = max_ast_files
    for line in repo.diff_numstat(base_sha, head_sha).splitlines():
        added, deleted, path = line.split("\t", 2)
        ctype = change_types.get(path, "M")
        old_src = new_src = ""
        if path.endswith(".py") and ast_budget > 0:
            ast_budget -= 1
            if ctype != "A":
                old_src = _safe_show(repo, base_sha, path)
            if ctype != "D":
                new_src = _safe_show(repo, head_sha, path)
        files.append(
            FileChange(
                path=path,
                change_type=ctype,
                additions=0 if added == "-" else int(added),  # "-" = binary file
                deletions=0 if deleted == "-" else int(deleted),
                functions_changed=functions_changed(per_file.get(path, []), old_src, new_src),
                is_test=is_test_file(path),
                is_dependency=is_dependency_file(path),
                is_config=is_config_file(path),
            )
        )
    return DiffSummary(base_sha=base_sha, head_sha=head_sha, files=files, patch=patch)
