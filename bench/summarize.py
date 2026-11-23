"""Summarise bench results: python bench/summarize.py bench/results/main"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path


def truthy(v: str) -> bool:
    return v.strip().lower() == "true"


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "bench/results/main")
    rows = list(csv.DictReader((out / "results.csv").open()))
    summary = {}
    lines = ["| Condition | Runs | Resolved (hidden test) | Claimed fixed | False claims | Bug reproduced | "
             "Finish refused | Median steps | Median time |", "|---|---|---|---|---|---|---|---|---|"]
    for cond in ("gates", "nogates"):
        r = [x for x in rows if x["condition"] == cond]
        if not r:
            continue
        claimed = [x for x in r if x["result"] == "fixed"]
        s = {
            "runs": len(r),
            "resolved": sum(truthy(x["resolved"]) for x in r),
            "claimed_fixed": len(claimed),
            "false_claims": sum(not truthy(x["resolved"]) for x in claimed),
            "reproduced": sum(truthy(x["reproduced"]) for x in r),
            "gate_rejections": sum(int(x["gate_rejections"] or 0) for x in r),
            "runs_with_rejection": sum(int(x["gate_rejections"] or 0) > 0 for x in r),
            "median_steps": statistics.median(int(x["steps"]) for x in r),
            "median_seconds": round(statistics.median(float(x["seconds"]) for x in r)),
            "api_errors": sum(x["status"] == "api_error" for x in r),
            "models": sorted({m for x in r for m in x["models"].split("|") if m}),
            "tokens_in": sum(int(x["input_tokens"]) for x in r),
            "tokens_out": sum(int(x["output_tokens"]) for x in r),
        }
        summary[cond] = s
        lines.append(f"| {cond} | {s['runs']} | {s['resolved']} | {s['claimed_fixed']} | {s['false_claims']} | "
                     f"{s['reproduced']} | {s['gate_rejections']} | {s['median_steps']} | {s['median_seconds']} s |")
    lines += ["", "| Bug | Kind | gates: resolved / reproduced / steps | nogates: resolved / reproduced / steps |",
              "|---|---|---|---|"]
    for bug in dict.fromkeys(x["bug"] for x in rows):
        cells = []
        for cond in ("gates", "nogates"):
            x = next((y for y in rows if y["bug"] == bug and y["condition"] == cond), None)
            cells.append("-" if x is None else
                         f"{'yes' if truthy(x['resolved']) else 'NO'} / {'yes' if truthy(x['reproduced']) else 'no'}"
                         f" / {x['steps']} ({x['status']})")
        kind = next(y["kind"] for y in rows if y["bug"] == bug)
        lines.append(f"| {bug} | {kind} | {cells[0]} | {cells[1]} |")
    text = "\n".join(lines)
    (out / "summary.md").write_text(text + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(text)
    print("\n" + json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
