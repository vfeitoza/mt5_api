# Guia — Criar Novo Operacional & Usar Configurações

> Leia `ARCHITECTURE.md` antes. Este doc é o passo-a-passo prático.

## 1. Onde fica o `timeframe`?

Por operacional, em `config/config.yaml` (não global):

```yaml
strategies:
  smc_xau:
    symbol: "XAUUSD"
    timeframe: "M5"       # <-- aqui: M1 | M5 | M15 | M30 | H1 | H4 | D1 | W1 | MN1
    params:
      trigger: "hybrid"  # hybrid | close_only | polling (sobrescreve trigger_mode global)
```

`ops/smc.py` lê `self.timeframe` para BOS/OB/FVG. M1 de reteste/MSS é fixo lá dentro. Cada operacional pode ter TF diferente e rodar junto no mesmo `engine` (loop único).

Global em `config.yaml` só tem o **modo de gatilho** e **intervalo do loop**:

```yaml
loop_interval_s: 30.0
trigger_mode: "hybrid"   # default p/ todos; params.trigger por strat sobrescreve
```

| trigger_mode | O que faz | Quando usar |
|---|---|---|
| `hybrid` **(recomendado)** | BOS só no candle fechado (`rev[-2]`), reteste/MSS a cada 30s | SMC sem repaint, sem perder entrada |
| `close_only` | Tudo só quando fecha M5 (dedup por `time`). 1 check/candle | Conservador, zero repaint intra-candle |
| `polling` | Legado: checa a cada `loop_interval_s` mesmo intra-candle | Scalper puro, mais sinais/falsos |

---

## 2. Estrutura de `config.yaml` completa

```yaml
bridge:
  host: "0.0.0.0"
  port: 5555              # deve bater com EA ServerPort

llm:
  base_url: "https://models.servtec.net.br/v1"
  api_key: ""             # ou export SERVTEC_API_KEY=xxx (tem prioridade)
  model: "gemini-3.8-flash-high"
  fallbacks: [deepseek-v4.1-flash, glm-5.3-flash, gpt-6-luna]
  timeout_s: 7.0
  max_tokens: 120

calendar:
  news_block_min: 30      # bloqueia ±30min de evento High/Medium
  hours_ahead: 24
  use_ea_primary: true
  fallback_url: "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

loop_interval_s: 30.0     # 30s casa com LLM 7s + folga; close_only use 60s
trigger_mode: "hybrid"
dry_run: false            # true = loga would BUY/SELL sem enviar
log_level: "INFO"

strategies:
  smc_xau:                # <-- nome = chave do dict, vira strat.name
    enabled: true
    symbol: "XAUUSD"      # exato do broker (XAUUSD, XAUUSD.a, GOLD)
    timeframe: "M5"
    magic: 61001          # ÚNICO por operacional, isola HistoryDeals
    volume: 0.05
    max_trades_day: 8     # por magic, via EA GET_HISTORY
    max_losses_day: 3
    params:               # livre, cada strat lê o que precisa
      spread_max: 35
      bos_lookback: 20
      bos_expire: 15
      rr: 1.8
      sl_pad: 1.2
      trigger: "hybrid"   # opcional override

  # segundo exemplo
  rsi_div:
    enabled: false
    symbol: "XAUUSD"
    timeframe: "M15"
    magic: 61002
    volume: 0.03
    max_trades_day: 6
    max_losses_day: 2
    params:
      rsi_period: 14
      rr: 1.5
```

**Regras de ouro:**
- `magic` nunca repetir. Faixa sugerida: `61001-61999`.
- `symbol` exatamente como no Market Watch (rode `get_symbols()` se dúvida).
- `volume` respeita `SYMBOL_VOLUME_MIN/MAX` do broker; EA rejeita se fora.
- `max_trades_day` / `max_losses_day` são por **magic**, não global. 8 do SMC não consome do RSI.
- `params` é dict livre — documente no cabeçalho do seu `ops/*.py`.

---

## 3. Passo-a-passo: criar novo operacional

### Passo 1 — Criar arquivo `ops/<nome>.py`

```python
# ops/rsi_div.py
"""RSI divergência XAUUSD M15 — exemplo template."""
from typing import Optional
from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
import logging
logger = logging.getLogger(__name__)

def _tf(s: str) -> Timeframe:
    return Timeframe(s) if s in Timeframe._value2member_map_ else Timeframe.M15

class RSIDivStrategy(BaseStrategy):
    name = "rsi_div"      # deve bater com chave do YAML
    magic = 61002
    symbol = "XAUUSD"
    timeframe = "M15"

    async def check_signal(self, bridge) -> Optional[Signal]:
        p = self.config
        rsi_period = int(p.get("rsi_period", 14))
        rr = float(p.get("rr", 1.5))

        # 1) dados — SEMPRE via bridge, nunca direto
        rates = await bridge.get_rates(self.symbol, _tf(self.timeframe), 50)
        tick = await bridge.get_tick(self.symbol)
        if len(rates) < 30:
            return None

        # 2) sua lógica aqui (ex: RSI)
        # ... calcule rsi, detecte divergência ...

        # 3) sem setup = return None (não chama LLM nem trade)
        # if not divergence: return None

        # 4) com setup = return Signal
        price = tick.ask  # BUY usa ask, SELL usa bid
        sl = price - 4.0
        tp = price + (price - sl) * rr
        return Signal(direction="BUY", entry=price, sl=sl, tp=tp, meta="RSI div bull M15")
```

**Contrato `check_signal`:**
- `async def check_signal(self, bridge) -> Signal | None`
- **Não** chamar `bridge.buy/sell` — só retorna direção. Engine faz risk/calendar/LLM/execução.
- `Signal(direction="BUY"|"SELL", entry=float, sl=float, tp=float, meta=str)`
- `meta` vai para o LLM gate (`bos_info`), seja descritivo: `"BOS BULL swing 2660 OB 2654-2656"`.

### Passo 2 — Registrar em `ops/registry.py`

```python
# ops/registry.py — adicione 1 linha em known
from .smc import SMCStrategy
from .rsi_div import RSIDivStrategy

known: dict[str, type[BaseStrategy]] = {
    "smc_xau": SMCStrategy,
    "rsi_div": RSIDivStrategy,  # <-- novo
}
```

> Se não adicionar, o registry tenta `importlib.import_module(f"ops.{name}")` e pega primeira subclasse de `BaseStrategy` automaticamente. Mas explícito é preferível.

### Passo 3 — Adicionar bloco no `config/config.yaml`

Copie de `config.example.yaml` e ajuste `magic/volume/params`. `enabled: true` para ativar.

### Passo 4 — Testar em dry-run

```bash
python bot.py --config config/config.yaml --dry-run
# log esperado: [rsi_div] sinal BUY ... | LLM aprovado=true ... | DRY-RUN would BUY ...
```

Sem EA conectado, `risk.can_trade` vai bloquear (fail-closed) — conecte EA ou teste unitário com mock (ver §5).

---

## 4. Template mínimo (copie e cole)

`ops/meu_op.py`:

```python
from typing import Optional
from .base import BaseStrategy, Signal
from mt5_bridge.protocol import Timeframe
import logging
logger = logging.getLogger(__name__)

class MeuOpStrategy(BaseStrategy):
    name = "meu_op"; magic = 61010; symbol = "XAUUSD"; timeframe = "M5"
    async def check_signal(self, bridge) -> Optional[Signal]:
        rates = await bridge.get_rates(self.symbol, Timeframe(self.timeframe), 50)
        tick = await bridge.get_tick(self.symbol)
        # ... sua lógica ...
        # return Signal(direction="BUY", entry=tick.ask, sl=..., tp=..., meta="motivo")
        return None
```

---

## 5. Testes locais (sem MT5)

```python
# test_meu_op.py
import asyncio
from unittest.mock import AsyncMock
from ops.meu_op import MeuOpStrategy

def candle(o,h,l,c): return {"open":o,"high":h,"low":l,"close":c,"volume":100,"time":"2026.10.12 10:00:00"}

bridge = AsyncMock()
bridge.get_rates.return_value = [candle(2650+i,2651+i,2649+i,2650.5+i) for i in range(50)][::-1]
bridge.get_tick.return_value = type("T",(),{"bid":2655,"ask":2655.3})()

strat = MeuOpStrategy(config={"rr":1.8})
sig = asyncio.run(strat.check_signal(bridge))
print(sig)  # Signal ou None
assert sig is None or sig.direction in ("BUY","SELL")
```

Para validar o fluxo completo sem operar:

```bash
python bot.py --config config/config.yaml --dry-run  # requer EA conectado, mas não envia ordem
```

---

## 6. Dicas de configuração

- **Conservador vs agressivo:** comece `volume 0.03-0.05`, `max_trades 6-8`, `max_losses 2-3`, `rr 1.5-1.8`. Aumente após 2 semanas de log.
- **Spread:** XAU típico 20-30 pts. `spread_max 35` filtra abertura de mercado. Notícias = spread dispara.
- **LLM:** `timeout_s 7` + `loop 30s` = 23s folga. Não baixe loop para < timeout.
- **Calendário:** `news_block_min 30` é conservador. 15 se quiser mais trades em dia de CPI.
- **Dry-run primeiro:** sempre rode 1 dia `dry_run: true` no VPS teste antes de real.

---

## 7. Checklist novo operacional

- [ ] `ops/<nome>.py` com `check_signal` retornando `Signal|None`
- [ ] `magic` único (61001 já usado pelo SMC)
- [ ] registrado em `ops/registry.py`
- [ ] bloco em `config/config.yaml` + `config.example.yaml` documentado
- [ ] `python bot.py --dry-run` loga sinal
- [ ] 1 teste mock passa
