"""Gate tests on a copy of the toy repo, with local and Docker runners."""
import shutil
import subprocess
from pathlib import Path

import pytest

from bugfix_agent.agent import prepare_workspace
from bugfix_agent.gates import is_test_path, verify
from bugfix_agent.runner import DockerRunner, docker_available, make_factory

TOY = Path(__file__).resolve().parent.parent / "examples" / "toy_repo"
STATS = "textstats/stats.py"
BUG = "    return ordered[middle]\n"
FIX = ("    if len(ordered) % 2 == 0:\n        return (ordered[middle - 1] + ordered[middle]) / 2\n"
       "    return ordered[middle]\n")
NEW_TEST = "from textstats import median\n\n\ndef test_median_even_length():\n    assert median([1, 2, 3, 4]) == 2.5\n"


@pytest.fixture
def ws(tmp_path):
    workdir, base = prepare_workspace(TOY, tmp_path / "run")
    return workdir, base


def edit(workdir, rel, old, new):
    p = workdir / rel
    p.write_text(p.read_text().replace(old, new, 1))


def names(rep):
    return {c.name: c.passed for c in rep.checks}


def test_is_test_path():
    assert is_test_path("tests/test_x.py") and is_test_path("pkg/test_y.py") and is_test_path("a/b_test.py")
    assert not is_test_path("textstats/stats.py")


def test_good_fix_passes_all_checks(ws):
    workdir, base = ws
    (workdir / "tests" / "test_even.py").write_text(NEW_TEST)
    edit(workdir, STATS, BUG, FIX)
    rep = verify(workdir, base, make_factory("local"))
    assert rep.passed, rep.text()
    assert len(rep.checks) == 7


def test_fix_without_test_fails(ws):
    workdir, base = ws
    edit(workdir, STATS, BUG, FIX)
    rep = verify(workdir, base, make_factory("local"))
    assert not rep.passed and names(rep)["regression test added or changed"] is False


def test_test_that_does_not_reproduce_fails(ws):
    workdir, base = ws
    (workdir / "tests" / "test_even.py").write_text(
        "from textstats import median\n\n\ndef test_odd():\n    assert median([1, 2, 3]) == 2\n")
    edit(workdir, STATS, BUG, FIX)
    rep = verify(workdir, base, make_factory("local"))
    assert names(rep)["new test fails on the original code"] is False


def test_deleting_or_skipping_tests_fails(ws):
    workdir, base = ws
    (workdir / "tests" / "test_even.py").write_text(NEW_TEST)
    edit(workdir, STATS, BUG, FIX)
    edit(workdir, "tests/test_stats.py", "def test_mode():\n    assert mode([1, 2, 2, 3]) == 2\n", "")
    edit(workdir, "tests/test_stats.py", "def test_mean():", "@pytest.mark.skip\ndef test_mean():")
    rep = verify(workdir, base, make_factory("local"))
    n = names(rep)
    assert n["no existing tests deleted"] is False
    assert n["no skips or lint/type suppressions added"] is False


def test_broken_suite_fails(ws):
    workdir, base = ws
    (workdir / "tests" / "test_even.py").write_text(NEW_TEST)
    edit(workdir, STATS, BUG, FIX)
    edit(workdir, STATS, "return sum(values) / len(values)", "return sum(values)")  # breaks mean()
    assert names(verify(workdir, base, make_factory("local")))["full test suite passes"] is False


def test_clean_git_repo_keeps_its_head_as_base(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(TOY, src)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "init", "-q"], cwd=src, check=True)
    subprocess.run([*g, "add", "-A"], cwd=src, check=True)
    subprocess.run([*g, "commit", "-qm", "init"], cwd=src, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    _, base = prepare_workspace(src, tmp_path / "run")
    assert base == head   # no extra baseline commit, so a PR contains only the agent's changes


docker = pytest.mark.skipif(not docker_available(), reason="Docker sandbox image not available")


@docker
def test_docker_runner_is_locked_down(tmp_path):
    (tmp_path / "f.txt").write_text("hello")
    r = DockerRunner(tmp_path)
    try:
        assert r.run(["cat", "f.txt"], 30).stdout == "hello"
        net = r.run(["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)"], 30)
        assert net.returncode != 0                                   # no network
        assert r.run(["id", "-un"], 30).stdout.strip() == "agent"     # not root
        r.run(["python", "-c", "open('made_in_box.txt','w').write('x')"], 30)
        assert (tmp_path / "made_in_box.txt").exists()               # writes reach the workspace
    finally:
        r.close()


@docker
def test_gates_in_docker(ws):
    workdir, base = ws
    (workdir / "tests" / "test_even.py").write_text(NEW_TEST)
    edit(workdir, STATS, BUG, FIX)
    rep = verify(workdir, base, make_factory("docker"))
    assert rep.passed, rep.text()
