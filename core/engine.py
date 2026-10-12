"""Engine multi-operacional: loop 5s, risk -> calendar -> signal -> llm -> trade."""

from __future__ import annotations
import asyncio, logging
from mt5_bridge import MT5Bridge
from .config import AppConfig
from .risk import RiskManager
from .calendar import CalendarFilter
from .llm_gate import LLMGate
from ops.registry import get_strategies

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, bridge: MT5Bridge, config: AppConfig):
        self.bridge = bridge
        self.config = config
        self.risk = RiskManager(bridge)
        self.calendar = CalendarFilter(
            bridge,
            block_min=config.calendar.news_block_min,
            hours_ahead=config.calendar.hours_ahead,
            fallback_url=config.calendar.fallback_url,
        )
        self.llm = LLMGate(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key,
            model=config.llm.model,
            fallbacks=config.llm.fallbacks,
            timeout_s=config.llm.timeout_s,
            max_tokens=config.llm.max_tokens,
            temperature=config.llm.temperature,
        )
        self.strategies = get_strategies(config)
        logger.info(f"Engine: {len(self.strategies)} estratégias: {[s.name+':'+s.symbol+'#'+str(s.magic) for s in self.strategies]}")

    async def run_once(self) -> None:
        # calendário global (se bloqueado, pula todos — conservador)
        # ponytail: um is_blocked global economiza chamadas LLM
        cal_blocked, cal_msg = await self.calendar.is_blocked()
        if cal_blocked:
            logger.info(f"Calendário BLOQUEADO: {cal_msg} — pulando ciclo")
            return

        for strat in self.strategies:
            sc = self.config.strategies.get(strat.name)
            if not sc or not sc.enabled:
                continue

            # 1) risk
            ok, reason = await self.risk.can_trade(strat.magic, sc.max_trades_day, sc.max_losses_day)
            if not ok:
                logger.info(f"[{strat.name}] risk block: {reason}")
                continue

            # 2) sinal
            try:
                signal = await strat.check_signal(self.bridge)
            except Exception as e:
                logger.warning(f"[{strat.name}] check_signal erro: {e}")
                continue
            if not signal:
                continue

            logger.info(f"[{strat.name}] sinal {signal.direction} entry {signal.entry:.2f} SL {signal.sl:.2f} TP {signal.tp:.2f} | {signal.meta}")

            # 3) LLM gate
            approved, motivo = await self.llm.approve(
                symbol=strat.symbol,
                direction=signal.direction,
                price=signal.entry,
                bos_info=signal.meta,
                news_title="",
            )
            logger.info(f"[{strat.name}] LLM aprovado={approved} motivo={motivo}")
            if not approved:
                continue

            # 4) executa
            if self.config.dry_run:
                logger.info(f"[{strat.name}] DRY-RUN would {signal.direction} {strat.symbol} {sc.volume} SL {signal.sl:.2f} TP {signal.tp:.2f} magic {strat.magic}")
                continue

            try:
                if signal.direction == "BUY":
                    res = await self.bridge.buy(strat.symbol, sc.volume, stop_loss=signal.sl, take_profit=signal.tp, magic=strat.magic, comment=strat.name[:10])
                else:
                    res = await self.bridge.sell(strat.symbol, sc.volume, stop_loss=signal.sl, take_profit=signal.tp, magic=strat.magic, comment=strat.name[:10])
                logger.info(f"[{strat.name}] ordem executada ticket={res.ticket} price={res.price_executed}")
            except Exception as e:
                logger.error(f"[{strat.name}] ordem falhou: {e}")

    async def run_forever(self) -> None:
        interval = self.config.loop_interval_s
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.exception(f"Engine ciclo erro: {e}")
            await asyncio.sleep(interval)
