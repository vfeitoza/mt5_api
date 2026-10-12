"""Scalp M1 — operacional scalper XAUUSD M1 (holding 5-15min).

Complementar ao champion M5: micro-pullbacks em M1 com M5 como tendência,
stop/ganho dinâmicos por ATR e proteção (BE + trailing atr/structure).

Gate: 1 posição por magic+symbol via Engine (já aplicado). Score leve 0-10
com threshold configurável; não conflita com champion (magic distinto).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
from mt5_bridge.market_data.indicators import calc_cci, calc_atr, calc_ema_from_rev, pip_size
from mt5_bridge.market_data.smc import detect_fvgs, detect_bos, detect_sr

logger = logging.getLogger(__name__)


def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.M1


def _get(cfg: dict, key: str, default):
    if not isinstance(cfg, dict):
        return default
    if "params" in cfg and isinstance(cfg["params"], dict):
        merged = {**cfg, **cfg["params"]}
        return merged.get(key, default)
    return cfg.get(key, default)


def _session_ok(now_utc: datetime, start_h: int, end_h: int) -> bool:
    return start_h <= now_utc.hour < end_h


class ScalpM1Strategy(BaseStrategy):
    name = "scalp_m1"
    magic = 102012
    symbol = "XAUUSD"
    timeframe = "M1"

    async def check_signal(self, bridge) -> Optional[Signal]:
        cfg = self.config

        def gp(k, d): return _get(cfg, k, d)

        # params
        spread_max = float(gp("spread_max", 6))
        rr = float(gp("rr", 1.5))
        sl_pips = int(gp("sl_pips", 0))  # 0 = ATR
        sl_atr_mult = float(gp("sl_atr_mult", 1.2))
        atr_period = int(gp("atr_period", 14))
        min_atr_pips = float(gp("min_atr_pips", 8))  # SL mínimo para não ficar muito apertado
        max_atr_pips = float(gp("max_atr_pips", 25))  # SL máximo para scalp
        ema_fast = int(gp("ema_fast", 9))
        ema_slow = int(gp("ema_slow", 21))
        cci_period = int(gp("cci_period", 14))
        cci_buy_level = float(gp("cci_buy_level", -80))
        cci_sell_level = float(gp("cci_sell_level", 80))
        use_cci = bool(gp("use_cci", True))
        use_m5_trend = bool(gp("use_m5_trend", True))
        m5_ema_period = int(gp("m5_ema_period", 20))
        news_before = int(gp("news_before", 15))
        news_after = int(gp("news_after", 15))
        use_news = bool(gp("use_news", True))
        news_include_moderate = bool(gp("news_include_moderate", False))
        use_session = bool(gp("use_session", True))
        session_start = int(gp("session_start", 7))
        session_end = int(gp("session_end", 17))
        min_score = int(gp("min_score", 6))
        show_log = bool(gp("show_score_log", True))
        lot_size = float(gp("lot_size", 0.01))
        min_lot = float(gp("min_lot", 0.01))
        max_lot = float(gp("max_lot", 10.0))

        # 0) tick + gates leves
        try:
            tick = await bridge.get_tick(self.symbol)
        except Exception as e:
            logger.debug(f"Scalp get_tick fail {e}")
            return None

        psize = pip_size(self.symbol)
        if spread_max and spread_max > 0:
            spread_pips = (tick.ask - tick.bid) / psize if psize else 999
            if spread_pips > spread_max:
                logger.debug(f"Scalp spread {spread_pips:.1f} > {spread_max}")
                return None

        now_utc = datetime.now(timezone.utc)
        if use_session and not _session_ok(now_utc, session_start, session_end):
            logger.debug("Scalp fora da sessão")
            return None

        if use_news:
            try:
                events = await bridge.get_calendar(hours_ahead=24)
                filt = []
                for ev in events:
                    imp = (ev.impact or "").lower()
                    if imp == "high":
                        filt.append(ev)
                    elif imp == "medium" and news_include_moderate:
                        filt.append(ev)
                for ev in filt:
                    ev_time = None
                    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            ev_time = datetime.strptime(ev.time, fmt).replace(tzinfo=timezone.utc)
                            break
                        except Exception:
                            continue
                    if ev_time is None:
                        continue
                    dmin = (ev_time - now_utc).total_seconds() / 60
                    if -news_after <= dmin <= news_before:
                        logger.debug(f"Scalp news block {ev.title} {dmin:.0f}min")
                        return None
            except Exception as e:
                logger.debug(f"Scalp calendar fail {e}")

        # 1) dados: M1 principal + M5 tendência
        try:
            rates_m1 = await bridge.get_rates(self.symbol, _tf(self.timeframe), 80)
        except Exception as e:
            logger.debug(f"Scalp get_rates M1 fail {e}")
            return None
        if len(rates_m1) < max(ema_slow + 5, atr_period + 5):
            return None
        rev = list(reversed(rates_m1))

        rev_m5 = None
        if use_m5_trend:
            try:
                rates_m5 = await bridge.get_rates(self.symbol, Timeframe.M5, 60)
                if len(rates_m5) >= m5_ema_period + 3:
                    rev_m5 = list(reversed(rates_m5))
            except Exception:
                rev_m5 = None

        # 2) indicadores M1
        ema_f = calc_ema_from_rev(rev, ema_fast)
        ema_s = calc_ema_from_rev(rev, ema_slow)
        cci_vals = calc_cci(rev, cci_period) if use_cci else [None] * len(rev)
        atr_vals = calc_atr(rev, atr_period)
        cur_atr = atr_vals[-1] if atr_vals and atr_vals[-1] else None

        # estruturas leves
        fvgs = detect_fvgs(rev, fvg_lookback=12, fvg_min_pips=5, symbol=self.symbol)
        bos = detect_bos(rev, bos_lookback=20)
        sr_levels = detect_sr(rev, sr_lookback=60, sr_min_touches=2, sr_threshold_pips=12, symbol=self.symbol)

        # 3) tendência M1 (EMA) e M5
        trend_m1 = 0
        try:
            f1, s1 = ema_f[-1], ema_s[-1]
            f2, s2 = ema_f[-2], ema_s[-2] if len(ema_f) >= 2 and len(ema_s) >= 2 else (None, None)
            if f1 is not None and s1 is not None:
                if f1 > s1 and (f2 is None or s2 is None or f1 - s1 > (f2 - s2 if f2 is not None and s2 is not None else 0)):
                    trend_m1 = 1
                elif f1 < s1:
                    trend_m1 = -1
        except Exception:
            pass

        trend_m5 = 0
        if rev_m5 is not None:
            try:
                ema_m5 = calc_ema_from_rev(rev_m5, m5_ema_period)
                if ema_m5[-2] is not None and ema_m5[-3] is not None:
                    if rev_m5[-2]["close"] > ema_m5[-2] and ema_m5[-2] > ema_m5[-3]:
                        trend_m5 = 1
                    elif rev_m5[-2]["close"] < ema_m5[-2] and ema_m5[-2] < ema_m5[-3]:
                        trend_m5 = -1
            except Exception:
                pass

        # 4) CCI momentum M1 (sem exigir extremo, scalp reage a momentum)
        cci0 = cci_vals[-1] if cci_vals and cci_vals[-1] is not None else 0
        cci_buy = cci0 <= cci_buy_level or (cci_vals[-2] is not None and cci_vals[-2] < cci_buy_level and cci0 > cci_buy_level)  # cruzou de baixo
        cci_sell = cci0 >= cci_sell_level or (cci_vals[-2] is not None and cci_vals[-2] > cci_sell_level and cci0 < cci_sell_level)

        # S/R proximidade (12 pips)
        threshold = 12 * psize
        has_sup = any(abs(tick.bid - sr["level"]) <= threshold and sr["isSupport"] for sr in sr_levels)
        has_res = any(abs(tick.ask - sr["level"]) <= threshold and not sr["isSupport"] for sr in sr_levels)
        sup_touches = max((sr["touches"] for sr in sr_levels if sr["isSupport"] and abs(tick.bid - sr["level"]) <= threshold), default=0)
        res_touches = max((sr["touches"] for sr in sr_levels if not sr["isSupport"] and abs(tick.ask - sr["level"]) <= threshold), default=0)

        # FVG micro (se preço ainda não mitigado)
        has_bull_fvg = any(f["isBullish"] and not f.get("isFilled") and tick.bid < f["gapHigh"] for f in fvgs)
        has_bear_fvg = any(not f["isBullish"] and not f.get("isFilled") and tick.ask > f["gapLow"] for f in fvgs)

        # BOS M1 alinhado
        bos_buy = bos == 1
        bos_sell = bos == -1

        # 5) score 0-10 leve
        score_buy = 0
        score_sell = 0
        if trend_m1 == 1:
            score_buy += 2
        if trend_m1 == -1:
            score_sell += 2
        if trend_m5 == 1:
            score_buy += 1
        if trend_m5 == -1:
            score_sell += 1
        if cci_buy:
            score_buy += 2
        if cci_sell:
            score_sell += 2
        if has_sup:
            score_buy += 2 if sup_touches >= 3 else 1
        if has_res:
            score_sell += 2 if res_touches >= 3 else 1
        if has_bull_fvg:
            score_buy += 1
        if has_bear_fvg:
            score_sell += 1
        if bos_buy:
            score_buy += 1
        if bos_sell:
            score_sell += 1
        # penalidade contratendência M5
        if trend_m5 == 1 and score_sell > 0:
            score_sell -= 1
        if trend_m5 == -1 and score_buy > 0:
            score_buy -= 1
        score_buy = max(0, score_buy)
        score_sell = max(0, score_sell)

        if show_log and (score_buy >= 3 or score_sell >= 3):
            logger.info(f"Scalp M1 scores BUY {score_buy}/10 SELL {score_sell}/10 | EMA M1:{trend_m1} M5:{trend_m5} | CCI {cci0:.0f} buy:{cci_buy} sell:{cci_sell} | FVG b:{has_bull_fvg} s:{has_bear_fvg} | S/R sup:{has_sup}:{sup_touches} res:{has_res}:{res_touches} | BOS {bos}")

        direction = None
        if score_buy >= min_score and score_buy > score_sell:
            direction = "BUY"
        elif score_sell >= min_score and score_sell > score_buy:
            direction = "SELL"
        else:
            return None

        # 6) STOP/TP dinâmicos por ATR (scalp)
        if sl_pips > 0:
            sl_dist = sl_pips * psize
        elif cur_atr and cur_atr > 0:
            sl_dist = cur_atr * sl_atr_mult
            # clamp por ATR scalp
            min_sl = min_atr_pips * psize
            max_sl = max_atr_pips * psize
            sl_dist = max(min_sl, min(max_sl, sl_dist))
        else:
            sl_dist = 15 * psize  # fallback M1

        # proteção: SL mínimo broker ~ 20 pips *0.5 para M1 (hot-reload clamp)
        entry = tick.ask if direction == "BUY" else tick.bid
        sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
        tp = entry + sl_dist * rr if direction == "BUY" else entry - sl_dist * rr

        # arredondamento XAU
        dec = 2 if "XAU" in self.symbol or "GOLD" in self.symbol else 5
        sl = round(sl, dec)
        tp = round(tp, dec)

        meta = (f"Scalp M1 score B{score_buy}/S{score_sell} EMA M1:{trend_m1} M5:{trend_m5} "
                f"CCI {cci0:.0f} BOS {bos} FVG b:{has_bull_fvg} s:{has_bear_fvg} "
                f"S/R sup:{sup_touches} res:{res_touches} ATR {sl_dist/psize:.1f}p RR 1:{rr:.1f}")

        vol = lot_size if lot_size > 0 else float(_get(cfg, "volume", 0.01))
        vol = max(min_lot, min(max_lot, vol))

        logger.info(f"Scalp sinal {direction} score {score_buy if direction=='BUY' else score_sell}/{min_score} entry {entry:.2f} SL {sl:.2f} TP {tp:.2f} ({sl_dist/psize:.1f}p RR 1:{rr:.1f}) | {meta}")

        sig = Signal(direction=direction, entry=entry, sl=sl, tp=tp, meta=meta)
        try:
            sig.volume = vol  # type: ignore
        except Exception:
            pass
        return sig
