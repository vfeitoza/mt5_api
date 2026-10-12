"""scalping_Huy_5pts — adaptação fiel do SKILL huynguyen03dev/xauusd-trading
para o sistema interno (sem coleta de terceiros).

Filosofia: structure first (price > candle > indicators > TradingView).
Time: M5 primário (scalping), M15/1H contexto. SL fixo 5 pontos (5 * pip),
TP1 5 (1:1) / TP2 10 (1:2). Gates HARD/SOFT + checklist 6 + gestão BE/trailing.

Bridge nativo: get_tick / get_rates (sem TwelveData/TradingView).
LLM: ativo (params.use_llm true por padrão — engine filtra via llm_gate).
Magic 102014 separado para comparar no dashboard 5556.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
from mt5_bridge.market_data.indicators import (
    pip_size,
    calc_atr,
    calc_ema_from_rev,
    calc_rsi,
    calc_macd,
    calc_stochastic,
    calc_cci,
    calc_adx,
)

logger = logging.getLogger(__name__)


def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.M5


def _get(cfg: dict, key: str, default):
    if not isinstance(cfg, dict):
        return default
    if "params" in cfg and isinstance(cfg["params"], dict):
        merged = {**cfg, **cfg["params"]}
        return merged.get(key, default)
    return cfg.get(key, default)


def _utc_hour(now_utc: datetime) -> int:
    return now_utc.hour  # UTC sem DST


def _is_rollover_utc(now_utc: datetime) -> bool:
    # HARD Gate 5: 21:00-22:00 UTC = rollover, não operar (ou SL 8)
    return 21 <= now_utc.hour < 22


def _session_label_utc(now_utc: datetime) -> str:
    h = now_utc.hour
    # Soft Gate 6 mapeado para UTC
    if 13 <= h < 17:
        return "London-NY overlap (best)"
    if 8 <= h < 12:
        return "London"
    if 13 <= h < 21:
        return "NY"
    if 0 <= h < 8:
        return "Asian (warn)"
    return "off-hours"


def _swing_high_low(rev: list[dict], look: int = 10, pivot: int = 2):
    """Retorna (high_val, low_val, high_idx, low_idx) mais recente em look."""
    n = len(rev)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(pivot, min(look, n - pivot)):
        idx = n - 1 - i
        if idx < pivot or idx >= n - pivot:
            continue
        h = rev[idx]["high"]
        lo = rev[idx]["low"]
        is_high = True
        is_low = True
        for j in range(1, pivot + 1):
            if rev[idx - j]["high"] >= h or rev[idx + j]["high"] >= h:
                is_high = False
            if rev[idx - j]["low"] <= lo or rev[idx + j]["low"] <= lo:
                is_low = False
        if is_high:
            highs.append((idx, h))
        if is_low:
            lows.append((idx, lo))
    # mais recente = maior idx
    best_h = max(highs, key=lambda x: x[0]) if highs else (None, None)
    best_l = min(lows, key=lambda x: x[0]) if lows else (None, None)  # recent low: maior idx
    # na prática pegar último
    if lows:
        best_l = max(lows, key=lambda x: x[0])
    return best_h[1] if best_h[0] is not None else None, best_l[1] if best_l[0] is not None else None, best_h[0], best_l[0]


def _micro_consolidation(rev: list[dict], bars: int = 5) -> bool:
    if len(rev) < bars:
        return False
    seg = rev[-bars:]
    rng = max(r["high"] for r in seg) - min(r["low"] for r in seg)
    return rng < 1.0  # <10 points = <1.0 USD (pip 0.1 => 10 points =1.0)


def _rejection_wick(c: dict) -> tuple[bool, str]:
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    if rng == 0:
        return False, ""
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    if lower > rng * 0.55 and c["close"] > c["open"]:
        return True, f"rejection wick up lower {lower:.2f}/{rng:.2f}"
    if upper > rng * 0.55 and c["close"] < c["open"]:
        return True, f"rejection wick down upper {upper:.2f}/{rng:.2f}"
    return False, ""


def _engulfing(c0: dict, c1: dict) -> tuple[int, str]:
    b0 = c0["close"] - c0["open"]
    b1 = c1["close"] - c1["open"]
    if b1 < 0 and b0 > 0 and c0["open"] < c1["close"] and c0["close"] > c1["open"]:
        return 1, "bullish engulfing"
    if b1 > 0 and b0 < 0 and c0["open"] > c1["close"] and c0["close"] < c1["open"]:
        return -1, "bearish engulfing"
    return 0, ""


class ScalpingHuy5ptsStrategy(BaseStrategy):
    name = "scalping_huy_5pts"
    magic = 102014
    symbol = "XAUUSD"
    timeframe = "M5"

    async def check_signal(self, bridge) -> Optional[Signal]:
        cfg = self.config

        def gp(k, d): return _get(cfg, k, d)

        # ---- params (defaults fiéis ao SKILL) ----
        sl_points = int(gp("sl_points", 5))  # 5 pontos = 5*pip
        tp1_points = int(gp("tp1_points", 5))
        tp2_points = int(gp("tp2_points", 10))
        atr_gate_m5 = float(gp("atr_gate_m5", 8))  # HARD Gate 1: 5m ATR >8 -> SKIP
        atr_gate_m15 = float(gp("atr_gate_m15", 20))
        spread_warn_usd = float(gp("spread_warn_usd", 0.35))  # SOFT Gate 4 alerta (~3 pontos)
        news_before = int(gp("news_before", 15))
        news_after = int(gp("news_after", 15))
        rollover_block = bool(gp("rollover_block", True))
        use_adx = bool(gp("use_adx", False))  # opcional: ADX como contexto, não hard
        rsi_period = int(gp("rsi_period", 14))
        stoch_k = int(gp("stoch_k", 14))
        adx_period = int(gp("adx_period", 14))
        cci_period = int(gp("cci_period", 14))
        ema_fast = int(gp("ema_fast", 9))
        ema_slow = int(gp("ema_slow", 21))
        lot_size = float(gp("lot_size", 0.03))
        min_lot = float(gp("min_lot", 0.01))
        max_lot = float(gp("max_lot", 10.0))
        show_log = bool(gp("show_score_log", True))
        # SL widen pós 2 losses é tratado no PositionManager/gate externo; aqui mantém fixo

        psize = pip_size(self.symbol)  # XAU 0.1
        now_utc = datetime.now(timezone.utc)

        # 0) tick + HARD Gate 5 rollover
        try:
            tick = await bridge.get_tick(self.symbol)
        except Exception as e:
            logger.debug(f"Huy5pts get_tick fail {e}")
            return None

        if rollover_block and _is_rollover_utc(now_utc):
            logger.debug(f"Huy5pts HARD Gate 5 rollover 21-22 UTC NO_TRADE")
            return None

        spread = tick.ask - tick.bid
        spread_points = spread / psize if psize else 999
        # SOFT Gate 4: só alerta, não bloqueia (mas loga custo real)
        effective_sl_points = sl_points + spread_points + 1  # +1 slip
        effective_tp1 = tp1_points - spread_points
        effective_tp2 = tp2_points - spread_points

        # 1) candles 5m primário + 15m para GATE e tendência
        try:
            rates_m5 = await bridge.get_rates(self.symbol, _tf(self.timeframe), 50)
        except Exception as e:
            logger.debug(f"Huy5pts get_rates M5 fail {e}")
            return None
        if len(rates_m5) < 20:
            return None
        rev = list(reversed(rates_m5))

        # HARD Gate 3: stale check — se último candle não moveu vs penúltimo por 2 fetches seria stale; aqui só checa se rates vazios já tratado

        # spike $5+ com no setup -> STAND ASIDE (Huy)
        last_range = rev[-1]["high"] - rev[-1]["low"]
        if last_range >= 5.0:
            # só bloqueia se não há rejection/engulfing (sem setup)
            # deixa passar para stop-hunt reversal, mas marca
            pass

        # ATR gates
        atr_vals = calc_atr(rev, 14)
        cur_atr = atr_vals[-1] if atr_vals and atr_vals[-1] else 0
        cur_atr_points = cur_atr / psize if psize else 0
        if cur_atr_points > atr_gate_m5:
            logger.info(f"Huy5pts HARD Gate 1 vol 5m ATR {cur_atr:.2f}={cur_atr_points:.0f}pts >{atr_gate_m5} SKIP")
            return None

        rev_m15 = None
        try:
            rates_m15 = await bridge.get_rates(self.symbol, Timeframe.M15, 30)
            if len(rates_m15) >= 14:
                rev_m15 = list(reversed(rates_m15))
                atr15 = calc_atr(rev_m15, 14)
                atr15_cur = atr15[-1] if atr15 and atr15[-1] else 0
                atr15_pts = atr15_cur / psize if psize else 0
                if atr15_pts > atr_gate_m15:
                    logger.info(f"Huy5pts HARD Gate 1 vol 15m ATR {atr15_cur:.2f}={atr15_pts:.0f}pts >{atr_gate_m15} SKIP")
                    return None
                # HITF trend para checklist #4
        except Exception:
            pass

        # HARD Gate 2: news ±15
        try:
            events = await bridge.get_calendar(hours_ahead=24)
            high_events = [e for e in events if (e.impact or "").lower() == "high"]
            for ev in high_events:
                ev_t = None
                for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        ev_t = datetime.strptime(ev.time, fmt).replace(tzinfo=timezone.utc)
                        break
                    except Exception:
                        continue
                if ev_t is None:
                    continue
                dmin = (ev_t - now_utc).total_seconds() / 60
                if -news_after <= dmin <= news_before:
                    logger.info(f"Huy5pts HARD Gate 2 news {ev.title} {dmin:.0f}min SKIP")
                    return None
        except Exception:
            pass

        # 2) Micro estrutura
        sw_high, sw_low, hi_idx, lo_idx = _swing_high_low(rev, look=10, pivot=2)
        consolidating = _micro_consolidation(rev, 5)
        # micro S/R tight consolidation <10 points já checado

        # 3) Entry timing: AT level + pattern
        # AT level: preço perto de swing ou round $10/$5 ou micro S/R
        price = (tick.bid + tick.ask) / 2
        at_level = False
        at_reason = "mid-range"
        if sw_high is not None and abs(price - sw_high) < 0.9:
            at_level = True
            at_reason = f"near swing high {sw_high:.2f}"
        elif sw_low is not None and abs(price - sw_low) < 0.9:
            at_level = True
            at_reason = f"near swing low {sw_low:.2f}"
        else:
            # round numbers $10/$5
            r10 = round(price / 10) * 10
            r5 = round(price / 5) * 5
            if abs(price - r10) < 0.6:
                at_level = True
                at_reason = f"near round $10 {r10:.0f}"
            elif abs(price - r5) < 0.5:
                at_level = True
                at_reason = f"near round $5 {r5:.0f}"

        c0, c1 = rev[-1], rev[-2]
        wick_ok, wick_reason = _rejection_wick(c0)
        eng_dir, eng_reason = _engulfing(c0, c1)
        # Stop hunt reversal: wick sweeps prev high/low by 3-5 pts then V-recovers
        hunt = None
        if sw_high is not None:
            sweep = c0["high"] - sw_high
            if 0.3 <= sweep <= 0.55 and c0["close"] < sw_high:
                hunt = f"stop hunt high sweep {sweep:.2f}"
        if sw_low is not None and hunt is None:
            sweep = sw_low - c0["low"]
            if 0.3 <= sweep <= 0.55 and c0["close"] > sw_low:
                hunt = f"stop hunt low sweep {sweep:.2f}"

        pattern_ok = wick_ok or eng_dir != 0 or hunt is not None
        pattern_reason = hunt or wick_reason or eng_reason or "no pattern"

        # SL lógico atrás de estrutura?
        # Para BUY: SL = entry -5 deve ficar abaixo de sw_low ou wick low
        # Para SELL: SL acima de sw_high
        # Heurística: se sw próximo, SL é lógico; se mid-range, não
        sl_behind = at_level  # simplificado: se AT level, SL atrás dele é lógico

        # 4) Momentum 5m: last 3-5 candles bias, sem wick contra, volume crescente
        last5 = rev[-5:]
        closes = [c["close"] for c in last5]
        bullish_bias = closes[-1] > closes[-3] > closes[0]
        bearish_bias = closes[-1] < closes[-3] < closes[0]
        # wick contra
        wick_against_buy = any((c["high"] - max(c["open"], c["close"])) > (c["high"] - c["low"]) * 0.5 for c in last5[-3:])
        wick_against_sell = any((min(c["open"], c["close"]) - c["low"]) > (c["high"] - c["low"]) * 0.5 for c in last5[-3:])
        vol_up = False
        try:
            vols = [c.get("tick_volume", c.get("volume", 0)) or 0 for c in last5]
            vol_up = vols[-1] > vols[-2] > vols[0] if len(vols) >= 3 else False
        except Exception:
            pass
        momentum_buy = bullish_bias and not wick_against_buy
        momentum_sell = bearish_bias and not wick_against_sell

        # 5) Extreme readings
        stoch = calc_stochastic(rev, 14, 3, 3)
        cci_vals = calc_cci(rev, cci_period)
        rsi_vals = calc_rsi(rev, rsi_period)
        k_cur = stoch["k"][-1] if stoch["k"] and stoch["k"][-1] is not None else 50
        cci_cur = cci_vals[-1] if cci_vals and cci_vals[-1] is not None else 0
        rsi_cur = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else 50
        long_extreme = (k_cur < 10) or (cci_cur < -100) or (rsi_cur < 30)
        short_extreme = (k_cur > 90) or (cci_cur > 100) or (rsi_cur > 70)
        multi_extreme_long = sum([k_cur < 10, cci_cur < -100, rsi_cur < 30]) >= 2
        multi_extreme_short = sum([k_cur > 90, cci_cur > 100, rsi_cur > 70]) >= 2

        # HTF direção para checklist #4
        htf_agree_buy = htf_agree_sell = False
        if rev_m15 and len(rev_m15) >= 21:
            ema9_m15 = calc_ema_from_rev(rev_m15, 9)
            ema21_m15 = calc_ema_from_rev(rev_m15, 21)
            if ema9_m15[-1] and ema21_m15[-1]:
                if rev_m15[-1]["close"] > ema9_m15[-1] and ema9_m15[-1] > ema21_m15[-1]:
                    htf_agree_buy = True
                if rev_m15[-1]["close"] < ema9_m15[-1] and ema9_m15[-1] < ema21_m15[-1]:
                    htf_agree_sell = True

        # Checklist 6 (3.2 SOFT Gate: majority Yes e #2 + #6 pass)
        atr_ok = cur_atr_points <= atr_gate_m5
        no_news = True  # já passou HARD gate 2
        checklist = {
            "1 AT level": at_level,
            "2 ATR ok": atr_ok,
            "3 pattern/momentum": pattern_ok,
            "4 HTF agree": (htf_agree_buy or htf_agree_sell),
            "5 SL behind": sl_behind,
            "6 no news": no_news,
        }
        yes_count = sum(1 for v in checklist.values() if v)
        # Trade if majority Yes (>=4) and #2 and #6 pass
        checklist_pass = yes_count >= 4 and atr_ok and no_news

        # Scores para probabilística (structure first)
        # Buys se: AT low / hunt low + wick up + extreme long + momentum buy + HTF buy
        buy_score = 0
        sell_score = 0
        if at_level and sw_low is not None and abs(price - sw_low) < 1.0:
            buy_score += 2 if hunt else 1
        if at_level and sw_high is not None and abs(price - sw_high) < 1.0:
            sell_score += 2 if hunt else 1
        if wick_ok and c0["close"] > c0["open"]:
            buy_score += 1
        if wick_ok and c0["close"] < c0["open"]:
            sell_score += 1
        if eng_dir == 1:
            buy_score += 1
        if eng_dir == -1:
            sell_score += 1
        if momentum_buy:
            buy_score += 1
        if momentum_sell:
            sell_score += 1
        if multi_extreme_long:
            buy_score += 2
        elif k_cur < 10 or cci_cur < -100 or rsi_cur < 30:
            buy_score += 1
        if multi_extreme_short:
            sell_score += 2
        elif k_cur > 90 or cci_cur > 100 or rsi_cur > 70:
            sell_score += 1
        if htf_agree_buy:
            buy_score += 1
        if htf_agree_sell:
            sell_score += 1
        if consolidating:
            # em consolidação, não bonus
            pass

        # ADX contextual
        cur_adx = None
        try:
            adx_pack = calc_adx(rev, adx_period)
            cur_adx = adx_pack["adx"][-1] if adx_pack["adx"] and adx_pack["adx"][-1] is not None else None
        except Exception:
            pass

        session = _session_label_utc(now_utc)

        if show_log:
            logger.info(
                f"Huy5pts checklist {yes_count}/6 {checklist} | buy {buy_score} sell {sell_score} | "
                f"{at_reason} | {pattern_reason} | mom B{ momentum_buy} S{ momentum_sell} | "
                f"Stoch {k_cur:.1f} CCI {cci_cur:.0f} RSI {rsi_cur:.0f} multi L{multi_extreme_long} S{multi_extreme_short} | "
                f"ATR {cur_atr:.2f}({cur_atr_points:.0f}pts) {session} ADX {cur_adx:.0f if cur_adx else 'n/a'} | hunt {hunt}"
            )

        if not checklist_pass:
            return None

        direction = None
        if buy_score > sell_score and buy_score >= 3:
            direction = "BUY"
        elif sell_score > buy_score and sell_score >= 3:
            direction = "SELL"
        else:
            return None

        # ---- Gate: caça só se direção alinha com extreme/hunt ----
        # evita BUY sem extreme em resistência
        if direction == "BUY" and not (long_extreme or hunt or wick_ok):
            # mas se AT low com HTF buy, permitir
            if not (htf_agree_buy and at_level):
                return None
        if direction == "SELL" and not (short_extreme or hunt or wick_ok):
            if not (htf_agree_sell and at_level):
                return None

        # ---- Levels exatos (Huy 5 pts fixo) ----
        sl_dist = sl_points * psize
        tp1_dist = tp1_points * psize
        tp2_dist = tp2_points * psize

        entry = tick.ask if direction == "BUY" else tick.bid
        sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
        tp1 = entry + tp1_dist if direction == "BUY" else entry - tp1_dist
        tp2 = entry + tp2_dist if direction == "BUY" else entry - tp2_dist

        # SL atrás de estrutura? Ajuste fino: se swing está mais longe que 5pts, mantém 5 (Huy fixo).
        # Se hunt, garantir SL abaixo/ acima do wick + 0.1
        if hunt and direction == "BUY":
            wick_low = c0["low"]
            sl = min(sl, wick_low - 0.1)
        if hunt and direction == "SELL":
            wick_high = c0["high"]
            sl = max(sl, wick_high + 0.1)

        dec = 2 if "XAU" in self.symbol or "GOLD" in self.symbol else 5
        sl = round(sl, dec)
        tp1 = round(tp1, dec)
        tp2 = round(tp2, dec)

        # Heuristic probabilities (honestas, range, max 70%)
        # Se multi-extreme + hunt + momentum => TP2 ~45-55%, SL ~30%
        # Senão TP1 ~35-45%, SL ~40-50%
        if hunt and (multi_extreme_long if direction == "BUY" else multi_extreme_short):
            tp2_prob, tp1_prob, sl_prob = "45-55%", "35-45%", "25-35%"
            verdict = "WORTH TAKING"
        elif multi_extreme_long or multi_extreme_short:
            tp2_prob, tp1_prob, sl_prob = "35-45%", "40-50%", "35-45%"
            verdict = "WORTH TAKING"
        else:
            tp2_prob, tp1_prob, sl_prob = "25-35%", "35-45%", "40-50%"
            verdict = "MARGINAL" if yes_count >= 5 else "SKIP"
            if verdict == "SKIP":
                # deixar LLM decidir se vale? Mas Huy diz NO_TRADE se sem edge -> return None
                # manter SKIP como None para não poluir
                return None

        # Pre-signal checklist para LLM
        checklist_str = " | ".join(f"{k}:{'Yes' if v else 'No'}" for k, v in checklist.items())
        effective = f"Effective SL {effective_sl_points:.1f}pts (5+{spread_points:.1f}+1) TP1 net {effective_tp1:.1f} TP2 net {effective_tp2:.1f}"

        meta = (
            f"Huy5pts {direction} {checklist_str} | buy {buy_score} sell {sell_score} | "
            f"{at_reason} | {pattern_reason} {('HUNT '+hunt) if hunt else ''} | "
            f"mom B{ momentum_buy} S{ momentum_sell} | "
            f"Stoch {k_cur:.1f} CCI {cci_cur:.0f} RSI {rsi_cur:.0f} multi L{multi_extreme_long} S{multi_extreme_short} | "
            f"ATR {cur_atr:.2f}({cur_atr_points:.0f}),{session} ADX {cur_adx:.0f if cur_adx else 'n/a'} | "
            f"Prob SL {sl_prob} TP1 {tp1_prob} TP2 {tp2_prob} | {effective} → {verdict}"
        )

        vol = lot_size if lot_size > 0 else float(_get(cfg, "volume", 0.03))
        vol = max(min_lot, min(max_lot, vol))

        logger.info(
            f"Huy5pts sinal {direction} entry {entry:.2f} SL {sl:.2f}({sl_dist/psize:.0f}p) TP1 {tp1:.2f} TP2 {tp2:.2f} | {meta}"
        )

        sig = Signal(direction=direction, entry=entry, sl=sl, tp=tp1, meta=meta)
        try:
            sig.volume = vol  # type: ignore
            sig.tp2 = tp2  # type: ignore
            sig.verdict = verdict  # type: ignore
        except Exception:
            pass
        return sig
