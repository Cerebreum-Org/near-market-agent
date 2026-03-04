"""Shared JSON extraction utility.

Single implementation used by both job_evaluator and work_engine
to parse JSON from LLM responses that may contain markdown or reasoning.
"""

from __future__ import annotations

import json
import re


_SENTINEL = object()


def extract_json(text: str, fallback: dict | None = _SENTINEL) -> dict | None:
    """Extract a JSON object from text that may contain surrounding prose.

    Tries in order:
    1. Direct parse (pure JSON)
    2. Markdown fenced block (```json ... ```)
    3. First balanced { ... } block via depth tracking

    Args:
        text: Raw LLM response text.
        fallback: Default dict if extraction fails. Pass None to return None on failure.
                  Default uses a generic fallback dict.

    Returns:
        Parsed dict from the JSON, or fallback.
    """
    if fallback is _SENTINEL:
        fallback = {"score": 0.5, "pass": False, "feedback": text[:500] if text else ""}

    text = text.strip()
    if not text:
        return fallback

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown fenced blocks
    if "```" in text:
        parts = text.split("```")
        for part in parts[1::2]:  # odd-indexed = inside fences
            content = part.strip()
            if content.startswith("json"):
                content = content[4:].strip()
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue

    # 3. First balanced { ... } block (depth tracking)
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return fallback
