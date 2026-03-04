"""Job router — classifies jobs into tiers and routes to appropriate builders."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from .models import Job


class JobTier(enum.Enum):
    """Complexity tiers for marketplace jobs."""
    TEXT = "text"           # Guides, docs, tutorials, articles
    PACKAGE = "package"     # npm/pypi packages, MCP servers, libraries
    SERVICE = "service"     # Bots, extensions, deployed services
    SYSTEM = "system"       # Multi-component, orchestration, agents


# Agent name → matches .claude/agents/<name>.md
TIER_AGENTS = {
    JobTier.TEXT: "text-writer",
    JobTier.PACKAGE: "package-builder",
    JobTier.SERVICE: "service-builder",
    JobTier.SYSTEM: "system-builder",
}


@dataclass
class RoutingResult:
    """Result of job classification."""
    tier: JobTier
    agent: str
    template: str | None  # Template directory name, if any
    language: str          # Primary language hint
    reason: str            # Why this tier was chosen


def _lower_set(items: list[str] | None) -> set[str]:
    return {t.lower() for t in (items or [])}


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def classify(job: Job) -> RoutingResult:
    """Classify a job into a tier based on title, tags, and description.

    Fast keyword-based classification — no LLM call needed.
    """
    title = (job.title or "").lower()
    desc = (job.description or "").lower()
    tags = _lower_set(job.tags)
    combined = f"{title} {desc}"

    # Detect language
    language = "typescript"  # default for NEAR ecosystem
    if "python" in tags or "python" in combined or "pypi" in tags:
        language = "python"
    elif "rust" in tags or "rust" in combined or "cargo" in combined:
        language = "rust"
    elif "solidity" in tags or "solidity" in combined:
        language = "solidity"

    # --- Tier 4: System (check first — most specific) ---
    if _has_any(combined, ["multi-agent", "swarm", "orchestrat", "agent-to-agent",
                            "multi-marketplace", "cross-platform agent"]):
        return RoutingResult(
            tier=JobTier.SYSTEM, agent=TIER_AGENTS[JobTier.SYSTEM],
            template=None, language=language,
            reason="Multi-agent or orchestration keywords detected",
        )

    # --- Tier 3: Service (bots, extensions, deployed things) ---
    is_bot = _has_any(combined, ["discord bot", "telegram bot", "slack bot",
                                  "twitter bot", "chatbot"])
    is_extension = _has_any(combined, ["chrome extension", "vscode extension",
                                        "browser extension", "vs code"])
    is_deployed = (
        _has_any(combined, ["deploy", "hosting", "live server", "production"])
        and not _has_any(combined, ["guide", "tutorial", "write", "document"])
        and not _has_any(tags, ["github-action"])
    )
    if is_bot or is_extension or is_deployed:
        return RoutingResult(
            tier=JobTier.SERVICE, agent=TIER_AGENTS[JobTier.SERVICE],
            template=None, language=language,
            reason=f"Service: bot={is_bot}, extension={is_extension}, deploy={is_deployed}",
        )

    # --- Tier 2: Package (npm, pypi, MCP, library) ---
    is_npm = "npm" in tags or "npm" in title or "package" in title
    is_pypi = "pypi" in tags or "pypi" in title or "pip install" in combined
    is_mcp = ("mcp" in tags or "mcp" in combined) and _has_any(combined, ["server", "tool"])
    is_lib = _has_any(combined, ["library", "sdk", "module", "framework"])
    is_github_action = "github-action" in tags or "github action" in combined
    is_code_build = (
        _has_any(title, ["build", "create", "develop", "implement"])
        and not _has_any(combined, ["guide", "tutorial", "write about", "article", "blog",
                                     "course", "documentation"])
    )

    template = None
    if is_mcp:
        template = "mcp-server"
    elif is_pypi or (is_code_build and language == "python"):
        template = "pypi-package"
    elif is_npm or (is_code_build and language == "typescript"):
        template = "npm-package"

    if is_npm or is_pypi or is_mcp or is_lib or is_github_action or is_code_build:
        return RoutingResult(
            tier=JobTier.PACKAGE, agent=TIER_AGENTS[JobTier.PACKAGE],
            template=template, language=language,
            reason=f"Package: npm={is_npm}, pypi={is_pypi}, mcp={is_mcp}, "
                   f"lib={is_lib}, action={is_github_action}, build={is_code_build}",
        )

    # --- Tier 1: Text (default fallback) ---
    return RoutingResult(
        tier=JobTier.TEXT, agent=TIER_AGENTS[JobTier.TEXT],
        template=None, language=language,
        reason="No code/package/service signals — defaulting to text",
    )
