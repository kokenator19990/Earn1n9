from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .binance_rest import BinanceRestClient
from .storage import Storage
from .telegram_notifier import TelegramNotifier
from .trade_rating import compute_rate, percentile_rank, find_swing_lows

# --- RecentCandidatesCache ---
class RecentCandidatesCache:
    """Mantiene símbolos calificados recientes con first_seen_at, last_seen_at, age_minutes y TTL."""
    def __init__(self, ttl_sec: int = 1200):
        self._data: Dict[str, dict] = {}
        self._ttl_sec = ttl_sec
        self._lock = threading.Lock()

    def on_candidate(self, symbol: str, qualifies: bool, now: Optional[float] = None) -> None:
        now = now or time.time()
        with self._lock:
            if qualifies:
                if symbol not in self._data:
                    self._data[symbol] = {
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "metrics": {},
                    }
                else:
                    self._data[symbol]["last_seen_at"] = now
            else:
                # No lo mostramos, pero mantenemos last_seen_at para TTL
                if symbol in self._data:
                    self._data[symbol]["last_seen_at"] = now

    def update_metrics(self, symbol: str, metrics: dict[str, Any]) -> None:
        with self._lock:
            if symbol in self._data:
                self._data[symbol]["metrics"] = metrics

    def get_recent(self, max_age_min: float = 5, min_rate: float = 7.2, rate_map: Optional[dict] = None) -> list:
        now = time.time()
        result = []
        with self._lock:
            for symbol, entry in self._data.items():
                age_min = (now - entry["first_seen_at"]) / 60.0
                if age_min > max_age_min:
                    continue
                
                # Check metrics rate if available
                metrics = entry.get("metrics", {})
                rate = metrics.get("rate", 0.0)
                if rate < min_rate:
                    continue
                    
                result.append({
                    "symbol": symbol,
                    "first_seen_at": entry["first_seen_at"],
                    "last_seen_at": entry["last_seen_at"],
                    "age_minutes": age_min,
                    "metrics": metrics,
                    "rate": rate 
                })
        
        # Sort by Rate DESC
        result.sort(key=lambda x: x["rate"], reverse=True)
        return result

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            to_del = [s for s, e in self._data.items() if (now - e["last_seen_at"]) > self._ttl_sec]
            for s in to_del:
                del self._data[s]


@dataclass
class TickerView:
    symbol: str
    last_price: float
    change_pct: float
    quote_volume: float


@dataclass
class TopSnapshot:
    timestamp: float
    symbols: set[str]
    data: dict[str, dict[str, Any]]


class Scanner:
    """Scan Binance futures and emit alerts based on filters and cooldowns."""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        notifier: TelegramNotifier,
        storage: Storage,
        min_24h_change_pct: float,
        min_quote_volume_usdt: float,
        top_n: int,
        cooldown_min: int,
        logger,
    ) -> None:
        self._rest = rest_client
        self._notifier = notifier
        self._storage = storage
        self._min_24h_change_pct = min_24h_change_pct
        self._min_quote_volume_usdt = min_quote_volume_usdt
        self._top_n = top_n
        self._cooldown_sec = cooldown_min * 60
        self._logger = logger
        self._cooldowns: dict[str, float] = {}
        self._lock = threading.Lock()
        self._current_top: list[TickerView] = []
        self._snapshot_history: deque[TopSnapshot] = deque(maxlen=20)
        self._snapshot_retention_sec = 150
        self._entry_times: dict[str, float] = {}
        self._kline_cache: dict[tuple[str, str], tuple[float, list[dict[str, float]]]] = {}
        self._kline_cache_ttl_sec = 90
        
        self._funding_cache: dict[str, tuple[float, float]] = {} # symbol -> (timestamp, rate)
        self._funding_cache_ttl_sec = 120

        # --- RecentCandidatesCache ---
        self._recent_candidates = RecentCandidatesCache(ttl_sec=1200)

    def get_top(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "symbol": item.symbol,
                    "lastPrice": item.last_price,
                    "priceChangePercent": item.change_pct,
                    "quoteVolume": item.quote_volume,
                }
                for item in self._current_top
            ]

    def get_top_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                item.symbol: {
                    "lastPrice": item.last_price,
                    "priceChangePercent": item.change_pct,
                    "quoteVolume": item.quote_volume,
                }
                for item in self._current_top
            }

    async def _calculate_metrics(self, symbol: str, regime_score: float = 1.0, allowed_symbols_count: int = 150) -> dict[str, Any]:
        """Calculate advanced metrics and Rate for a symbol."""
        now = time.time()
        
        # 1. Base Ticker Data
        params = self._get_ticker_view(symbol)
        last_price = params.last_price
        change_pct = params.change_pct
        quote_vol = params.quote_volume
        
        # Calculate Volume Rank
        vol_rank_score = 0.5 # Default
        with self._lock:
            vols = [t.quote_volume for t in self._current_top]
        
        if vols:
            vol_rank_score = percentile_rank(vols, quote_vol)
        
        # 2. Fetch Klines (1m, 1h, 1d) with Cache
        # 1m Klines
        k1m = await self._ensure_klines_cached(symbol, "1m", limit=60)
        # 1h Klines (for support)
        k1h = await self._ensure_klines_cached(symbol, "1h", limit=100)
        
        # 3. Compute Features
        vol_z = 0.0
        wick_ratio = 0.0
        ret_5m = 0.0
        
        if k1m and len(k1m) > 5:
            current = k1m[-1]
            # 5m Return
            closes = [k["close"] for k in k1m]
            if len(closes) > 5:
                prev_5m = closes[-6]
                ret_5m = (current["close"] - prev_5m) / prev_5m if prev_5m else 0.0

            # Vol Z (vs 60m avg)
            volumes = [k["volume"] for k in k1m]
            if len(volumes) >= 20:
                recent_vols = volumes[:-1] # exclude current incomplete
                if recent_vols:
                    avg_v = sum(recent_vols) / len(recent_vols)
                    std_v = (sum((x - avg_v)**2 for x in recent_vols) / len(recent_vols))**0.5
                    if std_v > 0:
                        vol_z = (current["volume"] - avg_v) / std_v
            
            # Wick Ratio
            rng = current["high"] - current["low"]
            if rng > 0:
                wick = current["high"] - max(current["open"], current["close"])
                wick_ratio = wick / rng

        # Support Logic (1h swings)
        nearest_support = None
        if k1h and len(k1h) > 5:
            lows = [k["low"] for k in k1h]
            swings = find_swing_lows(lows)
            candidates = [s for s in swings if s < last_price]
            if candidates:
                nearest_support = max(candidates)
        
        # Recent High
        recent_high = None
        if k1m:
             recent_high = max(k["high"] for k in k1m)

        # 4. Fetch Funding (Cached)
        funding_rate = await self._get_cached_funding(symbol)

        # 5. Compute Rate
        rate_res = compute_rate(
            change24h_pct=change_pct,
            quote_volume=quote_vol,
            volume_rank=vol_rank_score,
            last_price=last_price,
            recent_high=recent_high,
            nearest_support=nearest_support,
            funding_rate=funding_rate,
            regime_score=regime_score,
            wick_ratio=wick_ratio,
            vol_z=vol_z
        )

        return {
            "rate": rate_res.rate,
            "rate_components": asdict(rate_res.components),
            "debug": rate_res.debug,
            "ret_5m": ret_5m,
            "vol_z_1m": vol_z,
            "fundingRate": funding_rate
        }

    async def run(self, refresh_sec: int, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            start = time.time()
            try:
                allowed_symbols = await self._rest.get_perpetual_usdt_symbols()
                tickers = await self._rest.get_ticker_24h()
                top = self._compute_top(tickers, allowed_symbols)
                with self._lock:
                    self._current_top = top
                    self._save_snapshot(top)
                await self._refresh_kline_cache(top)
                await self._process_alerts(top)
                
                # --- Update RecentCandidatesCache ---
                now = time.time()
                top_symbols = {item.symbol for item in top}
                
                # Update candidates based on current top
                for item in top:
                    self._recent_candidates.on_candidate(item.symbol, qualifies=True, now=now)
                
                # Mark missing ones as not qualifying
                for symbol in list(self._recent_candidates._data.keys()):
                    if symbol not in top_symbols:
                        self._recent_candidates.on_candidate(symbol, qualifies=False, now=now)
                
                self._recent_candidates.cleanup()

                # --- Regime Score (BTCUSDT) ---
                regime_score = 1.0
                try:
                    btc_klines = await self._ensure_klines_cached("BTCUSDT", "15m", limit=60)
                    if btc_klines and len(btc_klines) > 10:
                        # Trend vs Volatility
                        closes = [k["close"] for k in btc_klines]
                        if len(closes) > 4:
                            ret_60m = abs((closes[-1] - closes[-5]) / closes[-5])
                            returns = [(closes[i] - closes[i-1])/closes[i-1] for i in range(1, len(closes))]
                            recent_ret = returns[-4:]
                            if len(recent_ret) > 1:
                                mean_r = sum(recent_ret)/len(recent_ret)
                                vol_60m = (sum((r - mean_r)**2 for r in recent_ret)/len(recent_ret))**0.5
                                if vol_60m > 0:
                                    ratio = ret_60m / vol_60m
                                    if ratio > 3.0:
                                        regime_score = 0.0 # Extreme
                                    elif ratio > 2.0:
                                        regime_score = 0.5
                except Exception:
                    pass

                # --- Calculate & Update Metrics for Recent Candidates ---
                recent_objs = self._recent_candidates.get_recent(max_age_min=30, min_rate=0)
                
                # Limit to top 20 verified candidates
                for obj in recent_objs[:20]: 
                    symbol = obj["symbol"]
                    metrics = await self._calculate_metrics(symbol, regime_score=regime_score)
                    self._recent_candidates.update_metrics(symbol, metrics)

                self._logger.info(
                    "scan_cycle",
                    extra={"top_count": len(top), "recent_count": len(recent_objs)},
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.error("scan_failed", extra={"error": str(exc)})

            elapsed = time.time() - start
            sleep_for = max(0.0, refresh_sec - elapsed)
            await self._wait_or_stop(sleep_for, stop_event)

    async def _wait_or_stop(self, seconds: float, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    def _compute_top(
        self, tickers: list[dict[str, Any]], allowed_symbols: set[str]
    ) -> list[TickerView]:
        results: list[TickerView] = []
        for item in tickers:
            symbol = item.get("symbol")
            if not symbol or symbol not in allowed_symbols:
                continue
            if not symbol.endswith("USDT"):
                continue

            change_pct = self._parse_float(item.get("priceChangePercent"))
            quote_volume = self._parse_float(item.get("quoteVolume"))
            last_price = self._parse_float(item.get("lastPrice"))

            if change_pct < self._min_24h_change_pct:
                continue
            if quote_volume < self._min_quote_volume_usdt:
                continue

            results.append(
                TickerView(
                    symbol=symbol,
                    last_price=last_price,
                    change_pct=change_pct,
                    quote_volume=quote_volume,
                )
            )

        results.sort(key=lambda item: item.change_pct, reverse=True)
        return results[: self._top_n]

    async def _process_alerts(self, top: list[TickerView]) -> None:
        now_ts = time.time()
        for item in top:
            last_sent = self._cooldowns.get(item.symbol, 0.0)
            if (now_ts - last_sent) < self._cooldown_sec:
                continue

            payload = {
                "symbol": item.symbol,
                "lastPrice": item.last_price,
                "priceChangePercent": item.change_pct,
                "quoteVolume": item.quote_volume,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "link": f"https://www.binance.com/es-LA/futures/{item.symbol}",
            }
            message = self._format_message(payload)

            try:
                sent = await self._notifier.send_alert(message)
            except Exception as exc:  # noqa: BLE001
                self._logger.error(
                    "telegram_failed",
                    extra={"symbol": item.symbol, "error": str(exc)},
                )
                continue

            if sent:
                self._cooldowns[item.symbol] = now_ts
                self._storage.insert_alert(item.symbol, "24h_change", payload)
                self._logger.info("alert_sent", extra=payload)

    @staticmethod
    def _format_message(payload: dict[str, Any]) -> str:
        return (
            "Alerta 24h USDT Perp\n"
            f"Symbol: {payload['symbol']}\n"
            f"Last: {payload['lastPrice']}\n"
            f"24h%: {payload['priceChangePercent']}\n"
            f"QuoteVol: {payload['quoteVolume']}\n"
            f"Time: {payload['timestamp']}\n"
            f"Link: {payload['link']}"
        )

    def _save_snapshot(self, top: list[TickerView]) -> None:
        now = time.time()
        symbols = {item.symbol for item in top}
        data = {
            item.symbol: {
                "lastPrice": item.last_price,
                "priceChangePercent": item.change_pct,
                "quoteVolume": item.quote_volume,
            }
            for item in top
        }
        prev_symbols = self._snapshot_history[-1].symbols if self._snapshot_history else set()
        if not prev_symbols:
            for symbol in symbols:
                self._entry_times[symbol] = now
        else:
            new_entries = symbols - prev_symbols
            for symbol in new_entries:
                self._entry_times[symbol] = now

        for symbol in list(self._entry_times.keys()):
            if symbol not in symbols:
                del self._entry_times[symbol]

        snapshot = TopSnapshot(timestamp=now, symbols=symbols, data=data)
        self._snapshot_history.append(snapshot)

    def get_entry_ages(self, max_age_sec: int = 300) -> dict[str, float]:
        with self._lock:
            now = time.time()
            return {
                symbol: now - ts
                for symbol, ts in self._entry_times.items()
                if (now - ts) <= max_age_sec
            }

    def _get_ticker_view(self, symbol: str) -> TickerView:
        with self._lock:
            found = next((t for t in self._current_top if t.symbol == symbol), None)
            if found:
                return found
        return TickerView(symbol, 0.0, 0.0, 0.0)

    async def _ensure_klines_cached(self, symbol: str, interval: str, limit: int) -> list[dict]:
        await self._refresh_kline_cache_single(symbol, interval, limit)
        return self.get_cached_klines(symbol, interval)

    async def _get_cached_funding(self, symbol: str) -> float | None:
        now = time.time()
        with self._lock:
             cached = self._funding_cache.get(symbol)
             if cached and (now - cached[0] < self._funding_cache_ttl_sec):
                 return cached[1]
        
        try:
             data = await self._rest.get_mark_price(symbol)
             if data:
                 fr = float(data.get("lastFundingRate", 0.0))
                 with self._lock:
                     self._funding_cache[symbol] = (now, fr)
                 return fr
        except Exception:
             pass
        return None

    # Helper for single symbol kline refresh
    async def _refresh_kline_cache_single(self, symbol: str, interval: str, limit: int):
         now = time.time()
         cache_key = (symbol, interval)
         with self._lock:
             cached = self._kline_cache.get(cache_key)
             if cached and (now - cached[0] < self._kline_cache_ttl_sec):
                  return
         
         try:
             raw = await self._rest.get_klines(symbol, interval, limit=limit)
             parsed = [
                {
                    "open": self._parse_float(row[1]),
                    "high": self._parse_float(row[2]),
                    "low": self._parse_float(row[3]),
                    "close": self._parse_float(row[4]),
                    "volume": self._parse_float(row[5]),
                }
                for row in raw
             ]
             with self._lock:
                 self._kline_cache[cache_key] = (now, parsed)
         except Exception:
             pass

    async def _refresh_kline_cache(self, top: list[TickerView]) -> None:
        """Refresh kline cache for top symbols by change%."""
        if not top:
            return

        # Only cache top 30 by change%
        top_sorted = sorted(top, key=lambda item: item.change_pct, reverse=True)
        count = min(30, len(top_sorted))
        symbols = [item.symbol for item in top_sorted[:count]]
        now = time.time()
        intervals = [("1m", 60), ("1h", 30), ("1d", 30)]

        for symbol in symbols:
            for interval, limit in intervals:
                cache_key = (symbol, interval)
                cached = self._kline_cache.get(cache_key)
                if cached and (now - cached[0]) < self._kline_cache_ttl_sec:
                    if len(cached[1]) >= limit:
                        continue

                try:
                    raw = await self._rest.get_klines(symbol, interval, limit=limit)
                    parsed = [
                        {
                            "open": self._parse_float(row[1]),
                            "high": self._parse_float(row[2]),
                            "low": self._parse_float(row[3]),
                            "close": self._parse_float(row[4]),
                            "volume": self._parse_float(row[5]),
                        }
                        for row in raw
                        if len(row) > 5
                    ]
                    self._kline_cache[cache_key] = (now, parsed)
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "klines_fetch_failed",
                        extra={"symbol": symbol, "interval": interval, "error": str(exc)},
                    )

    def get_cached_klines(self, symbol: str, interval: str) -> list[dict[str, float]]:
        """Return cached klines for symbol and interval."""
        with self._lock:
            cached = self._kline_cache.get((symbol, interval))
            if not cached:
                return []
            return list(cached[1])

    def get_recent_entries(self) -> list[dict[str, Any]]:
        """Return symbols that entered the Top N in the last 5 minutes."""
        with self._lock:
            now = time.time()
            recent_symbols = [
                s for s, t in self._entry_times.items() 
                if (now - t) <= 300
            ]
            
            if not recent_symbols:
                return []
            
            current_data_map = {}
            if self._snapshot_history:
                current_data_map = self._snapshot_history[-1].data
            
            results: list[dict[str, Any]] = []
            for symbol in recent_symbols:
                data = current_data_map.get(symbol)
                entry_ts = self._entry_times.get(symbol, now)
                if data:
                    results.append(
                        {
                            "symbol": symbol,
                            "lastPrice": data["lastPrice"],
                            "priceChangePercent": data["priceChangePercent"],
                            "quoteVolume": data["quoteVolume"],
                            "entryTimestamp": datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat(),
                        }
                    )
            
            results.sort(key=lambda x: x["entryTimestamp"], reverse=True)
            return results

    def get_recent_candidates(self, max_age_min: float = 5, min_rate: float = 7.2, rate_map: Optional[dict] = None) -> list:
        """Get recent candidates from cache."""
        return self._recent_candidates.get_recent(max_age_min=max_age_min, min_rate=min_rate, rate_map=rate_map)

    @staticmethod
    def _parse_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
