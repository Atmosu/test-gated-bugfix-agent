"""Mini evaluation: run the agent on the 10 planted bugs, with and without the enforced gates.

  python bench/run_bench.py --validate          # check every bug is real (no model calls)
  python bench/run_bench.py                     # run all bugs x both conditions (free Gemini)
  python bench/run_bench.py --out bench/results/NAME   # resume a run: finished rows are skipped

A run counts as RESOLVED when the hidden test (never shown to the agent) and all visible tests pass
on the agent's final code, run in the Docker sandbox.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

from bugs import BUGS  # noqa: E402

from bugfix_agent.agent import run_agent  # noqa: E402
from bugfix_agent.runner import SandboxError, make_factory  # noqa: E402

BASE = ROOT / "bench" / "shopkit"
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
FIELDS = ["bug", "kind", "condition", "status", "result", "resolved", "reproduced", "gates_passed",
          "gate_rejections", "steps", "seconds", "models", "input_tokens", "output_tokens", "error"]


def make_buggy_repo(bug: dict, dest: Path) -> Path:
    shutil.copytree(BASE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    f = dest / bug["file"]
    text = f.read_text()
    assert text.count(bug["old"]) == 1, f"{bug['id']}: old text not found exactly once"
    f.write_text(text.replace(bug["old"], bug["new"]))
    return dest


def hidden_name(bug: dict) -> str:
    return f"tests/test_zz_hidden_{bug['id'].replace('-', '_')}.py"


def run_tests(repo: Path, make_runner, paths: list[str]) -> int:
    r = make_runner(repo)
    try:
        return r.run([*PYTEST, *paths], 300).returncode
    finally:
        r.close()


def validate(make_runner) -> bool:
    ok = True
    for bug in BUGS:
        with tempfile.TemporaryDirectory() as tmp:
            buggy = make_buggy_repo(bug, Path(tmp) / "buggy")
            clean = Path(tmp) / "clean"
            shutil.copytree(BASE, clean)
            for repo in (buggy, clean):
                (repo / hidden_name(bug)).write_text(bug["hidden"])
            visible = run_tests(buggy, make_runner, ["--ignore", hidden_name(bug)])
            hidden_buggy = run_tests(buggy, make_runner, [hidden_name(bug)])
            hidden_clean = run_tests(clean, make_runner, [hidden_name(bug)])
            good = visible == 0 and hidden_buggy != 0 and hidden_clean == 0
            ok &= good
            print(f"{'OK  ' if good else 'BAD '} {bug['id']:16} visible tests with bug: "
                  f"{'pass' if visible == 0 else 'FAIL'}, hidden test with bug: "
                  f"{'fail' if hidden_buggy else 'PASS'}, hidden test on correct code: "
                  f"{'pass' if hidden_clean == 0 else 'FAIL'}")
    return ok


def score(summary: dict, bug: dict, make_runner) -> bool:
    workdir = Path(summary["workdir"])
    (workdir / hidden_name(bug)).write_text(bug["hidden"])
    try:
        return run_tests(workdir, make_runner, []) == 0
    finally:
        (workdir / hidden_name(bug)).unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--conditions", default="gates,nogates")
    ap.add_argument("--bugs", default="all", help="comma-separated bug ids, or 'all'")
    ap.add_argument("--sandbox", default="docker", choices=["docker", "local"])
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--model", help="comma-separated Gemini models (default: the backend's fallback list)")
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / time.strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()
    make_runner = make_factory(args.sandbox)

    if args.validate:
        return 0 if validate(make_runner) else 1

    from bugfix_agent.backends import GeminiBackend
    bugs = BUGS if args.bugs == "all" else [b for b in BUGS if b["id"] in args.bugs.split(",")]
    conditions = args.conditions.split(",")
    args.out.mkdir(parents=True, exist_ok=True)
    csv_path = args.out / "results.csv"
    done = set()
    if csv_path.exists():
        with csv_path.open() as f:
            done = {(r["bug"], r["condition"]) for r in csv.DictReader(f) if r["status"] != "api_error"}
    else:
        with csv_path.open("w", newline="") as f:
            csv.DictWriter(f, FIELDS).writeheader()

    models = args.model.split(",") if args.model else None
    for bug in bugs:
        for cond in conditions:  # both conditions on the same bug back to back, so they see similar models
            if (bug["id"], cond) in done:
                continue
            backend = GeminiBackend(models=models)  # fresh: tries the strongest free model first
            tag = f"{bug['id']}__{cond}"
            run_dir = Path.home() / "bugfix-agent-runs" / "bench" / args.out.name / tag  # outside macOS-protected folders
            print(f"\n=== {tag} ===", flush=True)
            shutil.rmtree(run_dir, ignore_errors=True)  # leftovers of an interrupted try
            with tempfile.TemporaryDirectory() as tmp:
                repo = make_buggy_repo(bug, Path(tmp) / "shopkit")
                try:
                    s = run_agent(repo, bug["issue"], backend, run_dir, max_steps=args.max_steps,
                                  verbose=False, make_runner=make_runner, gates=(cond == "gates"))
                except SandboxError as e:
                    print(f"{tag}: sandbox error, skipped: {e}", flush=True)
                    continue
            resolved = score(s, bug, make_runner) if s["diff"].strip() else False
            events = [json.loads(l) for l in (run_dir / "trajectory.jsonl").read_text().splitlines()]
            calls = [e for e in events if e["event"] == "model"]
            checks = {c["name"]: c["passed"] for c in (s["gates"] or {}).get("checks", [])}
            row = {
                "bug": bug["id"], "kind": bug["kind"], "condition": cond, "status": s["status"],
                "result": s["result"], "resolved": resolved,
                "reproduced": checks.get("new test fails on the original code", False),
                "gates_passed": bool(s["gates"] and s["gates"]["passed"]),
                "gate_rejections": s["gate_rejections"], "steps": s["steps"], "seconds": s["seconds"],
                "models": "|".join(sorted({e["model"] for e in calls})),
                "input_tokens": sum(e["usage"]["input_tokens"] for e in calls),
                "output_tokens": sum(e["usage"]["output_tokens"] for e in calls),
                "error": s["error"] or "",
            }
            with csv_path.open("a", newline="") as f:
                csv.DictWriter(f, FIELDS).writerow(row)
            print(f"{tag}: status={row['status']} resolved={resolved} reproduced={row['reproduced']} "
                  f"steps={row['steps']} {row['seconds']}s models={row['models']}", flush=True)
    print(f"\nResults: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
