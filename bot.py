"""Entrypoint: python bot.py --config config/config.yaml [--dry-run]"""

from __future__ import annotations
import argparse, asyncio, logging, signal, sys
from pathlib import Path

from mt5_bridge import MT5Bridge
from core.config import load_config
from core.engine import Engine

def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

async def main():
    ap = argparse.ArgumentParser(description="MT5 Multi-Operacional Bot (SMC + LLM gate)")
    ap.add_argument("--config", default="config/config.yaml", help="YAML config path")
    ap.add_argument("--dry-run", action="store_true", help="não envia ordens")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True
    setup_logging(cfg.log_level)
    log = logging.getLogger("bot")

    bridge = MT5Bridge(host=cfg.bridge.host, port=cfg.bridge.port)
    await bridge.start()
    log.info(f"Bridge escutando {cfg.bridge.host}:{cfg.bridge.port} — aguardando EA conectar")
    try:
        await bridge.wait_for_connection(timeout=120)
    except TimeoutError:
        log.error("EA não conectou em 120s. Verifique MT5SocketClient.mq5 ServerAddress/Port.")
        await bridge.stop()
        sys.exit(1)
    log.info("EA conectado!")

    engine = Engine(bridge, cfg)

    # graceful shutdown
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    task = asyncio.create_task(engine.run_forever())
    await stop.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await bridge.stop()
    log.info("Bot parado.")

if __name__ == "__main__":
    asyncio.run(main())
