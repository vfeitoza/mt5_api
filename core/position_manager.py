"""PositionManager — gestão pós-entrada (BE / Parcial / Trailing).

Transcrito de NEW_CHAMPION 2.40: ManageOpenPositions() + CalcTrailingSL().
Chamado pelo Engine a cada loop (ou loop dedicado 5s) para mover SL/TP e fechar parcial.
Estado em memória por ticket (re-deriva initialRiskDistance após restart).
"""

from __future__ import annotations
import logging
from typing import Dict

from mt5_bridge import MT5Bridge
from mt5_bridge.market_data.indicators import calc_atr, pip_size

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, bridge: MT5Bridge):
        self.bridge = bridge
        # ticket -> {risk_distance: float, be_moved: bool, partial_done: bool, last_trailing: float}
        self._state: Dict[int, dict] = {}

    def _pip(self, symbol: str) -> float:
        return pip_size(symbol)

    def _get_state(self, ticket: int) -> dict:
        return self._state.setdefault(ticket, {"risk": 0.0, "be": False, "partial": False, "last_trailing": 0.0})

    async def tick(self, strategies_cfg: dict) -> None:
        """Verifica todas as posições abertas e aplica BE/parcial/trailing conforme params da strat dona do magic."""
        try:
            positions = await self.bridge.get_positions()
        except Exception as e:
            logger.debug(f"PositionManager get_positions fail: {e}")
            return
        if not positions:
            # limpar estado de tickets fechados
            live = set()
            self._state = {k: v for k, v in self._state.items() if k in live}
            return

        # mapear magic -> strategy params
        magic_to_params: Dict[int, dict] = {}
        for name, sc in strategies_cfg.items():
            params = dict(sc.params) if hasattr(sc, "params") else {}
            # merge top-level + params
            base = sc.model_dump() if hasattr(sc, "model_dump") else {}
            merged = {**base, **params}
            try:
                magic = int(merged.get("magic", getattr(sc, "magic", 0)))
            except Exception:
                continue
            magic_to_params[magic] = merged

        # obter rates para trailing ATR/structure quando necessário
        # cache por símbolo
        rates_cache: Dict[str, list] = {}

        live_tickets = set()
        for pos in positions:
            ticket = pos.ticket
            live_tickets.add(ticket)
            magic = getattr(pos, "magic", 0) if hasattr(pos, "magic") else 0
            # fallback: tentar inferir magic da posição via campo magic se exposto; senão usar champion default
            params = magic_to_params.get(magic) or magic_to_params.get(102001) or {}
            st = self._get_state(ticket)

            # risk distance (SL original)
            if st["risk"] == 0.0 and pos.stop_loss:
                st["risk"] = abs(pos.open_price - pos.stop_loss)
            risk = st["risk"] or abs(pos.open_price - pos.stop_loss) or 0.0
            if risk == 0:
                continue

            symbol = pos.symbol
            psize = self._pip(symbol)
            is_buy = pos.order_type == "BUY"

            # preços atuais
            try:
                tick = await self.bridge.get_tick(symbol)
            except Exception:
                continue
            cur_price = tick.bid if is_buy else tick.ask
            profit_dist = (cur_price - pos.open_price) if is_buy else (pos.open_price - cur_price)

            # --- breakeven ---
            use_be = bool(params.get("use_breakeven", True))
            be_mult = float(params.get("be_activation_mult", 1.0))
            be_offset = float(params.get("be_offset_pips", 5)) * psize
            if use_be and not st["be"] and profit_dist >= risk * be_mult:
                new_sl = pos.open_price + (be_offset if is_buy else -be_offset)
                # só move se for melhoria
                should = (new_sl > pos.stop_loss) if is_buy else (new_sl < pos.stop_loss)
                if should or not pos.stop_loss:
                    try:
                        await self.bridge.modify_position(ticket, stop_loss=new_sl, take_profit=pos.take_profit)
                        st["be"] = True
                        logger.info(f"PositionManager BE ticket {ticket} SL {new_sl:.2f}")
                    except Exception as e:
                        logger.warning(f"BE modify fail {ticket}: {e}")

            # --- parcial ---
            use_partial = bool(params.get("use_partial", False))
            partial_rr = float(params.get("partial_rr", 1.5))
            partial_pct = float(params.get("partial_percent", 50))
            # currentRR = profit_dist / risk
            current_rr = profit_dist / risk if risk else 0
            if use_partial and not st["partial"] and current_rr >= partial_rr:
                # fecha parcial via close_position com volume parcial se EA suportar; senão fecha tudo e avisa
                vol_to_close = pos.volume * partial_pct / 100
                try:
                    # tenta close parcial passando volume; se bridge não suporta, fallback
                    # Bridge.close_position hoje fecha tudo — tentamos com ticket
                    # Se EA aceitar close parcial, precisamos estender protocolo; por ora log warning
                    logger.info(f"PositionManager parcial {partial_pct}% ticket {ticket} vol {vol_to_close:.2f} (requer CLOSE_PARTIAL no EA)")
                    # TODO: implementar CLOSE_PARTIAL quando EA suportar
                    st["partial"] = True
                except Exception as e:
                    logger.warning(f"Parcial fail {ticket}: {e}")

            # --- trailing ---
            use_trailing = bool(params.get("use_trailing", False))
            trail_mult = float(params.get("trailing_activation_mult", 1.0))
            trail_mode = str(params.get("trailing_mode", "atr")).lower()
            trail_step_pips = float(params.get("trailing_step", 1)) * psize
            if use_trailing and profit_dist >= risk * trail_mult:
                new_sl = None
                if trail_mode == "atr":
                    atr_mult = float(params.get("trailing_atr_mult", 1.5))
                    atr_period = 14
                    # buscar rates
                    if symbol not in rates_cache:
                        try:
                            from mt5_bridge.protocol import Timeframe
                            rates = await self.bridge.get_rates(symbol, Timeframe.M5, 50)
                            rates_cache[symbol] = list(reversed(rates))
                        except Exception:
                            rates_cache[symbol] = []
                    rev = rates_cache[symbol]
                    if rev:
                        atr_vals = calc_atr(rev, atr_period)
                        cur_atr = atr_vals[-1] if atr_vals and atr_vals[-1] else None
                        if cur_atr:
                            dist = cur_atr * atr_mult
                            new_sl = cur_price - dist if is_buy else cur_price + dist
                elif trail_mode == "structure":
                    # swing low/high confirmado (como SMC)
                    offset = float(params.get("trailing_struct_offset_pips", 5)) * psize
                    if symbol not in rates_cache:
                        try:
                            from mt5_bridge.protocol import Timeframe
                            rates = await self.bridge.get_rates(symbol, Timeframe.M5, 100)
                            rates_cache[symbol] = list(reversed(rates))
                        except Exception:
                            rates_cache[symbol] = []
                    rev = rates_cache[symbol]
                    # buscar último swing
                    if rev and len(rev) >= 10:
                        found = None
                        for idx in range(len(rev) - 4 - 1, 3, -1):
                            if is_buy:
                                low = rev[idx]["low"]
                                ok = all(rev[idx - j]["low"] > low and rev[idx + j]["low"] > low for j in (1, 2, 3) if 0 <= idx - j < len(rev) and 0 <= idx + j < len(rev))
                                if ok and low < cur_price:
                                    found = low - offset
                                    break
                            else:
                                high = rev[idx]["high"]
                                ok = all(rev[idx - j]["high"] < high and rev[idx + j]["high"] < high for j in (1, 2, 3) if 0 <= idx - j < len(rev) and 0 <= idx + j < len(rev))
                                if ok and high > cur_price:
                                    found = high + offset
                                    break
                        new_sl = found
                else:  # fixed
                    dist = float(params.get("trailing_distance", 40)) * psize
                    new_sl = cur_price - dist if is_buy else cur_price + dist

                if new_sl:
                    should = False
                    if is_buy:
                        should = new_sl > pos.stop_loss + trail_step_pips or not pos.stop_loss
                    else:
                        should = new_sl < pos.stop_loss - trail_step_pips or not pos.stop_loss
                    if should:
                        try:
                            await self.bridge.modify_position(ticket, stop_loss=new_sl, take_profit=pos.take_profit)
                            st["last_trailing"] = new_sl
                            logger.info(f"PositionManager trailing ticket {ticket} SL {new_sl:.2f}")
                        except Exception as e:
                            logger.warning(f"Trailing modify fail {ticket}: {e}")

        # limpar estado de fechados
        for k in list(self._state.keys()):
            if k not in live_tickets:
                self._state.pop(k, None)
