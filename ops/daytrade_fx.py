"""Daytrade FX — M15 Killzone + London Breakout para majors.

Pares: EURUSD, USDJPY, GBPUSD (config symbols).
Tese: London breakout (range Ásia 00-07 UTC) + ICT Killzone 07-10 UTC (London) / 13-16 UTC (NY AM)
       filtrado por EMA 50/200 H1 + ADX + ATR + round levels.
Payroll (NFP) = NO_TRADE dia todo. LLM ativo para validação macro.

Score 0-10 (min 6). 1 posição por magic+symbol via Engine.
Hold 2-8h. SL ATR 14 M15 ×1.5 clamp por par. TP 1:2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
from mt5_bridge.market_data.indicators import pip_size, calc_atr, calc_ema_from_rev, calc_rsi, calc_adx

logger = logging.getLogger(__name__)


def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.M15


def _get(cfg: dict, key: str, default):
    if not isinstance(cfg, dict):
        return default
    if "params" in cfg and isinstance(cfg["params"], dict):
        merged = {**cfg, **cfg["params"]}
        return merged.get(key, default)
    return cfg.get(key, default)


def _is_payroll_day(now_utc: datetime, events) -> bool:
    for ev in events or []:
        title = (ev.title or ev.get("title", "") if isinstance(ev, dict) else ev.title or "").lower()
        if any(k in title for k in ("nonfarm", "non-farm", "nfp", "payroll")):
            # se evento é hoje (data igual)
            t = ev.time if not isinstance(ev, dict) else ev.get("time", "")
            try:
                ev_dt = None
                for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        ev_dt = datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
                        break
                    except Exception:
                        continue
                if ev_dt and ev_dt.date() == now_utc.date():
                    return True
            except Exception:
                return True  # conservador
    return False


def _killzone_bonus(now_utc: datetime) -> tuple[int, str]:
    h = now_utc.hour
    # London Killzone 07-10 UTC = 02-05 ET, NY AM 13-16 UTC = 08-11 ET
    if 7 <= h < 10:
        return 1, "London Killzone"
    if 13 <= h < 16:
        return 1, "NY AM Killzone"
    if 0 <= h < 7:
        return 0, "Asian"
    return 0, "off-hours"


def _asian_range(rev_m15: list[dict]) -> tuple[float | None, float | None]:
    # Asian 00-07 UTC do dia atual em rev_m15
    if not rev_m15:
        return None, None
    # today date from last candle time
    try:
        last_time = rev_m15[-1].get("time", "")
        day = None
        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M"):
            try:
                day = datetime.strptime(last_time, fmt).date()
                break
            except Exception:
                continue
        if not day:
            return None, None
        highs, lows = [], []
        for r in rev_m15:
            t = r.get("time", "")
            dt = None
            for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M"):
                try:
                    dt = datetime.strptime(t, fmt)
                    break
                except Exception:
                    continue
            if dt and dt.date() == day and 0 <= dt.hour < 7:
                highs.append(r["high"])
                lows.append(r["low"])
        if highs and lows:
            return max(highs), min(lows)
    except Exception:
        pass
    return None, None


def _round_level(symbol: str, price: float) -> tuple[bool, str]:
    # Forex round: EURUSD 1.0850 / 1.0800 (50 pips), USDJPY 150.00 / 150.50
    s = symbol.upper()
    if "JPY" in s:
        frac = price % 0.50
        if frac < 0.03 or frac > 0.47:
            return True, f"round JPY 0.50 {price:.2f}"
        return False, ""
    # majors
    frac = (price * 10000) % 50  # 50 pips = 0.0050
    if frac < 3 or frac > 47:
        return True, f"round 50pips {price:.5f}"
    return False, ""


# Per-symbol defaults (pips)
PER_SYMBOL = {
    "EURUSD": {"min_sl_pips": 18, "max_sl_pips": 40, "sl_atr_mult": 1.5, "rr": 2.0},
    "USDJPY": {"min_sl_pips": 22, "max_sl_pips": 45, "sl_atr_mult": 1.5, "rr": 2.0},
    "GBPUSD": {"min_sl_pips": 22, "max_sl_pips": 45, "sl_atr_mult": 1.5, "rr": 2.0},
    "AUDUSD": {"min_sl_pips": 18, "max_sl_pips": 38, "sl_atr_mult": 1.5, "rr": 2.0},
    "EURJPY": {"min_sl_pips": 22, "max_sl_pips": 45, "sl_atr_mult": 1.5, "rr": 2.0},
}


class DaytradeFxStrategy(BaseStrategy):
    name = "daytrade_fx"
    magic = 102015
    symbol = "EURUSD"
    timeframe = "M15"

    async def check_signal(self, bridge) -> Optional[Signal]:
        cfg = self.config

        def gp(k, d): return _get(cfg, k, d)

        symbols: list[str] = gp("symbols", [self.symbol, "USDJPY", "GBPUSD"])
        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = [s.upper().strip() for s in symbols]

        use_llm = bool(gp("use_llm", True))
        show_log = bool(gp("show_score_log", True))
        spread_max_pips = float(gp("spread_max_pips", 1.5))
        min_score = int(gp("min_score", 6))
        news_before = int(gp("news_before", 20))
        news_after = int(gp("news_after", 20))
        news_include_moderate = bool(gp("news_include_moderate", False))
        # Payroll já trata, mas news geral também
        adx_min = float(gp("adx_min", 18))

        now_utc = datetime.now(timezone.utc)
        kzone_bonus, kzone_label = _killzone_bonus(now_utc)

        # News + Payroll check global
        try:
            events = await bridge.get_calendar(hours_ahead=24)
        except Exception:
            events = []
        if _is_payroll_day(now_utc, events):
            logger.info("DaytradeFX payroll day — NO_TRADE todo o dia (NFP)")
            return None
        # news block ±20 por símbolo (apenas se evento relevante para par)
        # simplificado: se houver High USD/EUR/GBP/JPY próximo, bloquear todos os pares FX
        try:
            filt = []
            for ev in events:
                imp = (ev.impact or "").lower() if not isinstance(ev, dict) else (ev.get("impact","").lower())
                if imp == "high" or (imp == "medium" and news_include_moderate):
                    filt.append(ev)
            for ev in filt:
                t = ev.time if not isinstance(ev, dict) else ev.get("time","")
                title = ev.title if not isinstance(ev, dict) else ev.get("title","")
                impact = ev.impact if not isinstance(ev, dict) else ev.get("impact","")
                ev_t = None
                for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        ev_t = datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
                        break
                    except Exception:
                        continue
                if ev_t is None:
                    continue
                dmin = (ev_t - now_utc).total_seconds() / 60
                if -news_after <= dmin <= news_before:
                    # se USD event, bloqueia todos FX majors; se não, bloqueia só se moeda do par
                    currency = (ev.currency if not isinstance(ev, dict) else ev.get("currency","")).upper()
                    # se evento USD ou sem moeda, bloqueia todos FX
                    if currency in ("USD", "", "ALL") or "USD" in title:
                        logger.info(f"DaytradeFX news block {title} ({impact}) em {dmin:.0f}min")
                        return None
        except Exception:
            pass

        best_signal: Optional[Signal] = None
        best_score = -1
        best_meta = ""

        for symbol in symbols:
            try:
                tick = await bridge.get_tick(symbol)
            except Exception as e:
                logger.debug(f"Daytrade {symbol} tick fail {e}")
                continue
            psize = pip_size(symbol)
            spread_pips = (tick.ask - tick.bid) / psize if psize else 999
            if spread_pips > spread_max_pips:
                logger.debug(f"Daytrade {symbol} spread {spread_pips:.1f} > {spread_max_pips}")
                continue
            # dados M15 + H1 + D1
            try:
                rates_m15 = await bridge.get_rates(symbol, Timeframe.M15, 120)
                if len(rates_m15) < 50:
                    continue
                rev = list(reversed(rates_m15))
            except Exception:
                continue
            rev_h1 = rev_d1 = None
            try:
                r_h1 = await bridge.get_rates(symbol, Timeframe.H1, 80)
                if len(r_h1) >= 50:
                    rev_h1 = list(reversed(r_h1))
            except Exception:
                pass
            try:
                r_d1 = await bridge.get_rates(symbol, Timeframe.D1, 60)
                if len(r_d1) >= 50:
                    rev_d1 = list(reversed(r_d1))
            except Exception:
                pass

            # indicadores
            atr_vals = calc_atr(rev, 14)
            cur_atr = atr_vals[-1] if atr_vals and atr_vals[-1] else None
            rsi_vals = calc_rsi(rev, 14)
            cur_rsi = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else 50
            adx_pack = calc_adx(rev, 14)
            cur_adx = adx_pack["adx"][-1] if adx_pack["adx"] and adx_pack["adx"][-1] is not None else 0

            if cur_adx is not None and cur_adx < adx_min:
                if show_log:
                    logger.debug(f"Daytrade {symbol} ADX {cur_adx:.1f} < {adx_min} skip")
                continue

            # EMA H1 filter
            trend_h1 = 0
            if rev_h1:
                ema50_h1 = calc_ema_from_rev(rev_h1, 50)
                ema200_h1 = calc_ema_from_rev(rev_h1, 200)
                if ema50_h1[-1] and ema200_h1[-1]:
                    if ema50_h1[-1] > ema200_h1[-1] and rev_h1[-1]["close"] > ema50_h1[-1]:
                        trend_h1 = 1
                    elif ema50_h1[-1] < ema200_h1[-1] and rev_h1[-1]["close"] < ema50_h1[-1]:
                        trend_h1 = -1

            # London breakout: Asian range
            asian_high, asian_low = _asian_range(rev)
            breakout_dir = 0
            breakout_str = ""
            if asian_high and asian_low:
                last_close = rev[-1]["close"]
                # breakout com buffer 2-3 pips
                buf = psize * 2.5
                if last_close > asian_high + buf and tick.bid > asian_high:
                    breakout_dir = 1
                    breakout_str = f"break Asian H {asian_high:.5f}" if "JPY" not in symbol else f"break Asian H {asian_high:.3f}"
                elif last_close < asian_low - buf and tick.ask < asian_low:
                    breakout_dir = -1
                    breakout_str = f"break Asian L {asian_low:.5f}" if "JPY" not in symbol else f"break Asian L {asian_low:.3f}"

            # Round level
            is_round, round_str = _round_level(symbol, tick.bid)

            # Momentum: RSI + volume (tick_volume crescente)
            vols = [r.get("tick_volume", r.get("volume", 0)) or 0 for r in rev[-5:]]
            vol_up = len(vols) >= 3 and vols[-1] > vols[-2]

            # Score 0-10 por símbolo
            score_buy = score_sell = 0
            # Breakout
            if breakout_dir == 1:
                score_buy += 3
            elif breakout_dir == -1:
                score_sell += 3
            # Killzone timing
            if kzone_bonus and breakout_dir != 0:
                if breakout_dir == 1:
                    score_buy += 1
                else:
                    score_sell += 1
            # H1 EMA trend align
            if trend_h1 == 1:
                score_buy += 2
            elif trend_h1 == -1:
                score_sell += 2
            # RSI momentum
            if cur_rsi > 55 and trend_h1 >= 0:
                score_buy += 1
            if cur_rsi < 45 and trend_h1 <= 0:
                score_sell += 1
            # Round confluence
            if is_round and breakout_dir != 0:
                if breakout_dir == 1:
                    score_buy += 1
                else:
                    score_sell += 1
            # Volume
            if vol_up and breakout_dir != 0:
                if breakout_dir == 1:
                    score_buy += 1
                else:
                    score_sell += 1
            # Penalidade contratendência H1
            if trend_h1 == 1 and score_sell > 0:
                score_sell -= 1
            if trend_h1 == -1 and score_buy > 0:
                score_buy -= 1
            score_buy = max(0, score_buy)
            score_sell = max(0, score_sell)

            if show_log and (score_buy >= 3 or score_sell >= 3):
                logger.info(f"DaytradeFX {symbol} B{score_buy}/10 S{score_sell}/10 | H1 {trend_h1} RSI {cur_rsi:.0f} ADX {cur_adx:.0f} | {breakout_str} | {kzone_label} | round {is_round} vol {vol_up}")

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

            # SL/TP por par (tabela PER_SYMBOL)
            cfg_sym = PER_SYMBOL.get(symbol, PER_SYMBOL["EURUSD"])
            min_sl = cfg_sym["min_sl_pips"] * psize
            max_sl = cfg_sym["max_sl_pips"] * psize
            rr = float(_get(cfg, "rr", cfg_sym["rr"]))
            # ATR baseado
            if cur_atr and cur_atr > 0:
                sl_dist = cur_atr * 1.5
            else:
                sl_dist = min_sl * 1.2
            sl_dist = max(min_sl, min(max_sl, sl_dist))
            tp_dist = sl_dist * rr

            entry = tick.ask if direction == "BUY" else tick.bid
            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            tp = entry + tp_dist if direction == "BUY" else entry - tp_dist

            # decimais por símbolo
            dec = 3 if "JPY" in symbol else 5
            sl = round(sl, dec)
            tp = round(tp, dec)

            meta = (
                f"Daytrade M15 {symbol} score {score}/{min_score} | "
                f"H1 {trend_h1} RSI {cur_rsi:.0f} ADX {cur_adx:.0f} | "
                f"{breakout_str or 'no breakout'} | {kzone_label} | "
                f"{round_str or 'no round'} | vol {vol_up} | "
                f"ATR {cur_atr/psize:.1f}p SL {sl_dist/psize:.0f}p RR 1:{rr:.1f}"
            )

            cfg_vol = float(_get(cfg, "volume", 0.03))
            lot = float(_get(cfg, "lot_size", cfg_vol))
            vol_lot = max(float(_get(cfg, "min_lot", 0.01)), min(float(_get(cfg, "max_lot", 10.0)), lot))

            sig = Signal(direction=direction, entry=entry, sl=sl, tp=tp, meta=meta)
            try:
                sig.volume = vol_lot  # type: ignore
                sig.use_llm = use_llm  # type: ignore
            except Exception:
                pass

            # escolher melhor score entre os símbolos
            if score > best_score:
                best_score = score
                best_signal = sig
                best_meta = meta
                # guardar symbol no signal para engine log (engine usa strat.symbol mas meta contém símbolo real)
                try:
                    best_signal.symbol = symbol  # type: ignore
                except Exception:
                    pass

        if best_signal:
            # sobrescrever log para engine usar símbolo correto no trade
            # Engine faz bridge.buy(strat.symbol) — precisamos que strat.symbol reflita melhor símbolo
            # Guardamos em meta e deixamos Engine resolver via params.symbols; mas para simplicidade
            # definimos best_signal com atributo symbol e Engine vai usar meta; melhor: ajustar strat.symbol temporário?
            # Vamos anexar chosen_symbol
            chosen = getattr(best_signal, "symbol", symbols[0])
            logger.info(f"DaytradeFX escolhido {chosen} score {best_score} entry {best_signal.entry:.5f} SL {best_signal.sl:.5f} TP {best_signal.tp:.5f}")
            # Engine vai usar strat.symbol — precisamos contornar: setar signal.symbol e Engine ler?
            # Fallback: se Engine ignorar, o trade sai no símbolo primário mas meta indica divergência — vamos fazer Engine suportar signal.symbol
            return best_signal
        return None
