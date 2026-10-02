import pytest

from app.ci.test_runner import run_tests
from app.models.schemas import TestStatus

PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING = "def test_bad():\n    assert 1 + 1 == 3\n"


def test_pass(make_project):
    root = make_project({"tests/test_a.py": PASSING})
    r = run_tests(root)
    assert r.status == TestStatus.PASS
    assert (r.tests_run, r.tests_passed, r.tests_failed, r.exit_code) == (1, 1, 0, 0)


def test_fail_is_reported_with_node_id(make_project):
    root = make_project({"tests/test_a.py": PASSING + "\n" + FAILING})
    r = run_tests(root)
    assert r.status == TestStatus.FAIL
    assert r.tests_failed == 1 and r.tests_passed == 1
    assert r.failures[0].node_id == "tests/test_a.py::test_bad"
    assert "== 3" in r.failures[0].message


def test_class_based_node_id(make_project):
    root = make_project({"tests/test_a.py": "class TestX:\n    def test_y(self):\n        assert False\n"})
    r = run_tests(root)
    assert r.failures[0].node_id == "tests/test_a.py::TestX::test_y"


def test_timeout_is_never_pass(make_project):
    root = make_project({"tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(30)\n"})
    r = run_tests(root, timeout=2)
    assert r.status == TestStatus.TIMEOUT
    assert not r.passed
    assert r.exit_code is None


def test_no_tests_is_never_pass(make_project):
    root = make_project({"tests/test_empty.py": "x = 1\n"})
    r = run_tests(root)
    assert r.status == TestStatus.ERROR
    assert not r.passed


def test_collection_error_is_never_pass(make_project):
    root = make_project({
        "tests/test_a.py": PASSING,
        "tests/test_broken.py": "def test_x(:\n    pass\n",
    })
    r = run_tests(root)
    assert r.status == TestStatus.ERROR
    assert not r.passed


def test_targeted_run(make_project):
    root = make_project({"tests/test_a.py": PASSING, "tests/test_b.py": FAILING})
    r = run_tests(root, targets=["tests/test_a.py"])
    assert r.status == TestStatus.PASS and r.tests_run == 1


@pytest.mark.parametrize("target", ["--co", "../outside.py", "/etc/passwd"])
def test_invalid_targets_rejected(make_project, target):
    root = make_project({"tests/test_a.py": PASSING})
    with pytest.raises(ValueError):
        run_tests(root, targets=[target])


def test_node_id_normalises_windows_separators():
    import xml.etree.ElementTree as ET

    from app.ci.test_runner import _node_id

    case = ET.fromstring('<testcase classname="tests.sub.test_a.TestX" name="test_y" file="tests\\sub\\test_a.py"/>')
    assert _node_id(case) == "tests/sub/test_a.py::TestX::test_y"
