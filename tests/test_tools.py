from pathlib import Path

import pytest

from bugfix_agent.tools import TOOL_SPECS, Toolbox, truncate, validate_input


@pytest.fixture
def box(tmp_path: Path) -> Toolbox:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def f(x):\n    return x + 1\n\n\ndef g(x):\n    return x + 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret = 1\n")
    return Toolbox(tmp_path)


def test_specs_are_strict_objects():
    for spec in TOOL_SPECS:
        s = spec["input_schema"]
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert set(s["required"]) <= set(s["properties"])


def test_validate_input_catches_bad_fields():
    assert validate_input("read_file", {"path": "a.py"}) is None
    assert "Missing" in validate_input("read_file", {})
    assert "Unknown field" in validate_input("read_file", {"path": "a", "x": 1})
    assert "type integer" in validate_input("read_file", {"path": "a", "start_line": "3"})
    assert "type integer" in validate_input("read_file", {"path": "a", "start_line": True})
    assert "one of" in validate_input("finish", {"status": "maybe", "root_cause": "", "summary": "",
                                                 "files_changed": [], "tests_run": ""})
    assert "Unknown tool" in validate_input("rm_rf", {})


def test_path_escape_blocked(box):
    r = box.run("read_file", {"path": "../outside.txt"})
    assert r.is_error and "outside" in r.output


def test_list_files_skips_git(box):
    r = box.run("list_files", {"path": "."})
    assert "pkg/mod.py" in r.output and ".git" not in r.output


def test_search_code(box):
    r = box.run("search_code", {"pattern": r"def g"})
    assert r.output.strip() == "pkg/mod.py:5: def g(x):"
    assert box.run("search_code", {"pattern": "("}).is_error


def test_read_file_line_range(box):
    r = box.run("read_file", {"path": "pkg/mod.py", "start_line": 2, "end_line": 2})
    assert "    2      return x + 1" in r.output and "showing lines 2-2" in r.output


def test_edit_requires_unique_match(box):
    r = box.run("edit_file", {"path": "pkg/mod.py", "old_str": "return x + 1", "new_str": "return x"})
    assert r.is_error and "2 times" in r.output
    r = box.run("edit_file", {"path": "pkg/mod.py", "old_str": "def g(x):\n    return x + 1",
                              "new_str": "def g(x):\n    return x + 2"})
    assert not r.is_error and "line 5" in r.output
    assert "return x + 2" in (box.root / "pkg" / "mod.py").read_text()
    assert box.run("edit_file", {"path": "pkg/mod.py", "old_str": "nope", "new_str": "x"}).is_error


def test_edit_creates_new_file_only_if_absent(box):
    assert not box.run("edit_file", {"path": "tests/test_new.py", "old_str": "", "new_str": "x = 1\n"}).is_error
    assert (box.root / "tests" / "test_new.py").exists()
    assert box.run("edit_file", {"path": "tests/test_new.py", "old_str": "", "new_str": "y"}).is_error


@pytest.mark.parametrize("cmd", ["rm -rf .", "curl http://example.com", "ls; rm x", "cat a | head",
                                 "ls && rm x", "python -m pytest > out.txt", "python -m pytest 2>&1",
                                 "cat $HOME", "git push", "git reset --hard"])
def test_run_command_blocks_unsafe(box, cmd):
    assert box.run("run_command", {"command": cmd}).is_error


def test_run_command_runs_python(box):
    r = box.run("run_command", {"command": "python -c 'print(6*7)'"})
    assert not r.is_error and "[exit code 0]" in r.output and "42" in r.output


def test_run_command_timeout(box):
    r = box.run("run_command", {"command": "python -c 'import time; time.sleep(5)'", "timeout_s": 1})
    assert r.is_error and "timed out" in r.output


def test_finish_returns_payload(box):
    args = {"status": "fixed", "root_cause": "c", "summary": "s", "files_changed": ["a.py"], "tests_run": "t"}
    r = box.run("finish", args)
    assert r.finished == args


def test_truncate_keeps_head_and_tail():
    text = "A" * 10_000 + "B" * 10_000
    out = truncate(text, limit=3000)
    assert out.startswith("A") and out.endswith("B") and "omitted" in out and len(out) < 3200
