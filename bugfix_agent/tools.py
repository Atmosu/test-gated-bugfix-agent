"""Tools the agent can call. Every tool works inside one repository root.

The schemas are plain JSON Schema so the same tool set can later be given to an
open model. Inputs are validated here before a tool runs, because eager input
streaming means the API does not validate them for us.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}
MAX_OUTPUT_CHARS = 12_000
MAX_READ_LINES = 400
MAX_TIMEOUT_S = 600

# Executables run_command may start. git is limited to read-only subcommands.
ALLOWED_COMMANDS = {"python", "python3", "pytest", "ls", "cat", "head", "tail", "wc", "find", "grep"}
ALLOWED_GIT = {"diff", "status", "log", "show"}
# A word that is, starts with, or ends with a shell operator: ';', '|', '&&', '>', '2>&1', '$VAR', '`cmd`'.
SHELL_OPERATOR = re.compile(r"^(\d?[<>|&;]|\$|`)|[;|&`]$")


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_SPECS: list[dict] = [
    {
        "name": "list_files",
        "description": "List files under a directory of the repository (recursive glob). "
                       "Use this first to see the project layout. Skips .git, virtualenvs and caches.",
        "input_schema": _obj({
            "path": {"type": "string", "description": "Directory relative to the repo root, e.g. '.' or 'src'."},
            "pattern": {"type": "string", "description": "Glob pattern such as '*.py' or 'test_*.py'. Default '*'."},
        }, ["path"]),
    },
    {
        "name": "search_code",
        "description": "Search text files for a Python regular expression. Returns 'file:line: text' matches. "
                       "Use it to find where a function, class, error message or symbol is defined or used.",
        "input_schema": _obj({
            "pattern": {"type": "string", "description": "Python regular expression."},
            "path": {"type": "string", "description": "File or directory to search, relative to the repo root. Default '.'."},
            "max_results": {"type": "integer", "description": "Maximum matches to return (default 50, at most 200)."},
        }, ["pattern"]),
    },
    {
        "name": "read_file",
        "description": "Read a file with line numbers. Read only the lines you need for large files "
                       f"(at most {MAX_READ_LINES} lines per call).",
        "input_schema": _obj({
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "start_line": {"type": "integer", "description": "First line to show, 1-based. Default 1."},
            "end_line": {"type": "integer", "description": "Last line to show, inclusive."},
        }, ["path"]),
    },
    {
        "name": "edit_file",
        "description": "Replace exactly one occurrence of old_str with new_str in a file. old_str must match the "
                       "file exactly, including indentation, and appear once; include surrounding lines to make it "
                       "unique. With old_str empty, creates a new file containing new_str (fails if it exists).",
        "input_schema": _obj({
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "old_str": {"type": "string", "description": "Exact text to replace, or empty to create a new file."},
            "new_str": {"type": "string", "description": "Replacement text."},
        }, ["path", "old_str", "new_str"]),
    },
    {
        "name": "run_command",
        "description": "Run one command in the repository root, without a shell. Allowed programs: "
                       + ", ".join(sorted(ALLOWED_COMMANDS)) + ", and git " + "/".join(sorted(ALLOWED_GIT))
                       + ". Pipes, redirects, ';', '&&' and '$' are not allowed. Use it to run tests, "
                         "e.g. 'python -m pytest -q' or 'python -m pytest tests/test_x.py::test_y -q'.",
        "input_schema": _obj({
            "command": {"type": "string", "description": "The command line."},
            "timeout_s": {"type": "integer", "description": f"Timeout in seconds (default 120, at most {MAX_TIMEOUT_S})."},
        }, ["command"]),
    },
    {
        "name": "finish",
        "description": "Call this once when the bug is fixed and the tests pass, or when you cannot fix it. "
                       "This ends the run.",
        "input_schema": _obj({
            "status": {"type": "string", "enum": ["fixed", "not_fixed"]},
            "root_cause": {"type": "string", "description": "One or two sentences on what caused the bug."},
            "summary": {"type": "string", "description": "What you changed and why, or why you could not fix it."},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "tests_run": {"type": "string", "description": "The test command(s) you ran last and their result."},
        }, ["status", "root_cause", "summary", "files_changed", "tests_run"]),
    },
]
SPECS_BY_NAME = {s["name"]: s for s in TOOL_SPECS}

_PY_TYPES = {"string": str, "integer": int, "array": list, "object": dict, "boolean": bool}


def validate_input(name: str, args: object) -> str | None:
    """Return an error message if args do not match the tool's schema, else None."""
    spec = SPECS_BY_NAME.get(name)
    if spec is None:
        return f"Unknown tool '{name}'."
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    schema = spec["input_schema"]
    props = schema["properties"]
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}."
    extra = [k for k in args if k not in props]
    if extra:
        return f"Unknown field(s): {', '.join(extra)}."
    for key, value in args.items():
        expected = _PY_TYPES[props[key]["type"]]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            return f"Field '{key}' must be of type {props[key]['type']}."
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"Field '{key}' must be one of {props[key]['enum']}."
        if props[key]["type"] == "array" and not all(isinstance(v, str) for v in value):
            return f"Field '{key}' must be a list of strings."
    return None


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail of long output; the middle is usually least useful."""
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    cut = len(text) - head - tail
    return f"{text[:head]}\n\n[... {cut} characters omitted ...]\n\n{text[-tail:]}"


class ToolError(Exception):
    """An error the model should see and recover from."""


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    finished: dict | None = None  # set by the finish tool


class Toolbox:
    def __init__(self, root: Path, default_timeout_s: int = 120, runner=None):
        from .runner import LocalRunner
        self.root = Path(root).resolve()
        self.default_timeout_s = default_timeout_s
        self.runner = runner or LocalRunner(self.root)  # DockerRunner in normal runs

    # -- helpers -----------------------------------------------------------
    def _path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise ToolError(f"Path '{rel}' is outside the repository.")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def _walk(self, base: Path, pattern: str = "*"):
        if base.is_file():
            yield base
            return
        for p in sorted(base.rglob(pattern)):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(self.root).parts):
                yield p

    # -- dispatcher --------------------------------------------------------
    def run(self, name: str, args: dict) -> ToolResult:
        err = validate_input(name, args)
        if err:
            return ToolResult(f"Invalid input for {name}: {err}", is_error=True)
        try:
            if name == "finish":
                return ToolResult("Run finished.", finished=dict(args))
            out = getattr(self, f"_t_{name}")(**args)
            return ToolResult(truncate(out))
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        except (OSError, UnicodeDecodeError) as e:
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    # -- tools -------------------------------------------------------------
    def _t_list_files(self, path: str, pattern: str = "*") -> str:
        base = self._path(path)
        if not base.exists():
            raise ToolError(f"'{path}' does not exist.")
        files = [self._rel(p) for p in self._walk(base, pattern)]
        if not files:
            return "No files match."
        shown = files[:300]
        more = f"\n... and {len(files) - 300} more" if len(files) > 300 else ""
        return "\n".join(shown) + more

    def _t_search_code(self, pattern: str, path: str = ".", max_results: int = 50) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}")
        base = self._path(path)
        if not base.exists():
            raise ToolError(f"'{path}' does not exist.")
        limit = max(1, min(max_results, 200))
        hits: list[str] = []
        for p in self._walk(base):
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable file
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(f"{self._rel(p)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n[stopped at {limit} matches]"
        return "\n".join(hits) if hits else "No matches."

    def _t_read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"'{path}' is not a file.")
        lines = p.read_text(encoding="utf-8").splitlines()
        start = max(1, start_line)
        end = len(lines) if end_line is None else min(end_line, len(lines))
        end = min(end, start + MAX_READ_LINES - 1)
        if start > len(lines):
            return f"File has {len(lines)} lines."
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        note = f"\n[showing lines {start}-{end} of {len(lines)}]" if (start > 1 or end < len(lines)) else ""
        return body + note

    def _t_edit_file(self, path: str, old_str: str, new_str: str) -> str:
        p = self._path(path)
        if old_str == "":
            if p.exists():
                raise ToolError(f"'{path}' already exists; give old_str to edit it.")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_str, encoding="utf-8")
            return f"Created {path} ({len(new_str.splitlines())} lines)."
        if not p.is_file():
            raise ToolError(f"'{path}' is not a file.")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            raise ToolError("old_str was not found. Re-read the file and copy the text exactly, including indentation.")
        if count > 1:
            raise ToolError(f"old_str appears {count} times; add surrounding lines so it is unique.")
        p.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        line = text[: text.index(old_str)].count("\n") + 1
        return f"Edited {path} at line {line}."

    def _t_run_command(self, command: str, timeout_s: int | None = None) -> str:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"Could not parse command: {e}")
        if not argv:
            raise ToolError("Empty command.")
        # No shell runs, so operators would be passed as plain text; reject them so the model
        # learns to run one command at a time. Quoted code such as python -c 'a; b' is fine.
        if any(SHELL_OPERATOR.match(tok) for tok in argv):
            raise ToolError("Pipes, redirects, ';', '&&' and '$' are not allowed. Run one plain command.")
        prog = Path(argv[0]).name
        if prog == "git":
            if len(argv) < 2 or argv[1] not in ALLOWED_GIT:
                raise ToolError(f"Only git {', '.join(sorted(ALLOWED_GIT))} are allowed.")
        elif prog not in ALLOWED_COMMANDS:
            raise ToolError(f"'{prog}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}, git.")
        from .runner import CommandTimeout
        timeout = max(1, min(timeout_s or self.default_timeout_s, MAX_TIMEOUT_S))
        try:
            proc = self.runner.run(argv, timeout)
        except CommandTimeout as e:
            raise ToolError(str(e))
        except FileNotFoundError:
            raise ToolError(f"'{argv[0]}' was not found.")
        out = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return f"[exit code {proc.returncode}]\n{out.strip()}"
