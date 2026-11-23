"""Command line.

  python -m bugfix_agent --repo PATH --issue "text"        # local repository
  python -m bugfix_agent --github OWNER/REPO#12             # GitHub issue in, draft PR out (after approval)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .agent import run_agent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_HOME = Path.home() / "bugfix-agent-runs"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bugfix_agent", description="Fix a reported bug in a Python repository.")
    ap.add_argument("--repo", type=Path, help="Local repository (it is copied, not changed).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--issue", help="Issue text (with --repo).")
    g.add_argument("--issue-file", type=Path, help="File containing the issue text (with --repo).")
    ap.add_argument("--github", metavar="OWNER/REPO#N",
                    help="Read this GitHub issue, fix it in a fresh clone, and offer to open a draft PR.")
    ap.add_argument("--yes", action="store_true", help="With --github: open the draft PR without asking.")
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
    ap.add_argument("--sandbox", default="docker", choices=["docker", "local"],
                    help="Where the agent's commands run: a locked Docker container (default) or this machine.")
    ap.add_argument("--no-gates", action="store_true",
                    help="Do not enforce the checks at finish (for comparison runs only).")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_HOME,
                    help="Where run folders go (default ~/bugfix-agent-runs: Docker cannot mount the macOS-protected Desktop/Documents folders without permission).")
    args = ap.parse_args(argv)

    if args.github and (args.repo or args.issue or args.issue_file):
        ap.error("use either --github, or --repo with --issue/--issue-file")
    if not args.github and not (args.repo and (args.issue or args.issue_file)):
        ap.error("give --repo PATH with --issue/--issue-file, or --github OWNER/REPO#N")
    if args.repo and not args.repo.is_dir():
        ap.error(f"{args.repo} is not a directory")
    if args.github and args.no_gates:
        ap.error("--no-gates is for comparison runs and cannot open pull requests")

    from .runner import SANDBOX_IMAGE, docker_available, make_factory
    if args.sandbox == "docker" and not docker_available():
        ap.error(f"Docker sandbox not available: start Docker Desktop and run "
                 f"'docker build -t {SANDBOX_IMAGE} sandbox/', or use --sandbox local.")

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

    ref = issue_data = None
    if args.github:
        from . import github
        try:
            ref = github.parse_ref(args.github)
            issue_data = github.fetch_issue(ref)
            print(f"Issue {ref}: {issue_data['title']}")
            repo = github.clone(ref, run_dir / "clone")
        except github.GitHubError as e:
            print(f"GitHub error: {e}")
            return 2
        issue = github.issue_text(issue_data)
    else:
        repo = args.repo
        issue = args.issue if args.issue is not None else args.issue_file.read_text(encoding="utf-8")

    summary = run_agent(repo, issue, backend, run_dir, max_steps=args.max_steps, max_cost_usd=args.max_cost,
                        make_runner=make_factory(args.sandbox), gates=not args.no_gates)

    print(f"\nStatus: {summary['status']}  result: {summary['result']}  steps: {summary['steps']}  "
          f"cost: ${summary['cost_usd']}  time: {summary['seconds']} s  sandbox: {summary['sandbox']}")
    if summary["gates"]:
        print(("Checks passed" if summary["gates"]["passed"] else "Checks FAILED") +
              f" (finish refused {summary['gate_rejections']} time(s)):")
        for c in summary["gates"]["checks"]:
            print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}")
    if summary["error"]:
        print(f"Error: {summary['error']}")
    if summary["finish"]:
        print(f"Root cause: {summary['finish']['root_cause']}\nSummary: {summary['finish']['summary']}")
    print("\nDiff:\n" + (summary["diff"] or "(no changes)"))

    fixed = summary["result"] == "fixed" and bool(summary["gates"] and summary["gates"]["passed"])
    if args.github and fixed:
        from . import github
        answer = "y" if args.yes else input(f"\nOpen a draft pull request on {ref.slug} with this change? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            try:
                url = github.open_draft_pr(ref, issue_data, summary)
            except github.GitHubError as e:
                print(f"Could not open the pull request: {e}")
                return 2
            print(f"Draft pull request: {url}")
            (run_dir / "pull_request.txt").write_text(url + "\n")
        else:
            print("No pull request opened.")
    elif args.github:
        print("\nThe fix did not pass the checks, so no pull request is offered.")
    return 0 if summary["result"] == "fixed" else 1


if __name__ == "__main__":
    sys.exit(main())
