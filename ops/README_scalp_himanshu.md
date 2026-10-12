# Scalp-Himanshu2025 — 102013 · XAUUSD M1

> Himanshu2025 9-source `9/9 ±5` — `PriceAction, SMC, S/R, EMA 9/21/50/200, RSI div +2, MACD/Stoch, Volume, MTF 5/15/1H, DXY, ATR/session`.

## Quando usar / quando não usar

* **Use:** `RSI divergence +2` / `liquidity sweep +2` / `Asian BO +1`, `DXY -2 penal`, `overlap +1`, `min 5`.
* **Não use:** `AD X <20 chop` (hard), `DXY` contra, fora overlap em vol baixa.

## Lógica (score ±9, bônus/penal)

9 fontes `+1/-1`, `RSI div +2`, `sweep +2`, `Asian +1`, `DXY align -2`, `overlap 08-12 ET +1`. `±5/9` gatilho.

## Parâmetros chave

| Param | Default | O que faz |
|---|---|---|
| `min_score` | `5` | /9 |
| `adx_chop` | `20` | ADX |
| `sl_min/max` | `1.00/3.00 USD` | `10-30p` XAU |
| `tp_ratios` | `[1,2,3]` | `TP1/2/3` |
| `time_exit` | `15m` | atraso |
| `spread_max_usd` | `0.30` | |

## Gestão (SL/TP/BE/trailing)

`SL estrutura` clamp `$1-3`, `TP1/2/3 1:1/1:2/1:3` `33%` parciais, `BE 1×+3p`, `trailing ATR 1.5×` após `2×`, `time exit 15m`.

## Exemplo de sinal

```
Himanshu 9-source total 5/9 S5 +2 RSI div ... DXY -2
```

## Como ativar no dashboard / config

`scal p_himanshu 102013 M1 0.01` — `use_llm: false` (opt-in Servtec). Modal README.

## Checklist de teste

* `calc_rsi/macd/stoch/adx` em `rev` `M1`.
* `score 5` dispara `BUY` (divergence).
* `GET /api/readme?name=scalp_himanshu`.
