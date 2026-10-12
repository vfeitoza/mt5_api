"""SMC helpers — OrderBlocks / FVG / BOS / S/R / touches.

Todos recebem `rev` chronological (rev[0] oldest, rev[-1] newest/current).
Taxa de correspondência com EA NEW_CHAMPION_2-40: lógica 1:1 onde possível.
"""

from __future__ import annotations
from typing import List, Tuple
from .indicators import pip_size


def avg_candle_body(rev: List[dict], period: int = 20) -> float:
    if len(rev) == 0:
        return 0.0
    take = min(period, len(rev))
    seg = rev[-take:]
    return sum(abs(r["close"] - r["open"]) for r in seg) / max(1, take)


def detect_order_blocks(
    rev: List[dict],
    ob_lookback: int = 50,
    ob_min_size: float = 1.5,
) -> List[dict]:
    """Replica DetectOrderBlocks.

    Retorna lista de dict {low, high, isBullish, touches, time}
    """
    n = len(rev)
    if n < 30:
        return []
    avg = avg_candle_body(rev, 20)
    if avg == 0:
        return []
    out: List[dict] = []
    # varredura de 5..lookback a partir do fim (mais recente -> mais antigo)
    # EA loop i =5..lookback where i = shift
    # Para mapear para rev index, shift i corresponde a rev_idx = n-1 - i
    for shift in range(5, min(ob_lookback, n - 6)):
        idx = n - 1 - shift  # rev index do candle candidato
        if idx < 0 or idx >= n:
            continue
        r = rev[idx]
        body = abs(r["close"] - r["open"])
        if body <= avg * ob_min_size:
            continue
        is_bear = r["close"] < r["open"]
        is_bull = r["close"] > r["open"]
        if is_bear:
            # bullish OB: bear candle seguido de push bullish nos próximos 5 candles
            high_max = r["high"]
            for j in range(max(0, idx + 1), min(n, idx + 6)):
                if rev[j]["high"] > high_max:
                    high_max = rev[j]["high"]
            if high_max > r["high"] and (high_max - r["close"]) > body * 1.5:
                # dedup
                exists = any(abs(o["low"] - r["low"]) < 0.0001 * 100 for o in out if o["isBullish"])
                if not exists:
                    out.append({"time": r.get("time",""), "high": r["high"], "low": r["low"], "isBullish": True, "touches": 0})
        if is_bull:
            low_min = r["low"]
            for j in range(max(0, idx + 1), min(n, idx + 6)):
                if rev[j]["low"] < low_min:
                    low_min = rev[j]["low"]
            if low_min < r["low"] and (r["close"] - low_min) > body * 1.5:
                exists = any(abs(o["high"] - r["high"]) < 0.0001 * 100 for o in out if not o["isBullish"])
                if not exists:
                    out.append({"time": r.get("time",""), "high": r["high"], "low": r["low"], "isBullish": False, "touches": 0})
    return out


def detect_fvgs(
    rev: List[dict],
    fvg_lookback: int = 20,
    fvg_min_pips: float = 10.0,
    symbol: str = "XAUUSD",
) -> List[dict]:
    """Replica DetectFairValueGaps.

    FVG bullish: c_low > a_high no trio a,b,c consecutivos.
    Retorna [{gapLow, gapHigh, isBullish, isFilled:False, time}]
    """
    psize = pip_size(symbol)
    min_gap = fvg_min_pips * psize
    n = len(rev)
    if n < 3:
        return []
    out: List[dict] = []
    # Janela: últimos fvg_lookback trios anteriores ao atual
    start = max(0, n - 1 - fvg_lookback - 3)
    for k in range(start, n - 2):
        a, b, c = rev[k], rev[k + 1], rev[k + 2]
        # bullish: c.low > a.high and b bullish
        bull_gap = c["low"] - a["high"]
        if bull_gap >= min_gap and b["close"] > b["open"]:
            # dedup por gapLow prox
            if not any(abs(o["gapLow"] - a["high"]) < psize * 5 for o in out if o["isBullish"]):
                out.append({"time": b.get("time",""), "gapLow": a["high"], "gapHigh": c["low"], "isBullish": True, "isFilled": False})
        bear_gap = a["low"] - c["high"]
        if bear_gap >= min_gap and b["close"] < b["open"]:
            if not any(abs(o["gapHigh"] - a["low"]) < psize * 5 for o in out if not o["isBullish"]):
                out.append({"time": b.get("time",""), "gapLow": c["high"], "gapHigh": a["low"], "isBullish": False, "isFilled": False})
    return out


def count_touches(rev: List[dict], level: float, is_support: bool, threshold: float, lookback: int = 100) -> int:
    n = len(rev)
    look = min(lookback, n)
    seg = rev[-look:] if n >= look else rev
    cnt = 0
    for r in seg:
        if is_support:
            if abs(r["low"] - level) <= threshold:
                cnt += 1
        else:
            if abs(r["high"] - level) <= threshold:
                cnt += 1
    return cnt


def detect_sr(
    rev: List[dict],
    sr_lookback: int = 100,
    sr_min_touches: int = 2,
    sr_threshold_pips: float = 20.0,
    symbol: str = "XAUUSD",
) -> List[dict]:
    """Replica DetectSupportResistance (swing + touches)."""
    psize = pip_size(symbol)
    threshold = sr_threshold_pips * psize
    n = len(rev)
    if n < 10:
        return []
    out: List[dict] = []
    look = min(sr_lookback, n)
    # swing high/low com 3 barras de cada lado
    for i in range(3, look - 3):
        idx = n - 1 - i  # shift i from newest
        if idx < 3 or idx >= n - 3:
            continue
        high_i = rev[idx]["high"]
        low_i = rev[idx]["low"]
        # swing high
        is_high = True
        for j in range(1, 4):
            if rev[idx - j]["high"] >= high_i or rev[idx + j]["high"] >= high_i:
                is_high = False
                break
        if is_high:
            touches = count_touches(rev, high_i, False, threshold, sr_lookback)
            if touches >= sr_min_touches:
                out.append({"level": high_i, "touches": touches, "isSupport": False, "time": rev[idx].get("time","")})
        is_low = True
        for j in range(1, 4):
            if rev[idx - j]["low"] <= low_i or rev[idx + j]["low"] <= low_i:
                is_low = False
                break
        if is_low:
            touches = count_touches(rev, low_i, True, threshold, sr_lookback)
            if touches >= sr_min_touches:
                out.append({"level": low_i, "touches": touches, "isSupport": True, "time": rev[idx].get("time","")})
    return out


def detect_bos(rev: List[dict], bos_lookback: int = 30) -> int:
    """Replica DetectBOS: +1 alta, -1 baixa, 0 nenhum."""
    n = len(rev)
    if n < 10:
        return 0
    last_high = 0.0
    last_low = 0.0
    # varre de 4..lookback
    for shift in range(4, min(bos_lookback, n - 4)):
        idx = n - 1 - shift
        if idx < 3 or idx >= n - 3:
            continue
        if last_high != 0 and last_low != 0:
            break
        h = rev[idx]["high"]
        l = rev[idx]["low"]
        if last_high == 0:
            ok = True
            for j in range(1, 4):
                if idx - j < 0 or idx + j >= n:
                    ok = False
                    break
                if rev[idx - j]["high"] >= h or rev[idx + j]["high"] >= h:
                    ok = False
                    break
            if ok:
                last_high = h
        if last_low == 0:
            ok = True
            for j in range(1, 4):
                if idx - j < 0 or idx + j >= n:
                    ok = False
                    break
                if rev[idx - j]["low"] <= l or rev[idx + j]["low"] <= l:
                    ok = False
                    break
            if ok:
                last_low = l
    last_close = rev[-2]["close"] if n >= 2 else rev[-1]["close"]
    if last_high > 0 and last_close > last_high:
        return 1
    if last_low > 0 and last_close < last_low:
        return -1
    return 0
