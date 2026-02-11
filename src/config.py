from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class BinanceConfig(BaseModel):
    base_url: str
    exchange_info_endpoint: str
    ticker_24h_endpoint: str


class ScannerConfig(BaseModel):
    refresh_24h_sec: int = 30
    min_24h_change_pct: float = 15.0
    min_quote_volume_usdt: float = 5_000_000
    topN: int = 60
    cooldown_min: int = 10


class WebsocketConfig(BaseModel):
    base_url: str = "wss://fstream.binance.com/stream"
    max_streams_per_connection: int = 1024
    reconnect_max_sec: int = 60
    symbol_refresh_sec: int = 10


class ExplosionConfig(BaseModel):
    ret_th: float = 0.02
    vr_th: float = 4.0
    trades_pctl: float = 0.90
    min_points: int = 2
    cooldown_min: int = 10
    median_min_samples: int = 20
    trades_floor: int = 50
    buffer_size: int = 60


class ShortSetupConfig(BaseModel):
    retest_zone_pct: float = 0.004
    fail_drop_pct: float = 0.006
    retest_timeout_min: int = 90
    retest_confirm_window_sec: int = 60
    cooldown_min: int = 10
    max_setups_per_symbol_per_day: int = 6
    funding_abs_max: float = 0.002
    alert_mode: str = "both"
    beep_on_short_setup: bool = False


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/app.jsonl"


class StorageConfig(BaseModel):
    path: str = "data/alerts.db"


class TelegramConfig(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None


class AppConfig(BaseModel):
    binance: BinanceConfig
    scanner: ScannerConfig
    websocket: WebsocketConfig
    explosion: ExplosionConfig
    short_setup: ShortSetupConfig
    dashboard: DashboardConfig
    logging: LoggingConfig
    storage: StorageConfig
    telegram: TelegramConfig


def load_config(config_path: str, env_path: str = ".env") -> AppConfig:
    """Load YAML config and environment overrides."""
    load_dotenv(env_path)
    with open(config_path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    data["telegram"] = {
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    }

    config = AppConfig.model_validate(data)

    config.logging.file = str(Path(config.logging.file))
    config.storage.path = str(Path(config.storage.path))

    return config
