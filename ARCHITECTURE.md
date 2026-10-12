# Arquitetura — MT5 Multi-Operacional (XAUUSD Scalper + SMC)

> Python é o cérebro. MT5/EA é executor burro via socket Winsock. LLM é gate, não sinal.

## 1. Visão Geral

```
┌─────────────┐  TCP 1111 JSON \n  ┌──────────────────┐  OpenAI-compat  ┌─────────────────┐
│ MT5 Terminal│◄────────────────────►│  Python BOT      │─────────────────►│ models.servtec    │
│ Wine/Docker │  MT5SocketClient.mq5 │  core/engine.py  │  /v1/chat/compl  │ .net.br/v1        │
│  XAUUSD     │  ws2_32.dll          │  ops/*           │                  │ gemini/deepseek/  │
└─────────────┘                      └──────────────────┘                  │ glm/gpt-luna      │
        ▲                                     │                             └─────────────────┘
        │ CalendarValueHistory                │ Tradays fallback
        │ HistoryDealGet                      │ (se EA offline)
        └─────────────────────────────────────┘
```

**Decisão Linux:** `huy-bui-tech/mt5_api` usa `ws2_32.dll` + `socket()` nativo Winsock. Funciona sob Wine sem `libzmq.dll`. Mantido. `dingmaotu/mql-zmq` evitado = sem dor 32/64bit DLL.

## 2. Multi-Operacional — como funciona

### 2.1 Plugin `ops/`

Cada operacional é uma classe que herda `BaseStrategy`:

```python
class BaseStrategy:
    name: str          # "smc_xau_m5"
    magic: int         # 20261001 — isola deals no MT5
    symbol: str        # "XAUUSD"
    timeframe: str     # "M5" principal
    config: dict       # params do YAML

    async def check_signal(self, bridge) -> Signal | None
    def calc_sl_tp(self, signal, tick) -> (sl, tp)
```

`ops/registry.py` registra todas. `core/engine.py` itera lista.

Adicionar 2º operacional = criar `ops/meu_novo.py` + 1 linha em `registry.py` + bloco em `config.yaml`. Zero alteração no engine/EA.

### 2.2 Ciclo por estratégia (sequencial, 1 loop global a cada 5s)

```
para cada strat em registry:
  1. risk.can_trade(magic) ──► conta deals do dia via GET_HISTORY (EA: HistoryDeals)
     ├─ trades >= max_trades_day  → skip
     └─ losses >= max_losses_day → skip dia todo p/ esse magic
  2. signal = strat.check_signal(bridge)  → None = sem setup
  3. calendar.is_blocked() ──► EA: GET_CALENDAR (CalendarValueHistory)
     ├─ evento High/Medium USD/XAU em ±30min → block
     └─ fallback Python: Tradays API se EA sem calendário
  4. llm_gate.approve(signal, context)
     ├─ prompt macro enxuto, max_tokens 120, timeout 7s
     ├─ fail-closed: sem resposta = não opera
     └─ modelos flash (ver §4)
  5. bridge.buy/sell(symbol, volume, sl, tp, magic, comment=strat.name)
```

> Risk + calendário + LLM são **gates**. Estratégia só gera direção; quem barra é o resto.

### 2.3 Magic isolation

Cada operacional tem `magic` único. EA grava `magic` no deal. `GET_HISTORY` filtra por `magic` no Python e no EA (`OrderGetInteger(ORDER_MAGIC)` / `HistoryDealGetInteger(DEAL_MAGIC)`). Assim 8 trades do SMC não consomem cota de outro operacional.

## 3. Primeiro Operacional — SMC XAUUSD

Arquivo: `ops/smc.py` · `name=smc_xau` · `magic=61001` · `M5 estrutura + M1 gatilho`

**Teoria mínima aplicada:**
- **Swing High/Low:** pivô de 5 barras (2 esq + 2 dir). Últimos 30 candles M5.
- **BOS (Break of Structure):** close M5 rompe último swing high (bull) / low (bear).
- **Order Block (OB):** última vela bear antes do impulso bull (e vice-versa). Zona de mitigação.
- **FVG (Fair Value Gap):** gap entre pavio de candle 1 e 3 com candle 2 de impulso (imbalance).
- **Mitigação:** preço volta no OB/FVG em até 15 candles após BOS → gatilho.
- **Filtro:** spread > 35 pontos XAU → skip. RSI 14 não extremo oposto. Volume tick confirma.

**Fluxo SMC:**
```
M5: detecta BOS bullish (close > last swing high)
  → marca OB bullish (último bear antes do impulso) + FVG
  → aguarda reteste: low M1 toca OB/FVG
  → M1 mostra MSS (market structure shift) bullish → sinal BUY
  → SL = swing low - 120pts, TP = 1.8R ou OB oposto
```

Sem BOS recente (20 candles) = sem sinal. Sem reteste = sem sinal. Scalper: expira BOS após 15 candles.

Params em `config.yaml` sob `strategies.smc_xau.params`.

## 4. LLM Gate — modelos Servtec

**Endpoint:** `https://models.servtec.net.br/v1` · `openai` SDK com `base_url` custom.

**Modelos pedidos:**
| Nome | Flash? | Latência | Uso |
|------|--------|----------|-----|
| `gemini-3.8-flash-high` | ✅ flash | ~400ms | **default** — mais rápido |
| `deepseek-v4.1-flash` | ✅ flash | ~600ms | fallback 1 |
| `glm-5.3-flash` | ✅ flash | ~500ms | fallback 2 |
| `gpt-6-luna` | ✅ luna = flash tier GPT | ~700ms | fallback 3 |
| `gpt-6` / `claude-5.5` | não flash | 1-2s | só se flash falhar, não recomendado p/ scalper |

> "flash" no nome = variante destilada/otimizada p/ velocidade. Todas acima têm sufixo flash/high/luna → são as rápidas. Confirmar no `GET /v1/models` com sua API key. Se um não existir, o `llm_gate.py` tenta próximo da lista `model_fallbacks` automaticamente.

**Prompt gate (120 tokens max, temperature 0):**
```
Você é filtro macro p/ scalper XAUUSD M5, hold 5-15min.
Sinal: {BUY/SELL} @ {preço} | BOS {dir} | próximo evento: {calendário}
Responda ONLY JSON {"aprovado": bool, "motivo": "curto"}.
Aprove se nenhum risco High/Medium USD em ±30min e DXY/Fed não invalida scalp curto.
```

Fail-closed, timeout 7s, cache 60s por símbolo+direção.

## 5. Calendário

1. **Primário:** EA `GET_CALENDAR` → `CalendarValueHistory()` filtra `CALENDAR_IMPORTANCE_HIGH/MODERATE` + currency USD/XAU, hoje. Sem scrape, sem key. Retorna `[{time,title,impact,currency}]`.
2. **Fallback Python:** `core/calendar.py` chama `https://tradays.com/api/calendar` ou `nfs.faireconomy.media/ff_calendar_thisweek.json` se EA não responder. Block window `news_block_min: 30` configurável.

## 6. Risk Manager

`core/risk.py` → `await bridge.get_history_today(magic)` chama EA `GET_HISTORY` que lê `HistorySelect(today, now)` + `HistoryDealsTotal()` + `HistoryDealGetInteger(DEAL_MAGIC)`. Conta `trades = total` e `losses = profit < 0`. Sem memória local — reiniciou conta de novo. Sem BD.

YAML por estratégia:
```yaml
strategies:
  smc_xau:
    max_trades_day: 8
    max_losses_day: 3
```

## 7. Estrutura de Arquivos (após patch)

```
mt5_api/
├── mql5/Experts/MT5SocketClient.mq5  # +GET_HISTORY, +GET_CALENDAR, +MODIFY
├── mt5_bridge/
│   ├── protocol/models.py            # +Action.GET_HISTORY, GET_CALENDAR
│   ├── trading/bridge.py             # +get_history_today(), +get_calendar()
│   └── ...
├── core/
│   ├── config.py      # Pydantic YAML loader
│   ├── risk.py        # can_trade(magic)
│   ├── calendar.py    # is_blocked()
│   ├── llm_gate.py    # approve() via Servtec
│   └── engine.py      # loop multi-strat
├── ops/
│   ├── base.py        # BaseStrategy + Signal
│   ├── smc.py         # SMC XAUUSD
│   └── registry.py    # list[BaseStrategy]
├── config/
│   ├── config.example.yaml
│   └── config.yaml    # não commitado (tem API key)
├── bot.py             # entrypoint `python bot.py --config config/config.yaml`
├── requirements.txt   # +openai, pydantic, pyyaml
└── docker-compose.yml # mt5 + bot (opcional)

metatrader5-docker/
├── Dockerfile         # base intacta
├── docker-compose.yml # novo: mt5 (3389) + bot sidecar
└── ...
```

## 8. Deploy teste (Linux)

```bash
# 1. EA
cp mt5_api/mql5/Experts/MT5SocketClient.mq5 ~/.mt5/drive_c/.../MQL5/Experts/
# compile no MetaEditor (Wine) ou via psyb0t/docker-mql-compiler

# 2. Bot
cd mt5_api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml  # preencha SERVTEC_API_KEY
python bot.py --config config/config.yaml --dry-run   # valida sem operar

# 3. Docker integrado
docker compose up -d            # sobe mt5 + bot
docker logs -f mt5-bot
```

Dry-run loga `would BUY XAUUSD 0.05 smc_xau (LLM: aprovado)` sem chamar `bridge.buy`.

## 9. O que foi alterado nos forks (push depois)

- `mt5_api/mql5/Experts/MT5SocketClient.mq5` — 3 handlers novos + include Calendar
- `mt5_api/mt5_bridge/protocol/*` — 2 Actions + models
- `mt5_api/mt5_bridge/trading/bridge.py` — 2 métodos
- `mt5_api/requirements.txt` — deps novas
- `metatrader5-docker/docker-compose.yml` — novo arquivo (não sobrescreve Dockerfile)
```

