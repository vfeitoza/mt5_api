# swing_fx — 102016 · FX Swing H1/H4

> `EURUSD, USDJPY, GBPUSD` (`AUDUSD, EURJPY, USDCHF` extras) — `H4 Supply/Demand + liquidity sweep + EMA 50/200 D1/H4 + RSI divergence + OB/FVG H1`. Hold `2-7 dias`.

## Quando usar / quando não usar

* **Use:** `H4` `S/D` `2+ touches` + `BOS H1` + `EMA 50/200 D1 & H4` alinhados + `RSI div` / `CCI`.
* **Não use:** `spread >2 pips`, `ADX <15 weak`, `news High ±60min`, **`payroll (NFP) = NO_TRADE dia todo`**.

## Pares e valores por par

`SL = ATR H1 ×1.8` clampeado:

| Par | Símbolo | `min SL` | `max SL` | `RR` | Notas |
|---|---|---|---|---|---|
| `EURUSD` | `EURUSD` | `45` pips | `90` | `1:2.8` | mais líquido |
| `USDJPY` | `USDJPY` | `50` | `90` | `1:2.5` | volatilidade JPY |
| `GBPUSD` | `GBPUSD` | `50` | `90` | `1:2.8` | |
| `AUDUSD` | `AUDUSD` | `40` | `85` | `1:2.5` | |
| `EURJPY` | `EURJPY` | `50` | `90` | `1:2.5` | cross |
| `USDCHF` | `USDCHF` | `40` | `85` | `1:2.5` | |

`symbols: ["EURUSD","USDJPY","GBPUSD"]` — varredura H4/H1/D1 por símbolo, melhor `score` vence.

## Lógica (score 0-10, min 7)

`H4+D1 trend +2` + `S/D 1-2` + `BOS H1 +2` + `RSI div +2` + `CCI/ADX/VWAP +1` cada, penal contra `H4`.

## Gestão (SL/TP/BE/trailing)

`SL 45-90p`, `TP RR 2.5-2.8`, `BE 1×+5p`, `trailing ATR 1.5×` após `1.5×`, `step 5`. `max 4/dia` (swing baixa frequência).

## Exemplo de sinal

```
SwingFX EURUSD B8/10 H4 1 D1 1 BOS 1 RSI 42 CCI -85 ADX 22 | S/D sup 3 bearDiv | ATR 38p SL 55p RR 1:2.8
```

## Como ativar no dashboard / config

`strategies.swing_fx.enabled: true` `H1 102016 0.03` `use_llm: true`. Dashboard `5556` → `GET /api/readme?name=swing_fx`.

## Checklist de teste

* `H4 80 + H1 120 + D1 80` dados.
* `payroll` bloqueia `NFP`.
* `EMA 50/200 D1` filter evita contra-tendência semanal.
* `news_before 60` bloqueia `60min` antes/depois.

## Pesquisa base

`MQL5 Adaptive Swings 149836 EURUSD`, `StrategyQuant stream pullback EURUSD M30`, `H4 S/D r/Forex + EMA 50/200 VectorVest`.
