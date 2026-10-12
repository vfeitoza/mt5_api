# scalping_Huy_5pts — 102014 · XAUUSD M5

> Adaptação `huynguyen03dev/xauusd-trading` `structure first` — `5 pontos (0.5 USD, 5×0.1) SL fixo`, `TP1 5 (1:1) TP2 10 (1:2)`.

## Quando usar / quando não usar

* **Use:** `AT level` (swing/round `$10/5`) + `rejection wick`/`engulfing` + `hunt Stoch<10/>90` + `M15 agree`.
* **Não use:** `ATR 5m>8 / 15m>20 hard`, `news ±15 hard`, `rollover 21-22 UTC hard`, `mid-range`, `consolidating range<1.0+MACD ±0.1+CCI ±30`.

## Lógica (checklist 6 + probabilístico max 70%)

`AT level + pattern + SL atrás + momentum 3-5 candles + HTF agree` (`≥4 Yes` + Gates 1/2 pass). Extremos `Stoch<10/CCI<-100/RSI<30`, multi-extreme `≥2` aumenta bounce. `Hunt 3-5pts V-recovery + Stoch extreme = high prob`.

## Parâmetros chave

| Param | Default | O que faz |
|---|---|---|
| `sl/tp1/tp2` | `5/5/10` pts | `0.5 USD` |
| `atr_gate_m5/m15` | `8/20` pts | hard |
| `spread_warn` | `0.35 USD` | soft, expõe net |
| `use_llm` | `true` | sempre Servtec |
| `show_score_log` | `true` | |

## Gestão (SL/TP/BE/trailing)

`SL 5 fixo` (widen `8` em hunt), `BE +2.5-3`, `trail +2 após +5`, `2 losses → stop sessão` (`PL $`, nosso `max_trades 10`), `15m time exit`, `CHoCH exit`.

## Exemplo de sinal

```
Huy5pts BUY checklist 5/6 | Stoch 7.2 CCI -92 multi L true | ATR 6.2 → Prob SL 25-35% TP1 45-55% WORTH TAKING
```

## Como ativar no dashboard / config

`scalping_huy_5pts 102014 M5 0.03 LLM sempre`. Modal exibe README antes da config. `GET /api/readme?name=scalping_huy_5pts`.

## Checklist de teste

* `Bridge` `M5 50 + M15 30` `ATR` gates.
* `rejection wick + AT level` dispara.
* `roadmap`/dashboard `SPP 0.5 USD SL`.
