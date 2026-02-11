from __future__ import annotations

import time
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class BinanceRestClient:
    """REST client for Binance USD-M public endpoints."""

    def __init__(
        self,
        base_url: str,
        exchange_info_endpoint: str,
        ticker_24h_endpoint: str,
        client: httpx.AsyncClient,
        exchange_cache_ttl_sec: int = 300,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._exchange_info_endpoint = exchange_info_endpoint
        self._ticker_24h_endpoint = ticker_24h_endpoint
        self._client = client
        self._exchange_cache_ttl_sec = exchange_cache_ttl_sec
        self._exchange_cache: set[str] | None = None
        self._exchange_cache_ts = 0.0

    @retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _get_json(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        response = await self._client.get(url, timeout=10.0)
        response.raise_for_status()
        return response.json()

    async def get_perpetual_usdt_symbols(self) -> set[str]:
        """Return a cached set of USDT perpetual symbols."""
        now = time.time()
        if self._exchange_cache and (now - self._exchange_cache_ts) < self._exchange_cache_ttl_sec:
            return self._exchange_cache

        data = await self._get_json(self._exchange_info_endpoint)
        symbols: set[str] = set()
        for item in data.get("symbols", []):
            if (
                item.get("quoteAsset") == "USDT"
                and item.get("contractType") == "PERPETUAL"
                and item.get("status") == "TRADING"
            ):
                symbols.add(item.get("symbol"))

        self._exchange_cache = symbols
        self._exchange_cache_ts = now
        return symbols

    async def get_ticker_24h(self) -> list[dict[str, Any]]:
        """Return the 24h ticker list."""
        data = await self._get_json(self._ticker_24h_endpoint)
        if isinstance(data, list):
            return data
        return []


    async def get_klines(self, symbol: str, interval: str, limit: int = 60) -> list[list[Any]]:
        """Return kline/candlestick data for symbol."""
        url = f"{self._base_url}/fapi/v1/klines"
        response = await self._client.get(
            url,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        return []

    async def get_mark_price(self, symbol: str) -> dict[str, Any]:
        """Return mark price and funding rate for symbol."""
        url = f"{self._base_url}/fapi/v1/premiumIndex"
        response = await self._client.get(
            url,
            params={"symbol": symbol},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()

    async def get_open_interest(self, symbol: str) -> dict[str, Any]:
        """Return open interest for symbol."""
        url = f"{self._base_url}/fapi/v1/openInterest"
        response = await self._client.get(
            url,
            params={"symbol": symbol},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()

    async def get_top_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict[str, Any]]:
        """Return top long/short account ratio."""
        url = f"{self._base_url}/fapi/v1/topLongShortAccountRatio"
        response = await self._client.get(
            url,
            params={"symbol": symbol, "period": period, "limit": limit},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        return []
