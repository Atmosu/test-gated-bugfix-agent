# Test-gated bug-fix agent

An AI agent that fixes a reported bug in a Python repository and has to prove it. It investigates the code, writes a test that reproduces the bug, fixes the code and runs the tests. When it says the bug is fixed, the harness (not the model) checks the proof: the new test must fail on the original code and pass with the fix, the whole suite must pass, and no test may be deleted or skipped. Every command runs in a locked Docker container. With a GitHub issue as input, it opens a draft pull request after a person approves.

**Software development phase:** maintenance (corrective maintenance: fixing bugs reported after release), using tests from the testing phase as proof.

**Presentation:** [presentation/Test-Gated_Bug-Fix_Agent_Detailed.pptx](presentation/Test-Gated_Bug-Fix_Agent_Detailed.pptx) (19 slides: phase, purpose, design, functionality, implementation, demo, results). The 11-slide version used for the class talk is [presentation/Test-Gated_Bug-Fix_Agent.pptx](presentation/Test-Gated_Bug-Fix_Agent.pptx).

## Demo results

Two GitHub issues on a demo repository (a copy of `bench/shopkit` with planted bugs; the repository is private, so the logs are copied here):

| Issue | Steps | Time | Checks | Fix | Log |
|---|---|---|---|---|---|
| #1 Cart total ignores item quantities | 10 | 156 s | 7 / 7 | `price` → `price * qty` in `Cart.total()`, plus a regression test; draft PR opened after approval | [docs/sample_runs/issue-1](docs/sample_runs/issue-1) |
| #2 Settings in capital letters are ignored | 13 | 132 s | 7 / 7 | `value.strip()` → `value.strip().lower()` in `parse_bool()`, plus test cases | [docs/sample_runs/issue-2](docs/sample_runs/issue-2) |

Each folder has `summary.json` (status, checks, root cause, diff) and `trajectory.jsonl` (every model call and tool call, with the model's one-line reason).

**Quick demo without GitHub:** after the setup below, run the agent on the small example project with a planted median bug:

```bash
.venv/bin/python -m bugfix_agent --repo examples/toy_repo --issue-file examples/toy_issue.txt
```

With no API key, `.venv/bin/python -m pytest -q` runs the agent's own 60 tests with a fake model.

## How it works

1. **Input:** a GitHub issue (`--github OWNER/REPO#N`) or a local repository plus issue text.
2. **Agent loop:** the model picks one tool call at a time: `list_files`, `search_code`, `read_file`, `edit_file` (replace one exact match, or create a file), `run_command` (allowlisted programs, no shell, timeout) and `finish`.
3. **Sandbox:** commands run in a Docker container with no network, a non-root user, 2 GB memory, 2 CPUs, 256 processes and all capabilities dropped.
4. **Gates** (`bugfix_agent/gates.py`), run when the agent calls `finish` with status `fixed`:
   1. a regression test was added or changed
   2. application code was changed
   3. no existing test was deleted
   4. no skip, xfail, `noqa`, `pylint: disable` or `type: ignore` was added
   5. the new test fails on the original code (checked in a separate copy with only the source changes undone)
   6. the new test passes with the fix
   7. the full test suite passes

   If a check fails, `finish` is refused and the agent sees the report; after 3 refusals the run stops.
5. **Output:** a diff, a run log and, in GitHub mode, a draft pull request (after you type `y`) with the root cause and the check table. GitHub credentials never enter the sandbox.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
docker build -t bugfix-agent-sandbox:py3.12 sandbox/
export GEMINI_API_KEY=...   # free key from aistudio.google.com (no card needed)
```

Docker Desktop must be running. GitHub mode needs the GitHub CLI signed in (`gh auth login`).

**Models.** The default backend is the free Gemini API. It tries `gemini-3.8-flash`, `3.7-flash`, `3.6-flash`, `3.5-flash`, `3.5-flash-lite` and `3.1-flash-lite` in order, skipping any model that is overloaded (503), retired (404) or out of daily quota, and waits when it hits a per-minute limit. Google may use free-tier prompts to improve its products, so use it on public or demo code, not private code. A paid Claude backend (`--backend claude`, `ANTHROPIC_API_KEY`) is also available.

## Run

```bash
# GitHub issue in, draft pull request out (asks before opening the PR); use a repository you can push to
.venv/bin/python -m bugfix_agent --github OWNER/REPO#N

# Local repository
.venv/bin/python -m bugfix_agent --repo examples/toy_repo --issue-file examples/toy_issue.txt
```

| Option | Default | Meaning |
|---|---|---|
| `--github` | | Issue to fix, `OWNER/REPO#N` or an issue URL |
| `--yes` | off | GitHub mode: open the draft PR without asking |
| `--repo`, `--issue` / `--issue-file` | | Local repository (copied, never changed) and bug report |
| `--backend` | `gemini` | `gemini` (free) or `claude` (paid) |
| `--model` | fallback list | Gemini: one model or a comma-separated list |
| `--sandbox` | `docker` | `docker` or `local` (commands run on this machine) |
| `--max-steps` | 40 | Maximum model calls |
| `--max-cost` | 1.0 | Paid backends: stop once this many US dollars are spent |
| `--no-gates` | off | Trust the agent's `finish` (comparison runs only; not allowed with `--github`) |
| `--runs-dir` | `~/bugfix-agent-runs` | Where run folders go |

Run folders live outside `~/Desktop` and `~/Documents` on purpose: on macOS, Docker Desktop needs extra permission for those folders, and a blocked mount freezes Docker.

Each run folder holds `repo/` (the working copy), `trajectory.jsonl` (every model call, tool call and gate report), `summary.json` (status, checks, diff) and `messages.json`.

## Evaluation

`bench/` holds a small shop library (`bench/shopkit`, 12 visible tests) and 10 planted bugs (`bench/bugs.py`), each with an issue text and a hidden test the agent never sees.

```bash
.venv/bin/python bench/run_bench.py --validate   # every bug: visible tests pass, hidden test fails, passes on correct code
.venv/bin/python bench/run_bench.py --out bench/results/main   # 10 bugs x {gates, nogates}; resumable
.venv/bin/python bench/summarize.py bench/results/main
```

A run counts as resolved when the hidden test and all visible tests pass on the agent's final code, in the sandbox.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The agent's own tests use a scripted fake model and a fake Gemini client, so they need no API key and cost nothing. Docker tests are skipped when the sandbox image is missing.

## Layout

```
bugfix_agent/   agent.py (loop), tools.py, gates.py, runner.py (Docker/local), backends.py (Gemini, Claude),
                github.py, prompts.py, __main__.py
sandbox/        Dockerfile for the sandbox image
bench/          shopkit/, bugs.py, run_bench.py, summarize.py, results/
examples/       toy_repo/ with a planted median bug, toy_issue.txt
tests/          tests for tools, gates, loop, Gemini backend and GitHub helpers
docs/           sample_runs/ (logs of the two demo runs), planning/ (research summary written before the build;
                it planned an open model with Claude escalation, replaced by the free Gemini fallback list)
presentation/   slides
```
