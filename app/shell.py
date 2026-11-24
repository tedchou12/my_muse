"""Approval-gated shell execution inside the Muse container.

This is deliberately a small, dependency-free runner. It is not a security
sandbox: the container is the boundary. The runner enforces a workspace
directory, a minimal environment, a wall-clock timeout, and output limits.
Commands run as the Muse container user, so they can still read files that user
can read; normal workspace commands run directly, while install commands,
unsafe paths, and non-GET downloads are approval-gated and remembered after approval.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Settings

MAX_COMMAND_CHARS = 12_000
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 120
DEFAULT_OUTPUT_CHARS = 16_000
MAX_OUTPUT_CHARS = 32_000

INSTALL_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\s)(?:sudo\s+)?"
    r"(?:pipx?|pip[0-9.]*|python[0-9.]*(?:\s+-m\s+pip)|uv|poetry|pdm|pipenv|"
    r"conda|mamba|micromamba|npm|yarn|pnpm|bun|apt(?:-get)?|apk|snap|flatpak|"
    r"zypper|pacman|yay|paru|yum|dnf|brew|gem|cargo|go|rustup)\s+"
    r"(?:install|i|add|ci|get|update|upgrade|require|download|sync|-S\w*)\b"
    r"|(?:^|[;&|]\s*)make\s+install\b"
    r"|(?:^|[;&|]\s*)bundle\s+install\b"
    r"|(?:^|[;&|]\s*)composer\s+require\b"
    r"|(?:^|[;&|]\s*)(?:npm|yarn|pnpm|bun)\s+(?:global|--global|-g)\s+(?:add|install)\b"
    r"|(?:^|[;&|]\s*)uv\s+pip\s+(?:install|sync)\b"
    r"|(?:^|[;&|]\s*)rustup\s+(?:toolchain|component|target)\s+(?:install|add)\b"
)
INSTALL_WORD_RE = re.compile(r"(?i)(?:^|[;&|]\s*)install\b")
CURL_MUTATING = {
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "-F",
    "--form",
    "-T",
    "--upload-file",
    "-X",
    "--request",
    "-K",
    "--config",
}
WGET_MUTATING = {
    "--post-data",
    "--post-file",
    "--method",
    "--body-data",
    "--body-file",
    "--config",
}
READ_COMMANDS = {
    "cat",
    "cd",
    "cut",
    "diff",
    "du",
    "df",
    "echo",
    "file",
    "find",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "printf",
    "sed",
    "sort",
    "stat",
    "tail",
    "tree",
    "uniq",
    "wc",
    "which",
    "curl",
    "wget",
}
GIT_READ = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "grep",
    "describe",
    "cat-file",
    "show-ref",
    "ls-remote",
    "clone",
    "fetch",
}
SAFE_GIT_REMOTE = {"-v", "--verbose", "show", "get-url"}
SAFE_SCRATCH_PATHS = {
    "/tmp",
    "/var/tmp",
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
}
SENSITIVE_PATH_ROOTS = (
    "/app",
    "/bin",
    "/boot",
    "/data",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/media",
    "/mnt",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/usr",
    "/var",
)
# `for p in "" "/blog" "/home/pricing"; do ...` is a list of string values
# (usually URL paths), not file access. Mask the list so URL-path fragments
# like /home/pricing do not look like sensitive filesystem paths.
FOR_LIST_RE = re.compile(r"\bfor\s+[A-Za-z_][A-Za-z0-9_]*\s+in\b[^;]*;")


def _segments(command: str) -> List[List[str]]:
    segments = []
    for raw in re.split(r"\s*(?:&&|\|\||[|;])\s*", command):
        if not raw.strip():
            continue
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            return []
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if tokens and tokens[0] in ("env", "sudo"):
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                tokens.pop(0)
        if tokens:
            segments.append(tokens)
    return segments


def _git_is_read(tokens: List[str]) -> bool:
    args = tokens[1:]
    while args and args[0] in ("-C", "--git-dir", "--work-tree"):
        args = args[1:]
        if args:
            args = args[1:]
    if not args:
        return False
    sub = args[0]
    if any(
        item in ("--ext-diff", "--textconv", "--upload-pack", "--receive-pack")
        or item.startswith(
            ("--ext-diff=", "--textconv=", "--upload-pack=", "--receive-pack=")
        )
        for item in args[1:]
    ):
        return False
    if sub in GIT_READ:
        return True
    if sub == "branch":
        rest = args[1:]
        if any(flag in rest for flag in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy")):
            return False
        return not any(not item.startswith("-") for item in rest)
    if sub == "remote":
        rest = args[1:]
        if not rest:
            return True
        return all(item in SAFE_GIT_REMOTE or item.startswith("--get") for item in rest)
    if sub == "config":
        return any(item in ("--get", "--get-all", "--list", "-l") for item in args[1:])
    return False


def _read_segment(tokens: List[str]) -> bool:
    first = tokens[0].rsplit("/", 1)[-1]
    if first == "git":
        return _git_is_read(tokens)
    if first == "pip" or first == "pip3":
        return len(tokens) > 1 and tokens[1] in ("list", "show", "freeze", "--version")
    if first in ("npm", "yarn", "pnpm"):
        return len(tokens) > 1 and tokens[1] in ("list", "ls", "--version", "-v")
    if first == "find":
        return not any(
            item in ("-exec", "-execdir", "-delete", "-ok", "-okdir")
            for item in tokens[1:]
        )
    if first == "sed":
        return not any(item == "-i" or item.startswith("--in-place") for item in tokens[1:])
    if first not in READ_COMMANDS:
        return False
    if first == "curl":
        for index, item in enumerate(tokens[1:]):
            if item.startswith("--request="):
                if item.split("=", 1)[1].upper() != "GET":
                    return False
                continue
            if item.startswith(("--data", "--form", "--upload-file", "--config")):
                return False
            if item.startswith(("-d", "-F", "-T", "-K")):
                return False
            if item in CURL_MUTATING:
                if item in ("-X", "--request"):
                    value = tokens[index + 2] if index + 2 < len(tokens) else ""
                    if value.upper() != "GET":
                        return False
                    continue
                return False
            if item.startswith("-X") and item[2:].upper() not in ("GET", ""):
                return False
        return True
    if first == "wget":
        return not any(
            item.startswith(tuple(WGET_MUTATING)) for item in tokens[1:]
        )
    return True


def _outside_path(candidate: str) -> bool:
    candidate = candidate.rstrip(";,")
    if not candidate:
        return False
    if candidate in ("/", "//") or candidate.startswith("//"):
        return False
    if "\\" in candidate or candidate.startswith(("/*", "/(?", "/[", "/:")):
        return False
    if "/../" in candidate or candidate.endswith("/.."):
        return True
    if candidate in SAFE_SCRATCH_PATHS or candidate.startswith(("/tmp/", "/var/tmp/")):
        return False
    if candidate == "/workspace" or candidate.startswith("/workspace/"):
        return False
    if candidate == ".." or candidate.startswith("../"):
        return True
    if candidate in ("~", "$HOME", "${HOME}"):
        return False
    if candidate.startswith(("~/", "$HOME/", "${HOME}/")):
        return False
    if candidate.startswith("~"):
        return True
    if re.match(r"^/\$[A-Za-z_][A-Za-z0-9_]*$", candidate):
        return False
    if candidate.startswith("/"):
        return any(
            candidate == root or candidate.startswith(root + "/")
            for root in SENSITIVE_PATH_ROOTS
        )
    return False


def _has_sensitive_path(command: str) -> bool:
    if re.search(r"(?i)\b(?:file|gopher|dict)://", command):
        return True
    # Loop variable lists are string values, not file paths.
    command = FOR_LIST_RE.sub(" ", command)
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    for token in tokens:
        values = [token]
        if "=" in token:
            values.append(token.split("=", 1)[1])
        for value in values:
            candidate = value
            if candidate.startswith("-C") and len(candidate) > 2:
                candidate = candidate[2:]
            if candidate.startswith(("-o", "-O")) and len(candidate) > 2:
                candidate = candidate[2:]
            if _outside_path(candidate):
                return True
    for match in re.finditer(r"""(['"])([^'"]*)\1""", command):
        if _outside_path(match.group(2)):
            return True
    for match in re.finditer(r"""(?<=['"=])(/[^'"\s;|&)]+)""", command):
        if _outside_path(match.group(1)):
            return True
    for match in re.finditer(r"""(?<![\w:/])(/[^\s'"`;|&)]+)""", command):
        if _outside_path(match.group(1)):
            return True
    return False


def shell_approval_reason(command: str) -> str:
    """Return the approval category, or '' when the command can run directly."""
    text = str(command or "").strip()
    if not text:
        return "empty"
    if INSTALL_RE.search(text) or INSTALL_WORD_RE.search(text):
        return "install"
    if _has_sensitive_path(text):
        return "path"
    for tokens in _segments(text):
        first = tokens[0].rsplit("/", 1)[-1]
        if first in ("curl", "wget") and not _read_segment(tokens):
            return "download"
    return ""


def shell_needs_approval(command: str) -> bool:
    """Only installs, unsafe paths, and non-GET downloads need approval."""
    return bool(shell_approval_reason(command))


def shell_command_family(command: str) -> str:
    """Return a trusted family for a command, currently only 'curl'."""
    text = str(command or "").strip()
    if not text:
        return ""
    for tokens in _segments(text):
        for token in tokens:
            if token.rsplit("/", 1)[-1] == "curl":
                return "curl"
    return ""


def shell_command_signature(command: str) -> str:
    return str(command or "").strip()


class ShellError(RuntimeError):
    pass


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def workspace_path(settings: Settings) -> Path:
    base = Path(settings.shell_workspace)
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def resolve_cwd(settings: Settings, requested: str = "") -> Path:
    base = workspace_path(settings)
    raw = (requested or "").strip()
    target = Path(raw) if raw else base
    if not target.is_absolute():
        target = base / target
    try:
        target = target.resolve()
        target.relative_to(base)
    except (OSError, ValueError) as exc:
        raise ShellError(
            "cwd must stay inside the shell workspace (%s)." % base
        ) from exc
    if not target.exists():
        raise ShellError("Working directory does not exist: %s" % target)
    if not target.is_dir():
        raise ShellError("Working directory is not a directory: %s" % target)
    return target


def shell_environment(settings: Settings) -> Dict[str, str]:
    home = str(workspace_path(settings))
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": home,
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _argv(settings: Settings, command: str) -> list[str]:
    return ["/bin/bash", "-c", command]


async def run_shell_command(
    settings: Settings,
    command: str,
    cwd: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    command = (command or "").strip()
    if not command:
        raise ShellError("command is required.")
    if len(command) > MAX_COMMAND_CHARS:
        raise ShellError("command is too long (max %d characters)." % MAX_COMMAND_CHARS)
    timeout = max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
    limit = max(1000, min(int(output_chars), MAX_OUTPUT_CHARS))
    working_dir = resolve_cwd(settings, cwd)
    started = time.monotonic()
    env = shell_environment(settings)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    proc = await asyncio.create_subprocess_exec(
        *_argv(settings, command),
        cwd=str(working_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        await proc.wait()
        stdout_b, stderr_b = b"", b""
    except asyncio.CancelledError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        await proc.wait()
        raise
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    return {
        "command": command,
        "cwd": str(working_dir),
        "exit_code": proc.returncode,
        "stdout": _clip(stdout, limit),
        "stderr": _clip(stderr, limit),
        "stdout_truncated": len(stdout) > limit,
        "stderr_truncated": len(stderr) > limit,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
