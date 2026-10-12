# Champion — 102011 · XAUUSD M5

> Transcrição fiel `NEW_CHAMPION 2.40` (`xauusd_102001_m5.set`) — swing/SCM com score 0-23. Swing trading intraday com proteção ATR e LLM macro.

## Quando usar / quando não usar

* **Use:** tendência H1 clara, sobreposição `12-16 GMT`, `score ≥12`.
* **Não use:** `spread >5 pips`, fora `07-17 GMT`, news High `±20min`, `wait 30min pós-loss`.

## Lógica (score 0-23)

`OB 0-4` + `FVG 0-3` + `CCI 0-4` (`extreme 100→4`) + `S/R 0-3` (`4 touches`) + `conflu OB+S/R +3` + `sessão +1/+2` + `MTF H1 EMA50 ±2` + `BOS +2`. `min_score 12`. Gate `1 posição por magic+symbol`.

## Parâmetros chave

| Param | Default | O que faz |
|---|---|---|
| `lot_size` | `0.02` | `0.01` scalp  |
| `rr` | `3.0` | `SL×RR` TP |
| `sl_pips` | `0` → `ATR 14×2` | clamp `2.2` |
| `spread_max` | `5` | pips XAU |
| `news` | `20/20` | High only |
| `min_score` | `12` | /23 |

## Gestão (SL/TP/BE/trailing)

`SL ATR×2 (22 pips)`, `TP RR 3`, `BE 0.8×+5p`, `trailing ATR 1.5×` após `1×`, `partial 50% @0.8R`, gate `daily $100/-$50` + `wait 30m`.

## Exemplo de sinal

```
Champion SELL 15/12 entry 4168.74 SL 4175.49 TP 4141.72 | BOS high, CCI 76, MTF H1:-1, overlap
```

## Como ativar no dashboard / config

`strategies.champion.enabled: true` — `magic 102011 M5 0.02`. Dashboard `5556` clique no EA → README antes da config. `use_llm: true` (Servtec `gemini-3.8-flash-high`).

## Checklist de teste

* `python bot.py --dry-run` → `champion:XAUUSD#102011` + `show_score_log`.
* Mock `AsyncMock` CCI extreme → score `15/23`.
* `GET /api/readme?name=champion` no dashboard.
