"""Run a project's pytest suite for real and turn the outcome into a TestReport.

Status is derived from evidence, conservatively:

* TIMEOUT  - the process exceeded the time limit (never PASS)
* ERROR    - no JUnit report, zero tests ran, collection errors, or the exit
             code contradicts the parsed results (never PASS)
* FAIL     - tests ran and at least one failed
* PASS     - exit code 0 AND >=1 test ran AND 0 failures AND 0 errors
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from app.config import settings
from app.models.schemas import TestCaseResult, TestReport, TestStatus

MAX_CAPTURE_CHARS = 20_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURE_CHARS:
        return text
    return text[:MAX_CAPTURE_CHARS] + f"\n... [truncated {len(text) - MAX_CAPTURE_CHARS} chars]"


def _node_id(case: ET.Element) -> str:
    """Rebuild a pytest node id (path::Class::test) from an xunit1 testcase element."""
    name = case.get("name", "")
    classname = case.get("classname", "")
    file = case.get("file")
    if not file:
        return f"{classname}::{name}" if classname else name
    file = file.replace("\\", "/")  # pytest reports OS-native separators; node ids are always POSIX
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file
    cls = classname[len(module) + 1 :] if classname.startswith(module + ".") else ""
    return "::".join(p for p in (file, *cls.split("."), name) if p)


def parse_junit(xml_path: Path) -> list[TestCaseResult]:
    root = ET.parse(xml_path).getroot()
    results: list[TestCaseResult] = []
    for case in root.iter("testcase"):
        outcome, message, tb = "passed", "", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = el.get("message", "")
                tb = (el.text or "").strip()
                break
        results.append(
            TestCaseResult(
                node_id=_node_id(case),
                outcome=outcome,
                message=message,
                traceback=tb,
                duration_seconds=float(case.get("time", 0) or 0),
            )
        )
    return results


def run_tests(
    project_dir: str | Path,
    targets: list[str] | None = None,
    timeout: int | None = None,
) -> TestReport:
    project_dir = Path(project_dir).resolve()
    timeout = timeout or settings.test_timeout_seconds
    targets = targets or []

    # Targets are passed as argv entries (no shell); still reject anything
    # that looks like an option or escapes the project directory.
    for t in targets:
        path_part = t.split("::", 1)[0]
        if t.startswith("-") or not (project_dir / path_part).resolve().is_relative_to(project_dir):
            raise ValueError(f"invalid test target: {t!r}")

    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "junit.xml"
        cmd = [
            sys.executable, "-m", "pytest",
            "-q", "-p", "no:cacheprovider",
            f"--junitxml={xml_path}", "-o", "junit_family=xunit1",
            *targets,
        ]
        display_cmd = shlex.join(["pytest", "-q", *targets])
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=project_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, env=env
            )
        except subprocess.TimeoutExpired as exc:
            return TestReport(
                status=TestStatus.TIMEOUT,
                command=display_cmd,
                exit_code=None,
                duration_seconds=round(time.monotonic() - start, 3),
                stdout=_truncate(exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""),
                stderr=_truncate(exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""),
                notes=[f"Test run exceeded the {timeout}s timeout and was killed."],
            )
        duration = round(time.monotonic() - start, 3)

        notes: list[str] = []
        cases: list[TestCaseResult] = []
        if xml_path.exists():
            try:
                cases = parse_junit(xml_path)
            except ET.ParseError as exc:
                notes.append(f"Could not parse JUnit report: {exc}")
        else:
            notes.append("pytest produced no JUnit report.")

    passed = sum(c.outcome == "passed" for c in cases)
    failed = sum(c.outcome == "failed" for c in cases)
    errored = sum(c.outcome == "error" for c in cases)
    skipped = sum(c.outcome == "skipped" for c in cases)
    run = passed + failed + errored

    if notes or run == 0:
        status = TestStatus.ERROR
        if run == 0:
            notes.append("No tests were executed.")
    elif proc.returncode == 0 and failed == 0 and errored == 0:
        status = TestStatus.PASS
    elif proc.returncode == 1 and failed > 0 and errored == 0:
        status = TestStatus.FAIL
    elif errored > 0 or proc.returncode not in (0, 1):
        status = TestStatus.ERROR
        notes.append(f"pytest exit code {proc.returncode} with {errored} error(s).")
    else:
        status = TestStatus.ERROR
        notes.append(
            f"Exit code {proc.returncode} contradicts parsed results "
            f"({passed} passed, {failed} failed); refusing to report PASS."
        )

    return TestReport(
        status=status,
        command=display_cmd,
        exit_code=proc.returncode,
        tests_run=run,
        tests_passed=passed,
        tests_failed=failed,
        tests_errored=errored,
        tests_skipped=skipped,
        duration_seconds=duration,
        failures=[c for c in cases if c.outcome in ("failed", "error")],
        stdout=_truncate(proc.stdout),
        stderr=_truncate(proc.stderr),
        notes=notes,
    )
