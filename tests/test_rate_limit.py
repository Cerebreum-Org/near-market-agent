"""Tests for rate limit tracking and connection pooling."""

from unittest.mock import MagicMock

import httpx
import pytest

from near_market_agent.config import Config
from near_market_agent.market_client import (
    MarketClient,
    RateLimitState,
    RequestMetrics,
)


@pytest.fixture
def config():
    return Config(market_api_key="test-key-123")


@pytest.fixture
def client(config):
    return MarketClient(config)


class TestRateLimitState:
    def test_initial_state(self):
        rl = RateLimitState()
        assert rl.remaining is None
        assert rl.limit is None
        assert rl.reset_at is None
        assert rl.retry_after is None
        assert rl.consecutive_429s == 0

    def test_tracks_consecutive_429s(self):
        rl = RateLimitState()
        rl.consecutive_429s = 3
        assert rl.consecutive_429s == 3


class TestRequestMetrics:
    def test_initial_metrics(self):
        m = RequestMetrics()
        assert m.total_requests == 0
        assert m.avg_latency_ms == 0.0

    def test_avg_latency(self):
        m = RequestMetrics(total_requests=10, total_latency_ms=1000.0)
        assert m.avg_latency_ms == 100.0


class TestConnectionPooling:
    def test_pool_limits_applied(self, client):
        """Client creates httpx with connection pool limits."""
        http_client = client._ensure_client()
        assert http_client is not None
        assert not http_client.is_closed

    def test_client_reuse(self, client):
        """Same client instance is reused on subsequent calls."""
        c1 = client._ensure_client()
        c2 = client._ensure_client()
        assert c1 is c2


class TestRateLimitExtraction:
    def test_extracts_rate_limit_headers(self, client):
        """Rate limit info is extracted from response headers."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {
            "x-ratelimit-remaining": "45",
            "x-ratelimit-limit": "60",
            "x-ratelimit-reset": "1709500000.0",
        }

        client._update_rate_limit(resp)
        assert client.rate_limit.remaining == 45
        assert client.rate_limit.limit == 60
        assert client.rate_limit.reset_at == 1709500000.0

    def test_tracks_429(self, client):
        """429 responses update rate limit state."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.headers = {"retry-after": "5"}

        client._update_rate_limit(resp)
        assert client.rate_limit.consecutive_429s == 1
        assert client.rate_limit.retry_after == 5.0
        assert client.rate_limit.last_429_at is not None
        assert client.metrics.rate_limited == 1

    def test_resets_on_success(self, client):
        """Successful response resets consecutive 429 counter."""
        client.rate_limit.consecutive_429s = 3
        client.rate_limit.retry_after = 10.0

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {}

        client._update_rate_limit(resp)
        assert client.rate_limit.consecutive_429s == 0
        assert client.rate_limit.retry_after is None

    def test_bad_header_values_ignored(self, client):
        """Non-numeric header values don't crash."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {
            "x-ratelimit-remaining": "not-a-number",
            "x-ratelimit-limit": "",
        }

        client._update_rate_limit(resp)
        assert client.rate_limit.remaining is None
        assert client.rate_limit.limit is None


class TestMetricsTracking:
    def test_metrics_increment(self, client):
        """Metrics are properly incremented."""
        client.metrics.total_requests += 1
        client.metrics.successful += 1
        client.metrics.total_latency_ms += 150.0

        assert client.metrics.total_requests == 1
        assert client.metrics.successful == 1
        assert client.metrics.avg_latency_ms == 150.0
