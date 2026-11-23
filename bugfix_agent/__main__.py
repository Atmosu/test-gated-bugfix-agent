"""Command line: python -m bugfix_agent --repo PATH --issue "text" """
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .agent import run_agent

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bugfix_agent", description="Fix a reported bug in a Python repository.")
    ap.add_argument("--repo", required=True, type=Path, help="Path to the repository (it is copied, not changed).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--issue", help="Issue text.")
    g.add_argument("--issue-file", type=Path, help="File containing the issue text.")
    ap.add_argument("--backend", default="gemini", choices=["gemini", "claude"],
                    help="gemini = free tier (GEMINI_API_KEY); claude = paid (ANTHROPIC_API_KEY).")
    ap.add_argument("--model", help="Gemini: one model or a comma-separated fallback list, tried in order "
                                    "(default: 3.8-flash, 3.7-flash, 3.6-flash, 3.5-flash, 3.5-flash-lite, "
                                    "3.1-flash-lite). Claude: one model (default claude-sonnet-5-5).")
    ap.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh", "max"],
                    help="Reasoning depth. Default: the model's own default (gemini), medium (claude).")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--max-cost", type=float, default=1.0,
                    help="Paid backends: stop before a model call once this many USD are spent.")
    ap.add_argument("--min-interval", type=float, default=0.0,
                    help="Gemini: minimum seconds between requests, to stay under a requests-per-minute limit.")
    ap.add_argument("--no-strict-tools", action="store_true", help="Claude: send tool schemas without strict: true.")
    ap.add_argument("--runs-dir", type=Path, default=PROJECT_ROOT / "runs")
    args = ap.parse_args(argv)

    if not args.repo.is_dir():
        ap.error(f"{args.repo} is not a directory")
    issue = args.issue if args.issue is not None else args.issue_file.read_text(encoding="utf-8")

    if args.backend == "gemini":
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            ap.error("GEMINI_API_KEY is not set (free key from aistudio.google.com).")
        from .backends import GeminiBackend
        models = [m.strip() for m in args.model.split(",") if m.strip()] if args.model else None
        backend = GeminiBackend(models=models, effort=args.effort, min_interval_s=args.min_interval)
    else:
        from .backends import ClaudeBackend
        backend = ClaudeBackend(model=args.model or "claude-sonnet-5-5", effort=args.effort or "medium",
                                strict_tools=not args.no_strict_tools)
    run_dir = args.runs_dir / time.strftime("%Y%m%d-%H%M%S")
    print(f"Run folder: {run_dir}")
    summary = run_agent(args.repo, issue, backend, run_dir, max_steps=args.max_steps, max_cost_usd=args.max_cost)

    print(f"\nStatus: {summary['status']}  result: {summary['result']}  steps: {summary['steps']}  "
          f"cost: ${summary['cost_usd']}  time: {summary['seconds']} s")
    if summary["error"]:
        print(f"Error: {summary['error']}")
    if summary["finish"]:
        print(f"Root cause: {summary['finish']['root_cause']}\nSummary: {summary['finish']['summary']}")
    print("\nDiff:\n" + (summary["diff"] or "(no changes)"))
    return 0 if summary["result"] == "fixed" else 1


if __name__ == "__main__":
    sys.exit(main())
