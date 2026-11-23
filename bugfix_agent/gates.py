"""Checks the harness runs when the agent says the bug is fixed. The model cannot skip them."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .runner import CommandTimeout, Runner, RunnerFactory

TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
SUPPRESSION = re.compile(r"pytest\.mark\.(skip|skipif|xfail)|unittest\.skip|#\s*noqa|pylint:\s*disable|"
                         r"type:\s*ignore|pragma:\s*no cover|pytest\.skip\(")
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
GIT = ["git", "-c", "core.quotepath=false"]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GateReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, passed, detail))
        return passed

    def text(self) -> str:
        return "\n".join(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}" + (f": {c.detail}" if c.detail else "")
                         for c in self.checks)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": [asdict(c) for c in self.checks]}


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) for every change since base, including new untracked files."""
    _git(repo, "add", "-A", "--intent-to-add")
    out = _git(repo, "diff", "--name-status", "--no-renames", base)
    return [(line[0], line.split("\t", 1)[1]) for line in out.splitlines() if "\t" in line]


def _tail(text: str, n: int = 12) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def _pytest(runner: Runner, paths: list[str], timeout: int) -> tuple[int | None, str]:
    try:
        r = runner.run([*PYTEST, *paths], timeout)
    except CommandTimeout as e:
        return None, str(e)
    return r.returncode, r.stdout + r.stderr


def verify(workdir: Path, base: str, make_runner: RunnerFactory, runner: Runner | None = None,
           timeout: int = 300) -> GateReport:
    rep = GateReport()
    changes = changed_files(workdir, base)
    tests = [p for s, p in changes if is_test_path(p) and s != "D" and p.endswith(".py")
             and not p.endswith("conftest.py")]
    sources = [(s, p) for s, p in changes if not is_test_path(p)]

    rep.add("regression test added or changed", bool(tests), ", ".join(tests) or "no test file changed")
    rep.add("application code changed", bool(sources), ", ".join(p for _, p in sources) or "only tests changed")

    # Existing tests must survive: no deleted test files, no removed test functions.
    removed: list[str] = [p for s, p in changes if s == "D" and is_test_path(p)]
    for s, p in changes:
        if s == "M" and is_test_path(p):
            before = set(TEST_DEF.findall(_git(workdir, "show", f"{base}:{p}")))
            after = set(TEST_DEF.findall((workdir / p).read_text(encoding="utf-8", errors="replace")))
            removed += [f"{p}::{name}" for name in sorted(before - after)]
    rep.add("no existing tests deleted", not removed, ", ".join(removed))

    added = [l[1:] for l in _git(workdir, "diff", "-U0", base).splitlines()
             if l.startswith("+") and not l.startswith("+++")]
    bad = [l.strip() for l in added if SUPPRESSION.search(l)]
    rep.add("no skips or lint/type suppressions added", not bad, "; ".join(bad[:3]))

    if not (tests and sources):
        return rep

    # Fail before: a copy with only the test changes, source changes reverted.
    tmp = Path(tempfile.mkdtemp(prefix="gate-"))
    before_dir = tmp / "repo"
    try:
        shutil.copytree(workdir, before_dir)
        for s, p in sources:
            if s == "A":
                (before_dir / p).unlink(missing_ok=True)
            else:
                _git(before_dir, "checkout", base, "--", p)
        r_before = make_runner(before_dir)
        try:
            code, out = _pytest(r_before, tests, timeout)
        finally:
            r_before.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # 1 = tests failed; 2 = error while collecting (e.g. import of the buggy behaviour). Both reproduce.
    rep.add("new test fails on the original code", code in (1, 2),
            "it passed, so it does not reproduce the bug" if code == 0 else
            ("ran out of time" if code is None else _tail(out, 4)))

    own = runner is None
    runner = runner or make_runner(workdir)
    try:
        code, out = _pytest(runner, tests, timeout)
        rep.add("new test passes with the fix", code == 0, "" if code == 0 else _tail(out))
        code, out = _pytest(runner, [], timeout)
        rep.add("full test suite passes", code == 0, _tail(out, 1) if code == 0 else _tail(out))
    finally:
        if own:
            runner.close()
    return rep
