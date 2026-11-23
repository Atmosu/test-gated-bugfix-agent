"""The agent loop: ask the model, run the tools it calls, repeat until finish or a limit."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from .backends import Backend, BackendError, ToolOutput, Turn
from .prompts import NUDGE, SYSTEM_PROMPT, task_message
from .tools import SKIP_DIRS, TOOL_SPECS, Toolbox, truncate

GIT = ["git", "-c", "user.name=bugfix-agent", "-c", "user.email=agent@localhost", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def prepare_workspace(src: Path, run_dir: Path) -> Path:
    """Copy the target repo into the run folder and commit a baseline so the final diff is exact."""
    dest = run_dir / "repo"
    ignore = shutil.ignore_patterns(*(d for d in SKIP_DIRS if d != ".git"))
    shutil.copytree(src, dest, ignore=ignore)
    if not (dest / ".git").exists():
        _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", "baseline before agent run")
    return dest


def final_diff(repo: Path) -> str:
    _git(repo, "add", "-A", "--intent-to-add")  # include files the agent created
    return _git(repo, "diff")


def _history_to_json(h: dict) -> dict:
    """Provider-neutral view of one history item, for messages.json."""
    if h["kind"] == "user":
        return {"role": "user", "text": h["text"]}
    if h["kind"] == "assistant":
        t = h["turn"]
        return {"role": "assistant", "model": t.model, "text": t.text,
                "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in t.tool_calls]}
    return {"role": "tool", "results": [asdict(r) for r in h["results"]]}


class RunLog:
    def __init__(self, run_dir: Path):
        self.path = run_dir / "trajectory.jsonl"

    def write(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_agent(repo: Path, issue: str, backend: Backend, run_dir: Path,
              max_steps: int = 40, max_cost_usd: float = 1.0, verbose: bool = True) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = prepare_workspace(Path(repo).resolve(), run_dir)
    tools = Toolbox(workdir)
    log = RunLog(run_dir)
    say = print if verbose else (lambda *a, **k: None)

    history: list[dict] = [{"kind": "user", "text": task_message(issue)}]
    cost = 0.0
    steps = 0
    nudged = False
    status, finish_payload, error = "max_steps", None, None
    t0 = time.time()
    log.write({"event": "start", "backend": backend.name, "repo": str(repo), "issue": issue,
               "max_steps": max_steps, "max_cost_usd": max_cost_usd})

    while steps < max_steps:
        if cost >= max_cost_usd:
            status = "budget_exceeded"
            break
        steps += 1
        try:
            turn: Turn = backend.step(SYSTEM_PROMPT, history, TOOL_SPECS)
        except BackendError as e:
            status, error = "api_error", str(e)
            break
        cost += turn.cost_usd
        log.write({"event": "model", "step": steps, "model": turn.model, "stop_reason": turn.stop_reason,
                   "usage": asdict(turn.usage), "cost_usd": round(turn.cost_usd, 6),
                   "total_cost_usd": round(cost, 6), "text": turn.text,
                   "tool_calls": [{"name": c.name, "input": c.input} for c in turn.tool_calls]})
        if turn.text:
            say(f"[step {steps}] {turn.text.strip()[:300]}")

        if turn.stop_reason == "refusal":
            status = "refused"
            break
        if turn.stop_reason == "max_tokens" and turn.tool_calls:
            status, error = "max_tokens", "Response hit max_tokens in the middle of a tool call."
            break

        history.append({"kind": "assistant", "turn": turn})  # backend passes it back unchanged

        if turn.stop_reason == "pause_turn":
            continue
        if not turn.tool_calls:
            if nudged:
                status = "stopped_without_finish"
                break
            nudged = True
            history.append({"kind": "user", "text": NUDGE})
            continue

        results: list[ToolOutput] = []
        for call in turn.tool_calls:
            res = tools.run(call.name, call.input)
            results.append(ToolOutput(call.id, call.name, res.output, res.is_error))
            log.write({"event": "tool", "step": steps, "name": call.name, "input": call.input,
                       "is_error": res.is_error, "output": truncate(res.output, 4000)})
            first_line = res.output.splitlines()[0] if res.output else ""
            say(f"  -> {call.name}({_short(call.input)}){' ERROR' if res.is_error else ''}: {first_line[:120]}")
            if res.finished:
                finish_payload = res.finished
        history.append({"kind": "tool_results", "results": results})  # all results in one message

        if finish_payload:
            status = "finished"
            break
        say(f"  cost so far ${cost:.4f}")

    diff = final_diff(workdir)
    summary = {
        "status": status,
        "result": (finish_payload or {}).get("status"),
        "finish": finish_payload,
        "error": error,
        "steps": steps,
        "cost_usd": round(cost, 4),
        "seconds": round(time.time() - t0, 1),
        "backend": backend.name,
        "workdir": str(workdir),
        "diff": diff,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    (run_dir / "messages.json").write_text(json.dumps([_history_to_json(h) for h in history],
                                                      indent=1, ensure_ascii=False))
    log.write({"event": "end", **{k: v for k, v in summary.items() if k != "diff"}})
    return summary


def _short(d: object, n: int = 80) -> str:
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 3] + "..."
