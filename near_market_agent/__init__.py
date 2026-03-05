"""Autonomous agent for market.near.ai"""

__version__ = "0.1.0"

__all__ = ["extract_llm_text", "__version__"]


def extract_llm_text(response: object) -> str:
    """Extract text from Anthropic response content blocks."""
    blocks = getattr(response, "content", None)
    if not isinstance(blocks, list):
        return ""
    parts = [b.text for b in blocks if isinstance(getattr(b, "text", None), str)]
    return "\n".join(parts).strip()
