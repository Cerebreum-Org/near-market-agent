"""Claude CLI wrapper — supports both prompt mode (-p) and agentic mode.

Claude CLI requires a PTY on stdin to avoid hanging, so we allocate one
via Python's pty module while still capturing stdout via PIPE.

Agentic mode uses --print with --agent and --dangerously-skip-permissions
to let Claude Code read/edit files, run commands, etc.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pty
import subprocess

log = logging.getLogger(__name__)


class ClaudeCLI:
    """Wrapper around Claude Code CLI supporting prompt and agentic modes."""

    DEFAULT_TIMEOUT = 600  # 10 minutes

    def __init__(self, model: str = "sonnet", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens

    def _run(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int | None = None,
        agent: str | None = None,
        workdir: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout: int | None = None,
        model: str | None = None,
    ) -> str:
        """Run a prompt through claude CLI and return text output.

        Args:
            prompt: The user prompt.
            system: System prompt (prompt mode only, ignored with agent).
            max_tokens: Max output tokens (not directly supported by CLI, reserved).
            agent: Agent name (e.g. 'code-simplifier'). Enables agentic mode.
            workdir: Working directory for agentic mode.
            allowed_tools: List of allowed tools for agentic mode.
            timeout: Timeout in seconds (overrides DEFAULT_TIMEOUT).
            model: Model override (uses self.model if not specified).
        """
        effective_model = model or self.model
        effective_timeout = timeout or self.DEFAULT_TIMEOUT

        cmd = [
            "claude",
            "-p",
            "--model", effective_model,
            "--output-format", "text",
        ]

        if agent:
            cmd.extend(["--agent", agent])
            cmd.append("--dangerously-skip-permissions")

        if allowed_tools:
            cmd.extend(["--allowedTools", *allowed_tools])

        if system and not agent:
            cmd.extend(["--system-prompt", system])
        elif system and agent:
            cmd.extend(["--append-system-prompt", system])

        cmd.append(prompt)

        # Claude CLI needs a PTY on stdin or it hangs
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                cwd=workdir,
            )
            os.close(slave_fd)
            slave_fd = -1

            stdout, stderr = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError(
                f"claude CLI timed out after {effective_timeout}s "
                f"(agent={agent}, model={effective_model})"
            )
        finally:
            if slave_fd != -1:
                os.close(slave_fd)
            os.close(master_fd)

        if proc.returncode != 0:
            err = stderr.decode().strip() if stderr else "unknown error"
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err}")

        return stdout.decode().strip()

    async def _run_async(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int | None = None,
        agent: str | None = None,
        workdir: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout: int | None = None,
        model: str | None = None,
    ) -> str:
        """Async version — runs in a thread."""
        return await asyncio.to_thread(
            self._run, prompt, system, max_tokens, agent, workdir,
            allowed_tools, timeout, model,
        )

    def create_message(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """Prompt mode — returns raw text."""
        return self._run(user, system=system, max_tokens=max_tokens, timeout=timeout)

    async def create_message_async(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> str:
        return await self._run_async(user, system=system, max_tokens=max_tokens, timeout=timeout)

    def run_agent(
        self,
        agent: str,
        prompt: str,
        system: str | None = None,
        workdir: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout: int | None = None,
        model: str | None = None,
    ) -> str:
        """Agentic mode — Claude Code with tools, file access, etc."""
        return self._run(
            prompt,
            system=system,
            agent=agent,
            workdir=workdir,
            allowed_tools=allowed_tools,
            timeout=timeout,
            model=model,
        )

    async def run_agent_async(
        self,
        agent: str,
        prompt: str,
        system: str | None = None,
        workdir: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout: int | None = None,
        model: str | None = None,
    ) -> str:
        return await self._run_async(
            prompt,
            system=system,
            agent=agent,
            workdir=workdir,
            allowed_tools=allowed_tools,
            timeout=timeout,
            model=model,
        )

    def simplify_file(self, file_path: str) -> str:
        """Run the code-simplifier agent on a file and return simplified content."""
        workdir = os.path.dirname(os.path.abspath(file_path))
        filename = os.path.basename(file_path)

        prompt = (
            f"Simplify the file '{filename}' in the current directory. "
            f"Read it, apply all simplification rules, and write the simplified "
            f"version back to the same file. Keep all functionality intact."
        )

        self.run_agent(
            agent="code-simplifier",
            prompt=prompt,
            workdir=workdir,
        )

        with open(file_path) as f:
            return f.read()

    def create_conversation(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
    ) -> str:
        """Multi-turn via flattened prompt (CLI doesn't support multi-turn natively)."""
        parts = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                parts.append(f"[User]\n{content}")
            elif role == "assistant":
                parts.append(f"[Assistant]\n{content}")
        prompt = "\n\n".join(parts)
        return self._run(prompt, system=system, max_tokens=max_tokens)

    async def create_conversation_async(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.create_conversation, system, messages, max_tokens
        )
