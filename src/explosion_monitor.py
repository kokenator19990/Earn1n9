from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any

import websockets

from .scanner import Scanner
from .storage import Storage
from .telegram_notifier import TelegramNotifier


STATUS_IDLE = "IDLE"
STATUS_WATCHING = "WATCHING"
STATUS_EXPLOSION_ACTIVE = "EXPLOSION_ACTIVE"
STATUS_WAIT_RETEST = "WAIT_RETEST"
STATUS_RETEST_SEEN = "RETEST_SEEN"
STATUS_SHORT_SENT = "SHORT_SETUP_SENT"
STATUS_COOLDOWN = "COOLDOWN"


@dataclass
class Candle:
    open: float
    close: float
    high: float
    volume: float
    trades: int


@dataclass
class SymbolState:
    candles: deque[Candle]
    trades_history: list[int] = field(default_factory=list)
    last_explosion_ts: float = 0.0
    status: str = STATUS_IDLE
    event_high: float = 0.0
    wait_start_ts: float = 0.0
    retest_seen_ts: float = 0.0
    cooldown_until_ts: float = 0.0
    last_price: float | None = None
    funding_rate: float | None = None
    explosion_metrics: dict[str, Any] = field(default_factory=dict)
    daily_count_date: str | None = None
    daily_count: int = 0


class ExplosionMonitor:
    """Monitor WS streams to detect explosions and short setup alerts."""

    def __init__(
        self,
        scanner: Scanner,
        notifier: TelegramNotifier,
        storage: Storage,
        logger,
        base_url: str,
        max_streams_per_connection: int,
        reconnect_max_sec: int,
        symbol_refresh_sec: int,
        ret_th: float,
        vr_th: float,
        trades_pctl: float,
        min_points: int,
        cooldown_min: int,
        median_min_samples: int,
        trades_floor: int,
        buffer_size: int,
        retest_zone_pct: float,
        fail_drop_pct: float,
        retest_timeout_min: int,
        retest_confirm_window_sec: int,
        short_setup_cooldown_min: int,
        max_setups_per_symbol_per_day: int,
        funding_abs_max: float,
        alert_mode: str,
        beep_on_short_setup: bool,
    ) -> None:
        self._scanner = scanner
        self._notifier = notifier
        self._storage = storage
        self._logger = logger
        self._base_url = base_url
        self._max_streams = max_streams_per_connection
        self._reconnect_max_sec = reconnect_max_sec
        self._symbol_refresh_sec = symbol_refresh_sec
        self._ret_th = ret_th
        self._vr_th = vr_th
        self._trades_pctl = trades_pctl
        self._min_points = min_points
        self._cooldown_sec = cooldown_min * 60
        self._median_min_samples = median_min_samples
        self._trades_floor = trades_floor
        self._buffer_size = buffer_size
        self._retest_zone_pct = retest_zone_pct
        self._fail_drop_pct = fail_drop_pct
        self._retest_timeout_sec = retest_timeout_min * 60
        self._retest_confirm_window_sec = retest_confirm_window_sec
        self._short_setup_cooldown_sec = short_setup_cooldown_min * 60
        self._max_setups_per_symbol_per_day = max_setups_per_symbol_per_day
        self._funding_abs_max = funding_abs_max
        self._alert_mode = alert_mode
        self._beep_on_short_setup = beep_on_short_setup
        self._streams_per_symbol = 2

        self._states: dict[str, SymbolState] = {}
        self._current_symbols: set[str] = set()
        self._symbol_snapshot: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._refresh_symbols(stop_event)
            await self._wait_or_stop(self._symbol_refresh_sec, stop_event)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def get_symbol_statuses(self) -> list[dict[str, Any]]:
        now = time.time()
        results: list[dict[str, Any]] = []
        symbols = set(self._current_symbols) | set(self._states.keys())
        for symbol in sorted(symbols):
            state = self._states.get(symbol)
            status = state.status if state else STATUS_IDLE
            if symbol not in self._current_symbols and status != STATUS_COOLDOWN:
                status = STATUS_IDLE
            elif symbol in self._current_symbols and status == STATUS_IDLE:
                status = STATUS_WATCHING

            if state and status == STATUS_COOLDOWN and state.cooldown_until_ts <= now:
                status = STATUS_WATCHING

            funding_rate = state.funding_rate if state else None
            funding_tag = self._funding_tag(funding_rate)
            results.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "fundingRate": funding_rate,
                    "fundingTag": funding_tag,
                    "lastPrice": state.last_price if state else None,
                }
            )
        return results

    async def _refresh_symbols(self, stop_event: asyncio.Event) -> None:
        snapshot = self._scanner.get_top_snapshot()
        symbols = set(snapshot.keys())
        async with self._lock:
            self._symbol_snapshot = snapshot
            if symbols == self._current_symbols:
                return

            removed = self._current_symbols - symbols
            for symbol in removed:
                state = self._states.get(symbol)
                if state and state.status != STATUS_COOLDOWN:
                    state.status = STATUS_IDLE

            self._current_symbols = symbols
            await self._restart_connections(symbols, stop_event)

    async def _restart_connections(
        self, symbols: set[str], stop_event: asyncio.Event
    ) -> None:
        await self.stop()
        if not symbols:
            return

        symbol_list = sorted(symbols)
        chunk_size = max(1, self._max_streams // self._streams_per_symbol)
        chunks = [
            symbol_list[i : i + chunk_size]
            for i in range(0, len(symbol_list), chunk_size)
        ]
        for chunk in chunks:
            task = asyncio.create_task(self._consume_stream(chunk, stop_event))
            self._tasks.append(task)

    async def _consume_stream(
        self, symbols: list[str], stop_event: asyncio.Event
    ) -> None:
        if not symbols:
            return

        stream_items: list[str] = []
        for symbol in symbols:
            stream_items.append(f"{symbol.lower()}@kline_1m")
            stream_items.append(f"{symbol.lower()}@markPrice@1s")
        streams = "/".join(stream_items)
        url = f"{self._base_url}?streams={streams}"
        backoff = 1

        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20
                ) as websocket:
                    backoff = 1
                    while not stop_event.is_set():
                        message = await websocket.recv()
                        await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._logger.error("ws_error", extra={"error": str(exc)})
                await self._wait_or_stop(backoff, stop_event)
                backoff = min(backoff * 2, self._reconnect_max_sec)

    async def _handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        data = payload.get("data", payload)
        if "k" in data:
            await self._handle_kline(data)
        elif data.get("e") == "markPriceUpdate":
            await self._handle_mark_price(data)

    async def _handle_kline(self, data: dict[str, Any]) -> None:
        kline = data.get("k")
        if not kline or not kline.get("x"):
            return

        symbol = kline.get("s")
        if not symbol:
            return

        candle = Candle(
            open=self._parse_float(kline.get("o")),
            close=self._parse_float(kline.get("c")),
            high=self._parse_float(kline.get("h")),
            volume=self._parse_float(kline.get("v")),
            trades=int(kline.get("n") or 0),
        )
        await self._evaluate_explosion(symbol, candle)

    async def _handle_mark_price(self, data: dict[str, Any]) -> None:
        symbol = data.get("s")
        if not symbol:
            return

        mark_price = self._parse_float(data.get("p"))
        funding_rate = self._parse_float(data.get("r")) if data.get("r") is not None else None
        state = self._states.setdefault(
            symbol, SymbolState(candles=deque(maxlen=self._buffer_size))
        )
        state.last_price = mark_price
        if funding_rate is not None:
            state.funding_rate = funding_rate

        await self._evaluate_short_setup(symbol, state)

    async def _evaluate_explosion(self, symbol: str, candle: Candle) -> None:
        state = self._states.setdefault(
            symbol, SymbolState(candles=deque(maxlen=self._buffer_size))
        )
        state.candles.append(candle)
        state.trades_history.append(candle.trades)

        if len(state.candles) < self._median_min_samples:
            return

        median_vol = median([item.volume for item in state.candles])
        if median_vol <= 0:
            return

        r1m = (candle.close / candle.open - 1) if candle.open else 0.0
        vr = self.compute_vr(candle.volume, median_vol)
        pctl_trades = self._trades_floor
        if len(state.trades_history) >= self._median_min_samples:
            pctl_trades = self._percentile(state.trades_history, self._trades_pctl)

        rules = [
            r1m >= self._ret_th,
            vr >= self._vr_th,
            candle.trades >= pctl_trades,
        ]
        if sum(1 for item in rules if item) < self._min_points:
            return

        now_ts = time.time()
        if (now_ts - state.last_explosion_ts) < self._cooldown_sec:
            return

        snapshot = self._symbol_snapshot.get(symbol, {})
        event_high = max(state.event_high, candle.high)
        event = {
            "event_id": str(uuid.uuid4()),
            "symbol": symbol,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "event_high": event_high,
            "r1m": r1m,
            "vr": vr,
            "n_trades": candle.trades,
            "vol_1m": candle.volume,
            "lastPrice": snapshot.get("lastPrice"),
            "priceChangePercent": snapshot.get("priceChangePercent"),
        }

        state.last_explosion_ts = now_ts
        state.event_high = event_high
        state.wait_start_ts = now_ts
        state.retest_seen_ts = 0.0
        state.status = STATUS_EXPLOSION_ACTIVE
        state.explosion_metrics = {
            "r1m": r1m,
            "vr": vr,
            "n_trades": candle.trades,
        }

        self._storage.insert_event(symbol, "EXPLOSION", event)
        self._logger.info("explosion_detected", extra=event)

        if self._should_alert_explosion():
            message = (
                "🔥 EXPLOSION DETECTED\n"
                f"Symbol: {symbol}\n"
                f"r1m: {r1m:.4f}\n"
                f"VR: {vr:.2f}\n"
                f"n_trades: {candle.trades}\n"
                f"24h%: {snapshot.get('priceChangePercent')}\n"
                f"Last: {snapshot.get('lastPrice')}\n"
                f"Link: https://www.binance.com/es-LA/futures/{symbol}"
            )
            try:
                await self._notifier.send_alert(message)
            except Exception as exc:  # noqa: BLE001
                self._logger.error(
                    "explosion_telegram_failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )

        if state.status == STATUS_EXPLOSION_ACTIVE:
            state.status = STATUS_WAIT_RETEST

    async def _evaluate_short_setup(self, symbol: str, state: SymbolState) -> None:
        if symbol not in self._current_symbols and state.status != STATUS_COOLDOWN:
            return

        now_ts = time.time()
        self._refresh_daily_count(state)

        if state.status == STATUS_COOLDOWN:
            if state.cooldown_until_ts <= now_ts:
                state.status = STATUS_WATCHING
            else:
                return

        if state.status in {STATUS_IDLE, STATUS_WATCHING}:
            return

        if state.status in {STATUS_EXPLOSION_ACTIVE, STATUS_WAIT_RETEST}:
            if (now_ts - state.wait_start_ts) > self._retest_timeout_sec:
                state.status = STATUS_WATCHING
                return

            if state.last_price is None:
                return

            if self.in_retest_zone(state.last_price, state.event_high, self._retest_zone_pct):
                state.status = STATUS_RETEST_SEEN
                state.retest_seen_ts = now_ts
            return

        if state.status == STATUS_RETEST_SEEN:
            if state.last_price is None:
                return

            if (now_ts - state.retest_seen_ts) > self._retest_confirm_window_sec:
                state.status = STATUS_WAIT_RETEST
                state.wait_start_ts = now_ts
                return

            if self._breaks_above(state.last_price, state.event_high):
                state.event_high = max(state.event_high, state.last_price)
                state.status = STATUS_WAIT_RETEST
                state.wait_start_ts = now_ts
                return

            if self.is_reject_confirmed(state.last_price, state.event_high, self._fail_drop_pct):
                if self._funding_is_weird(state.funding_rate):
                    state.status = STATUS_WAIT_RETEST
                    state.wait_start_ts = now_ts
                    return

                if state.daily_count >= self._max_setups_per_symbol_per_day:
                    state.status = STATUS_COOLDOWN
                    state.cooldown_until_ts = now_ts + self._short_setup_cooldown_sec
                    return

                await self._emit_short_setup(symbol, state)

    async def _emit_short_setup(self, symbol: str, state: SymbolState) -> None:
        snapshot = self._symbol_snapshot.get(symbol, {})
        funding_rate = state.funding_rate
        event = {
            "event_id": str(uuid.uuid4()),
            "symbol": symbol,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "event_high": state.event_high,
            "r1m": state.explosion_metrics.get("r1m"),
            "vr": state.explosion_metrics.get("vr"),
            "n_trades": state.explosion_metrics.get("n_trades"),
            "priceChangePercent": snapshot.get("priceChangePercent"),
            "lastPrice": state.last_price,
            "fundingRate": funding_rate,
            "fundingTag": self._funding_tag(funding_rate),
        }

        message = (
            "✅ SHORT SETUP (retest + rechazo + funding OK)\n"
            f"Symbol: {symbol}\n"
            f"r1m: {event['r1m']}\n"
            f"VR: {event['vr']}\n"
            f"n_trades: {event['n_trades']}\n"
            f"24h%: {event['priceChangePercent']}\n"
            f"Funding: {funding_rate} ({event['fundingTag']})\n"
            f"Event High: {state.event_high}\n"
            f"Price: {state.last_price}\n"
            f"Link: https://www.binance.com/es-LA/futures/{symbol}"
        )

        try:
            await self._notifier.send_alert(message)
            if self._beep_on_short_setup:
                self._beep()
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "short_setup_telegram_failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return

        self._storage.insert_event(symbol, "SHORT_SETUP", event)
        self._logger.info("short_setup_detected", extra=event)
        state.status = STATUS_COOLDOWN
        state.cooldown_until_ts = time.time() + self._short_setup_cooldown_sec
        state.daily_count += 1

    def _refresh_daily_count(self, state: SymbolState) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if state.daily_count_date != today:
            state.daily_count_date = today
            state.daily_count = 0

    def _should_alert_explosion(self) -> bool:
        return self._alert_mode.lower() in {"both", "explosion_only"}

    def _funding_is_weird(self, funding_rate: float | None) -> bool:
        return self.funding_is_weird(funding_rate, self._funding_abs_max)

    def _funding_tag(self, funding_rate: float | None) -> str:
        return "RARO" if self._funding_is_weird(funding_rate) else "OK"

    @staticmethod
    def funding_is_weird(funding_rate: float | None, funding_abs_max: float) -> bool:
        if funding_rate is None:
            return False
        return abs(funding_rate) > funding_abs_max

    @staticmethod
    def compute_vr(volume: float, median_vol: float) -> float:
        if median_vol <= 0:
            return 0.0
        return volume / median_vol

    @staticmethod
    def in_retest_zone(price: float, event_high: float, zone_pct: float) -> bool:
        lower = event_high * (1 - zone_pct)
        upper = event_high * (1 + zone_pct)
        return lower <= price <= upper

    @staticmethod
    def is_reject_confirmed(price: float, event_high: float, fail_drop_pct: float) -> bool:
        return price <= event_high * (1 - fail_drop_pct)

    @staticmethod
    def _breaks_above(price: float, event_high: float) -> bool:
        return price >= event_high * 1.001

    @staticmethod
    def _parse_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _percentile(values: list[int], percentile_value: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return float(ordered[0])
        index = (len(ordered) - 1) * percentile_value
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return float(ordered[int(index)])
        weight = index - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    @staticmethod
    def _beep() -> None:
        try:
            import winsound

            winsound.Beep(1200, 200)
        except Exception:
            return

    @staticmethod
    async def _wait_or_stop(seconds: float, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
