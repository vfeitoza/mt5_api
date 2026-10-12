"""LLM gate via Servtec OpenAI-compatible API. Flash models, fail-closed, cached."""

import json, time, logging
from typing import Optional

logger = logging.getLogger(__name__)


class LLMGate:
    def __init__(self, base_url: str, api_key: str, model: str,
                 fallbacks: list[str] | None = None,
                 timeout_s: float = 7.0, max_tokens: int = 120, temperature: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.fallbacks = fallbacks or []
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._cache: dict[str, tuple[bool, str, float]] = {}  # key -> (approved, motivo, ts)
        self._cache_ttl = 60.0

    def _cache_key(self, symbol: str, direction: str, news: str) -> str:
        return f"{symbol}:{direction}:{news[:80]}"

    async def approve(self, symbol: str, direction: str, price: float,
                      bos_info: str = "", news_title: str = "") -> tuple[bool, str]:
        """Returns (aprovado, motivo). Fail-closed on error/timeout."""
        if not self.api_key:
            logger.warning("LLM gate: no API key — blocking (fail-closed)")
            return False, "sem api_key"

        key = self._cache_key(symbol, direction, news_title)
        if key in self._cache:
            ok, motivo, ts = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                logger.debug(f"LLM cache hit {symbol} {direction}: {ok}")
                return ok, motivo + " (cache)"

        prompt = (
            f"Você é filtro de risco macro para scalper XAUUSD timeframe M5, hold 5-15min.\n"
            f"Sinal técnico: {direction} {symbol} @ {price:.2f} | {bos_info}\n"
            f"Evento próximo: {news_title or 'nenhum High/Medium em 30min'}\n"
            f"Considere DXY, Fed, CPI, payroll recentes se souber.\n"
            f"Responda APENAS JSON: {{\"aprovado\": true/false, \"motivo\": \"frase curta 10 palavras\"}}\n"
            f"Aprove apenas se nenhum risco macro High/Medium invalidar scalp de 5-15min."
        )

        models = [self.model] + self.fallbacks
        last_err = ""
        for mdl in models:
            ok, motivo, err = await self._call_one(mdl, prompt)
            if err is None:
                self._cache[key] = (ok, motivo, time.time())
                logger.info(f"LLM {mdl} -> aprovado={ok} motivo={motivo}")
                return ok, motivo
            last_err = err
            logger.warning(f"LLM {mdl} falhou: {err}, tentando fallback")

        return False, f"llm indisponível: {last_err}"

    async def _call_one(self, model: str, prompt: str) -> tuple[bool, str, Optional[str]]:
        import aiohttp
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, headers=headers, json=body,
                                  timeout=aiohttp.ClientTimeout(total=self.timeout_s)) as r:
                    if r.status != 200:
                        txt = await r.text()
                        return False, "", f"HTTP {r.status}: {txt[:200]}"
                    data = await r.json()
                    content = data["choices"][0]["message"]["content"].strip()
                    # strip markdown fences if present
                    if content.startswith("```"):
                        content = content.split("```")[1]
                        if content.startswith("json"):
                            content = content[4:]
                        content = content.strip()
                    obj = json.loads(content)
                    aprovado = bool(obj.get("aprovado", obj.get("approved", False)))
                    motivo = str(obj.get("motivo", obj.get("reason", "")))[:120]
                    return aprovado, motivo, None
        except Exception as e:
            return False, "", str(e)[:200]
