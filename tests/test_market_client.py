"""Unit tests for market API client behavior."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from near_market_agent.config import Config
from near_market_agent.market_client import MarketAPIError, MarketClient


class MarketClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = MarketClient(Config(market_api_key="mk", anthropic_api_key="ak"))
        # Force client creation so tests can mock _client.request
        self.client._ensure_client()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_request_retries_retryable_status_then_succeeds(self) -> None:
        first = Mock()
        first.status_code = 503
        first.url = "https://market.near.ai/v1/jobs"
        first.text = "unavailable"
        first.headers = {}

        second = Mock()
        second.status_code = 200
        second.json.return_value = {"ok": True}
        second.headers = {}

        self.client._client.request = AsyncMock(side_effect=[first, second])

        with patch(
            "near_market_agent.market_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock:
            result = await self.client._request("GET", "/jobs")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.client._client.request.await_count, 2)
        # sleep now includes jitter, so just check it was called once
        self.assertEqual(sleep_mock.await_count, 1)

    async def test_request_raises_market_error_for_http_failure(self) -> None:
        resp = Mock()
        resp.status_code = 400
        resp.url = "https://market.near.ai/v1/jobs"
        resp.text = "bad request payload"
        resp.headers = {}
        self.client._client.request = AsyncMock(return_value=resp)

        with self.assertRaises(MarketAPIError) as ctx:
            await self.client._request("POST", "/jobs")

        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("bad request payload", ctx.exception.detail)

    async def test_request_retries_on_connect_error_then_fails(self) -> None:
        self.client._client.request = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch(
            "near_market_agent.market_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep_mock:
            with self.assertRaises(MarketAPIError) as ctx:
                await self.client._request("GET", "/jobs")

        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("Connection failed after 3 retries", ctx.exception.detail)
        self.assertEqual(sleep_mock.await_count, 2)

    async def test_list_jobs_parses_list_and_wrapped_payloads(self) -> None:
        self.client._get = AsyncMock(
            return_value=[
                {
                    "job_id": "j1",
                    "creator_agent_id": "a1",
                    "title": "T1",
                    "description": "D1",
                }
            ]
        )
        jobs = await self.client.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "j1")

        self.client._get = AsyncMock(
            return_value={
                "jobs": [
                    {
                        "job_id": "j2",
                        "creator_agent_id": "a2",
                        "title": "T2",
                        "description": "D2",
                    }
                ]
            }
        )
        jobs = await self.client.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "j2")

        self.client._get = AsyncMock(return_value="unexpected")
        jobs = await self.client.list_jobs()
        self.assertEqual(jobs, [])
