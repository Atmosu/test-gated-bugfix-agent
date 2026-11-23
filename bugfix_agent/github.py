"""GitHub in and out: read an issue, clone the repo, and (after human approval) open a draft PR.

Uses the GitHub CLI (`gh`) on the host. The agent and its sandbox never see GitHub credentials;
only this module, after the checks pass and a person approves, pushes a branch.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REF = re.compile(r"^(?:https://github\.com/)?([\w.-]+)/([\w.-]+)(?:#|/issues/)(\d+)$")
# Use gh as the credential helper for this push only; global git config is not changed.
GIT_AUTH = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


class GitHubError(Exception):
    pass


@dataclass
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.slug}#{self.number}"


def parse_ref(text: str) -> IssueRef:
    m = REF.match(text.strip())
    if not m:
        raise GitHubError(f"'{text}' is not an issue reference like OWNER/REPO#12 or an issue URL.")
    return IssueRef(m.group(1), m.group(2), int(m.group(3)))


def _run(cmd: list[str], cwd: Path | None = None, input: str | None = None) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input)
    if p.returncode != 0:
        raise GitHubError(f"{' '.join(cmd[:3])} failed: {(p.stderr or p.stdout).strip()[:500]}")
    return p.stdout


def fetch_issue(ref: IssueRef) -> dict:
    data = json.loads(_run(["gh", "issue", "view", str(ref.number), "-R", ref.slug,
                            "--json", "title,body,url,state"]))
    if data["state"] != "OPEN":
        raise GitHubError(f"Issue {ref} is {data['state'].lower()}.")
    return data


def issue_text(issue: dict) -> str:
    return f"{issue['title']}\n\n{issue['body'] or ''}".strip()


def clone(ref: IssueRef, dest: Path) -> Path:
    _run(["gh", "repo", "clone", ref.slug, str(dest), "--", "--quiet"])
    return dest


def _identity() -> tuple[str, str]:
    """Commit as the signed-in GitHub user, with their private no-reply address."""
    user = json.loads(_run(["gh", "api", "user"]))
    return user.get("name") or user["login"], f"{user['id']}+{user['login']}@users.noreply.github.com"


def pr_body(ref: IssueRef, summary: dict) -> str:
    fin = summary["finish"] or {}
    checks = "\n".join(f"| {c['name']} | {'pass' if c['passed'] else 'FAIL'} |"
                       for c in (summary["gates"] or {}).get("checks", []))
    return f"""Fixes #{ref.number}

**Root cause.** {fin.get('root_cause', '')}

**Change.** {fin.get('summary', '')}

**Checks run by the harness** (the agent cannot skip these):

| Check | Result |
|---|---|
{checks}

**Last test run reported by the agent:** {fin.get('tests_run', '')}

Opened as a draft by bugfix-agent after human approval. Steps: {summary['steps']}, model: {summary['backend']}, \
sandbox: {summary['sandbox']}. Please review before merging.
"""


def open_draft_pr(ref: IssueRef, issue: dict, summary: dict) -> str:
    workdir = Path(summary["workdir"])
    branch = f"bugfix-agent/issue-{ref.number}"
    name, email = _identity()
    git = ["git", "-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]
    default = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=workdir).strip()
    default = default.split("/", 1)[1] if "/" in default else default
    _run([*git, "checkout", "-q", "-b", branch], cwd=workdir)
    _run([*git, "add", "-A"], cwd=workdir)
    title = f"Fix #{ref.number}: {issue['title']}"
    _run([*git, "commit", "-q", "-m", title, "-m", (summary["finish"] or {}).get("root_cause", "")], cwd=workdir)
    _run(["git", *GIT_AUTH, "push", "-q", "-u", "origin", branch], cwd=workdir)
    url = _run(["gh", "pr", "create", "-R", ref.slug, "--draft", "--base", default, "--head", branch,
                "--title", title, "--body-file", "-"], cwd=workdir, input=pr_body(ref, summary))
    return url.strip().splitlines()[-1]
