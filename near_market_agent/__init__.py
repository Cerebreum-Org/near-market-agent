"""Autonomous agent for market.near.ai"""
__version__ = "0.1.0"


def extract_llm_text(response: object) -> str:
    """Extract text from Anthropic response content blocks.

    Shared utility used by both JobEvaluator and WorkEngine.
    """
    blocks = getattr(response, "content", None)
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()
