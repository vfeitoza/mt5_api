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

        # Prompt enriquecido para champion: bos_info já traz CCI/MTF/BOS/SR/RR
        prompt = (
            f"Você é filtro macro para scalper XAUUSD M5 hold 5-15min (Champion SMC+CCI).\n"
            f"Sinal: {direction} {symbol} @ {price:.2f} | {bos_info}\n"
            f"Evento: {news_title or 'nenhum High/Medium em 20min'}\n"
            f"Considere DXY, Fed, CPI/payroll e momentum recente.\n"
            f"Responda ONLY JSON {{\"aprovado\": true/false, \"motivo\": \"curto 10 palavras\"}}.\n"
            f"Aprove apenas se macro não invalidar scalp curto."
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
        import aiohttp, asyncio
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s, connect=5)
            async with aiohttp.ClientSession() as s:
                async with s.post(url, headers=headers, json=body, timeout=timeout) as r:
                    if r.status != 200:
                        txt = await r.text()
                        return False, "", f"HTTP {r.status}: {txt[:300]}"
                    data = await r.json()
                    try:
                        content = data["choices"][0]["message"]["content"].strip()
                    except Exception:
                        return False, "", f"bad response shape: {str(data)[:300]}"
                    # strip markdown fences if present
                    if content.startswith("```"):
                        content = content.split("```")[1]
                        if content.startswith("json"):
                            content = content[4:]
                        content = content.strip()
                    try:
                        obj = json.loads(content)
                    except Exception as je:
                        # model returned non-JSON (e.g. plain approved)
                        logger.debug(f"LLM {model} non-JSON content: {content[:200]} err {je}")
                        return False, "", f"JSON parse fail: {content[:200]}"
                    aprovado = bool(obj.get("aprovado", obj.get("approved", False)))
                    motivo = str(obj.get("motivo", obj.get("reason", "")))[:120]
                    return aprovado, motivo, None
        except asyncio.TimeoutError as e:
            return False, "", f"TimeoutError after {self.timeout_s}s ({type(e).__name__}: {e!r})"
        except Exception as e:
            msg = str(e).strip() or repr(e)
            if not msg or msg == "''":
                msg = f"{type(e).__name__}: {e!r}"
            return False, "", msg[:400]
