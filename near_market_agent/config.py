"""Configuration management for the NEAR Market Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default, cast=float):
    """Read a numeric env var with safe fallback."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    """Read a comma-separated env var as a list."""
    raw = os.environ.get(name)
    if raw is None:
        return default or []
    return [s.strip() for s in raw.split(",") if s.strip()]


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
class TierConfig:
    """Per-tier configuration for timeouts and model overrides."""
    # Timeout in seconds for builder agent runs
    text_timeout: int = 300        # Text jobs are simpler
    package_timeout: int = 600     # Package builds need more time
    service_timeout: int = 900     # Service builds are complex
    system_timeout: int = 1200     # Multi-agent systems are the most complex

    # Optional per-tier model overrides (empty = use default)
    text_model: str = ""
    package_model: str = ""
    service_model: str = ""
    system_model: str = ""

    # Disabled tiers (won't bid on these)
    disabled_tiers: list[str] = field(default_factory=list)

    def timeout_for(self, tier: str) -> int:
        """Get timeout in seconds for a given tier."""
        return {
            "text": self.text_timeout,
            "package": self.package_timeout,
            "service": self.service_timeout,
            "system": self.system_timeout,
        }.get(tier, self.package_timeout)

    def model_for(self, tier: str, default: str) -> str:
        """Get model for a given tier, falling back to default."""
        override = {
            "text": self.text_model,
            "package": self.package_model,
            "service": self.service_model,
            "system": self.system_model,
        }.get(tier, "")
        return override or default

    def is_disabled(self, tier: str) -> bool:
        """Check if a tier is disabled."""
        return tier in self.disabled_tiers


@dataclass
class Config:
    """Agent configuration loaded from environment."""
    # API keys
    market_api_key: str = ""
    anthropic_api_key: str = ""  # Deprecated — using Claude CLI instead

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

    # Per-tier settings
    tiers: TierConfig = field(default_factory=TierConfig)

    # Capabilities
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)

    # GitHub publishing
    github_org: str = ""              # GitHub org/user for code delivery repos
    github_author_name: str = "NEAR Market Agent"
    github_author_email: str = "agent@market.near.ai"

    # Web search
    tavily_api_key: str = ""          # Tavily API key for research phase (free at tavily.com)

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
            min_budget_near=_env("MIN_BUDGET_NEAR", 1.0),
            max_concurrent_jobs=_env("MAX_CONCURRENT_JOBS", 3, int),
            poll_interval_seconds=_env("POLL_INTERVAL", 60, int),
            bid_confidence_threshold=_env("BID_THRESHOLD", 0.6),
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=_env("MAX_TOKENS", 4096, int),
            tiers=TierConfig(
                text_timeout=_env("TIER_TEXT_TIMEOUT", 300, int),
                package_timeout=_env("TIER_PACKAGE_TIMEOUT", 600, int),
                service_timeout=_env("TIER_SERVICE_TIMEOUT", 900, int),
                system_timeout=_env("TIER_SYSTEM_TIMEOUT", 1200, int),
                text_model=os.environ.get("TIER_TEXT_MODEL", ""),
                package_model=os.environ.get("TIER_PACKAGE_MODEL", ""),
                service_model=os.environ.get("TIER_SERVICE_MODEL", ""),
                system_model=os.environ.get("TIER_SYSTEM_MODEL", ""),
                disabled_tiers=_env_list("DISABLED_TIERS"),
            ),
            github_org=os.environ.get("GITHUB_ORG", ""),
            github_author_name=os.environ.get("GITHUB_AUTHOR_NAME", "NEAR Market Agent"),
            github_author_email=os.environ.get("GITHUB_AUTHOR_EMAIL", "agent@market.near.ai"),
            tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
            dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
            verbose=os.environ.get("VERBOSE", "").lower() in ("1", "true", "yes"),
            log_dir=os.environ.get("LOG_DIR", "logs"),
        )

    def validate(self) -> list[str]:
        """Return list of validation errors."""
        errors = []
        if not self.market_api_key:
            errors.append("NEAR_MARKET_API_KEY not set")
        if self.min_budget_near < 0:
            errors.append("MIN_BUDGET_NEAR must be >= 0")
        if self.max_concurrent_jobs < 1:
            errors.append("MAX_CONCURRENT_JOBS must be >= 1")
        if self.poll_interval_seconds < 1:
            errors.append("POLL_INTERVAL must be >= 1")
        if not 0 <= self.bid_confidence_threshold <= 1:
            errors.append("BID_THRESHOLD must be between 0 and 1")
        if self.max_tokens < 1:
            errors.append("MAX_TOKENS must be >= 1")
        valid_tiers = {"text", "package", "service", "system"}
        for t in self.tiers.disabled_tiers:
            if t not in valid_tiers:
                errors.append(f"Invalid disabled tier: {t}")
        return errors
