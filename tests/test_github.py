import pytest

from bugfix_agent.github import GitHubError, IssueRef, parse_ref, pr_body


def test_parse_ref_forms():
    assert parse_ref("Atmosu/demo#3") == IssueRef("Atmosu", "demo", 3)
    assert parse_ref("https://github.com/Atmosu/demo/issues/12") == IssueRef("Atmosu", "demo", 12)
    with pytest.raises(GitHubError):
        parse_ref("demo#3")


def test_pr_body_lists_checks_and_links_issue():
    summary = {"finish": {"root_cause": "rc", "summary": "s", "tests_run": "7 passed"}, "steps": 9,
               "backend": "gemini:x", "sandbox": "DockerRunner",
               "gates": {"passed": True, "checks": [{"name": "full test suite passes", "passed": True}]}}
    body = pr_body(IssueRef("a", "b", 5), summary)
    assert body.startswith("Fixes #5") and "| full test suite passes | pass |" in body and "rc" in body
