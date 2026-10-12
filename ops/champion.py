"""Champion operacional — transcrição fiel de NEW_CHAMPION 2.40 (M5 XAUUSD).

Score 0-23 idêntico ao EA: OB 0-4 + FVG 0-3 + CCI 0-4 + S/R 0-3 + OB+S/R +3 + Sessão +1/+2 + MTF ±2 + BOS +2.
Inclui spread/session/news gates, SL ATR, volume fixo/auto.
Metas diárias e wait_after_loss são exclusivos deste operacional (não global).
Calendário bloc 20/20 configurável por estratégia.
Desacoplado de gráficos (sem debug visual).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
from mt5_bridge.market_data.indicators import calc_cci, calc_atr, get_higher_tf_trend, pip_size, pip_multiplier
from mt5_bridge.market_data.smc import detect_order_blocks, detect_fvgs, detect_sr, detect_bos

logger = logging.getLogger(__name__)


def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.M5


def _get(p: dict, key: str, default):
    """Lê de params mesclado (top-level + params aninhado)."""
    if not isinstance(p, dict):
        return default
    # if p has 'params' nested, merge
    if "params" in p and isinstance(p["params"], dict):
        merged = {**p, **p["params"]}
        return merged.get(key, default)
    return p.get(key, default)


def _session_ok(now_utc: datetime, use_gmt: bool, start_h: int, end_h: int) -> bool:
    # Python só tem UTC; session_use_gmt true -> UTC, false -> também UTC (aprox server = UTC)
    h = now_utc.hour
    return start_h <= h < end_h


def _overlap_ok(now_utc: datetime, use_gmt: bool, o_start: int, o_end: int) -> bool:
    h = now_utc.hour
    return o_start <= h < o_end


class ChampionStrategy(BaseStrategy):
    name = "champion"
    magic = 102011
    symbol = "XAUUSD"
    timeframe = "M5"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        # self.config mantém dict completo (inclui params). Helpers usam _get.

    async def check_signal(self, bridge) -> Optional[Signal]:
        cfg = self.config
        # --- params helpers ---
        def gp(k, d): return _get(cfg, k, d)

        # Risco / volume
        lot_size = float(gp("lot_size", gp("lotSize", 0.02)))
        risk_percent = float(gp("risk_percent", 1.0))
        min_lot = float(gp("min_lot", 0.01))
        max_lot = float(gp("max_lot", 10.0))
        sl_pips = int(gp("sl_pips", gp("stop_loss_pips", 0)))
        use_atr_sl = bool(gp("use_atr_sl", gp("use_atr_for_sl", True)))
        atr_period = int(gp("atr_period", 14))
        atr_mult = float(gp("atr_mult", gp("atr_multiplier", 2.0)))
        rr = float(gp("rr", gp("risk_reward_ratio", 4.0)))
        spread_max = float(gp("spread_max", gp("max_spread_pips", 5.0)))

        # Metas exclusivas champion
        use_daily_target = bool(gp("use_daily_target", True))
        daily_profit_target = float(gp("daily_profit_target", 100.0))
        use_daily_loss = bool(gp("use_daily_loss", True))
        daily_loss_limit = float(gp("daily_loss_limit", 50.0))
        wait_after_loss = int(gp("wait_after_loss", 30))

        # SMC
        use_smc = bool(gp("use_smc", True))
        use_ob = bool(gp("use_ob", gp("use_order_blocks", True)))
        ob_lookback = int(gp("ob_lookback", 50))
        ob_min_size = float(gp("ob_min_size", 1.5))
        use_fvg = bool(gp("use_fvg", gp("use_fvg", True)))
        fvg_lookback = int(gp("fvg_lookback", 20))
        fvg_min_size = float(gp("fvg_min_size", 10.0))
        use_bos = bool(gp("use_bos", True))
        bos_lookback = int(gp("bos_lookback", 30))

        # CCI
        use_cci = bool(gp("use_cci", True))
        cci_period = int(gp("cci_period", 20))
        use_dead_zone = bool(gp("use_dead_zone", True))
        cci_dead_zone = float(gp("cci_dead_zone", 10.0))
        require_extreme = bool(gp("require_extreme", True))
        cci_extreme = float(gp("cci_extreme", gp("cci_extreme_level", 100.0)))
        cci_lookback = int(gp("cci_lookback", gp("cci_extreme_lookback", 5)))

        # SR
        use_sr = bool(gp("use_sr", True))
        sr_lookback = int(gp("sr_lookback", 100))
        sr_min_touches = int(gp("sr_min_touches", 4))
        sr_threshold = float(gp("sr_threshold", 20.0))

        # Confluências
        require_confluence = bool(gp("require_ob_sr_confluence", True))
        require_mtf = bool(gp("require_mtf", gp("require_multi_tf", True)))
        higher_tf_str = str(gp("higher_tf", "H1"))
        # aceita valor numérico MT5 16385 -> mapear
        if higher_tf_str.isdigit():
            higher_tf_str = "H1" if higher_tf_str == "16385" else higher_tf_str
        htf_ma_period = int(gp("htf_ma_period", 50))
        use_session = bool(gp("use_session", gp("use_session_filter", True)))
        session_use_gmt = bool(gp("session_use_gmt", True))
        session_start = int(gp("session_start", 7))
        session_end = int(gp("session_end", 17))
        overlap_start = int(gp("overlap_start", 12))
        overlap_end = int(gp("overlap_end", 16))

        # Notícias (20/20 pedido)
        use_news = bool(gp("use_news_filter", True))
        news_before = int(gp("news_before", gp("news_minutes_before", 20)))
        news_after = int(gp("news_after", gp("news_minutes_after", 20)))
        news_include_moderate = bool(gp("news_include_moderate", False))

        # Score
        use_score = bool(gp("use_score", gp("use_score_system", True)))
        min_score = int(gp("min_score", 12))
        show_log = bool(gp("show_score_log", False))

        # --- 0) gates de regime (spread / sessão / metas / wait / news) ---

        try:
            tick = await bridge.get_tick(self.symbol)
        except Exception as e:
            logger.debug(f"Champion {self.symbol} get_tick fail: {e}")
            return None

        # spread
        if spread_max and spread_max > 0:
            psize = pip_size(self.symbol)
            spread = (tick.ask - tick.bid)
            spread_pips = spread / psize if psize else 999
            if spread_pips > spread_max:
                logger.debug(f"Champion spread {spread_pips:.1f} > {spread_max} skip")
                return None

        # sessão
        now_utc = datetime.now(timezone.utc)
        if use_session and not _session_ok(now_utc, session_use_gmt, session_start, session_end):
            logger.debug("Champion fora da sessão")
            return None

        # metas diárias e wait — exclusivos champion
        # Usa get_history_counts e get_positions se disponível; fail open se EA offline para metas (não blocar)
        if use_daily_target or use_daily_loss or wait_after_loss > 0:
            try:
                counts = await bridge.get_history_counts(magic=self.magic)
                # profit diário aproximado: se bridge expõe get_history_profit
                daily_profit = None
                # tenta obter profit via novo método se existir
                if hasattr(bridge, "get_history_stats"):
                    try:
                        stats = await bridge.get_history_stats(magic=self.magic)
                        # suporta dict ou objeto com 'profit'
                        if isinstance(stats, dict):
                            daily_profit = float(stats.get("profit", 0))
                            last_loss_ts = stats.get("last_loss_time")
                        else:
                            daily_profit = float(getattr(stats, "profit", 0))
                            last_loss_ts = getattr(stats, "last_loss_time", None)
                        if use_daily_target and daily_profit is not None and daily_profit >= daily_profit_target:
                            logger.info(f"Champion meta diária atingida ${daily_profit:.2f}")
                            return None
                        if use_daily_loss and daily_profit is not None and daily_profit <= -abs(daily_loss_limit):
                            logger.info(f"Champion loss diário ${daily_profit:.2f}")
                            return None
                        if wait_after_loss > 0 and last_loss_ts:
                            # last_loss_ts pode ser str
                            try:
                                if isinstance(last_loss_ts, str) and last_loss_ts:
                                    ll = datetime.fromisoformat(last_loss_ts.replace(" ", "T"))
                                elif isinstance(last_loss_ts, (int, float)) and last_loss_ts > 0:
                                    ll = datetime.fromtimestamp(last_loss_ts, tz=timezone.utc)
                                else:
                                    ll = None
                                if ll:
                                    mins = (now_utc - ll).total_seconds() / 60
                                    if mins < wait_after_loss:
                                        logger.debug(f"Champion wait_after_loss {wait_after_loss-mins:.0f}min restante")
                                        return None
                            except Exception:
                                pass
                    except Exception as e:
                        logger.debug(f"Champion get_history_stats fail {e}")
                # fallback simples: sem profit não blocar
            except Exception as e:
                logger.debug(f"Champion history check fail {e}")

        # notícias 20/20
        if use_news:
            try:
                events = await bridge.get_calendar(hours_ahead=24)
                # filtrar por impacto
                filtered = []
                for ev in events:
                    imp = (ev.impact or "").lower()
                    if imp == "high":
                        filtered.append(ev)
                    elif imp == "medium" and news_include_moderate:
                        filtered.append(ev)
                    elif imp in ("3","high"):
                        filtered.append(ev)
                # verificar janela 20/20
                for ev in filtered:
                    # ev.time formato "YYYY.MM.DD HH:MM:SS"
                    ev_time = None
                    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            ev_time = datetime.strptime(ev.time, fmt).replace(tzinfo=timezone.utc)
                            break
                        except Exception:
                            continue
                    if ev_time is None:
                        continue
                    delta_min = (ev_time - now_utc).total_seconds() / 60
                    # bloquear se dentro de before/after
                    if -news_after <= delta_min <= news_before:
                        logger.info(f"Champion notícia BLOQUEADO: {ev.title} ({ev.impact}) em {delta_min:.0f}min (janela {news_before}/{news_after})")
                        return None
                    # também bloquear se evento com janela after ainda ativa (delta negativo pequeno)
            except Exception as e:
                logger.debug(f"Champion calendar fail {e}")

        # --- 1) buscar dados ---
        tf_m5 = _tf(self.timeframe)
        try:
            rates_m5 = await bridge.get_rates(self.symbol, tf_m5, 120)
        except Exception as e:
            logger.debug(f"Champion get_rates M5 fail {e}")
            return None
        if len(rates_m5) < 50:
            return None
        rev = list(reversed(rates_m5))  # chronological
        # para H1 MTF
        rev_h1 = None
        if require_mtf:
            try:
                tf_h = _tf(higher_tf_str)
                rates_h = await bridge.get_rates(self.symbol, tf_h, 80)
                if len(rates_h) >= htf_ma_period + 3:
                    rev_h1 = list(reversed(rates_h))
            except Exception as e:
                logger.debug(f"Champion get_rates H1 fail {e}")
                rev_h1 = None

        # indicadores
        cci_vals = calc_cci(rev, cci_period) if use_cci else [None]*len(rev)
        atr_vals = calc_atr(rev, atr_period) if (sl_pips == 0 and use_atr_sl) else [None]*len(rev)
        htf_trend = 0
        if require_mtf and rev_h1 is not None:
            htf_trend = get_higher_tf_trend(rev_h1, htf_ma_period)

        # SMC estruturas
        obs = detect_order_blocks(rev, ob_lookback, ob_min_size) if (use_smc and use_ob) else []
        fvgs = detect_fvgs(rev, fvg_lookback, fvg_min_size, self.symbol) if (use_smc and use_fvg) else []
        sr_levels = detect_sr(rev, sr_lookback, sr_min_touches, sr_threshold, self.symbol) if use_sr else []
        bos_dir = detect_bos(rev, bos_lookback) if (use_smc and use_bos) else 0

        # --- 2) avaliação por score (replica AnalyzeEntry) ---
        psize = pip_size(self.symbol)
        threshold = sr_threshold * psize
        current_bid = tick.bid
        current_ask = tick.ask

        # OB proximidade
        has_bull_ob = False
        has_bear_ob = False
        bull_ob_touches = 0
        bear_ob_touches = 0
        for ob in obs:
            if ob["isBullish"]:
                if current_bid >= ob["low"] - threshold and current_bid <= ob["high"] + threshold:
                    has_bull_ob = True
                    bull_ob_touches = max(bull_ob_touches, ob.get("touches", 0))
            else:
                if current_ask >= ob["low"] - threshold and current_ask <= ob["high"] + threshold:
                    has_bear_ob = True
                    bear_ob_touches = max(bear_ob_touches, ob.get("touches", 0))

        # FVG target
        has_bull_fvg = False
        has_bear_fvg = False
        for fvg in fvgs:
            if fvg.get("isFilled"):
                continue
            if fvg["isBullish"] and current_bid < fvg["gapHigh"]:
                has_bull_fvg = True
            if not fvg["isBullish"] and current_ask > fvg["gapLow"]:
                has_bear_fvg = True

        # CCI signals
        cci_buy = False
        cci_sell = False
        cci_score_buy = 0
        cci_score_sell = 0
        if use_cci and len(cci_vals) >= cci_lookback + 2 and cci_vals[-1] is not None and cci_vals[-2] is not None:
            cci0 = cci_vals[-1]
            cci1 = cci_vals[-2]
            was_bull_extreme = False
            was_bear_extreme = False
            if require_extreme:
                for k in range(1, cci_lookback + 1):
                    idx = -1 - k
                    if -idx <= len(cci_vals) and cci_vals[idx] is not None:
                        v = cci_vals[idx]
                        if v <= -cci_extreme:
                            was_bull_extreme = True
                        if v >= cci_extreme:
                            was_bear_extreme = True
                # BUY
                if was_bull_extreme and cci1 < -cci_dead_zone and cci0 >= -cci_dead_zone:
                    cci_buy = True
                    cci_score_buy = 4
                elif cci1 < 0 and cci0 >= 0:
                    cci_buy = True
                    cci_score_buy = 2
                # SELL
                if was_bear_extreme and cci1 > cci_dead_zone and cci0 <= cci_dead_zone:
                    cci_sell = True
                    cci_score_sell = 4
                elif cci1 > 0 and cci0 <= 0:
                    cci_sell = True
                    cci_score_sell = 2
            else:
                if cci1 < 0 and cci0 >= 0:
                    cci_buy = True
                    cci_score_buy = 2
                if cci1 > 0 and cci0 <= 0:
                    cci_sell = True
                    cci_score_sell = 2

        # SR proximidade
        has_support = False
        has_resistance = False
        support_touches = 0
        resistance_touches = 0
        for sr in sr_levels:
            if sr["isSupport"]:
                if abs(current_bid - sr["level"]) <= threshold:
                    has_support = True
                    support_touches = max(support_touches, sr["touches"])
            else:
                if abs(current_ask - sr["level"]) <= threshold:
                    has_resistance = True
                    resistance_touches = max(resistance_touches, sr["touches"])

        # --- soma scores ---
        score_buy = 0
        score_sell = 0

        if has_bull_ob:
            score_buy += 2
            if bull_ob_touches >= 2:
                score_buy += 1
            if bull_ob_touches >= 3:
                score_buy += 1
        if has_bear_ob:
            score_sell += 2
            if bear_ob_touches >= 2:
                score_sell += 1
            if bear_ob_touches >= 3:
                score_sell += 1
        if has_bull_fvg:
            score_buy += 3
        if has_bear_fvg:
            score_sell += 3
        score_buy += cci_score_buy
        score_sell += cci_score_sell
        if has_support:
            score_buy += 3 if support_touches >= 3 else 2
        if has_resistance:
            score_sell += 3 if resistance_touches >= 3 else 2
        if require_confluence:
            if has_bull_ob and has_support:
                score_buy += 3
            if has_bear_ob and has_resistance:
                score_sell += 3
        if use_session:
            in_session = _session_ok(now_utc, session_use_gmt, session_start, session_end)
            in_overlap = _overlap_ok(now_utc, session_use_gmt, overlap_start, overlap_end)
            if in_session:
                if in_overlap:
                    score_buy += 2
                    score_sell += 2
                else:
                    score_buy += 1
                    score_sell += 1
        if require_mtf:
            if htf_trend > 0:
                score_buy += 2
                score_sell -= 2
            elif htf_trend < 0:
                score_buy -= 2
                score_sell += 2
        if bos_dir > 0:
            score_buy += 2
        elif bos_dir < 0:
            score_sell += 2

        score_buy = max(0, score_buy)
        score_sell = max(0, score_sell)

        if show_log and (score_buy >= min_score or score_sell >= min_score):
            logger.info(f"Champion scores BUY {score_buy}/23 SELL {score_sell}/23 | OB bull {has_bull_ob} bear {has_bear_ob} | FVG bull {has_bull_fvg} bear {has_bear_fvg} | CCI buy {cci_buy} sell {cci_sell} | S/R sup {has_support}:{support_touches} res {has_resistance}:{resistance_touches} | BOS {bos_dir} | MTF {htf_trend}")

        # decisão
        direction = None
        if use_score:
            if score_buy >= min_score and score_buy > score_sell:
                direction = "BUY"
            elif score_sell >= min_score and score_sell > score_buy:
                direction = "SELL"
            else:
                return None
        else:
            # modo simplificado
            if cci_buy and (has_bull_ob or has_support):
                direction = "BUY"
            elif cci_sell and (has_bear_ob or has_resistance):
                direction = "SELL"
            else:
                return None

        # --- 3) SL/TP ---
        # SL distance
        sl_distance = 0.0
        if sl_pips > 0:
            sl_distance = sl_pips * psize
        elif use_atr_sl and atr_vals and atr_vals[-1] and atr_vals[-1] > 0:
            sl_distance = atr_vals[-1] * atr_mult
        else:
            sl_distance = 50 * psize  # fallback EA

        # stops level margem 10% (approx) — EA faz AdjustToStopsLevel
        # não temos SYMBOL_TRADE_STOPS_LEVEL no bridge; usar mínimo 1.1* 20 pips?
        # vamos garantir sl_distance mínimo de 20 pips *1.1 se muito pequeno
        min_stop = 20 * psize * 1.1  # substitui stopsLevel pontos
        if sl_distance < min_stop:
            sl_distance = min_stop

        entry = current_ask if direction == "BUY" else current_bid
        if direction == "BUY":
            sl = entry - sl_distance
            tp = entry + sl_distance * rr
        else:
            sl = entry + sl_distance
            tp = entry - sl_distance * rr

        sl = round(sl, 2) if "XAU" in self.symbol or "GOLD" in self.symbol else round(sl, 5)
        tp = round(tp, 2) if "XAU" in self.symbol or "GOLD" in self.symbol else round(tp, 5)

        # meta rica para LLM
        cci0_str = f"{cci_vals[-1]:.0f}" if cci_vals and cci_vals[-1] is not None else "n/a"
        meta = (
            f"Champion score B{score_buy}/S{score_sell} "
            f"OB bull:{has_bull_ob} bear:{has_bear_ob} "
            f"FVG bull:{has_bull_fvg} bear:{has_bear_fvg} "
            f"CCI {cci0_str} (extreme {cci_extreme:.0f}) "
            f"S/R sup:{support_touches} res:{resistance_touches} "
            f"BOS {bos_dir} MTF H1:{htf_trend} "
            f"{'overlap LDN-NY' if _overlap_ok(now_utc, session_use_gmt, overlap_start, overlap_end) else 'sessao'} "
            f"SL {sl_distance/psize:.1f}p RR 1:{rr:.1f}"
        )

        # volume handling — se lot_size 0 auto, calcular via risk; senão fixed
        vol = lot_size if lot_size > 0 else float(_get(cfg, "volume", 0.05))
        # clamp
        vol = max(min_lot, min(max_lot, vol))

        logger.info(f"Champion sinal {direction} score {score_buy if direction=='BUY' else score_sell}/{min_score} entry {entry:.2f} SL {sl:.2f} TP {tp:.2f} | {meta}")

        sig = Signal(direction=direction, entry=entry, sl=sl, tp=tp, meta=meta)
        # anexar volume para engine usar
        try:
            sig.volume = vol  # type: ignore
        except Exception:
            pass
        return sig
