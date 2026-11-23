"""Loop tests with a scripted fake model: no API key, no cost."""
import json
import shutil
from pathlib import Path

from bugfix_agent.agent import run_agent
from bugfix_agent.backends import ToolCall, Turn, Usage

TOY = Path(__file__).resolve().parent.parent / "examples" / "toy_repo"


def turn(calls=(), text="", stop="tool_use", cost=0.01):
    calls = [ToolCall(f"toolu_{i}", name, args) for i, (name, args) in enumerate(calls)]
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    return Turn(content=content, tool_calls=calls, text=text, stop_reason=stop,
                usage=Usage(100, 50), model="fake", cost_usd=cost)


class ScriptedBackend:
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_messages = []

    def step(self, system, messages, tools):
        self.seen_messages.append(len(messages))
        return self.turns.pop(0) if self.turns else turn(text="(out of script)", stop="end_turn")


FIX = ("    ordered = sorted(values)\n    middle = len(ordered) // 2\n    return ordered[middle]",
       "    ordered = sorted(values)\n    middle = len(ordered) // 2\n"
       "    if len(ordered) % 2 == 0:\n        return (ordered[middle - 1] + ordered[middle]) / 2\n"
       "    return ordered[middle]")
NEW_TEST = "from textstats import median\n\n\ndef test_median_even_length():\n    assert median([1, 2, 3, 4]) == 2.5\n"


def test_scripted_fix_reaches_finish(tmp_path):
    backend = ScriptedBackend([
        turn([("list_files", {"path": "."})], text="Look at the layout."),
        turn([("edit_file", {"path": "tests/test_median_even.py", "old_str": "", "new_str": NEW_TEST})]),
        turn([("run_command", {"command": "python -m pytest -q tests/test_median_even.py"})]),
        turn([("edit_file", {"path": "textstats/stats.py", "old_str": FIX[0], "new_str": FIX[1]})]),
        turn([("run_command", {"command": "python -m pytest -q"})]),
        turn([("finish", {"status": "fixed", "root_cause": "even-length lists took the upper middle value",
                          "summary": "average the two middle values", "files_changed": ["textstats/stats.py"],
                          "tests_run": "python -m pytest -q: 7 passed"})]),
    ])
    s = run_agent(TOY, "median([1,2,3,4]) returns 3", backend, tmp_path / "run", verbose=False)
    assert s["status"] == "finished" and s["result"] == "fixed" and s["steps"] == 6
    assert "(ordered[middle - 1] + ordered[middle]) / 2" in s["diff"]
    assert "test_median_even_length" in s["diff"]          # new file included in the diff
    assert abs(s["cost_usd"] - 0.06) < 1e-9

    events = [json.loads(l) for l in (tmp_path / "run" / "trajectory.jsonl").read_text().splitlines()]
    tool_events = [e for e in events if e["event"] == "tool"]
    assert "exit code 1" in tool_events[2]["output"]       # new test failed before the fix
    assert "exit code 0" in tool_events[4]["output"] and "7 passed" in tool_events[4]["output"]
    assert (TOY / "textstats" / "stats.py").read_text().count("% 2") == 0   # original untouched
    # Conversation grows by assistant + tool-results each step.
    assert backend.seen_messages == [1, 3, 5, 7, 9, 11]


FINISH_FIXED = ("finish", {"status": "fixed", "root_cause": "r", "summary": "s",
                           "files_changed": ["textstats/stats.py"], "tests_run": "t"})


def test_gates_refuse_finish_without_test_then_accept(tmp_path):
    backend = ScriptedBackend([
        turn([("edit_file", {"path": "textstats/stats.py", "old_str": FIX[0], "new_str": FIX[1]})]),
        turn([FINISH_FIXED]),                                   # refused: no regression test
        turn([("edit_file", {"path": "tests/test_median_even.py", "old_str": "", "new_str": NEW_TEST})]),
        turn([FINISH_FIXED]),                                   # accepted
    ])
    s = run_agent(TOY, "x", backend, tmp_path / "run", verbose=False)
    assert s["status"] == "finished" and s["gate_rejections"] == 1 and s["gates"]["passed"]
    events = [json.loads(l) for l in (tmp_path / "run" / "trajectory.jsonl").read_text().splitlines()]
    refused = [e for e in events if e["event"] == "tool" and e["name"] == "finish" and e["is_error"]]
    assert len(refused) == 1 and "regression test added or changed" in refused[0]["output"]


def test_repeated_refusals_stop_the_run(tmp_path):
    backend = ScriptedBackend([turn([FINISH_FIXED]) for _ in range(5)])
    s = run_agent(TOY, "x", backend, tmp_path / "run", verbose=False, max_gate_rejections=3)
    assert s["status"] == "gates_failed" and s["steps"] == 3 and s["result"] is None


def test_without_gates_finish_is_trusted_but_checks_are_recorded(tmp_path):
    backend = ScriptedBackend([
        turn([("edit_file", {"path": "textstats/stats.py", "old_str": FIX[0], "new_str": FIX[1]})]),
        turn([FINISH_FIXED])])
    s = run_agent(TOY, "x", backend, tmp_path / "run", verbose=False, gates=False)
    assert s["status"] == "finished" and s["result"] == "fixed"
    assert s["gates"]["passed"] is False                        # recorded for the evaluation


def test_step_cap(tmp_path):
    backend = ScriptedBackend([turn([("list_files", {"path": "."})]) for _ in range(10)])
    s = run_agent(TOY, "x", backend, tmp_path / "run", max_steps=3, verbose=False)
    assert s["status"] == "max_steps" and s["steps"] == 3


def test_cost_cap_stops_before_next_call(tmp_path):
    backend = ScriptedBackend([turn([("list_files", {"path": "."})], cost=0.6) for _ in range(10)])
    s = run_agent(TOY, "x", backend, tmp_path / "run", max_cost_usd=1.0, verbose=False)
    assert s["status"] == "budget_exceeded" and s["steps"] == 2


def test_stops_after_one_nudge(tmp_path):
    backend = ScriptedBackend([turn(text="I think it's fine.", stop="end_turn"),
                               turn(text="Still fine.", stop="end_turn")])
    s = run_agent(TOY, "x", backend, tmp_path / "run", verbose=False)
    assert s["status"] == "stopped_without_finish" and s["steps"] == 2 and s["diff"] == ""


def test_refusal_stops_run(tmp_path):
    s = run_agent(TOY, "x", ScriptedBackend([turn(stop="refusal")]), tmp_path / "run", verbose=False)
    assert s["status"] == "refused"


def test_invalid_tool_input_is_reported_not_run(tmp_path):
    backend = ScriptedBackend([turn([("edit_file", {"path": "textstats/stats.py"})]),
                               turn([("finish", {"status": "not_fixed", "root_cause": "-", "summary": "-",
                                                 "files_changed": [], "tests_run": "-"})])])
    s = run_agent(TOY, "x", backend, tmp_path / "run", verbose=False)
    events = [json.loads(l) for l in (tmp_path / "run" / "trajectory.jsonl").read_text().splitlines()]
    first_tool = next(e for e in events if e["event"] == "tool")
    assert first_tool["is_error"] and "Missing required" in first_tool["output"]
    assert s["result"] == "not_fixed"
