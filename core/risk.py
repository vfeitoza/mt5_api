"""Risk manager: daily trades/losses per magic via EA history."""

import logging
from mt5_bridge import MT5Bridge

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, bridge: MT5Bridge):
        self.bridge = bridge

    async def can_trade(self, magic: int, max_trades: int, max_losses: int) -> tuple[bool, str]:
        """Returns (allowed, reason). Counts via EA GET_HISTORY filtered by magic."""
        try:
            counts = await self.bridge.get_history_counts(magic=magic)
        except Exception as e:
            # EA offline or no history -> fail-closed if we can't verify
            logger.warning(f"Risk check failed (magic {magic}): {e} — blocking trade")
            return False, f"history unavailable: {e}"

        if counts.trades >= max_trades:
            return False, f"trades {counts.trades}/{max_trades} atingido"
        if counts.losses >= max_losses:
            return False, f"losses {counts.losses}/{max_losses} atingido"
        return True, f"ok {counts.trades}/{max_trades} trades, {counts.losses}/{max_losses} losses"
