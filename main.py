"""
Polymarket 5-Minute BTC Sniper Bot

Two async processes run concurrently:
  - Process A (Sniper): Observes BTC price, applies entry rules, buys in last 40s
  - Process B (Harvester): Claims winning positions on resolved markets

Modes:
  - PAPER_TRADING=true  -> simulates trades, no real money
  - PAPER_TRADING=false -> live trading (requires PRIVATE_KEY + WALLET_ADDRESS)

Usage:
    1. Copy .env.example to .env and configure
    2. python main.py
"""

import asyncio
import signal
import sys

from config import CHAIN_ID, CLOB_HOST, SIGNATURE_TYPE, BotConfig
from logger import log
from market_manager import MarketManager
from price_analyzer import PriceAnalyzer
from sniper import Sniper
from wallet_manager import WalletManager


async def main() -> None:
    config = BotConfig.from_env()

    if config.paper_trading:
        log.info("=" * 50)
        log.info("  MODE: PAPER TRADING (simulation)")
        log.info("  Paper balance: $%.2f", config.paper_balance)
        log.info("=" * 50)

        clob_client = None
        w3 = None
    else:
        log.info("MODE: LIVE TRADING")
        log.info("Wallet: %s...%s", config.wallet_address[:8], config.wallet_address[-4:])

        from py_clob_client.client import ClobClient
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        clob_client = ClobClient(
            host=CLOB_HOST,
            key=config.private_key,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
        )
        clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
        log.info("CLOB client initialized")

        w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not w3.is_connected():
            log.error("Cannot connect to Polygon RPC: %s", config.polygon_rpc_url)
            sys.exit(1)
        log.info("Web3 connected to Polygon (chain %d)", w3.eth.chain_id)

    # Shared components
    market_manager = MarketManager(config)
    price_analyzer = PriceAnalyzer(config)
    wallet_manager = WalletManager(config, clob_client)

    balance = await wallet_manager.get_usdc_balance(force_refresh=True)
    log.info("USDC balance: $%.2f", balance)

    # Processes
    sniper = Sniper(config, market_manager, price_analyzer, wallet_manager, clob_client)

    tasks_to_run = []

    # Harvester only in live mode
    if not config.paper_trading and w3 is not None:
        from harvester import Harvester
        harvester = Harvester(config, market_manager, wallet_manager, w3)
        tasks_to_run.append(asyncio.create_task(harvester.run(), name="harvester"))

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig, _frame):
        log.info("Received signal %s, shutting down...", sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start
    await price_analyzer.start()

    log.info("=" * 50)
    log.info("POLYMARKET 5-MIN BTC SNIPER BOT RUNNING")
    log.info("  Position size: %.0f%% of balance", config.position_size_fraction * 100)
    log.info("  Max buy price: $%.2f (min yield ~%.0f%%)",
             config.max_buy_price,
             ((1 - config.max_buy_price) / config.max_buy_price) * 100)
    log.info("  Entry window: last %ds", config.entry_window_seconds)
    log.info("=" * 50)

    tasks_to_run.append(asyncio.create_task(sniper.run(), name="sniper"))
    tasks_to_run.append(asyncio.create_task(shutdown_event.wait(), name="shutdown"))

    try:
        done, pending = await asyncio.wait(tasks_to_run, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        await price_analyzer.stop()
        await market_manager.close()

        # Print paper trading summary
        if config.paper_trading and sniper._paper_trades:
            log.info("=" * 50)
            log.info("PAPER TRADING SUMMARY")
            log.info("  Total trades: %d", len(sniper._paper_trades))
            total_bet = sum(t["amount"] for t in sniper._paper_trades)
            total_potential = sum(t["potential_payout"] for t in sniper._paper_trades)
            log.info("  Total wagered: $%.2f", total_bet)
            log.info("  Total potential payout: $%.2f", total_potential)
            log.info("  Final balance: $%.2f", await wallet_manager.get_usdc_balance())
            for t in sniper._paper_trades:
                log.info(
                    "    %s %s @ $%.3f | bet $%.2f | %.1f shares",
                    t["window"], t["side"], t["price"], t["amount"], t["shares"],
                )
            log.info("=" * 50)

        log.info("Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
