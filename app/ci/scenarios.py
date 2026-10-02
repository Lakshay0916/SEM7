"""Controlled demo scenarios.

Each scenario describes a developer commit that is applied on a feature branch
of a sandbox copy of a demo project. The edits are exact text replacements so
the resulting diff is small, readable, and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import PROJECT_ROOT

DEMO_PROJECTS_DIR = PROJECT_ROOT / "demo_projects"


@dataclass(frozen=True)
class Edit:
    """Replace `old` with `new` in `file`. With old="" the file is created with `new`."""

    file: str
    old: str
    new: str


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    project: str
    branch: str
    commit_message: str
    edits: list[Edit] = field(default_factory=list)

    @property
    def project_dir(self):
        return DEMO_PROJECTS_DIR / self.project


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in [
        Scenario(
            id="simple_bug",
            title="Scenario 1 - Simple bug",
            description="add() is changed to subtract; test_add fails. Expected: one repair attempt passes.",
            project="calculator",
            branch="feature/refactor-add",
            commit_message="Refactor add() helper",
            edits=[Edit("src/calculator.py", "    return a + b\n", "    return a - b\n")],
        ),
        Scenario(
            id="high_risk_change",
            title="Scenario 2 - Higher-risk change",
            description=(
                "A broad commit: rewrites several functions, adds a dependency manifest, changes "
                "test config and adds a security-related module, with no test changes. Tests still "
                "pass - risk is a signal, not proof of a bug."
            ),
            project="calculator",
            branch="feature/session-tokens",
            commit_message="Add session token helper and rework arithmetic helpers",
            edits=[
                Edit("src/calculator.py", "    return a + b\n", "    result = a + b\n    return result\n"),
                Edit("src/calculator.py", "    return a - b\n", "    result = a - b\n    return result\n"),
                Edit("src/calculator.py", "    return a * b\n", "    result = a * b\n    return result\n"),
                Edit(
                    "src/calculator.py",
                    "    return sum(values) / len(values)\n",
                    "    total = 0\n    for v in values:\n        total += v\n    return total / len(values)\n",
                ),
                Edit("requirements.txt", "", "itsdangerous>=2.1\n"),
                Edit("pytest.ini", "testpaths = tests\n", "testpaths = tests\naddopts = -ra\n"),
                Edit(
                    "src/session_token.py",
                    "",
                    '"""Signed session tokens for calculator API clients."""\n\n'
                    "import hashlib\nimport hmac\n\n\n"
                    "def sign(secret: bytes, user_id: str) -> str:\n"
                    "    mac = hmac.new(secret, user_id.encode(), hashlib.sha256).hexdigest()\n"
                    '    return f"{user_id}.{mac}"\n\n\n'
                    "def verify(secret: bytes, token: str) -> bool:\n"
                    '    user_id, _, mac = token.partition(".")\n'
                    "    return hmac.compare_digest(sign(secret, user_id), token)\n",
                ),
            ],
        ),
        Scenario(
            id="failed_first_repair",
            title="Scenario 3 - Failed first repair",
            description=(
                "divide() is 'simplified' to floor division and its zero check is removed. "
                "Two distinct defects: fixing only the first visible one leaves a failure, "
                "which should trigger reflection and a second attempt."
            ),
            project="calculator",
            branch="feature/simplify-divide",
            commit_message="Simplify divide()",
            edits=[
                Edit(
                    "src/calculator.py",
                    '    if b == 0:\n        raise ValueError("Cannot divide by zero")\n    return a / b\n',
                    "    return a // b\n",
                )
            ],
        ),
    ]
}


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError:
        raise KeyError(f"unknown scenario '{scenario_id}'. Available: {', '.join(SCENARIOS)}") from None
