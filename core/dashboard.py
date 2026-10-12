"""Dashboard profissional — aiohttp na porta 5556.

Exposto em / e /api/* . Polling JS atualiza sem reload.
Sem deps extras: aiohttp já no requirements.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# Estado compartilhado preenchido pelo Engine
_dashboard_state: dict[str, Any] = {
    "started_at": time.time(),
    "engine": None,  # Engine ref
    "bridge": None,  # MT5Bridge ref
    "config": None,  # AppConfig ref
    "last_signals": {},  # name -> {direction, entry, sl, tp, meta, ts}
    "last_llm": {},  # name -> {approved, motivo, ts}
    "trades": [],  # list of {time, strat, direction, ticket, price, ...}
    "logs": [],  # ring buffer 500 lines
}

def set_dashboard_refs(engine=None, bridge=None, config=None):
    if engine is not None:
        _dashboard_state["engine"] = engine
    if bridge is not None:
        _dashboard_state["bridge"] = bridge
    if config is not None:
        _dashboard_state["config"] = config

def record_signal(name: str, signal):
    _dashboard_state["last_signals"][name] = {
        "direction": getattr(signal, "direction", "?"),
        "entry": getattr(signal, "entry", 0),
        "sl": getattr(signal, "sl", 0),
        "tp": getattr(signal, "tp", 0),
        "meta": getattr(signal, "meta", ""),
        "ts": time.time(),
    }

def record_llm(name: str, approved: bool, motivo: str):
    _dashboard_state["last_llm"][name] = {"approved": approved, "motivo": motivo, "ts": time.time()}

def record_trade(name: str, direction: str, ticket: int, price: float, volume: float = 0, sl: float = 0, tp: float = 0):
    _dashboard_state["trades"].insert(0, {
        "time": datetime.now(timezone.utc).isoformat(),
        "strat": name,
        "direction": direction,
        "ticket": ticket,
        "price": price,
        "volume": volume,
        "sl": sl,
        "tp": tp,
    })
    if len(_dashboard_state["trades"]) > 500:
        _dashboard_state["trades"].pop()

# ---------- helpers ----------

async def _safe_positions():
    bridge = _dashboard_state.get("bridge")
    if not bridge:
        return []
    try:
        pos = await asyncio.wait_for(bridge.get_positions(), timeout=4)
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.order_type,
                "volume": p.volume,
                "open_price": p.open_price,
                "sl": p.stop_loss,
                "tp": p.take_profit,
                "profit": p.profit,
                "magic": getattr(p, "magic", 0),
            }
            for p in pos
        ]
    except Exception as e:
        logger.debug(f"dashboard positions fail: {e}")
        return []

async def _safe_history(magic: int = 0):
    bridge = _dashboard_state.get("bridge")
    if not bridge:
        return {"profit": 0, "wins": 0, "losses": 0, "trades": 0, "last_loss_time": ""}
    try:
        hs = await asyncio.wait_for(bridge.get_history_stats(magic=magic), timeout=4)
        if hasattr(hs, "__dict__"):
            return {"profit": hs.profit, "wins": hs.wins, "losses": hs.losses, "trades": hs.trades, "last_loss_time": hs.last_loss_time}
        return dict(hs) if isinstance(hs, dict) else {"profit": 0}
    except Exception as e:
        logger.debug(f"dashboard history {magic} fail: {e}")
        return {"profit": 0, "wins": 0, "losses": 0, "trades": 0, "last_loss_time": ""}

async def _safe_history_deals(magic: int = 0):
    bridge = _dashboard_state.get("bridge")
    if not bridge:
        return []
    try:
        deals = await asyncio.wait_for(bridge.get_history_deals(magic=magic), timeout=5)
        return [
            {
                "ticket": d.ticket,
                "time": d.time,
                "symbol": d.symbol,
                "type": d.type,
                "volume": d.volume,
                "price": d.price,
                "profit": d.profit,
                "swap": d.swap,
                "commission": d.commission,
                "magic": d.magic,
                "comment": d.comment,
            }
            for d in deals
        ]
    except Exception as e:
        logger.debug(f"dashboard deals {magic} fail: {e}")
        return []

async def _safe_tick(symbol: str):
    bridge = _dashboard_state.get("bridge")
    if not bridge:
        return None
    try:
        t = await asyncio.wait_for(bridge.get_tick(symbol), timeout=3)
        return {"bid": t.bid, "ask": t.ask, "time": t.time}
    except Exception:
        return None

async def _safe_account():
    bridge = _dashboard_state.get("bridge")
    if not bridge:
        return None
    try:
        a = await asyncio.wait_for(bridge.get_account(), timeout=3)
        return {"balance": a.balance, "equity": a.equity, "margin": a.margin, "free_margin": a.free_margin, "leverage": a.leverage}
    except Exception:
        return None

async def _safe_calendar(hours=24):
    # use CalendarFilter from engine if available else bridge
    eng = _dashboard_state.get("engine")
    if eng and hasattr(eng, "calendar"):
        try:
            evs = await asyncio.wait_for(eng.calendar.get_events(), timeout=4)
            return [{"time": e.time, "title": e.title, "impact": e.impact, "currency": e.currency} for e in evs[:20]]
        except Exception:
            pass
    bridge = _dashboard_state.get("bridge")
    if bridge:
        try:
            evs = await asyncio.wait_for(bridge.get_calendar(hours_ahead=hours), timeout=4)
            return [{"time": e.time, "title": e.title, "impact": e.impact, "currency": e.currency} for e in evs[:20]]
        except Exception:
            pass
    return []

# ---------- API handlers ----------

async def api_status(request: web.Request):
    bridge = _dashboard_state.get("bridge")
    cfg = _dashboard_state.get("config")
    eng = _dashboard_state.get("engine")
    is_connected = bool(bridge and bridge.is_connected) if bridge else False
    uptime = time.time() - _dashboard_state["started_at"]
    return web.json_response({
        "connected": is_connected,
        "uptime_s": round(uptime, 1),
        "uptime_h": round(uptime / 3600, 2),
        "loop_interval": getattr(cfg, "loop_interval_s", 30) if cfg else 30,
        "trigger_mode": getattr(cfg, "trigger_mode", "hybrid") if cfg else "hybrid",
        "dry_run": bool(getattr(cfg, "dry_run", False)) if cfg else False,
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "strategies": len(eng.strategies) if eng and hasattr(eng, "strategies") else 0,
    })

async def api_eas(request: web.Request):
    cfg = _dashboard_state.get("config")
    if not cfg:
        return web.json_response([])
    out = []
    for name, sc in cfg.strategies.items():
        p = sc.params or {}
        last_sig = _dashboard_state["last_signals"].get(name)
        last_llm = _dashboard_state["last_llm"].get(name)
        # history per magic
        hist = await _safe_history(magic=sc.magic)
        out.append({
            "name": name,
            "enabled": sc.enabled,
            "symbol": sc.symbol,
            "timeframe": sc.timeframe,
            "magic": sc.magic,
            "volume": sc.volume,
            "max_trades_day": sc.max_trades_day,
            "max_losses_day": sc.max_losses_day,
            "params": p,
            "last_signal": last_sig,
            "last_llm": last_llm,
            "history": hist,
        })
    return web.json_response(out)

async def api_positions(request: web.Request):
    pos = await _safe_positions()
    return web.json_response(pos)

async def api_history(request: web.Request):
    try:
        magic = int(request.query.get("magic", "0"))
    except Exception:
        magic = 0
    hist = await _safe_history(magic=magic)
    return web.json_response(hist)

async def api_history_deals(request: web.Request):
    try:
        magic = int(request.query.get("magic", "0"))
    except Exception:
        magic = 0
    deals = await _safe_history_deals(magic=magic)
    return web.json_response(deals)

async def api_account(request: web.Request):
    acc = await _safe_account()
    return web.json_response(acc or {})

async def api_calendar(request: web.Request):
    hours = int(request.query.get("hours", "24"))
    evs = await _safe_calendar(hours=hours)
    return web.json_response(evs)

async def api_ticks(request: web.Request):
    cfg = _dashboard_state.get("config")
    symbols = set()
    if cfg:
        for sc in cfg.strategies.values():
            symbols.add(sc.symbol)
    if not symbols:
        symbols.add("XAUUSD")
    out = {}
    for sym in symbols:
        t = await _safe_tick(sym)
        if t:
            out[sym] = t
    return web.json_response(out)

async def api_trades(request: web.Request):
    limit = int(request.query.get("limit", "50"))
    trades = _dashboard_state["trades"][:limit]
    return web.json_response(trades)

async def api_chart(request: web.Request):
    """Retorna últimas velas M1/M5 para Chart.js (via bridge)."""
    symbol = request.query.get("symbol", "XAUUSD")
    tf = request.query.get("tf", "M5")
    count = min(int(request.query.get("count", "120")), 500)
    bridge = _dashboard_state.get("bridge")
    if not bridge:
        return web.json_response({"rates": []})
    try:
        from mt5_bridge.protocol import Timeframe
        timeframe = Timeframe(tf) if tf in Timeframe._value2member_map_ else Timeframe.M5
        rates = await asyncio.wait_for(bridge.get_rates(symbol, timeframe, count), timeout=6)
        # rates[0] newest, reverse to chronological for chart
        rev = list(reversed(rates))
        return web.json_response({"symbol": symbol, "tf": tf, "rates": rev})
    except Exception as e:
        return web.json_response({"error": str(e), "rates": []})

async def api_logs(request: web.Request):
    # últimas trades + sinais como log
    n = int(request.query.get("n", "80"))
    trades = _dashboard_state["trades"][:n]
    sigs = []
    for name, s in _dashboard_state["last_signals"].items():
        sigs.append({"strat": name, **s})
    return web.json_response({"trades": trades, "signals": sigs})


# ---------- SSE ----------

async def _collect_eas_payload():
    """Reuse api_eas logic without HTTP overhead for SSE."""
    cfg = _dashboard_state.get("config")
    if not cfg:
        return []
    out = []
    for name, sc in cfg.strategies.items():
        p = sc.params or {}
        last_sig = _dashboard_state["last_signals"].get(name)
        last_llm = _dashboard_state["last_llm"].get(name)
        hist = await _safe_history(magic=sc.magic)
        out.append({
            "name": name, "enabled": sc.enabled, "symbol": sc.symbol,
            "timeframe": sc.timeframe, "magic": sc.magic, "volume": sc.volume,
            "max_trades_day": sc.max_trades_day, "max_losses_day": sc.max_losses_day,
            "params": p, "last_signal": last_sig, "last_llm": last_llm, "history": hist,
        })
    return out


def _escape_sse_data(s: str) -> str:
    # SSE data lines cannot contain bare newline; join with \n data:
    return s.replace("\r", "").replace("\n", " ")


async def api_stream(request: web.Request):
    """SSE: push eas/positions/account/ticks em tempo real. HTMX ou EventSource."""
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await resp.prepare(request)
    try:
        # hello
        await resp.write(b": connected\n\n")
        while True:
            # EAs (principal) — HTMX sse-swap="eas" espera HTML em data:
            try:
                eas = await _collect_eas_payload()
                # JSON for JS EventSource
                await resp.write(f"event: eas_json\ndata: {_escape_sse_data(json.dumps(eas, ensure_ascii=False))}\n\n".encode())
                # HTML fragment for HTMX (tbody rows)
                rows = []
                for ea in eas:
                    sig = f"{ea['last_signal']['direction']} {ea['last_signal']['entry']:.2f}" if ea.get("last_signal") else "—"
                    sig_meta = (ea["last_signal"]["meta"][:80] if ea.get("last_signal") and ea["last_signal"].get("meta") else "")
                    llm = (("✅ " if ea.get("last_llm") and ea["last_llm"]["approved"] else "⛔ ") + ea["last_llm"]["motivo"]) if ea.get("last_llm") else "—"
                    hist = ea.get("history") or {}
                    pnl = hist.get("profit", 0)
                    pnl_c = "#10b981" if pnl >= 0 else "#ef4444"
                    rr = ea["params"].get("rr", ea["params"].get("risk_reward_ratio", "—"))
                    score = f"{ea['params'].get('min_score','')} / {23 if 'champion' in ea['name'] else 10}"
                    rows.append(
                        f"<tr class='clickable' onclick=\"openEaModal('{ea['name']}')\" title='Clique para detalhes'>"
                        f"<td><span class='tag'>{ea['name']}</span></td>"
                        f"<td><span class='badge {'on' if ea['enabled'] else 'off'}'>{'ativo' if ea['enabled'] else 'inativo'}</span></td>"
                        f"<td>{ea['symbol']} / {ea['timeframe']}</td><td class='mono'>{ea['magic']}</td><td>{ea['volume']:.2f}</td>"
                        f"<td>{rr}</td><td class='small'>{score}</td>"
                        f"<td class='small'><div>{sig}</div><div class='muted'>{sig_meta}</div></td>"
                        f"<td class='small'>{llm}</td>"
                        f"<td class='ticker'>{hist.get('trades',0)} trades · <span style='color:{pnl_c}'>{'+$' if pnl>=0 else '-$'}{abs(pnl):.2f}</span> · {hist.get('wins',0)}W/{hist.get('losses',0)}L</td>"
                        f"</tr>"
                    )
                html = "".join(rows) if rows else "<tr><td colspan=10 class='muted small'>Sem EAs configurados</td></tr>"
                await resp.write(f"event: eas\ndata: {_escape_sse_data(html)}\n\n".encode())
            except Exception as e:
                logger.debug(f"SSE eas fail: {e}")

            # positions
            try:
                pos = await _safe_positions()
                await resp.write(f"event: positions\ndata: {_escape_sse_data(json.dumps(pos, ensure_ascii=False))}\n\n".encode())
            except Exception:
                pass

            await asyncio.sleep(3)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"SSE closed: {e}")
    return resp

# ---------- HTML ----------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MT5 Multi-Operacional — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"></script>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js"></script>
<style>
:root{--bg:#0b0e14;--card:#111827;--muted:#9ca3af;--accent:#3b82f6;--ok:#10b981;--warn:#f59e0b;--bad:#ef4444;--border:#1f2937}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:#e5e7eb}
a{color:var(--accent)}header{position:sticky;top:0;z-index:10;background:rgba(11,14,20,.9);backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}
.wrap{max-width:1280px;margin:0 auto;padding:14px 18px}
.card.full{padding:0;overflow:hidden}
.card.full h3{padding:12px 14px 0 14px}
.card.full table{margin:0}
.card.full .scroll{overflow:auto}
.card.full thead th{position:sticky;top:0;background:#111827;z-index:1}
.card.wide{grid-column:1 / -1}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.pill{padding:6px 10px;border-radius:999px;border:1px solid var(--border);background:#0f172a;font-size:12px}
.pill.ok{border-color:rgba(16,185,129,.3);color:var(--ok)}.pill.bad{border-color:rgba(239,68,68,.3);color:var(--bad)}
.grid{display:grid;gap:14px}
@media(min-width:900px){.grid.cols2{grid-template-columns:1fr 1fr}.grid.cols3{grid-template-columns:1fr 1fr 1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:14px}
.card h3{margin:0 0 8px 0;font-size:14px;letter-spacing:.02em;color:#cbd5e1}
.kpi{font-size:22px;font-weight:700}
.muted{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left}
th{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.badge{padding:3px 8px;border-radius:999px;font-size:11px;border:1px solid var(--border)}
.badge.on{background:rgba(16,185,129,.15);color:var(--ok);border-color:rgba(16,185,129,.3)}
.badge.off{background:rgba(239,68,68,.1);color:var(--bad)}
.tag{font-size:11px;padding:2px 6px;border-radius:6px;background:#0b1220;border:1px solid var(--border);color:#93c5fd}
canvas{width:100%!important;height:280px!important}
.small{font-size:11px}
.mono{font-family:ui-monospace,Consolas,monospace}
.ticker{font-variant-numeric:tabular-nums}
.clickable{cursor:pointer}
.clickable:hover{background:rgba(59,130,246,.08)}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;z-index:100;padding:18px}
.modal-overlay.open{display:flex}
.modal{width:min(920px, 96vw);max-height:90vh;overflow:auto;background:#0f172a;border:1px solid #263248;border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal header{position:sticky;top:0;background:#0f172a;border-bottom:1px solid #1f2937;padding:14px 16px;border-radius:18px 18px 0 0}
.modal .body{padding:14px 16px;display:grid;gap:14px}
.btn{padding:8px 12px;border-radius:10px;border:1px solid var(--border);background:#0b1220;color:#e5e7eb;cursor:pointer;font-size:12px}
.btn:hover{border-color:#334155}
.hint{border:1px dashed #334155;background:rgba(59,130,246,.06);border-radius:10px;padding:8px 10px}
</style>
</head>
<body>
<header>
  <div class="wrap row" style="justify-content:space-between">
    <div class="row">
      <div style="width:10px;height:10px;border-radius:50%;background:var(--ok);box-shadow:0 0 12px var(--ok)" id="dot"></div>
      <strong>MT5 Multi-Operacional</strong>
      <span class="pill" id="conn">conectando…</span>
      <span class="pill" id="uptime">—</span>
      <span class="pill" id="dry"></span>
    </div>
    <div class="row small muted">
      <span id="now"></span>
      <span>·</span>
      <span id="loop"></span>
      <span>·</span>
      <span>Bridge 5555</span>
      <span>·</span>
      <span>Dashboard 5556</span>
    </div>
  </div>
</header>

<div class="wrap" style="display:grid;gap:14px;margin-top:14px">
  <!-- KPIs -->
  <div class="grid cols3" id="kpis">
    <div class="card"><h3>Conta</h3><div class="kpi" id="kpi-balance">—</div><div class="muted" id="kpi-equity">—</div></div>
    <div class="card"><h3>Operações do dia</h3><div class="kpi" id="kpi-trades">—</div><div class="muted" id="kpi-pnl">—</div></div>
    <div class="card"><h3>Spread & Sessão</h3><div class="kpi" id="kpi-spread">—</div><div class="muted" id="kpi-session">—</div></div>
  </div>

  <!-- EAs — HTMX+SSE: push real sem polling; fallback polling se SSE cair -->
  <div class="card" style="width:100%">
    <div class="row" style="justify-content:space-between"><h3>EAs / Operacionais</h3><span class="muted small">Clique no EA para ver configuração e operações do dia</span><span class="muted small" id="eas-count"></span> <span class="muted small" id="sse-badge" style="border:1px solid #263248;border-radius:999px;padding:2px 8px">SSE ●</span></div>
    <div class="hint small muted">Atualização em tempo real via SSE (push do bot) — sem polling. HTMX reabre sozinho se cair; histórico e posições também via SSE.</div>
    <div style="overflow:auto;margin-top:8px">
      <table id="eas-table" style="width:100%;table-layout:auto"><thead><tr><th>EA</th><th>Status</th><th>Símbolo/TF</th><th>Magic</th><th>Volume</th><th>RR</th><th>Score</th><th>Último sinal</th><th>LLM</th><th>Histórico hoje</th></tr></thead><tbody id="eas-tbody"></tbody></table>
    </div>
  </div>

  <div class="grid cols2">
    <div class="card"><h3>Posições abertas</h3><div style="overflow:auto"><table id="pos-table"><thead><tr><th>Ticket</th><th>Símbolo</th><th>Tipo</th><th>Lote</th><th>Abertura</th><th>SL/TP</th><th>Lucro</th><th>Magic</th></tr></thead><tbody></tbody></table></div><div class="muted small" id="pos-empty" style="display:none">Sem posições abertas — gate 1 posição liberado</div></div>
    <div class="card"><h3>Calendário econômico (High/Medium)</h3><div style="overflow:auto;max-height:300px"><table id="cal-table"><thead><tr><th>Hora</th><th>Evento</th><th>Impacto</th><th>Moeda</th></tr></thead><tbody></tbody></table></div></div>
  </div>

  <!-- Charts -->
  <div class="grid cols2">
    <div class="card"><h3>Gráfico XAUUSD M1 (80 velas)</h3><canvas id="chart-m1"></canvas><div class="muted small">Posições marcadas em preço — atualizado a cada 10s</div></div>
    <div class="card"><h3>Gráfico XAUUSD M5 (80 velas)</h3><canvas id="chart-m5"></canvas><div class="muted small">Trend M5 usado pelo scalp + champion</div></div>
  </div>

  <div class="grid cols2">
    <div class="card"><h3>PnL por EA (hoje)</h3><canvas id="chart-pnl"></canvas></div>
    <div class="card"><h3>Trades recentes (por magic) — sessão atual</h3><div style="overflow:auto;max-height:360px"><table id="trades-table"><thead><tr><th>Hora</th><th>EA</th><th>Dir</th><th>Ticket</th><th>Preço</th><th>Volume</th></tr></thead><tbody></tbody></table></div><div class="muted small" style="margin-top:6px">Para operações do dia por EA, clique no EA acima.</div></div>
  </div>

  <div class="muted small" style="text-align:center;padding:8px">API: <span class="mono">/api/status /api/eas /api/positions /api/history?magic= /api/calendar /api/chart?symbol=XAUUSD&tf=M5 /api/trades</span> · Auto-refresh 2–5s · Clique no EA para detalhes</div>
</div>

<!-- Modal EA detalhe -->
<div class="modal-overlay" id="ea-modal" onclick="if(event.target===this) closeEaModal()">
  <div class="modal">
    <header>
      <div class="row" style="justify-content:space-between">
        <div><strong id="modal-title">EA</strong> <span class="muted small" id="modal-subtitle"></span></div>
        <button class="btn" onclick="closeEaModal()">Fechar ✕</button>
      </div>
    </header>
    <div class="body">
      <div class="grid cols2">
        <div class="card"><h3>Resumo do dia (por magic)</h3><div id="modal-history" class="small"></div></div>
        <div class="card"><h3>Último sinal / LLM</h3><div id="modal-signal" class="small mono" style="white-space:pre-wrap"></div></div>
      </div>
      <div class="card"><h3>Operações do dia — <span id="modal-ops-title">—</span></h3><div style="overflow:auto;max-height:300px"><table id="modal-trades"><thead><tr><th>Hora</th><th>Dir</th><th>Ticket</th><th>Preço</th><th>Volume</th><th>Preço fech.</th><th>Resultado</th><th>Comentário</th></tr></thead><tbody></tbody></table></div><div class="muted small" id="modal-trades-empty" style="display:none">Nenhuma operação registrada para este EA hoje (nesta sessão do bot). Histórico agregado acima vem do MT5 via magic.</div></div>
      <div class="card"><h3>Configuração do EA</h3><div class="muted small" style="margin-bottom:8px">Parâmetros efetivos do <span class="mono">config.yaml</span> para este magic</div><pre id="modal-config" class="mono small" style="white-space:pre-wrap;word-break:break-word;background:#0b1220;border:1px solid #1f2937;border-radius:10px;padding:10px;max-height:360px;overflow:auto"></pre></div>
    </div>
  </div>
</div>

<script>
const $ = s=>document.querySelector(s);
const fmt = (n,d=2)=> (n==null?'—': Number(n).toFixed(d));
const fmtMoney = n=> (n==null?'—': (n>=0?'+$':'-$')+Math.abs(n).toFixed(2));

let chartM1, chartM5, chartPnL;

function initCharts(){
  const ctxM1 = document.getElementById('chart-m1').getContext('2d');
  const ctxM5 = document.getElementById('chart-m5').getContext('2d');
  const ctxPnL = document.getElementById('chart-pnl').getContext('2d');
  const base = {responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
    scales:{x:{grid:{color:'#1f2937'},ticks:{color:'#9ca3af',maxTicksLimit:6}}, y:{grid:{color:'#1f2937'},ticks:{color:'#9ca3af'}}},
    plugins:{legend:{labels:{color:'#cbd5e1'}}}};
  chartM1 = new Chart(ctxM1, {type:'line', data:{labels:[], datasets:[{label:'Close M1', data:[], borderColor:'#60a5fa', tension:.2, pointRadius:0}]}, options:base});
  chartM5 = new Chart(ctxM5, {type:'line', data:{labels:[], datasets:[{label:'Close M5', data:[], borderColor:'#34d399', tension:.2, pointRadius:0}]}, options:base});
  chartPnL = new Chart(ctxPnL, {type:'bar', data:{labels:[], datasets:[{label:'PnL hoje $', data:[], backgroundColor:['#60a5fa','#34d399','#fbbf24']}]}, options:{...base, scales:{...base.scales, y:{...base.scales.y}}}});
}

async function jget(url){
  const r = await fetch(url, {cache:'no-store'});
  if(!r.ok) throw new Error(url+' '+r.status);
  return r.json();
}

async function refreshStatus(){
  try{
    const s = await jget('/api/status');
    const dot = $('#dot');
    dot.style.background = s.connected ? '#10b981' : '#ef4444';
    dot.style.boxShadow = s.connected ? '0 0 12px #10b981' : '0 0 12px #ef4444';
    $('#conn').textContent = s.connected ? 'EA conectado' : 'EA desconectado';
    $('#conn').className = 'pill '+(s.connected?'ok':'bad');
    $('#uptime').textContent = 'up '+ (s.uptime_h>=1 ? s.uptime_h.toFixed(1)+'h' : Math.round(s.uptime_s)+'s');
    $('#dry').textContent = s.dry_run ? 'DRY-RUN' : 'REAL';
    $('#dry').className = 'pill '+(s.dry_run?'bad':'');
    $('#loop').textContent = 'loop '+s.loop_interval+'s · '+s.trigger_mode;
    $('#now').textContent = new Date(s.now_utc).toLocaleString('pt-BR',{timeZone:'UTC', hour12:false})+' UTC';
  }catch(e){}
}

async function refreshAccount(){
  try{
    const a = await jget('/api/account');
    if(a && a.balance!=null){
      $('#kpi-balance').textContent = '$'+fmt(a.balance,2);
      $('#kpi-equity').textContent = 'Equity $'+fmt(a.equity,2)+' · Livre $'+fmt(a.free_margin,2)+' · Alav '+a.leverage;
    }
    const ticks = await jget('/api/ticks');
    if(ticks.XAUUSD){
      const spr = (ticks.XAUUSD.ask - ticks.XAUUSD.bid);
      $('#kpi-spread').textContent = fmt(spr,2)+' ('+fmt(spr/0.1,1)+' pips)';
      $('#kpi-session').textContent = 'XAU Bid '+fmt(ticks.XAUUSD.bid,2)+' Ask '+fmt(ticks.XAUUSD.ask,2);
    }
  }catch(e){}
}

let _easCache = [];
let _selectedEa = null;

function openEaModal(name){
  const ea = _easCache.find(e=>e.name===name);
  if(!ea) return;
  _selectedEa = ea;
  $('#modal-title').textContent = ea.name;
  $('#modal-subtitle').textContent = `${ea.symbol} ${ea.timeframe} · #${ea.magic} · vol ${fmt(ea.volume,2)} ${ea.enabled?'· ativo':'· inativo'}`;
  $('#modal-ops-title').textContent = ea.name;
  const hist = ea.history || {profit:0,wins:0,losses:0,trades:0};
  $('#modal-history').innerHTML = `<div><strong>${hist.trades||0} trades hoje</strong> · <span style="color:${hist.profit>=0?'#10b981':'#ef4444'}">${fmtMoney(hist.profit)}</span> · ${hist.wins||0}W / ${hist.losses||0}L</div><div class="muted">Última perda: ${hist.last_loss_time||'—'}</div><div class="muted">RR ${ea.params.rr??ea.params.risk_reward_ratio??'—'} · Score ${ea.params.min_score??'—'}/${ea.name.includes('champion')?23:10} · Max ${ea.max_trades_day}/dia</div>`;
  const sig = ea.last_signal ? `${ea.last_signal.direction} entry ${fmt(ea.last_signal.entry,2)} SL ${fmt(ea.last_signal.sl,2)} TP ${fmt(ea.last_signal.tp,2)}\n${ea.last_signal.meta||''}` : 'Sem sinal ainda';
  const llm = ea.last_llm ? ((ea.last_llm.approved?'✅ Aprovado':'⛔ Bloqueado')+' — '+ea.last_llm.motivo) : 'Sem avaliação LLM ainda';
  $('#modal-signal').textContent = `Sinal:\n${sig}\n\nLLM:\n${llm}`;
  $('#modal-config').textContent = JSON.stringify({symbol:ea.symbol,timeframe:ea.timeframe,magic:ea.magic,volume:ea.volume,max_trades_day:ea.max_trades_day,max_losses_day:ea.max_losses_day,params:ea.params}, null, 2);
  // trades do dia filtrados por EA
  refreshModalTrades();
  $('#ea-modal').classList.add('open');
}
function closeEaModal(){ $('#ea-modal').classList.remove('open'); }
async function refreshModalTrades(){
  if(!_selectedEa) return;
  const tbody = document.querySelector('#modal-trades tbody');
  const empty = document.getElementById('modal-trades-empty');
  tbody.innerHTML='';
  try{
    // 1) deals reais do dia vindos do MT5 via EA (GET_HISTORY_DEALS por magic)
    let deals = [];
    try{
      const r = await jget(`/api/history_deals?magic=${_selectedEa.magic}`);
      if(Array.isArray(r) && r.length) deals = r;
    }catch(e){}
    if(deals.length){
      empty.style.display='none';
      for(const d of deals){
        const tr=document.createElement('tr');
        const total = (Number(d.profit)||0) + (Number(d.swap)||0) + (Number(d.commission)||0);
        const col = total>=0 ? '#10b981' : '#ef4444';
        tr.innerHTML = `<td class="small">${d.time}</td><td>${d.type}</td><td class="mono">${d.ticket}</td><td>${fmt(d.price,2)}</td><td>${fmt(d.volume,2)}</td><td>${fmt(d.price,2)}</td><td style="color:${col}">${fmtMoney(total)}</td><td class="small">${d.comment||''}</td>`;
        tbody.appendChild(tr);
      }
      return;
    }
    // 2) fallback: trades desta sessão do bot (quando histórico MT5 ainda vazio)
    const trades = await jget('/api/trades?limit=200');
    const filtered = trades.filter(t=> t.strat===_selectedEa.name);
    if(!filtered.length){ empty.style.display='block'; empty.textContent='Nenhuma operação registrada para este EA hoje (nesta sessão do bot e também nenhuma no histórico MT5).'; return; }
    empty.style.display='none';
    for(const t of filtered){
      const tr=document.createElement('tr');
      const d = new Date(t.time);
      tr.innerHTML = `<td class="small">${d.toLocaleString('pt-BR')}</td><td>${t.direction}</td><td class="mono">${t.ticket}</td><td>${fmt(t.price,2)}</td><td>${fmt(t.volume,2)}</td><td>${fmt(t.sl,2)}</td><td style="color:#9ca3af">sessão bot</td><td class="small">—</td>`;
      tbody.appendChild(tr);
    }
  }catch(e){ empty.style.display='block'; }
}
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeEaModal(); });

let _pollingPausedForSSE = false;
let _sseConnected = false;

async function refreshEAs(){
  if(_pollingPausedForSSE) return; // SSE é fonte da verdade
  try{
    const eas = await jget('/api/eas');
    _easCache = eas;
    renderEAsTable(eas);
  }catch(e){}
}
function renderEAsTable(eas){
  $('#eas-count').textContent = eas.length+' ativo(s)';
  _easCache = eas;
  const tbody = document.querySelector('#eas-tbody') || document.querySelector('#eas-table tbody');
  if(!tbody) return;
  tbody.innerHTML='';
  // somar apenas EAs ativos (smc_xau inativo estava somando 0/0 e escondendo o fato que ambos ativos estão 0 hoje)
  let totalTrades=0, totalPnl=0;
  const pnlLabels=[], pnlData=[];
  for(const ea of eas){
    if(!ea.enabled) continue; // ---- fix: KPIs contam só EAs ativos
    const sig = ea.last_signal ? (ea.last_signal.direction+' '+fmt(ea.last_signal.entry,2)+' score') : '—';
    const sigMeta = ea.last_signal ? (ea.last_signal.meta||'').slice(0,120) : '';
    const llm = ea.last_llm ? ((ea.last_llm.approved?'✅':'⛔')+' '+ea.last_llm.motivo) : '—';
    const hist = ea.history || {trades:0, wins:0, losses:0, profit:0};
    totalTrades += hist.trades||0; totalPnl += hist.profit||0;
    pnlLabels.push(ea.name); pnlData.push(hist.profit||0);
    const tr = document.createElement('tr');
    tr.className = 'clickable';
    tr.title = 'Clique para ver configuração e operações do dia';
    tr.addEventListener('click', ()=> openEaModal(ea.name));
    tr.innerHTML = `<td><span class="tag">${ea.name}</span></td>
      <td><span class="badge ${ea.enabled?'on':'off'}">${ea.enabled?'ativo':'inativo'}</span></td>
      <td>${ea.symbol} / ${ea.timeframe}</td><td class="mono">${ea.magic}</td><td>${fmt(ea.volume,2)}</td>
      <td>${ea.params.rr??ea.params.risk_reward_ratio??'—'}</td>
      <td class="small">${ea.params.min_score!=null? ea.params.min_score : ''} / ${ea.name.includes('champion')?23:10}</td>
      <td class="small"><div>${sig}</div><div class="muted">${sigMeta}</div></td>
      <td class="small">${llm}</td>
      <td class="ticker">${hist.trades||0} trades · <span style="color:${hist.profit>=0?'#10b981':'#ef4444'}">${fmtMoney(hist.profit)}</span> · ${hist.wins||0}W/${hist.losses||0}L</td>`;
    tbody.appendChild(tr);
  }
  // tabela inativos (se houver) abaixo sem contaminar KPIs
  for(const ea of eas){
    if(ea.enabled) continue;
    const tr = document.createElement('tr');
    tr.className = 'clickable';
    tr.style.opacity='0.55';
    tr.addEventListener('click', ()=> openEaModal(ea.name));
    const hist = ea.history || {trades:0, wins:0, losses:0, profit:0};
    tr.innerHTML = `<td><span class="tag">${ea.name}</span></td><td><span class="badge off">inativo</span></td><td>${ea.symbol} / ${ea.timeframe}</td><td class="mono">${ea.magic}</td><td>${fmt(ea.volume,2)}</td><td>${ea.params.rr??'—'}</td><td class="small">${ea.params.min_score??''}/10</td><td class="muted small">—</td><td class="muted small">—</td><td class="ticker muted">${hist.trades||0} trades</td>`;
    tbody.appendChild(tr);
  }
  const tradesEl = document.getElementById('kpi-trades');
  const pnlEl = document.getElementById('kpi-pnl');
  if(tradesEl) tradesEl.textContent = totalTrades ? `${totalTrades} trades hoje` : '0 trades hoje — nenhum fechamento no dia';
  if(pnlEl) pnlEl.textContent = totalPnl ? fmtMoney(totalPnl)+' PnL hoje (ativos)' : '$0.00 PnL hoje';
  if(chartPnL){
    chartPnL.data.labels = pnlLabels;
    chartPnL.data.datasets[0].data = pnlData;
    chartPnL.data.datasets[0].backgroundColor = pnlData.map(v=> v>=0?'#10b981':'#ef4444');
    chartPnL.update('none');
  }
  if(_selectedEa && document.getElementById('ea-modal').classList.contains('open')){
    const updated = _easCache.find(e=>e.name===_selectedEa.name);
    if(updated){ _selectedEa = updated;
      const hist = updated.history || {profit:0,wins:0,losses:0,trades:0};
      document.getElementById('modal-history').innerHTML = `<div><strong>${hist.trades||0} trades hoje</strong> · <span style="color:${hist.profit>=0?'#10b981':'#ef4444'}">${fmtMoney(hist.profit)}</span> · ${hist.wins||0}W / ${hist.losses||0}L</div><div class="muted">Última perda: ${hist.last_loss_time||'—'}</div><div class="muted">RR ${updated.params.rr??updated.params.risk_reward_ratio??'—'} · Score ${updated.params.min_score??'—'}/${updated.name.includes('champion')?23:10} · Max ${updated.max_trades_day}/dia</div>`;
    }
    refreshModalTrades();
  }
}

async function refreshPositions(){
  try{
    const pos = await jget('/api/positions');
    const tbody = $('#pos-table tbody');
    tbody.innerHTML='';
    if(!pos.length){ $('#pos-empty').style.display='block'; return; }
    $('#pos-empty').style.display='none';
    for(const p of pos){
      const tr=document.createElement('tr');
      tr.innerHTML = `<td class="mono">${p.ticket}</td><td>${p.symbol}</td><td>${p.type}</td><td>${fmt(p.volume,2)}</td><td>${fmt(p.open_price,2)}</td><td>${fmt(p.sl,2)} / ${fmt(p.tp,2)}</td><td style="color:${p.profit>=0?'#10b981':'#ef4444'}">${fmtMoney(p.profit)}</td><td class="mono">${p.magic}</td>`;
      tbody.appendChild(tr);
    }
  }catch(e){}
}

async function refreshCalendar(){
  try{
    const evs = await jget('/api/calendar?hours=24');
    const tbody=$('#cal-table tbody');
    tbody.innerHTML='';
    if(!evs.length){ tbody.innerHTML='<tr><td colspan=4 class="muted small">Sem eventos High/Medium nas próximas 24h</td></tr>'; return;}
    for(const e of evs){
      const tr=document.createElement('tr');
      const imp = (e.impact||'');
      const badge = imp.toLowerCase()==='high' ? 'style="color:#ef4444"' : imp.toLowerCase()==='medium' ? 'style="color:#f59e0b"' : '';
      tr.innerHTML = `<td class="small">${e.time}</td><td class="small">${e.title}</td><td class="small" ${badge}>${e.impact}</td><td class="small">${e.currency}</td>`;
      tbody.appendChild(tr);
    }
  }catch(e){}
}

async function refreshChart(symbol, tf, chart){
  try{
    const data = await jget(`/api/chart?symbol=${symbol}&tf=${tf}&count=80`);
    const rates = data.rates||[];
    const labels = rates.map(r=> (r.time||'').slice(11,16));
    const closes = rates.map(r=> r.close);
    chart.data.labels = labels;
    chart.data.datasets[0].data = closes;
    // add position lines
    const pos = await jget('/api/positions');
    // annotate via extra dataset (horizontal line not trivial in Chart.js without plugin; skip)
    chart.update('none');
  }catch(e){}
}

async function refreshTrades(){
  try{
    const trades = await jget('/api/trades?limit=30');
    const tbody=$('#trades-table tbody');
    tbody.innerHTML='';
    for(const t of trades){
      const tr=document.createElement('tr');
      const d = new Date(t.time);
      tr.innerHTML = `<td class="small">${d.toLocaleTimeString('pt-BR')}</td><td><span class="tag">${t.strat}</span></td><td>${t.direction}</td><td class="mono">${t.ticket}</td><td>${fmt(t.price,2)}</td><td>${fmt(t.volume,2)}</td>`;
      tbody.appendChild(tr);
    }
    if(!trades.length) tbody.innerHTML='<tr><td colspan=6 class="muted small">Nenhum trade registrado nesta sessão (histórico do dia vem de /api/history)</td></tr>';
  }catch(e){}
}

function setupSSE(){
  // SSE principal para EAs (HTMX também reage; JS abaixo é fallback robusto)
  try{
    const es = new EventSource('/api/stream');
    const badge = document.getElementById('sse-badge');
    es.addEventListener('open', ()=>{ _sseConnected=true; _pollingPausedForSSE=true; if(badge){ badge.textContent='SSE ● live'; badge.style.color='#10b981'; badge.style.borderColor='rgba(16,185,129,.4)'; }});
    es.addEventListener('error', ()=>{ _sseConnected=false; _pollingPausedForSSE=false; if(badge){ badge.textContent='SSE ○ polling'; badge.style.color='#9ca3af'; }});
    es.addEventListener('eas_json', (ev)=>{
      try{
        const eas = JSON.parse(ev.data);
        renderEAsTable(eas);
      }catch(e){}
    });
    es.addEventListener('eas', (ev)=>{
      // HTMX já faz swap do tbody, mas garantimos fallback para eas_json
    });
    // positions via SSE (evita polling positions 2.5s)
    es.addEventListener('positions', (ev)=>{
      try{
        const pos = JSON.parse(ev.data);
        const tbody = document.querySelector('#pos-table tbody');
        const empty = document.getElementById('pos-empty');
        if(!tbody) return;
        tbody.innerHTML='';
        if(!pos.length){ empty.style.display='block'; return; }
        empty.style.display='none';
        for(const p of pos){
          const tr=document.createElement('tr');
          tr.innerHTML = `<td class="mono">${p.ticket}</td><td>${p.symbol}</td><td>${p.type}</td><td>${(p.volume).toFixed(2)}</td><td>${(p.open_price).toFixed(2)}</td><td>${(p.sl).toFixed(2)} / ${(p.tp).toFixed(2)}</td><td style="color:${p.profit>=0?'#10b981':'#ef4444'}">${fmtMoney(p.profit)}</td><td class="mono">${p.magic}</td>`;
          tbody.appendChild(tr);
        }
      }catch(e){}
    });
  }catch(e){}
}

function startPolling(){
  refreshStatus(); refreshAccount(); refreshEAs(); refreshPositions(); refreshCalendar(); refreshTrades();
  refreshChart('XAUUSD','M1', chartM1); refreshChart('XAUUSD','M5', chartM5);
  setInterval(refreshStatus, 2000);
  setInterval(refreshAccount, 5000);
  setInterval(refreshEAs, 4000);
  setInterval(refreshPositions, 2500);
  setInterval(refreshCalendar, 30000);
  setInterval(refreshTrades, 5000);
  setInterval(()=>refreshChart('XAUUSD','M1', chartM1), 10000);
  setInterval(()=>refreshChart('XAUUSD','M5', chartM5), 15000);
  setupSSE();
}

document.addEventListener('DOMContentLoaded', ()=>{ initCharts(); startPolling(); });

// HTMX SSE swap: quando hx-ext sse recebe evento "eas", injetar direto no tbody
document.addEventListener('htmx:sseMessage', function(e){
  if(e.detail && e.detail.type==='eas'){
    const tbody = document.getElementById('eas-tbody');
    if(tbody) tbody.innerHTML = e.detail.data;
  }
});
</script>
</body>
</html>
"""

async def index_handler(request: web.Request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")

def create_app():
    app = web.Application()
    app.add_routes([
        web.get("/", index_handler),
        web.get("/api/status", api_status),
        web.get("/api/eas", api_eas),
        web.get("/api/positions", api_positions),
        web.get("/api/history", api_history),
        web.get("/api/history_deals", api_history_deals),
        web.get("/api/account", api_account),
        web.get("/api/calendar", api_calendar),
        web.get("/api/ticks", api_ticks),
        web.get("/api/trades", api_trades),
        web.get("/api/chart", api_chart),
        web.get("/api/logs", api_logs),
        web.get("/api/stream", api_stream),
    ])
    # CORS para acesso externo
    async def cors_handler(request, handler):
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @web.middleware
    async def cors_mw(request, handler):
        try:
            resp = await handler(request)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
            return resp
        except Exception as e:
            raise

    app.middlewares.append(cors_mw)

    async def health(request):
        return web.json_response({"ok": True})

    app.add_routes([web.get("/health", health)])
    return app


class DashboardServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 5556):
        self.host = host
        self.port = port
        self.runner: web.AppRunner | None = None

    async def start(self):
        app = create_app()
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        logger.info(f"Dashboard em http://{self.host}:{self.port} — API /api/*")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("Dashboard parado")
