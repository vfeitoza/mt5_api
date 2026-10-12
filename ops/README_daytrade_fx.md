# daytrade_fx — 102015 · FX Daytrade M15

> `EURUSD, USDJPY, GBPUSD` (`AUDUSD, EURJPY` extras) — `London breakout 00-07 UTC + ICT Killzone 07-10 / 13-16 UTC` + `EMA 50/200 H1` + `ADX/ATR`.

## Quando usar / quando não usar

* **Use:** `M15` breakout de `Asian range 00-07 UTC` com `AD X>18`, `EMA 50/200 H1` alinhado, em `London 07-10 UTC` ou `NY AM 13-16 UTC`.
* **Não use:** `spread >1.5 pips`, `ATR` contrário, fora killzone (volume baixo), `news High ±20min`, **`payroll (NFP) = NO_TRADE dia todo`**.

## Pares e valores por par

`SL = ATR 14 M15 ×1.5` clampeado:

| Par | Símbolo | `min SL` | `max SL` | `RR` | `spread_max` |
|---|---|---|---|---|---|
| `EURUSD` | `EURUSD` | `18` pips | `40` | `1:2.0` | `1.5` |
| `USDJPY` | `USDJPY` | `22` | `45` | `1:2.0` | `0.03 JPY` |
| `GBPUSD` | `GBPUSD` | `22` | `45` | `1:2.0` | `1.5` |
| `AUDUSD` | `AUDUSD` | `18` | `38` | `1:2.0` | `1.5` |
| `EURJPY` | `EURJPY` | `22` | `45` | `1:2.0` | |

`symbols: ["EURUSD","USDJPY","GBPUSD"]` — varre por ciclo e escolhe o melhor `score` (evita dispersar). Decimais: `JPY 3`, demais `5`.

## Lógica (score 0-10, min 6)

`Breakout Asian +3` + `Killzone +1` + `H1 EMA 50/200 +2` + `RSI` / `round 50 pips` / `volume +1` cada, penal `contra H1 −1`. `Hold 2-8h`.

## Gestão (SL/TP/BE/trailing)

`BE 0.8× +3p`, `trailing ATR 1.0×` após `1×`, `step 3`. `PositionManager` por `magic 102015`.

## Exemplo de sinal

```
DaytradeFX EURUSD B7/10 H1 1 RSI 58 ADX 22 | break Asian H 1.08530 | London Killzone | round 50pips vol +1 | ATR 28p SL 30p RR 1:2.0
```

## Como ativar no dashboard / config

`strategies.daytrade_fx.enabled: true` `M15 102015 0.03` `use_llm: true` (Servtec `gemini-3.8-flash-high` valida macro USD/EUR). Dashboard `5556` clique no EA → README antes da config. `GET /api/readme?name=daytrade_fx`.

## Checklist de teste

* `Bridge` `M15 120 + H1 80 + D1 60`.
* `ADX 18` gate bloqueia chop.
* `payroll` title `NFP/payroll` → dia skip (log `payroll day — NO_TRADE`).
* `get_rates EURUSD/USDJPY/GBPUSD` todos retornam.

## Pesquisa base

`London Breakout QuantifiedStrategies 2003-2026`, `ICT Killzone Backtrex/LiteFinance`, `r/RealDayTrading EURUSD 2024 1 trade após open`.
