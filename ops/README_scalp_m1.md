# Scalp M1 — 102012 · XAUUSD M1

> Micro-pullbacks `M1` com confirmação `M5 EMA20`. Hold `5-15min`.

## Quando usar / quando não usar

* **Use:** `EMA 9>21 M1` + `CCI ≤-80 / ≥80`, `M5` alinha, `score ≥6`.
* **Não use:** `spread >6 pips`, fora `07-17 GMT`, news `±15`, `score <6`.

## Lógica (score 0-10, min 6)

`EMA M1 +2` / `M5 +1` + `CCI +2` + `FVG +1` + `BOS +1` + `S/R +1/+2` − `contra M5` penalidade `1`.

## Parâmetros chave

| Param | Default | O que faz |
|---|---|---|
| `sl_atr_mult` | `1.2` | `ATR 14` |
| `min/max atr` | `8/25` pips | clamp SL |
| `rr` | `1.5` | TP |
| `spread_max` | `6` | pips |
| `min_score` | `6` | /10 |

## Gestão (SL/TP/BE/trailing)

`SL ATR×1.2` (`8-25p`), `BE 0.8×+3p`, `trailing ATR 1.0×` após `1×`, `step 3`.

## Exemplo de sinal

```
Scalp M1 SELL 5/10 EMA M1:-1 M5:-1 CCI -20 S/R res:29
```

## Como ativar no dashboard / config

`strategies.scalp_m1.enabled: true` `M1 102012 0.01`. Modal mostra README antes da config. `GET /api/readme?name=scalp_m1`.

## Checklist de teste

* `AsyncMock` `rev` `M1` → `check_signal` `>6` dispara.
* `GET /api/readme?name=scalp_m1` + dashboard modal.
* `show_score_log: true`.
