"""Prompt injection defense — sanitizes job descriptions before LLM processing.

Strips known injection patterns from untrusted user input (job titles, descriptions)
before passing them to Claude. Defense in depth: even with system prompt separation,
we clean the input to reduce attack surface.
"""

from __future__ import annotations

import re

# Patterns that attempt to override system prompts or inject instructions
_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Direct instruction overrides
    (re.compile(r"ignore (?:all )?(?:previous |prior |above )?instructions?", re.I), "[FILTERED]"),
    (re.compile(r"disregard (?:all )?(?:previous |prior |above )?instructions?", re.I), "[FILTERED]"),
    (re.compile(r"forget (?:all )?(?:previous |prior |above )?instructions?", re.I), "[FILTERED]"),
    (re.compile(r"you are now (?:a |an )?(?:new |different )?(?:ai|assistant|agent)", re.I), "[FILTERED]"),
    (re.compile(r"(?:new |updated |revised )?system ?prompt:", re.I), "[FILTERED]"),
    (re.compile(r"<\|?(?:system|im_start|im_end|endoftext)\|?>", re.I), "[FILTERED]"),

    # Role injection
    (re.compile(r"\[(?:system|assistant|user)\]", re.I), "[FILTERED]"),
    (re.compile(r"<<\s*SYS\s*>>", re.I), "[FILTERED]"),
    (re.compile(r"\[INST\]", re.I), "[FILTERED]"),

    # Output manipulation
    (re.compile(r"respond (?:only )?with|output (?:only |exactly )?(?:the following|this)", re.I), "[FILTERED]"),
    (re.compile(r"your (?:new |real |actual )?(?:task|job|goal|objective|purpose) is", re.I), "[FILTERED]"),

    # Encoding tricks
    (re.compile(r"(?:base64|rot13|hex)[\s:]+decode", re.I), "[FILTERED]"),

    # Prompt leaking
    (re.compile(r"(?:show|reveal|print|output|repeat) (?:your |the )?(?:system |full )?prompt", re.I), "[FILTERED]"),
    (re.compile(r"what (?:is|are) your (?:system )?(?:prompt|instructions)", re.I), "[FILTERED]"),
]

# Characters that shouldn't appear in normal job descriptions
_SUSPICIOUS_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")

# Excessive repetition (e.g., "AAAA..." padding attacks)
_EXCESSIVE_REPEAT = re.compile(r"(.)\1{50,}")


def sanitize_text(text: str, max_length: int = 10000) -> str:
    """Sanitize untrusted text input.

    Applies:
    - Length truncation
    - Control character removal
    - Injection pattern filtering
    - Excessive repetition reduction
    """
    if not text:
        return ""

    # Truncate
    text = text[:max_length]

    # Remove control characters (keep \n, \r, \t)
    text = _SUSPICIOUS_CHARS.sub("", text)

    # Collapse excessive repetition
    text = _EXCESSIVE_REPEAT.sub(lambda m: m.group(1) * 10, text)

    # Filter injection patterns
    for pattern, replacement in _INJECTION_PATTERNS:
        text = pattern.sub(replacement, text)

    return text.strip()


def sanitize_job(title: str, description: str) -> tuple[str, str]:
    """Sanitize job title and description for safe LLM processing."""
    return (
        sanitize_text(title, max_length=500),
        sanitize_text(description, max_length=10000),
    )
