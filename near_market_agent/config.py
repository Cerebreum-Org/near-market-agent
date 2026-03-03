"""Configuration management for the NEAR Market Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AgentCapabilities:
    """What this agent can do."""
    skills: list[str] = field(default_factory=lambda: [
        "research", "analysis", "technical-writing", "blog-posts",
        "documentation", "tutorials", "code-review", "python",
        "javascript", "typescript", "rust", "solidity",
        "data-analysis", "competitive-intelligence", "seo",
        "content-creation", "api-integration", "web-scraping",
    ])

    skip_categories: list[str] = field(default_factory=lambda: [
        "video-creation", "image-creation", "physical-task",
        "social-media-account", "voice-recording", "photography",
    ])

    description: str = (
        "Full-stack autonomous agent powered by Claude. Specializes in research, "
        "technical writing, code generation (Python, JS/TS, Rust, Solidity), "
        "data analysis, and content creation. Can handle complex multi-step tasks "
        "with structured deliverables."
    )


@dataclass
class Config:
    """Agent configuration loaded from environment."""
    # API keys
    market_api_key: str = ""
    anthropic_api_key: str = ""

    # Market settings
    market_base_url: str = "https://market.near.ai"
    api_version: str = "v1"

    # Agent behavior
    min_budget_near: float = 1.0       # Skip jobs below this
    max_concurrent_jobs: int = 3       # Max jobs to work on simultaneously
    poll_interval_seconds: int = 60    # How often to check for updates
    bid_confidence_threshold: float = 0.6  # Min eval score to bid

    # LLM settings
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096

    # Capabilities
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)

    # Runtime flags
    dry_run: bool = False
    verbose: bool = False
    log_dir: str = "logs"

    @property
    def api_url(self) -> str:
        return f"{self.market_base_url}/{self.api_version}"

    @classmethod
    def from_env(cls) -> Config:
        """Load config from environment variables."""
        return cls(
            market_api_key=os.environ.get("NEAR_MARKET_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            market_base_url=os.environ.get("NEAR_MARKET_URL", "https://market.near.ai"),
            min_budget_near=float(os.environ.get("MIN_BUDGET_NEAR", "1.0")),
            max_concurrent_jobs=int(os.environ.get("MAX_CONCURRENT_JOBS", "3")),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL", "60")),
            bid_confidence_threshold=float(os.environ.get("BID_THRESHOLD", "0.6")),
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
            verbose=os.environ.get("VERBOSE", "").lower() in ("1", "true", "yes"),
            log_dir=os.environ.get("LOG_DIR", "logs"),
        )

    def validate(self) -> list[str]:
        """Return list of validation errors."""
        errors = []
        if not self.market_api_key:
            errors.append("NEAR_MARKET_API_KEY not set")
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY not set")
        return errors
