# Bug-fix agent

An AI agent that fixes a reported bug in a Python repository. It investigates the code, writes a test that reproduces the bug, fixes the code, runs the tests and reports what it did. It always works on a copy of the repository, so the original is never changed.

Status: Day 1 of 7. Working: the agent loop, code tools, a free Gemini backend (default) and a paid Claude backend, step and cost limits, run logs. Planned: Docker sandbox (Day 2), enforced reproduce-then-fix checks (Day 3), SWE-bench evaluation (Days 4 to 6), GitHub issue in and draft pull request out (Day 6).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...   # free key from aistudio.google.com (no card needed)
```

The Gemini free tier costs nothing but has per-minute and per-day request limits (shown in AI Studio); the agent waits and retries when it hits the per-minute limit and stops cleanly when the daily quota is used up. Google may use free-tier prompts to improve its products, so use it on public or toy code, not private code.

The paid Claude backend (`--backend claude`) needs `ANTHROPIC_API_KEY` and prepaid credits from console.anthropic.com.

## Run

```bash
.venv/bin/python -m bugfix_agent --repo examples/toy_repo --issue-file examples/toy_issue.txt
```

| Option | Default | Meaning |
|---|---|---|
| `--repo` | required | Repository to fix (copied into the run folder) |
| `--issue` / `--issue-file` | required | Bug report text |
| `--backend` | `gemini` | `gemini` (free tier) or `claude` (paid) |
| `--model` | Gemini fallback list / `claude-sonnet-5-5` | Gemini: one model or a comma-separated list tried in order; busy (503), retired (404) or out-of-quota models are skipped. Default list: 3.8-flash, 3.7-flash, 3.6-flash, 3.5-flash, 3.5-flash-lite, 3.1-flash-lite |
| `--effort` | model default | Reasoning depth: minimal, low, medium, high, xhigh, max |
| `--max-steps` | 40 | Maximum model calls |
| `--max-cost` | 1.0 | Paid backends: stop before the next model call once this many US dollars are spent |
| `--min-interval` | 0 | Gemini: minimum seconds between requests |

Each run creates `runs/<timestamp>/` with:
- `repo/`: the working copy with the agent's changes (`git diff` shows them)
- `trajectory.jsonl`: every model call and tool call, with tokens and cost
- `summary.json`: status, root cause, steps, cost, final diff
- `messages.json`: the full conversation

## Tools the agent has

`list_files`, `search_code`, `read_file` (with line numbers), `edit_file` (replace one exact match, or create a file), `run_command` (no shell; only python, pytest, ls, cat, head, tail, wc, find, grep and read-only git; timeout), `finish`. Paths cannot leave the repository. Until Day 2, commands run on this machine inside the copied repository.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The loop tests use a scripted fake model, so they need no API key and cost nothing.

## Layout

```
bugfix_agent/   tools.py, backends.py, agent.py, prompts.py, __main__.py
examples/       toy_repo/ (textstats with a planted median bug) and toy_issue.txt
tests/          tool tests and loop tests
runs/           run outputs (not tracked by git)
```
