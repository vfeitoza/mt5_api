"""Entrypoint: python bot.py --config config/config.yaml [--dry-run] [--hot-reload]"""

from __future__ import annotations
import argparse, asyncio, logging, os, signal, sys
from pathlib import Path

from mt5_bridge import MT5Bridge
from core.config import load_config
from core.engine import Engine
from core.dashboard import DashboardServer, set_dashboard_refs, record_signal, record_llm, record_trade

def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

# --- hot-reload: stdlib puro, sem dependência externa ---
def _collect_watch_files(config_path: str) -> list[Path]:
    base = Path(__file__).parent
    candidates: list[Path] = []
    for sub in ("mt5_bridge", "core", "ops"):
        d = base / sub
        if d.is_dir():
            candidates.extend(d.rglob("*.py"))
    candidates.append(base / "bot.py")
    cp = Path(config_path)
    if not cp.is_absolute():
        cp = (Path.cwd() / cp).resolve()
    # também observa config.yaml montado no container (/app/config/config.yaml)
    for p in (cp, Path("/app/config/config.yaml")):
        if p not in candidates and p.exists():
            candidates.append(p)
    # dedup
    seen = set()
    out: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp not in seen and rp.exists():
            seen.add(rp)
            out.append(rp)
    return out

async def _watch_and_reload(config_path: str, bridge: MT5Bridge, stop_event: asyncio.Event):
    log = logging.getLogger("hot-reload")
    watch = _collect_watch_files(config_path)
    log.info(f"Hot-reload ativo observando {len(watch)} arquivos (poll 1s)")
    mtimes: dict[Path, float] = {}
    for p in watch:
        try:
            mtimes[p] = p.stat().st_mtime
        except Exception:
            pass
    while not stop_event.is_set():
        await asyncio.sleep(1.0)
        changed: list[Path] = []
        # re-coletar para pegar novos arquivos (novo operacional)
        current = _collect_watch_files(config_path)
        for p in current:
            try:
                mt = p.stat().st_mtime
            except FileNotFoundError:
                continue
            prev = mtimes.get(p)
            if prev is None:
                mtimes[p] = mt
                # novo arquivo -> recarregar
                changed.append(p)
            elif mt != prev:
                mtimes[p] = mt
                changed.append(p)
        # remover deletados
        for p in list(mtimes.keys()):
            if not p.exists():
                mtimes.pop(p, None)
        if changed:
            names = ", ".join(pp.name for pp in changed[:6])
            more = f" +{len(changed)-6}" if len(changed) > 6 else ""
            log.warning(f"Alteração detectada ({names}{more}) — reiniciando bot em 0.5s (sem rebuild)...")
            await asyncio.sleep(0.5)
            # restart do processo: preserva args, troca imagem em memória
            try:
                await bridge.stop()
            except Exception:
                pass
            # flush logs
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:
                    pass
            os.execv(sys.executable, [sys.executable] + sys.argv)

async def main():
    ap = argparse.ArgumentParser(description="MT5 Multi-Operacional Bot (SMC + LLM gate)")
    ap.add_argument("--config", default="config/config.yaml", help="YAML config path")
    ap.add_argument("--dry-run", action="store_true", help="não envia ordens")
    ap.add_argument("--hot-reload", action="store_true", help="recarrega auto ao alterar .py/config (stdlib poll, sem rebuild)")
    args = ap.parse_args()

    # env var também ativa: HOT_RELOAD=1 (padrão no docker-compose)
    hot_reload = args.hot_reload or os.getenv("HOT_RELOAD", "0").lower() in ("1", "true", "yes", "on")

    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True
    setup_logging(cfg.log_level)
    log = logging.getLogger("bot")
    if hot_reload:
        log.info("Hot-reload habilitado — edite ops/*.py ou config.yaml e o bot recarrega sozinho")

    bridge = MT5Bridge(host=cfg.bridge.host, port=cfg.bridge.port)
    await bridge.start()
    log.info(f"Bridge escutando {cfg.bridge.host}:{cfg.bridge.port} — aguardando EA conectar")

    # Dashboard 5556 — profissional, polling, gráficos
    dashboard = DashboardServer(host=cfg.dashboard.host, port=cfg.dashboard.port)
    if cfg.dashboard.enabled:
        try:
            await dashboard.start()
        except Exception as e:
            log.warning(f"Dashboard não subiu em {cfg.dashboard.port}: {e}")
    else:
        log.info("Dashboard desabilitado (dashboard.enabled=false)")
    try:
        await bridge.wait_for_connection(timeout=120)
    except TimeoutError:
        log.error("EA não conectou em 120s. Verifique MT5SocketClient.mq5 ServerAddress/Port.")
        await bridge.stop()
        sys.exit(1)
    log.info("EA conectado!")

    engine = Engine(bridge, cfg)
    # injetar refs para dashboard
    set_dashboard_refs(engine=engine, bridge=bridge, config=cfg)

    # graceful shutdown
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    task = asyncio.create_task(engine.run_forever())
    watcher: asyncio.Task | None = None
    if hot_reload:
        watcher = asyncio.create_task(_watch_and_reload(args.config, bridge, stop))
    await stop.wait()
    task.cancel()
    if watcher:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        await dashboard.stop()
    except Exception:
        pass
    await bridge.stop()
    log.info("Bot parado.")

if __name__ == "__main__":
    asyncio.run(main())
