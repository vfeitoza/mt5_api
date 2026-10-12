"""Swing FX — H4 Supply/Demand + sweep + EMA 50/200 para majors.

Pares: EURUSD, USDJPY, GBPUSD, AUDUSD, EURJPY (config).
Tese: bias H4 S/D + break of structure em H1 + EMA 50/200 daily/H4
       com RSI divergence e OB/FVG. Payroll NO_TRADE dia.
Hold 2-7 dias. SL 60-90 pips estrutura, TP 1:2.5-3.

LLM ativo. 1 posição por magic+symbol.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
from mt5_bridge.market_data.indicators import pip_size, calc_atr, calc_ema_from_rev, calc_rsi, calc_adx, calc_cci
from mt5_bridge.market_data.smc import detect_sr, detect_bos

logger = logging.getLogger(__name__)


def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.H1


def _get(cfg: dict, key: str, default):
    if not isinstance(cfg, dict):
        return default
    if "params" in cfg and isinstance(cfg["params"], dict):
        merged = {**cfg, **cfg["params"]}
        return merged.get(key, default)
    return cfg.get(key, default)


def _is_payroll_day(now_utc: datetime, events) -> bool:
    for ev in events or []:
        title = (ev.title if not isinstance(ev, dict) else ev.get("title", "")).lower()
        if any(k in title for k in ("nonfarm", "non-farm", "nfp", "payroll")):
            t = ev.time if not isinstance(ev, dict) else ev.get("time", "")
            try:
                for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        ev_dt = datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
                        if ev_dt.date() == now_utc.date():
                            return True
                        break
                    except Exception:
                        continue
            except Exception:
                return True
    return False


PER_SYMBOL_SWING = {
    "EURUSD": {"min_sl_pips": 45, "max_sl_pips": 90, "rr": 2.8},
    "USDJPY": {"min_sl_pips": 50, "max_sl_pips": 90, "rr": 2.5},
    "GBPUSD": {"min_sl_pips": 50, "max_sl_pips": 90, "rr": 2.8},
    "AUDUSD": {"min_sl_pips": 40, "max_sl_pips": 85, "rr": 2.5},
    "EURJPY": {"min_sl_pips": 50, "max_sl_pips": 90, "rr": 2.5},
    "USDCHF": {"min_sl_pips": 40, "max_sl_pips": 85, "rr": 2.5},
}


class SwingFxStrategy(BaseStrategy):
    name = "swing_fx"
    magic = 102016
    symbol = "EURUSD"
    timeframe = "H1"

    async def check_signal(self, bridge) -> Optional[Signal]:
        cfg = self.config

        def gp(k, d): return _get(cfg, k, d)

        symbols: list[str] = gp("symbols", [self.symbol, "USDJPY", "GBPUSD"])
        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = [s.upper().strip() for s in symbols]

        use_llm = bool(gp("use_llm", True))
        show_log = bool(gp("show_score_log", True))
        spread_max_pips = float(gp("spread_max_pips", 2.0))
        min_score = int(gp("min_score", 7))
        rr_default = float(gp("rr", 2.8))
        news_before = int(gp("news_before", 60))  # swing foge mais
        news_after = int(gp("news_after", 60))
        news_include_moderate = bool(gp("news_include_moderate", True))

        now_utc = datetime.now(timezone.utc)

        # News + Payroll
        try:
            events = await bridge.get_calendar(hours_ahead=48)
        except Exception:
            events = []
        if _is_payroll_day(now_utc, events):
            logger.info("SwingFX payroll day — NO_TRADE todo o dia (NFP)")
            return None
        # se High em 60min, bloquear todos FX swing
        try:
            for ev in events:
                t = ev.time if not isinstance(ev, dict) else ev.get("time", "")
                title = ev.title if not isinstance(ev, dict) else ev.get("title", "")
                impact = ev.impact if not isinstance(ev, dict) else ev.get("impact", "")
                imp_l = (impact or "").lower()
                if imp_l == "high" or (imp_l == "medium" and news_include_moderate):
                    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            ev_t = datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
                            dmin = (ev_t - now_utc).total_seconds() / 60
                            if -news_after <= dmin <= news_before:
                                # USD High bloqueia todos majors
                                currency = (ev.currency if not isinstance(ev, dict) else ev.get("currency", "")).upper()
                                if currency in ("USD", "", "ALL") or "USD" in title:
                                    logger.info(f"SwingFX news block {title} ({impact}) em {dmin:.0f}min")
                                    return None
                            break
                        except Exception:
                            continue
        except Exception:
            pass

        best_signal: Optional[Signal] = None
        best_score = -1

        for symbol in symbols:
            try:
                tick = await bridge.get_tick(symbol)
            except Exception as e:
                logger.debug(f"Swing {symbol} tick fail {e}")
                continue
            psize = pip_size(symbol)
            spread_pips = (tick.ask - tick.bid) / psize if psize else 999
            if spread_pips > spread_max_pips:
                continue

            # dados H4 (bias) + H1 (entry) + D1 (EMA 200)
            try:
                rates_h4 = await bridge.get_rates(symbol, Timeframe.H4, 80)
                rates_h1 = await bridge.get_rates(symbol, Timeframe.H1, 120)
                if len(rates_h4) < 40 or len(rates_h1) < 60:
                    continue
                rev_h4 = list(reversed(rates_h4))
                rev_h1 = list(reversed(rates_h1))
            except Exception:
                continue
            rev_d1 = None
            try:
                r_d1 = await bridge.get_rates(symbol, Timeframe.D1, 80)
                if len(r_d1) >= 50:
                    rev_d1 = list(reversed(r_d1))
            except Exception:
                pass

            # EMA 50/200 D1/H4
            trend_d1 = trend_h4 = 0
            if rev_d1:
                ema50_d1 = calc_ema_from_rev(rev_d1, 50)
                ema200_d1 = calc_ema_from_rev(rev_d1, 200)
                if ema50_d1[-1] and ema200_d1[-1]:
                    if ema50_d1[-1] > ema200_d1[-1] and rev_d1[-1]["close"] > ema50_d1[-1]:
                        trend_d1 = 1
                    elif ema50_d1[-1] < ema200_d1[-1] and rev_d1[-1]["close"] < ema50_d1[-1]:
                        trend_d1 = -1
            ema50_h4 = calc_ema_from_rev(rev_h4, 50)
            ema200_h4 = calc_ema_from_rev(rev_h4, 200)
            if ema50_h4[-1] and ema200_h4[-1]:
                if ema50_h4[-1] > ema200_h4[-1]:
                    trend_h4 = 1
                elif ema50_h4[-1] < ema200_h4[-1]:
                    trend_h4 = -1

            # S/D + sweep H4: level recente rejeitado + wick sweep
            sr_h4 = detect_sr(rev_h4, sr_lookback=100, sr_min_touches=2, sr_threshold_pips=25 if "JPY" in symbol else 18, symbol=symbol)
            # S/R forte mais próximo
            has_sup = has_res = False
            sup_touches = res_touches = 0
            thresh = (25 if "JPY" in symbol else 18) * psize
            for sr in sr_h4:
                if sr["isSupport"] and abs(tick.bid - sr["level"]) <= thresh * 2:
                    has_sup = True
                    sup_touches = max(sup_touches, sr["touches"])
                if not sr["isSupport"] and abs(tick.ask - sr["level"]) <= thresh * 2:
                    has_res = True
                    res_touches = max(res_touches, sr["touches"])

            # BOS H1
            bos = detect_bos(rev_h1, bos_lookback=30)
            rsi_h1 = calc_rsi(rev_h1, 14)
            cur_rsi = rsi_h1[-1] if rsi_h1 and rsi_h1[-1] is not None else 50
            cci_h1 = calc_cci(rev_h1, 20)
            cur_cci = cci_h1[-1] if cci_h1 and cci_h1[-1] is not None else 0
            adx_pack = calc_adx(rev_h1, 14)
            cur_adx = adx_pack["adx"][-1] if adx_pack["adx"] and adx_pack["adx"][-1] is not None else 0
            atr_h1 = calc_atr(rev_h1, 14)
            cur_atr = atr_h1[-1] if atr_h1 and atr_h1[-1] else None

            # RSI divergence (simplificado swing)
            bull_div = bear_div = False
            if len(rev_h1) >= 20 and rsi_h1[-1] is not None and rsi_h1[-6] is not None and rsi_h1[-11] is not None:
                price_down = rev_h1[-1]["close"] < rev_h1[-6]["close"] < rev_h1[-11]["close"]
                rsi_up = rsi_h1[-1] > rsi_h1[-6]
                if price_down and rsi_up and rsi_h1[-1] < 45:
                    bull_div = True
                price_up = rev_h1[-1]["close"] > rev_h1[-6]["close"] > rev_h1[-11]["close"]
                if price_up and rsi_h1[-1] < rsi_h1[-6] and rsi_h1[-1] > 55:
                    bear_div = True

            # Score 0-10 (min 7) por símbolo
            score_buy = score_sell = 0
            # H4/H1 trend align
            if trend_h4 == 1 and trend_d1 >= 0:
                score_buy += 2
            if trend_h4 == -1 and trend_d1 <= 0:
                score_sell += 2
            # S/D
            if has_sup:
                score_buy += 2 if sup_touches >= 3 else 1
            if has_res:
                score_sell += 2 if res_touches >= 3 else 1
            # BOS H1
            if bos == 1:
                score_buy += 2
            elif bos == -1:
                score_sell += 2
            # RSI div
            if bull_div:
                score_buy += 2
            if bear_div:
                score_sell += 2
            # CCI extreme alinhado
            if cur_cci < -80 and trend_h4 >= 0:
                score_buy += 1
            if cur_cci > 80 and trend_h4 <= 0:
                score_sell += 1
            # ADX trend strength
            if cur_adx and cur_adx > 18:
                if trend_h4 == 1:
                    score_buy += 1
                if trend_h4 == -1:
                    score_sell += 1
            # VWAP-like: preço acima EMA50 H4
            if ema50_h4[-1] and rev_h4[-1]["close"] > ema50_h4[-1] and trend_h4 == 1:
                score_buy += 1
            if ema50_h4[-1] and rev_h4[-1]["close"] < ema50_h4[-1] and trend_h4 == -1:
                score_sell += 1
            # penal contra-trend
            if trend_h4 == 1 and score_sell > 0:
                score_sell -= 1
            if trend_h4 == -1 and score_buy > 0:
                score_buy -= 1
            score_buy = max(0, score_buy)
            score_sell = max(0, score_sell)

            if show_log and (score_buy >= 3 or score_sell >= 3):
                logger.info(f"SwingFX {symbol} B{score_buy}/10 S{score_sell}/10 | H4 {trend_h4} D1 {trend_d1} BOS {bos} RSI {cur_rsi:.0f} CCI {cur_cci:.0f} ADX {cur_adx:.0f} | S/D sup {has_sup}:{sup_touches} res {has_res}:{res_touches} {'bullDiv' if bull_div else ''}{'bearDiv' if bear_div else ''}")

            direction = None
            score = 0
            if score_buy >= min_score and score_buy > score_sell:
                direction = "BUY"
                score = score_buy
            elif score_sell >= min_score and score_sell > score_buy:
                direction = "SELL"
                score = score_sell
            else:
                continue

            # SL/TP por par
            cfg_sym = PER_SYMBOL_SWING.get(symbol, PER_SYMBOL_SWING["EURUSD"])
            rr = float(_get(cfg, "rr", cfg_sym["rr"]))
            min_sl = cfg_sym["min_sl_pips"] * psize
            max_sl = cfg_sym["max_sl_pips"] * psize
            atr_mult = float(_get(cfg, "sl_atr_mult", 1.8))
            if cur_atr and cur_atr > 0:
                sl_dist = cur_atr * atr_mult
            else:
                sl_dist = min_sl * 1.2
            sl_dist = max(min_sl, min(max_sl, sl_dist))
            tp_dist = sl_dist * rr

            entry = tick.ask if direction == "BUY" else tick.bid
            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            tp = entry + tp_dist if direction == "BUY" else entry - tp_dist

            dec = 3 if "JPY" in symbol else 5
            sl = round(sl, dec)
            tp = round(tp, dec)

            meta = (
                f"Swing H4/H1 {symbol} {score}/{min_score} | "
                f"H4 {trend_h4} D1 {trend_d1} BOS {bos} RSI {cur_rsi:.0f} CCI {cur_cci:.0f} ADX {cur_adx:.0f} | "
                f"S/D sup {sup_touches} res {res_touches} {'bullDiv' if bull_div else ''}{'bearDiv' if bear_div else ''} | "
                f"ATR {cur_atr/psize:.0f}p SL {sl_dist/psize:.0f}p RR 1:{rr:.1f}"
            )

            cfg_vol = float(_get(cfg, "volume", 0.03))
            lot = float(_get(cfg, "lot_size", cfg_vol))
            vol_lot = max(float(_get(cfg, "min_lot", 0.01)), min(float(_get(cfg, "max_lot", 10.0)), lot))

            sig = Signal(direction=direction, entry=entry, sl=sl, tp=tp, meta=meta)
            try:
                sig.volume = vol_lot  # type: ignore
                sig.use_llm = use_llm  # type: ignore
                sig.symbol = symbol  # type: ignore
            except Exception:
                pass

            if score > best_score:
                best_score = score
                best_signal = sig

        if best_signal:
            chosen = getattr(best_signal, "symbol", symbols[0])
            logger.info(f"SwingFX escolhido {chosen} score {best_score} entry {best_signal.entry:.5f} SL {best_signal.sl:.5f} TP {best_signal.tp:.5f}")
            return best_signal
        return None
