"""Base strategy interface."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

@dataclass
class Signal:
    direction: str  # BUY or SELL
    entry: float
    sl: float
    tp: float
    meta: str = ""  # BOS/OB/FVG info for LLM

class BaseStrategy:
    name: str = "base"
    magic: int = 0
    symbol: str = "XAUUSD"
    timeframe: str = "M5"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # allow override via config
        if "magic" in self.config:
            self.magic = int(self.config["magic"])
        if "symbol" in self.config:
            self.symbol = str(self.config["symbol"])
        if "timeframe" in self.config:
            self.timeframe = str(self.config["timeframe"])

    async def check_signal(self, bridge) -> Optional[Signal]:
        """Return Signal or None. Must not place orders."""
        raise NotImplementedError

    def calc_sl_tp(self, direction: str, entry: float, swing: float, rr: float = 1.8) -> tuple[float, float]:
        risk = abs(entry - swing)
        if direction == "BUY":
            sl = swing
            tp = entry + risk * rr
        else:
            sl = swing
            tp = entry - risk * rr
        return sl, tp
