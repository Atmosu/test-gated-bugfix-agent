"""Where commands run: on this machine (LocalRunner) or in a locked Docker container (DockerRunner)."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

SANDBOX_IMAGE = "bugfix-agent-sandbox:py3.12"
_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}


class CommandTimeout(Exception):
    pass


class SandboxError(Exception):
    pass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    root: Path

    def run(self, argv: list[str], timeout: int) -> Result: ...
    def close(self) -> None: ...


class LocalRunner:
    """Runs commands in the workspace on this machine, using this project's Python environment."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.env = dict(os.environ, **_ENV,
                        PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")

    def run(self, argv: list[str], timeout: int) -> Result:
        try:
            p = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise CommandTimeout(f"Command timed out after {timeout} s.")
        return Result(p.returncode, p.stdout or "", p.stderr or "")

    def close(self) -> None:
        pass


class DockerRunner:
    """One container per workspace: no network, capped memory/CPU/processes, workspace mounted at /repo."""

    def __init__(self, root: Path, image: str = SANDBOX_IMAGE, memory: str = "2g", cpus: str = "2"):
        self.root = Path(root).resolve()
        self.image, self.memory, self.cpus = image, memory, cpus
        self.container: str | None = None
        self._start()

    def _start(self, start_timeout: int = 60) -> None:
        last = ""
        for _ in range(2):  # Docker Desktop occasionally hangs on a start; give it one more chance
            name = f"bugfix-agent-{uuid.uuid4().hex[:10]}"
            cmd = ["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
                   "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", "256",
                   "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                   "-v", f"{self.root}:/repo", "-w", "/repo", self.image, "sleep", "infinity"]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=start_timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                last = f"container did not start within {start_timeout} s (restart Docker Desktop if this repeats)"
                continue
            if p.returncode == 0:
                self.container = name
                return
            last = p.stderr.strip()
        raise SandboxError("Could not start the Docker sandbox. Is Docker running and the image built "
                           f"(docker build -t {self.image} sandbox/)? {last}")

    def run(self, argv: list[str], timeout: int) -> Result:
        if self.container is None:
            self._start()
        cmd = ["docker", "exec", "-w", "/repo"] + [a for k, v in _ENV.items() for a in ("-e", f"{k}={v}")]
        try:
            p = subprocess.run([*cmd, self.container, *argv], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.close()  # kill whatever is still running; a fresh container starts on the next command
            raise CommandTimeout(f"Command timed out after {timeout} s; the sandbox was restarted.")
        if p.returncode in (126, 127) and "executable file not found" in (p.stderr or ""):
            return Result(127, "", f"'{argv[0]}' is not installed in the sandbox.")
        return Result(p.returncode, p.stdout or "", p.stderr or "")

    def close(self) -> None:
        if self.container:
            subprocess.run(["docker", "kill", self.container], capture_output=True)
            self.container = None


RunnerFactory = Callable[[Path], Runner]


def docker_available(image: str = SANDBOX_IMAGE) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def make_factory(kind: str) -> RunnerFactory:
    if kind == "docker":
        return lambda root: DockerRunner(root)
    if kind == "local":
        return lambda root: LocalRunner(root)
    raise ValueError(f"unknown sandbox '{kind}'")
