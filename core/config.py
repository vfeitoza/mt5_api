"""YAML config loader (Pydantic). Single source of truth."""

from __future__ import annotations
import os, yaml
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    enabled: bool = True
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    magic: int = 61001
    volume: float = 0.05
    max_trades_day: int = 8
    max_losses_day: int = 3
    params: dict = Field(default_factory=dict)


class LLMConfig(BaseModel):
    base_url: str = "https://models.servtec.net.br/v1"
    api_key: str = Field(default_factory=lambda: os.getenv("SERVTEC_API_KEY", ""))
    model: str = "gemini-3.8-flash-high"
    fallbacks: list[str] = Field(default_factory=lambda: [
        "deepseek-v4.1-flash", "glm-5.3-flash", "gpt-6-luna"
    ])
    timeout_s: float = 7.0
    max_tokens: int = 120
    temperature: float = 0.0


class CalendarConfig(BaseModel):
    news_block_min: int = 30
    hours_ahead: int = 24
    use_ea_primary: bool = True
    fallback_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


class BridgeConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 5555


class AppConfig(BaseModel):
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)
    loop_interval_s: float = 5.0
    dry_run: bool = False
    log_level: str = "INFO"


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p} (copy config.example.yaml)")
    data = yaml.safe_load(p.read_text()) or {}
    # env override for api key
    if os.getenv("SERVTEC_API_KEY"):
        data.setdefault("llm", {})["api_key"] = os.getenv("SERVTEC_API_KEY")
    return AppConfig.model_validate(data)
