"""Registry: lista de operacionais ativos. Adicionar novo = 1 import + 1 append."""

from typing import TYPE_CHECKING
from .base import BaseStrategy

def get_strategies(app_config) -> list[BaseStrategy]:
    """Instancia estratégias habilitadas a partir de AppConfig.strategies."""
    from .smc import SMCStrategy

    # mapa nome -> classe
    known: dict[str, type[BaseStrategy]] = {
        "smc_xau": SMCStrategy,
        # adicione novos aqui: "rsi_div": RSIDivStrategy, etc.
    }

    out: list[BaseStrategy] = []
    for name, sc in app_config.strategies.items():
        if not sc.enabled:
            continue
        cls = known.get(name)
        if not cls:
            # tenta carregar dinamicamente ops.<name>
            try:
                import importlib
                mod = importlib.import_module(f"ops.{name}")
                # pega primeira subclasse de BaseStrategy
                for v in vars(mod).values():
                    if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy:
                        cls = v
                        break
            except Exception:
                pass
        if not cls:
            continue
        strat = cls(config=sc.model_dump())
        # override nome/magic/symbol/timeframe já feito no __init__
        strat.name = name
        out.append(strat)
    # fallback: se config vazio, sobe SMC padrão
    if not out:
        out.append(SMCStrategy(config={}))
    return out
