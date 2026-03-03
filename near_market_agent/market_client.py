"""HTTP client for market.near.ai API."""

from __future__ import annotations

import asyncio
import httpx
import json
from typing import Any

from .config import Config
from .models import (
    AgentProfile, Job, Bid, Message, WalletBalance,
)

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 3, 10]  # seconds
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class MarketAPIError(Exception):
    """API error with status code and detail."""
    def __init__(self, status: int, detail: str, url: str = ""):
        self.status = status
        self.detail = detail
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {detail}")


class MarketClient:
    """Async client for the NEAR Agent Market API."""

    def __init__(self, config: Config):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily create (or recreate) the httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_url,
                headers={
                    "Authorization": f"Bearer {self.config.market_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "near-market-agent/0.1.0",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
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

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._ensure_client().request(method, path, **kwargs)
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    continue
                # Check 4xx/5xx BEFORE checking 204 (204 < 400 so order is fine,
                # but explicitly: non-retryable errors must raise immediately)
                if resp.status_code >= 400:
                    detail = resp.text[:500]
                    raise MarketAPIError(resp.status_code, detail, str(resp.url))
                if resp.status_code == 204:
                    return None
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return resp.text
            except MarketAPIError:
                raise  # Don't catch our own errors in the network handler below
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
            ) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    continue
                raise MarketAPIError(0, f"Connection failed after {MAX_RETRIES} retries: {e}", path)
        raise last_error or MarketAPIError(0, "Unknown retry failure", path)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict | None = None) -> Any:
        return await self._request("POST", path, json=json)

    async def _patch(self, path: str, json: dict | None = None) -> Any:
        return await self._request("PATCH", path, json=json)

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
        if isinstance(data, list):
            return [Job.model_validate(j) for j in data]
        if not isinstance(data, dict):
            return []
        # Some responses wrap in {"jobs": [...]} or {"data": [...]}
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
            json={
                "amount": amount,
                "eta_seconds": eta_seconds,
                "proposal": proposal,
            },
        )
        return Bid.model_validate(data)

    async def get_my_bids(self) -> list[Bid]:
        data = await self._get("/agents/me/bids")
        if isinstance(data, list):
            return [Bid.model_validate(b) for b in data]
        if not isinstance(data, dict):
            return []
        return [Bid.model_validate(b) for b in data.get("bids", [])]

    async def get_job_bids(self, job_id: str) -> list[Bid]:
        data = await self._get(f"/jobs/{job_id}/bids")
        if isinstance(data, list):
            return [Bid.model_validate(b) for b in data]
        if not isinstance(data, dict):
            return []
        return [Bid.model_validate(b) for b in data.get("bids", [])]

    async def withdraw_bid(self, bid_id: str) -> dict:
        return await self._post(f"/bids/{bid_id}/withdraw")

    # --- Work Submission ---

    async def submit_deliverable(
        self,
        job_id: str,
        deliverable: str,
        deliverable_hash: str | None = None,
    ) -> dict:
        if not deliverable or not deliverable.strip():
            raise ValueError("Cannot submit empty deliverable")
        body: dict[str, str] = {"deliverable": deliverable}
        if deliverable_hash:
            body["deliverable_hash"] = deliverable_hash
        return await self._post(f"/jobs/{job_id}/submit", json=body)

    async def submit_competition_entry(
        self,
        job_id: str,
        deliverable: str,
        deliverable_hash: str | None = None,
    ) -> dict:
        if not deliverable or not deliverable.strip():
            raise ValueError("Cannot submit empty competition entry")
        body: dict[str, str] = {"deliverable": deliverable}
        if deliverable_hash:
            body["deliverable_hash"] = deliverable_hash
        return await self._post(f"/jobs/{job_id}/entries", json=body)

    # --- Messages ---

    async def get_job_messages(self, job_id: str, limit: int = 50) -> list[Message]:
        data = await self._get(f"/jobs/{job_id}/messages", params={"limit": limit})
        if isinstance(data, list):
            return [Message.model_validate(m) for m in data]
        if not isinstance(data, dict):
            return []
        return [Message.model_validate(m) for m in data.get("messages", [])]

    async def get_assignment_messages(
        self, assignment_id: str, limit: int = 50
    ) -> list[Message]:
        data = await self._get(
            f"/assignments/{assignment_id}/messages", params={"limit": limit}
        )
        if isinstance(data, list):
            return [Message.model_validate(m) for m in data]
        if not isinstance(data, dict):
            return []
        return [Message.model_validate(m) for m in data.get("messages", [])]

    async def send_assignment_message(
        self, assignment_id: str, content: str
    ) -> Message:
        data = await self._post(
            f"/assignments/{assignment_id}/messages",
            json={"content": content},
        )
        return Message.model_validate(data)
