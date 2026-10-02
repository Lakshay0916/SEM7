"""Small, deterministic code-analysis helpers used by the rule-based strategy."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class Hunk:
    removed: list[tuple[int, str]] = field(default_factory=list)  # (old line no, text)
    added: list[tuple[int, str]] = field(default_factory=list)  # (new line no, text)


def file_hunks(patch: str, path: str) -> list[Hunk]:
    """Hunks of one file from a unified diff."""
    hunks: list[Hunk] = []
    in_file = False
    old_no = new_no = 0
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_file = line.split(" b/", 1)[-1] == path
            continue
        if not in_file:
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            hunks.append(Hunk())
            continue
        if not hunks or line.startswith(("---", "+++", "\\")):
            continue
        if line.startswith("-"):
            hunks[-1].removed.append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            hunks[-1].added.append((new_no, line[1:]))
            new_no += 1
        else:
            old_no += 1
            new_no += 1
    return hunks


def _tokens(line: str) -> list[tokenize.TokenInfo] | None:
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return [t for t in toks if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER)]


def operator_swap(old_line: str, new_line: str) -> list[tuple[str, str]] | None:
    """If two lines differ only in operator tokens, return the (old, new) operator pairs."""
    if old_line.strip() == new_line.strip():
        return None
    a, b = _tokens(old_line), _tokens(new_line)
    if a is None or b is None or len(a) != len(b):
        return None
    pairs = []
    for ta, tb in zip(a, b):
        if ta.string == tb.string:
            continue
        if ta.type == tokenize.OP and tb.type == tokenize.OP:
            pairs.append((ta.string, tb.string))
        else:
            return None
    return pairs or None


def function_span(source: str, qualname: str) -> tuple[int, int] | None:
    """1-based inclusive (start, end) line span of a def/class (decorators included)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    parts = qualname.split(".")

    def find(body, idx):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == parts[idx]:
                if idx == len(parts) - 1:
                    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                    return start, node.end_lineno or node.lineno
                return find(node.body, idx + 1)
        return None

    return find(tree.body, 0)


def function_source(source: str, qualname: str) -> str | None:
    span = function_span(source, qualname)
    if span is None:
        return None
    lines = source.splitlines(keepends=True)
    return "".join(lines[span[0] - 1 : span[1]])
