from __future__ import annotations

import asyncio
import os
import threading

import httpx
import uvicorn

from .binance_rest import BinanceRestClient
from .config import load_config
from .dashboard import create_app
from .explosion_monitor import ExplosionMonitor
from .scanner import Scanner
from .storage import Storage
from .telegram_notifier import TelegramNotifier
from .utils_logging import setup_logging


def _ensure_dirs() -> None:
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)


def _start_dashboard(app, host: str, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app=app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


async def main_async() -> None:
    _ensure_dirs()
    config = load_config("config/config.yaml")
    logger = setup_logging(config.logging.file, config.logging.level)

    storage = Storage(config.storage.path)
    storage.init_db()

    async with httpx.AsyncClient() as client:
        rest_client = BinanceRestClient(
            base_url=config.binance.base_url,
            exchange_info_endpoint=config.binance.exchange_info_endpoint,
            ticker_24h_endpoint=config.binance.ticker_24h_endpoint,
            client=client,
        )
        notifier = TelegramNotifier(
            bot_token=config.telegram.bot_token,
            chat_id=config.telegram.chat_id,
            client=client,
        )
        scanner = Scanner(
            rest_client=rest_client,
            notifier=notifier,
            storage=storage,
            min_24h_change_pct=config.scanner.min_24h_change_pct,
            min_quote_volume_usdt=config.scanner.min_quote_volume_usdt,
            top_n=config.scanner.topN,
            cooldown_min=config.scanner.cooldown_min,
            logger=logger,
        )

        monitor = ExplosionMonitor(
            scanner=scanner,
            notifier=notifier,
            storage=storage,
            logger=logger,
            base_url=config.websocket.base_url,
            max_streams_per_connection=config.websocket.max_streams_per_connection,
            reconnect_max_sec=config.websocket.reconnect_max_sec,
            symbol_refresh_sec=config.websocket.symbol_refresh_sec,
            ret_th=config.explosion.ret_th,
            vr_th=config.explosion.vr_th,
            trades_pctl=config.explosion.trades_pctl,
            min_points=config.explosion.min_points,
            cooldown_min=config.explosion.cooldown_min,
            median_min_samples=config.explosion.median_min_samples,
            trades_floor=config.explosion.trades_floor,
            buffer_size=config.explosion.buffer_size,
            retest_zone_pct=config.short_setup.retest_zone_pct,
            fail_drop_pct=config.short_setup.fail_drop_pct,
            retest_timeout_min=config.short_setup.retest_timeout_min,
            retest_confirm_window_sec=config.short_setup.retest_confirm_window_sec,
            short_setup_cooldown_min=config.short_setup.cooldown_min,
            max_setups_per_symbol_per_day=config.short_setup.max_setups_per_symbol_per_day,
            funding_abs_max=config.short_setup.funding_abs_max,
            alert_mode=config.short_setup.alert_mode,
            beep_on_short_setup=config.short_setup.beep_on_short_setup,
        )

        app = create_app(scanner, storage, monitor)
        server, thread = _start_dashboard(app, config.dashboard.host, config.dashboard.port)

        stop_event = asyncio.Event()
        monitor_task: asyncio.Task | None = None
        try:
            monitor_task = asyncio.create_task(monitor.run(stop_event))
            await scanner.run(config.scanner.refresh_24h_sec, stop_event)
        except asyncio.CancelledError:
            logger.info("shutdown_requested")
        finally:
            stop_event.set()
            await monitor.stop()
            if monitor_task:
                await asyncio.gather(monitor_task, return_exceptions=True)
            server.should_exit = True
            thread.join(timeout=5)
            storage.close()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
