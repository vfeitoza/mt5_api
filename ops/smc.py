"""SMC operacional para XAUUSD — BOS + OrderBlock/FVG + MSS gatilho.

Timeframes: M5 estrutura, M1 gatilho.
Sem dependência externa além de bridge (rates/tick).
# ponytail: pivô 2+2 e OB/FVG ingênuo, suficiente p/ scalper. Trocar por
# detecção fractal+volume imbalance se winrate provar fraco.
"""

from __future__ import annotations
import logging
from typing import Optional
from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe

logger = logging.getLogger(__name__)


def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.M5


def swing_highs_lows(rates: list[dict], lookback: int = 30, pivot: int = 2):
    """Retorna (highs, lows) como lista de (idx, price). Pivô pivot+1 pivot."""
    highs, lows = [], []
    n = len(rates)
    # rates[0] = mais recente (CopyRates series), mas nosso bridge retorna rates[0] mais recente?
    # EA set ArraySetAsSeries(true) -> rates[0] é candle atual. Invert para análise.
    rev = list(reversed(rates))  # rev[0] oldest, rev[-1] newest
    for i in range(pivot, len(rev) - pivot):
        h, l = rev[i]["high"], rev[i]["low"]
        is_high = all(h >= rev[i + k]["high"] for k in range(-pivot, pivot + 1) if k != 0)
        is_low = all(l <= rev[i + k]["low"] for k in range(-pivot, pivot + 1) if k != 0)
        if is_high:
            highs.append((i, h))
        if is_low:
            lows.append((i, l))
    return highs, lows, rev


def find_bos(rev: list[dict], highs, lows, lookback_bos: int = 20):
    """Detecta último BOS: close rompe swing mais recente (não maior absoluto)."""
    if not rev or (not highs and not lows):
        return None
    last_close = rev[-1]["close"]
    # último swing (maior idx = mais recente) antes do candle atual
    if highs:
        last_idx, last_high_price = max(highs, key=lambda x: x[0])
        if last_idx < len(rev) - 1 and last_close > last_high_price and (len(rev) - 1 - last_idx) <= lookback_bos:
            return ("BULL", last_high_price, last_idx)
    if lows:
        last_idx, last_low_price = max(lows, key=lambda x: x[0])
        if last_idx < len(rev) - 1 and last_close < last_low_price and (len(rev) - 1 - last_idx) <= lookback_bos:
            return ("BEAR", last_low_price, last_idx)
    return None


def find_ob_fvg(rev: list[dict], bos, pivot: int = 2):
    """OB: última vela oposta antes do impulso BOS. FVG: gap de imbalance."""
    if not bos:
        return None, None
    direction, swing_price, bos_idx = bos
    # busca impulso: velas entre swing e BOS
    # impulso bullish: fecha acima do swing
    # OB bullish = último candle bear antes do impulso
    ob = None
    fvg = None
    # janela: 5 candles antes do BOS
    start = max(0, bos_idx - 6)
    segment = rev[start:bos_idx + 1]
    if direction == "BULL":
        for j in range(len(segment) - 1, -1, -1):
            c = segment[j]
            if c["close"] < c["open"]:  # bear
                ob = (c["low"], c["high"])
                break
        # FVG bullish: low[2] > high[0] no trio
        for k in range(len(rev) - 2):
            a, b, c = rev[k], rev[k + 1], rev[k + 2]
            if c["low"] > a["high"] and b["close"] > b["open"]:
                # pega último FVG antes do BOS
                if k + 2 <= bos_idx and k + 2 >= bos_idx - 5:
                    fvg = (a["high"], c["low"])
    else:
        for j in range(len(segment) - 1, -1, -1):
            c = segment[j]
            if c["close"] > c["open"]:
                ob = (c["low"], c["high"])
                break
        for k in range(len(rev) - 2):
            a, b, c = rev[k], rev[k + 1], rev[k + 2]
            if c["high"] < a["low"] and b["close"] < b["open"]:
                if k + 2 <= bos_idx and k + 2 >= bos_idx - 5:
                    fvg = (a["low"], c["high"])
    return ob, fvg


class SMCStrategy(BaseStrategy):
    name = "smc_xau"
    magic = 61001
    symbol = "XAUUSD"
    timeframe = "M5"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._last_m5_closed_time: str = ""  # deduplica por candle fechado
        self._cached_bos = None  # (bos_tuple, zone) cache entre loops

    async def check_signal(self, bridge) -> Optional[Signal]:
        p = self.config
        tf_m5 = _tf(self.timeframe)
        tf_m1 = Timeframe.M1
        spread_max = int(p.get("spread_max", 35))
        bos_lookback = int(p.get("bos_lookback", 20))
        bos_expire = int(p.get("bos_expire", 15))
        rr = float(p.get("rr", 1.8))
        sl_pad = float(p.get("sl_pad", 1.2))
        trigger = str(p.get("trigger", p.get("trigger_mode", "hybrid"))).lower()

        # 1) spread guard
        try:
            tick = await bridge.get_tick(self.symbol)
        except Exception as e:
            logger.debug(f"SMC {self.symbol} get_tick fail: {e}")
            return None
        spread_pts = int(round((tick.ask - tick.bid) * 10)) if tick.ask and tick.bid else 999
        if spread_pts > spread_max:
            logger.debug(f"SMC spread {spread_pts} > {spread_max} skip")
            return None

        # 2) M5 estrutura — respeita trigger_mode
        try:
            rates_m5 = await bridge.get_rates(self.symbol, tf_m5, 50)
        except Exception as e:
            logger.debug(f"SMC get_rates M5 fail: {e}")
            return None
        if len(rates_m5) < 30:
            return None
        highs, lows, rev = swing_highs_lows(rates_m5, lookback=30, pivot=2)

        # trigger_mode: close_only -> só avalia quando M5 fecha; polling -> legado; hybrid -> BOS fechado + reteste 30s
        if trigger in ("close_only", "hybrid"):
            closed = rev[-2]  # último fechado
            closed_time = closed.get("time", "")
            is_new_candle = closed_time != self._last_m5_closed_time
            if trigger == "close_only" and not is_new_candle:
                logger.debug(f"SMC close_only aguardando novo candle M5 (last={self._last_m5_closed_time})")
                return None
            # para BOS usa série sem candle em formação (evita repaint)
            rev_closed = rev[:-1]
            # recalcula highs/lows sem formando
            highs_f = [(i, v) for i, v in highs if i < len(rev) - 1]
            lows_f = [(i, v) for i, v in lows if i < len(rev) - 1]
            bos = find_bos(rev_closed, highs_f, lows_f, lookback_bos=bos_lookback)
            if bos and is_new_candle:
                self._last_m5_closed_time = closed_time
        else:
            bos = find_bos(rev, highs, lows, lookback_bos=bos_lookback)
        if not bos:
            return None
        direction, swing_price, bos_idx = bos
        # expira BOS antigo
        if (len(rev) - 1 - bos_idx) > bos_expire:
            logger.debug(f"SMC BOS expirado {direction} {swing_price}")
            return None

        ob, fvg = find_ob_fvg(rev, bos)
        # zona de mitigação: OB preferível, senão FVG, senão swing
        if ob:
            zone_low, zone_high = (min(ob), max(ob))
        elif fvg:
            zone_low, zone_high = (min(fvg), max(fvg))
        else:
            # sem zona clara -> skip (evita entrada tardia)
            logger.debug("SMC sem OB/FVG após BOS")
            return None

        # 3) reteste: preço atual dentro/na zona (M1 confirma)
        price = tick.ask if direction == "BULL" else tick.bid
        # tolerância: 1.5 USD além da zona
        tol = 1.5
        in_zone = (zone_low - tol) <= price <= (zone_high + tol)
        # se não está na zona, checa último reteste em M1 (útil se já mitigado)
        if not in_zone:
            try:
                rates_m1 = await bridge.get_rates(self.symbol, tf_m1, 20)
                rev_m1 = list(reversed(rates_m1))
                # último low/high M1 tocou zona?
                touched = False
                for c in rev_m1[-8:]:
                    if direction == "BULL" and c["low"] <= zone_high and c["low"] >= zone_low - tol:
                        touched = True
                        break
                    if direction == "BEAR" and c["high"] >= zone_low and c["high"] <= zone_high + tol:
                        touched = True
                        break
                if not touched:
                    return None
            except Exception:
                return None

        # 4) MSS gatilho em M1: 1 candle fecha a favor após toque
        # ponytail: confirma com close M1 a favor da direção
        try:
            rates_m1 = await bridge.get_rates(self.symbol, tf_m1, 10)
            rev_m1 = list(reversed(rates_m1))
            last_m1 = rev_m1[-1]
            mss_ok = (last_m1["close"] > last_m1["open"]) if direction == "BULL" else (last_m1["close"] < last_m1["open"])
            if not mss_ok:
                return None
        except Exception:
            pass  # sem M1, segue com M5 apenas

        # 5) SL/TP
        # SL = swing oposto + pad
        # busca swing low/high recente para SL
        if direction == "BULL":
            # SL abaixo do último swing low
            sl_ref = min((l for _, l in lows), default=swing_price - 5)
            sl = sl_ref - sl_pad
            # guarda contra SL muito largo (>6 USD)
            if price - sl > 6.0:
                sl = price - 5.0
            tp = price + (price - sl) * rr
            dir_str = "BUY"
        else:
            sl_ref = max((h for _, h in highs), default=swing_price + 5)
            sl = sl_ref + sl_pad
            if sl - price > 6.0:
                sl = price + 5.0
            tp = price - (sl - price) * rr
            dir_str = "SELL"

        meta = f"BOS {direction} swing {swing_price:.1f} OB/FVG {zone_low:.1f}-{zone_high:.1f}"
        logger.info(f"SMC signal {dir_str} {self.symbol} entry {price:.2f} SL {sl:.2f} TP {tp:.2f} | {meta}")
        return Signal(direction=dir_str, entry=price, sl=sl, tp=tp, meta=meta)
