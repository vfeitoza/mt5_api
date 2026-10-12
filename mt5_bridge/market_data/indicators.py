"""Indicadores puros — CCI / ATR / EMA / helpers p/ Champion.

Porta fórmulas do EA NEW_CHAMPION_2-40.mq5 para Python usando dados de bridge.get_rates.
Sem dependência de MT5 handles.

Rev: lista chronological (rev[0] oldest, rev[-1] newest / current forming).
rates do bridge: rates[0] newest → converter via list(reversed(rates)).
"""

from __future__ import annotations
from typing import List


def pip_multiplier(symbol: str) -> float:
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s:
        return 10.0
    if "JPY" in s:
        return 0.01
    if "NAS" in s or "US100" in s:
        return 1.0
    return 1.0


def pip_size(symbol: str) -> float:
    """Tamanho de 1 pip em unidades de preço."""
    # Para XAU: point 0.01, pip = 10*point = 0.1
    # Para forex 5 dígitos: 0.0001, JPY 0.01
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s:
        return 0.1
    if "JPY" in s:
        return 0.01
    if "NAS" in s or "US100" in s:
        return 1.0  # Nasdaq: 1 pip = 1 ponto
    return 0.0001


def calc_cci(rev: List[dict], period: int = 20) -> List[float | None]:
    """CCI = (TP - SMA_TP) / (0.015 * MD). Retorna lista alinhada com rev."""
    n = len(rev)
    out: List[float | None] = [None] * n
    if n < period:
        return out
    tps = [ (r["high"] + r["low"] + r["close"]) / 3.0 for r in rev ]
    for i in range(period - 1, n):
        window = tps[i - period + 1 : i + 1]
        sma = sum(window) / period
        md = sum(abs(v - sma) for v in window) / period
        if md == 0:
            out[i] = 0.0
        else:
            out[i] = (tps[i] - sma) / (0.015 * md)
    return out


def calc_atr(rev: List[dict], period: int = 14) -> List[float | None]:
    """ATR Wilder (iATR MT5). TR = max(H-L, |H-Cprev|, |L-Cprev|)."""
    n = len(rev)
    out: List[float | None] = [None] * n
    if n == 0:
        return out
    tr = [0.0] * n
    for i in range(n):
        h, l, c_prev = rev[i]["high"], rev[i]["low"], rev[i-1]["close"] if i > 0 else rev[i]["close"]
        tr[i] = max(h - l, abs(h - c_prev), abs(l - c_prev))
    if n < period:
        return out
    # first ATR = SMA of TR
    first = sum(tr[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def calc_ema(values: List[float], period: int) -> List[float | None]:
    """EMA clássico com SMA seed."""
    n = len(values)
    out: List[float | None] = [None] * n
    if n < period:
        return out
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    k = 2.0 / (period + 1)
    prev = sma
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def calc_ema_from_rev(rev: List[dict], period: int) -> List[float | None]:
    closes = [r["close"] for r in rev]
    return calc_ema(closes, period)


def get_higher_tf_trend(
    rev_h1: List[dict],
    period: int = 50,
) -> int:
    """Replica GetHigherTFTrend(): +1 alta, -1 baixa, 0 lateral.

    Usa rev_h1 chronological. Compara close da barra fechada (rev[-2])
    com EMA period em [1] e [2] (dois últimos valores fechados).
    Se close > ma1 > ma2 = +1; close < ma1 < ma2 = -1; else 0.
    """
    if len(rev_h1) < period + 3:
        return 0
    ema = calc_ema_from_rev(rev_h1, period)
    # última barra em rev é forming (do ponto de vista M5); usar penúltima como fechada
    # mas se H1 estiver fechado no momento, rev[-1] já é fechada — toleramos ambos
    # Usar índices relativos ao fim: -2 e -3 para garantir fechadas
    try:
        ma1 = ema[-2]
        ma2 = ema[-3]
        close = rev_h1[-2]["close"]
        if ma1 is None or ma2 is None:
            return 0
        if close > ma1 and ma1 > ma2:
            return 1
        if close < ma1 and ma1 < ma2:
            return -1
        return 0
    except Exception:
        return 0
