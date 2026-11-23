"""The agent loop: ask the model, run the tools it calls, repeat until finish or a limit.

When the agent calls finish with status "fixed", the harness runs the gates (gates.py). If any
check fails, finish is refused, the agent sees the report and keeps working.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from .backends import Backend, BackendError, ToolOutput, Turn
from .gates import GateReport, verify
from .prompts import NUDGE, SYSTEM_PROMPT, task_message
from .runner import RunnerFactory, make_factory
from .tools import SKIP_DIRS, TOOL_SPECS, Toolbox, truncate

GIT = ["git", "-c", "user.name=bugfix-agent", "-c", "user.email=agent@localhost", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def prepare_workspace(src: Path, run_dir: Path) -> tuple[Path, str]:
    """Copy the target repo into the run folder. Returns (workdir, base commit).

    A clean git repo keeps its own history (so a pull request contains only the agent's
    changes); anything else gets a baseline commit so the final diff is exact."""
    dest = run_dir / "repo"
    ignore = shutil.ignore_patterns(*(d for d in SKIP_DIRS if d != ".git"))
    shutil.copytree(src, dest, ignore=ignore)
    if (dest / ".git").exists():
        if not _git(dest, "status", "--porcelain").strip():
            return dest, _git(dest, "rev-parse", "HEAD").strip()
    else:
        _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", "baseline before agent run")
    return dest, _git(dest, "rev-parse", "HEAD").strip()


def final_diff(repo: Path, base: str) -> str:
    _git(repo, "add", "-A", "--intent-to-add")  # include files the agent created
    return _git(repo, "diff", base)


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


def run_agent(repo: Path, issue: str, backend: Backend, run_dir: Path, max_steps: int = 40,
              max_cost_usd: float = 1.0, verbose: bool = True, make_runner: RunnerFactory | None = None,
              gates: bool = True, max_gate_rejections: int = 3) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    make_runner = make_runner or make_factory("local")
    workdir, base = prepare_workspace(Path(repo).resolve(), run_dir)
    runner = make_runner(workdir)
    tools = Toolbox(workdir, runner=runner)
    log = RunLog(run_dir)
    say = print if verbose else (lambda *a, **k: None)

    history: list[dict] = [{"kind": "user", "text": task_message(issue)}]
    cost, steps, rejections = 0.0, 0, 0
    nudged = False
    status, finish_payload, error = "max_steps", None, None
    report: GateReport | None = None
    t0 = time.time()
    log.write({"event": "start", "backend": backend.name, "repo": str(repo), "issue": issue, "base": base,
               "max_steps": max_steps, "max_cost_usd": max_cost_usd, "gates": gates,
               "sandbox": type(runner).__name__})
    try:
        while steps < max_steps:
            if cost >= max_cost_usd:
                status = "budget_exceeded"
                break
            steps += 1
            t_call = time.time()
            try:
                turn: Turn = backend.step(SYSTEM_PROMPT, history, TOOL_SPECS)
            except BackendError as e:
                status, error = "api_error", str(e)
                break
            cost += turn.cost_usd
            log.write({"event": "model", "step": steps, "model": turn.model, "stop_reason": turn.stop_reason,
                       "seconds": round(time.time() - t_call, 1), "usage": asdict(turn.usage),
                       "cost_usd": round(turn.cost_usd, 6), "total_cost_usd": round(cost, 6), "text": turn.text,
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
                output, is_error = res.output, res.is_error
                if res.finished and res.finished.get("status") == "fixed" and gates:
                    report = verify(workdir, base, make_runner, runner=runner)
                    log.write({"event": "gates", "step": steps, **report.to_dict()})
                    say("  gates:\n    " + report.text().replace("\n", "\n    "))
                    if report.passed:
                        finish_payload = res.finished
                    else:
                        rejections += 1
                        output, is_error = ("finish was refused because these checks failed:\n" + report.text()
                                            + "\nFix the problems, then call finish again."), True
                elif res.finished:
                    finish_payload = res.finished
                results.append(ToolOutput(call.id, call.name, output, is_error))
                log.write({"event": "tool", "step": steps, "name": call.name, "input": call.input,
                           "is_error": is_error, "output": truncate(output, 4000)})
                first_line = output.splitlines()[0] if output else ""
                say(f"  -> {call.name}({_short(call.input)}){' ERROR' if is_error else ''}: {first_line[:120]}")
            history.append({"kind": "tool_results", "results": results})  # all results in one message

            if finish_payload:
                status = "finished"
                break
            if rejections >= max_gate_rejections:
                status = "gates_failed"
                break

        # Always record the checks for the final state, enforced or not (used by the evaluation).
        if (report is None or not report.passed) and final_diff(workdir, base).strip():
            report = verify(workdir, base, make_runner, runner=runner)
    finally:
        runner.close()

    summary = {
        "status": status,
        "result": (finish_payload or {}).get("status"),
        "finish": finish_payload,
        "error": error,
        "steps": steps,
        "gate_rejections": rejections,
        "gates_enforced": gates,
        "gates": report.to_dict() if report else None,
        "cost_usd": round(cost, 4),
        "seconds": round(time.time() - t0, 1),
        "backend": backend.name,
        "sandbox": type(runner).__name__,
        "base": base,
        "workdir": str(workdir),
        "diff": final_diff(workdir, base),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    (run_dir / "messages.json").write_text(json.dumps([_history_to_json(h) for h in history],
                                                      indent=1, ensure_ascii=False))
    log.write({"event": "end", **{k: v for k, v in summary.items() if k != "diff"}})
    return summary


def _short(d: object, n: int = 80) -> str:
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 3] + "..."
