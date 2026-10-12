"""Calendar filter: EA primary (CalendarValueHistory), Tradays fallback."""

import logging, time
from datetime import datetime, timezone
from typing import Optional

from mt5_bridge import MT5Bridge, CalendarEvent

logger = logging.getLogger(__name__)


class CalendarFilter:
    def __init__(self, bridge: MT5Bridge, block_min: int = 30, hours_ahead: int = 24,
                 fallback_url: str = ""):
        self.bridge = bridge
        self.block_min = block_min
        self.hours_ahead = hours_ahead
        self.fallback_url = fallback_url
        self._cache: list[CalendarEvent] = []
        self._cache_ts: float = 0
        self._cache_ttl = 300  # 5 min

    async def get_events(self, force: bool = False) -> list[CalendarEvent]:
        if not force and self._cache and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cache
        # 1) try EA
        try:
            evs = await self.bridge.get_calendar(hours_ahead=self.hours_ahead)
            self._cache, self._cache_ts = evs, time.time()
            logger.debug(f"Calendar EA: {len(evs)} events")
            return evs
        except Exception as e:
            logger.warning(f"Calendar EA failed: {e}, trying fallback")
        # 2) fallback Tradays / FF
        evs = await self._fallback_fetch()
        self._cache, self._cache_ts = evs, time.time()
        return evs

    async def _fallback_fetch(self) -> list[CalendarEvent]:
        import aiohttp
        # Tradays free API
        urls = [
            "https://tradays.com/api/calendar?timeframe=week",
            self.fallback_url,
        ]
        for url in urls:
            if not url:
                continue
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                        data = await r.json()
                        # normalize FF/Tradays -> CalendarEvent
                        out: list[CalendarEvent] = []
                        items = data if isinstance(data, list) else data.get("events", data.get("data", []))
                        for it in items[:50]:
                            impact = str(it.get("impact", it.get("importance", ""))).lower()
                            if impact not in ("high", "medium", "3", "2"):
                                # FF uses "High"/"Medium"
                                imp_raw = str(it.get("impact", ""))
                                if imp_raw not in ("High", "Medium"):
                                    continue
                                impact = imp_raw
                            else:
                                impact = "High" if impact in ("high", "3") else "Medium"
                            out.append(CalendarEvent(
                                time=str(it.get("time", it.get("date", ""))),
                                title=str(it.get("title", it.get("event", ""))),
                                currency=str(it.get("currency", it.get("country", ""))),
                                impact=impact,
                            ))
                        if out:
                            return out
            except Exception as e:
                logger.debug(f"Fallback {url} failed: {e}")
        return []

    async def is_blocked(self) -> tuple[bool, str]:
        """True if High/Medium event within block_min window."""
        events = await self.get_events()
        # EA already filtered High/Medium USD/XAU. If we have any event -> blocked
        # Fallback list is also filtered. Treat any event as block (conservative).
        if not events:
            return False, ""
        # check time proximity if parsable
        now = datetime.now(timezone.utc)
        for ev in events:
            # try parse ev.time
            for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y %I:%M%p"):
                try:
                    t = datetime.strptime(ev.time, fmt).replace(tzinfo=timezone.utc)
                    mins = (t - now).total_seconds() / 60
                    if -self.block_min <= mins <= self.block_min:
                        return True, f"{ev.title} ({ev.impact}) em {mins:.0f}min"
                    break
                except Exception:
                    continue
            # if unparsable, conservatively block if we have event today
            return True, f"{ev.title} ({ev.impact}) — horário não parseado, bloqueio conservador"
        return False, ""
