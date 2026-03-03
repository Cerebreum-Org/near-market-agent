"""Claude CLI wrapper — replaces direct Anthropic API calls with `claude -p`.

Claude CLI requires a PTY on stdin to avoid hanging, so we allocate one
via Python's pty module while still capturing stdout via PIPE.
"""

from __future__ import annotations

import asyncio
import os
import pty
import subprocess


class ClaudeCLI:
    """Thin wrapper around `claude -p` for non-interactive LLM calls."""

    def __init__(self, model: str = "sonnet", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens

    def _run(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Run a single prompt through claude CLI and return the text output."""
        cmd = [
            "claude",
            "-p",
            "--model", self.model,
            "--output-format", "text",
        ]
        if system:
            cmd.extend(["--system-prompt", system])
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
            )
            os.close(slave_fd)
            slave_fd = -1  # mark as closed

            stdout, stderr = proc.communicate(timeout=300)
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
    ) -> str:
        """Async version — runs in a thread to not block the event loop."""
        return await asyncio.to_thread(self._run, prompt, system, max_tokens)

    def create_message(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> str:
        """Drop-in replacement for anthropic messages.create — returns raw text."""
        return self._run(user, system=system, max_tokens=max_tokens)

    async def create_message_async(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> str:
        """Async version of create_message."""
        return await self._run_async(user, system=system, max_tokens=max_tokens)

    def create_conversation(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
    ) -> str:
        """Handle multi-turn conversations by flattening into a single prompt.

        Claude CLI doesn't support multi-turn natively in -p mode,
        so we serialize the conversation into the prompt.
        """
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
        """Async version of create_conversation."""
        return await asyncio.to_thread(
            self.create_conversation, system, messages, max_tokens
        )
