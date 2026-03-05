"""HTTP client for market.near.ai API.

Features:
- Connection pooling (configurable max connections)
- Rate limit tracking (respects 429 + Retry-After headers)
- Exponential backoff with jitter
- Request metrics
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config
from .models import (
    AgentProfile,
    Bid,
    Job,
    Message,
    WalletBalance,
)

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 3, 10]  # seconds
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Connection pool config
MAX_CONNECTIONS = 20
MAX_KEEPALIVE_CONNECTIONS = 10


@dataclass
class RateLimitState:
    """Tracks rate limit state from API responses."""

    remaining: int | None = None
    limit: int | None = None
    reset_at: float | None = None  # unix timestamp
    retry_after: float | None = None
    last_429_at: float | None = None
    consecutive_429s: int = 0


@dataclass
class RequestMetrics:
    """Tracks API request metrics."""

    total_requests: int = 0
    successful: int = 0
    retried: int = 0
    rate_limited: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total_requests if self.total_requests else 0.0


class MarketAPIError(Exception):
    """API error with status code and detail."""

    def __init__(self, status: int, detail: str, url: str = ""):
        self.status = status
        self.detail = detail
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {detail}")


class MarketClient:
    """Async client for the NEAR Agent Market API.

    Features connection pooling, rate limit tracking, and request metrics.
    """

    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self.rate_limit = RateLimitState()
        self.metrics = RequestMetrics()

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily create (or recreate) the httpx client with connection pooling."""
        if self._client is None or self._client.is_closed:
            pool_limits = httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=30.0,
            )
            self._client = httpx.AsyncClient(
                base_url=self.config.api_url,
                headers={
                    "Authorization": f"Bearer {self.config.market_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "near-market-agent/0.1.0",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=pool_limits,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        self._ensure_client()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # --- Internal ---

    def _update_rate_limit(self, resp: httpx.Response) -> None:
        """Extract rate limit info from response headers."""
        rl = self.rate_limit
        if "x-ratelimit-remaining" in resp.headers:
            try:
                rl.remaining = int(resp.headers["x-ratelimit-remaining"])
            except ValueError:
                pass
        if "x-ratelimit-limit" in resp.headers:
            try:
                rl.limit = int(resp.headers["x-ratelimit-limit"])
            except ValueError:
                pass
        if "x-ratelimit-reset" in resp.headers:
            try:
                rl.reset_at = float(resp.headers["x-ratelimit-reset"])
            except ValueError:
                pass
        if resp.status_code == 429:
            rl.last_429_at = time.monotonic()
            rl.consecutive_429s += 1
            self.metrics.rate_limited += 1
            # Parse Retry-After header
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    rl.retry_after = float(retry_after)
                except ValueError:
                    rl.retry_after = None
        else:
            rl.consecutive_429s = 0
            rl.retry_after = None

    async def _wait_for_rate_limit(self) -> None:
        """Wait if we're rate-limited before making a request."""
        rl = self.rate_limit
        if rl.retry_after and rl.last_429_at:
            elapsed = time.monotonic() - rl.last_429_at
            remaining = rl.retry_after - elapsed
            if remaining > 0:
                await asyncio.sleep(min(remaining, 60))  # cap at 60s
        elif rl.remaining is not None and rl.remaining <= 1:
            # Preemptive slow-down when nearly exhausted
            await asyncio.sleep(1.0)

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        await self._wait_for_rate_limit()

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            start = time.monotonic()
            self.metrics.total_requests += 1
            try:
                resp = await self._ensure_client().request(method, path, **kwargs)
                elapsed_ms = (time.monotonic() - start) * 1000
                self.metrics.total_latency_ms += elapsed_ms
                self._update_rate_limit(resp)

                if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
                    # Use Retry-After if available, otherwise exponential backoff with jitter
                    wait = self.rate_limit.retry_after or RETRY_BACKOFF[attempt]
                    jitter = random.uniform(0, wait * 0.2)
                    self.metrics.retried += 1
                    await asyncio.sleep(wait + jitter)
                    continue
                if (
                    resp.status_code in RETRYABLE_STATUS
                    and resp.status_code != 429
                    and attempt < MAX_RETRIES - 1
                ):
                    jitter = random.uniform(0, RETRY_BACKOFF[attempt] * 0.3)
                    self.metrics.retried += 1
                    await asyncio.sleep(RETRY_BACKOFF[attempt] + jitter)
                    continue
                if resp.status_code >= 400:
                    self.metrics.errors += 1
                    raise MarketAPIError(resp.status_code, resp.text[:500], str(resp.url))
                if resp.status_code == 204:
                    self.metrics.successful += 1
                    return None
                self.metrics.successful += 1
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return resp.text
            except MarketAPIError:
                raise
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
            ) as e:
                elapsed_ms = (time.monotonic() - start) * 1000
                self.metrics.total_latency_ms += elapsed_ms
                self.metrics.errors += 1
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    jitter = random.uniform(0, RETRY_BACKOFF[attempt] * 0.3)
                    self.metrics.retried += 1
                    await asyncio.sleep(RETRY_BACKOFF[attempt] + jitter)
                    continue
                raise MarketAPIError(0, f"Connection failed after {MAX_RETRIES} retries: {e}", path)
        raise last_error or MarketAPIError(0, "Unknown retry failure", path)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict | None = None) -> Any:
        return await self._request("POST", path, json=json)

    async def _patch(self, path: str, json: dict | None = None) -> Any:
        return await self._request("PATCH", path, json=json)

    @staticmethod
    def _parse_list(data: Any, key: str, model_cls: type) -> list:
        """Parse API responses that may be a bare list or {key: [...]}."""
        if isinstance(data, list):
            return [model_cls.model_validate(item) for item in data]
        if isinstance(data, dict):
            return [model_cls.model_validate(item) for item in data.get(key, [])]
        return []

    # --- Agent ---

    async def get_profile(self) -> AgentProfile:
        data = await self._get("/agents/me")
        return AgentProfile.model_validate(data)

    async def get_agent(self, agent_id_or_handle: str) -> AgentProfile:
        data = await self._get(f"/agents/{agent_id_or_handle}")
        return AgentProfile.model_validate(data)

    # --- Wallet ---

    async def get_balance(self) -> WalletBalance:
        data = await self._get("/wallet/balance")
        return WalletBalance.model_validate(data)

    # --- Jobs ---

    async def list_jobs(
        self,
        status: str = "open",
        tags: str | None = None,
        search: str | None = None,
        job_type: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        params: dict[str, Any] = {
            "status": status,
            "sort": sort,
            "order": order,
            "limit": limit,
            "offset": offset,
        }
        if tags:
            params["tags"] = tags
        if search:
            params["search"] = search
        if job_type:
            params["job_type"] = job_type

        data = await self._get("/jobs", params=params)
        # list_jobs has an extra fallback: {"jobs": [...]} or {"data": [...]}
        if isinstance(data, list):
            return [Job.model_validate(j) for j in data]
        if not isinstance(data, dict):
            return []
        jobs_list = data.get("jobs") or data.get("data") or []
        return [Job.model_validate(j) for j in jobs_list]

    async def get_job(self, job_id: str) -> Job:
        data = await self._get(f"/jobs/{job_id}")
        return Job.model_validate(data)

    async def create_job(self, **kwargs) -> Job:
        data = await self._post("/jobs", json=kwargs)
        return Job.model_validate(data)

    # --- Bids ---

    async def place_bid(
        self,
        job_id: str,
        amount: str,
        eta_seconds: int,
        proposal: str,
    ) -> Bid:
        data = await self._post(
            f"/jobs/{job_id}/bids",
            json={"amount": amount, "eta_seconds": eta_seconds, "proposal": proposal},
        )
        return Bid.model_validate(data)

    async def get_my_bids(self) -> list[Bid]:
        return self._parse_list(await self._get("/agents/me/bids"), "bids", Bid)

    async def get_job_bids(self, job_id: str) -> list[Bid]:
        return self._parse_list(await self._get(f"/jobs/{job_id}/bids"), "bids", Bid)

    async def withdraw_bid(self, bid_id: str) -> dict:
        return await self._post(f"/bids/{bid_id}/withdraw")

    # --- Work Submission ---

    async def _submit_work(self, path: str, deliverable: str, deliverable_hash: str | None) -> dict:
        """Shared submission logic for deliverables and competition entries."""
        if not deliverable or not deliverable.strip():
            raise ValueError("Cannot submit empty deliverable")
        body: dict[str, str] = {"deliverable": deliverable}
        if deliverable_hash:
            body["deliverable_hash"] = deliverable_hash
        return await self._post(path, json=body)

    async def submit_deliverable(
        self,
        job_id: str,
        deliverable: str,
        deliverable_hash: str | None = None,
    ) -> dict:
        return await self._submit_work(f"/jobs/{job_id}/submit", deliverable, deliverable_hash)

    async def submit_competition_entry(
        self,
        job_id: str,
        deliverable: str,
        deliverable_hash: str | None = None,
    ) -> dict:
        return await self._submit_work(f"/jobs/{job_id}/entries", deliverable, deliverable_hash)

    # --- Messages ---

    async def get_job_messages(self, job_id: str, limit: int = 50) -> list[Message]:
        data = await self._get(f"/jobs/{job_id}/messages", params={"limit": limit})
        return self._parse_list(data, "messages", Message)

    async def get_assignment_messages(self, assignment_id: str, limit: int = 50) -> list[Message]:
        data = await self._get(f"/assignments/{assignment_id}/messages", params={"limit": limit})
        return self._parse_list(data, "messages", Message)

    async def send_assignment_message(self, assignment_id: str, content: str) -> Message:
        data = await self._post(
            f"/assignments/{assignment_id}/messages",
            json={"content": content},
        )
        return Message.model_validate(data)
