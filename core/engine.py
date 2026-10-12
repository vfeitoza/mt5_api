"""Engine multi-operacional: loop 5s, risk -> calendar -> signal -> llm -> trade."""

from __future__ import annotations
import asyncio, logging
from mt5_bridge import MT5Bridge
from .config import AppConfig
from .risk import RiskManager
from .calendar import CalendarFilter
from .llm_gate import LLMGate
from .position_manager import PositionManager
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
        self.position_mgr = PositionManager(bridge)
        self.strategies = get_strategies(config)
        logger.info(f"Engine: {len(self.strategies)} estratégias: {[s.name+':'+s.symbol+'#'+str(s.magic) for s in self.strategies]}")

    async def _calendar_blocked_for(self, strat_name: str) -> tuple[bool, str]:
        sc = self.config.strategies.get(strat_name)
        params = sc.params if sc and sc.params else {}
        # champion tem janela própria 20/20; demais usam global
        return await self.calendar.is_blocked_for_strategy(strat_name, params)

    async def run_once(self) -> None:
        # 0) gestão pós-entrada (BE/trailing) todo ciclo, independente de sinal
        try:
            await self.position_mgr.tick(self.config.strategies)
        except Exception as e:
            logger.debug(f"PositionManager tick fail: {e}")

        # 1) calendário — por estratégia (champion 20/20, outros global)
        # não bloqueia global conservador; cada strat checa sua janela
        for strat in self.strategies:
            sc = self.config.strategies.get(strat.name)
            if not sc or not sc.enabled:
                continue

            # calendário por strat
            cal_blocked, cal_msg = await self._calendar_blocked_for(strat.name)
            if cal_blocked:
                logger.info(f"[{strat.name}] calendário BLOQUEADO: {cal_msg}")
                continue

            # 2) risk
            ok, reason = await self.risk.can_trade(strat.magic, sc.max_trades_day, sc.max_losses_day)
            if not ok:
                logger.info(f"[{strat.name}] risk block: {reason}")
                continue

            # 3) sinal
            try:
                signal = await strat.check_signal(self.bridge)
            except Exception as e:
                logger.warning(f"[{strat.name}] check_signal erro: {e}")
                continue
            if not signal:
                continue

            logger.info(f"[{strat.name}] sinal {signal.direction} entry {signal.entry:.2f} SL {signal.sl:.2f} TP {signal.tp:.2f} | {signal.meta}")

            # 4) LLM gate — passa evento próximo se houver
            news_title = cal_msg or ""
            # se não bloqueado mas há evento próximo, buscar título
            if not news_title:
                try:
                    evs = await self.calendar.get_events()
                    if evs:
                        news_title = f"{evs[0].title} ({evs[0].impact})"
                except Exception:
                    pass
            approved, motivo = await self.llm.approve(
                symbol=strat.symbol,
                direction=signal.direction,
                price=signal.entry,
                bos_info=signal.meta,
                news_title=news_title,
            )
            logger.info(f"[{strat.name}] LLM aprovado={approved} motivo={motivo}")
            if not approved:
                continue

            # 5) executa — champion pode trazer volume no Signal
            vol = sc.volume
            if hasattr(signal, "volume") and getattr(signal, "volume"):
                try:
                    vol = float(getattr(signal, "volume"))
                except Exception:
                    pass
            if self.config.dry_run:
                logger.info(f"[{strat.name}] DRY-RUN would {signal.direction} {strat.symbol} {vol} SL {signal.sl:.2f} TP {signal.tp:.2f} magic {strat.magic}")
                continue

            try:
                if signal.direction == "BUY":
                    res = await self.bridge.buy(strat.symbol, vol, stop_loss=signal.sl, take_profit=signal.tp, magic=strat.magic, comment=strat.name[:10])
                else:
                    res = await self.bridge.sell(strat.symbol, vol, stop_loss=signal.sl, take_profit=signal.tp, magic=strat.magic, comment=strat.name[:10])
                logger.info(f"[{strat.name}] ordem executada ticket={res.ticket} price={res.price_executed}")
            except Exception as e:
                logger.error(f"[{strat.name}] ordem falhou: {e}")

    async def run_forever(self) -> None:
        interval = self.config.loop_interval_s
        logger.info(f"Engine loop_interval={interval}s trigger_mode={self.config.trigger_mode}")
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.exception(f"Engine ciclo erro: {e}")
            await asyncio.sleep(interval)
