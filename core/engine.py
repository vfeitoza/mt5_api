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

        # cache de posições abertas para gate "1 posição por ID+moeda" (EA HasMyPosition)
        try:
            _open_positions = await self.bridge.get_positions()
        except Exception as e:
            logger.debug(f"Engine get_positions fail (gate 1 pos): {e}")
            _open_positions = None  # fail-open para checagem; risk já é fail-closed se offline

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

            # 2b) gate 1 posição por (magic + symbol) — replica HasMyPosition() do EA
            # se já existe posição aberta com mesmo magic+symbol, não abre nova (apenas 1 por vez)
            if _open_positions is not None:
                has_open = False
                for p in _open_positions:
                    # Position.magic pode ser 0 se EA antigo; fallback: qualquer posição do mesmo símbolo bloqueia
                    # mas quando magic exposto, exige match exato magic+symbol
                    pmagic = getattr(p, "magic", 0) or 0
                    psym = getattr(p, "symbol", "") or ""
                    if pmagic and pmagic != strat.magic:
                        continue
                    if psym and psym != strat.symbol:
                        continue
                    # chegou aqui: magic+symbol coincidem (ou posição sem magic mas mesmo símbolo)
                    has_open = True
                    break
                if has_open:
                    logger.info(f"[{strat.name}] gate 1 posição: já existe posição aberta {strat.symbol} #{strat.magic} — aguardando fechamento (BE/trailing gerenciando)")
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
            # hook dashboard: último sinal
            try:
                from core.dashboard import record_signal as _record_signal
                _record_signal(strat.name, signal)
            except Exception:
                pass

            # 4) LLM gate — facultativo por strat (scalp_himanshu use_llm false = direto)
            use_llm = bool(sc.params.get("use_llm", True)) if isinstance(sc.params, dict) else True
            # compat: alguns EAs usam use_llm_gate
            if "use_llm_gate" in sc.params:
                use_llm = bool(sc.params["use_llm_gate"])
            # scalp_himanshu expõe params.use_llm já; champion/scalp default true
            if strat.name == "scalp_himanshu" and not use_llm:
                approved, motivo = True, "LLM desabilitado (scalp_himanshu use_llm=false)"
                logger.info(f"[{strat.name}] LLM skip (use_llm=false) → aprovado")
            else:
                news_title = cal_msg or ""
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
            try:
                from core.dashboard import record_llm as _record_llm
                _record_llm(strat.name, approved, motivo)
            except Exception:
                pass
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

            # re-checar gate 1 posição imediatamente antes de executar (evita corrida: sinal + LLM levou ~7s)
            try:
                fresh_positions = await self.bridge.get_positions()
                for p in fresh_positions:
                    pmagic = getattr(p, "magic", 0) or 0
                    psym = getattr(p, "symbol", "") or ""
                    if pmagic and pmagic != strat.magic:
                        continue
                    if psym and psym != strat.symbol:
                        continue
                    logger.info(f"[{strat.name}] gate 1 posição (pós-LLM): posição {strat.symbol} #{strat.magic} abriu enquanto LLM avaliava — cancelando ordem")
                    fresh_positions = None  # sentinel
                    break
                if fresh_positions is None:
                    continue
            except Exception as e:
                logger.debug(f"[{strat.name}] gate pós-LLM fail: {e}")

            try:
                if signal.direction == "BUY":
                    res = await self.bridge.buy(strat.symbol, vol, stop_loss=signal.sl, take_profit=signal.tp, magic=strat.magic, comment=strat.name[:10])
                else:
                    res = await self.bridge.sell(strat.symbol, vol, stop_loss=signal.sl, take_profit=signal.tp, magic=strat.magic, comment=strat.name[:10])
                logger.info(f"[{strat.name}] ordem executada ticket={res.ticket} price={res.price_executed}")
                try:
                    from core.dashboard import record_trade as _record_trade
                    _record_trade(strat.name, signal.direction, res.ticket, res.price_executed, vol, signal.sl, signal.tp)
                except Exception:
                    pass
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
