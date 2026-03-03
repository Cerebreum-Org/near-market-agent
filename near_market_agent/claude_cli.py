"""Claude CLI wrapper — replaces direct Anthropic API calls with `claude -p`."""

from __future__ import annotations

import asyncio
import json
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

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {stderr}")
        return result.stdout.strip()

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
