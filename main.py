"""
Polymarket 5-Minute BTC Sniper Bot

Two async processes run concurrently:
  - Process A (Sniper): Observes BTC price, applies entry rules, buys in last 40s
  - Process B (Harvester): Claims winning positions on resolved markets

Supports both USDC native and USDC.e:
  - Auto-swaps USDC native -> USDC.e at startup if needed
  - Polymarket uses USDC.e as collateral

Usage:
    1. Copy .env.example to .env and configure
    2. python -m contracts.allowances  (once per wallet)
    3. python main.py
"""

import asyncio
import os
import signal
import sys
import time

from balance_guard import BalanceGuard
from config import CHAIN_ID, CLOB_HOST, SIGNATURE_TYPE, BotConfig
from learning_engine import LearningEngine
from live_optimizer import LiveOptimizer
from logger import log
from market_manager import MarketManager
from paths import DATA_DIR, HEARTBEAT_FILE
from price_analyzer import PriceAnalyzer
from profit_taker import ProfitTaker
from sniper import Sniper
from wallet_manager import WalletManager


HEARTBEAT_INTERVAL = 30  # secondes ; le healthcheck Docker tolère 180 s


async def heartbeat() -> None:
    """Écrit l'heure courante dans HEARTBEAT_FILE tant que la boucle asyncio tourne.

    Le healthcheck Docker vérifie que ce fichier a moins de 180 s : il détecte un
    bot arrêté comme une boucle bloquée (le heartbeat ne s'écrit plus).
    """
    while True:
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            log.warning("Heartbeat: écriture impossible (%s): %s", HEARTBEAT_FILE, e)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def main() -> None:
    config = BotConfig.from_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Fichiers d'état: %s", DATA_DIR.resolve())

    # Learning engine partagé entre sniper et harvester
    learning_engine = LearningEngine()

    if config.paper_trading:
        log.info("=" * 50)
        log.info("  MODE: PAPER TRADING (simulation)")
        log.info("  Paper balance: $%.2f", config.paper_balance)
        log.info("=" * 50)

        market_manager = MarketManager(config)
        price_analyzer = PriceAnalyzer(config)
        wallet_manager = WalletManager(config)
        sniper = Sniper(config, market_manager, price_analyzer, wallet_manager,
                        learning_engine=learning_engine)
        w3 = None
        clob_client = None
    else:
        log.info("MODE: LIVE TRADING")
        log.info("Wallet: %s...%s", config.wallet_address[:8], config.wallet_address[-4:])

        from py_clob_client.client import ClobClient
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        # Web3
        w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not w3.is_connected():
            log.error("Cannot connect to Polygon RPC: %s", config.polygon_rpc_url)
            sys.exit(1)
        log.info("Web3 connected to Polygon (chain %d)", w3.eth.chain_id)

        # Auto-swap USDC native -> USDC.e if needed
        from contracts.swap import get_native_usdc_balance, get_usdc_e_balance, swap_native_usdc_to_usdc_e

        wallet = config.wallet_address
        native_bal = get_native_usdc_balance(w3, wallet) / 1e6
        usdc_e_bal = get_usdc_e_balance(w3, wallet) / 1e6
        log.info("USDC.e: $%.2f | USDC native: $%.2f", usdc_e_bal, native_bal)

        if native_bal > 1.0:
            log.info("Auto-swapping USDC native -> USDC.e...")
            success = swap_native_usdc_to_usdc_e(w3, config.private_key, wallet)
            if success:
                usdc_e_bal = get_usdc_e_balance(w3, wallet) / 1e6
                log.info("Swap done! New USDC.e balance: $%.2f", usdc_e_bal)
            else:
                log.error("Swap failed. Check logs above.")
                sys.exit(1)

        # CLOB client
        clob_client = ClobClient(
            host=CLOB_HOST,
            key=config.private_key,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
        )
        clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
        log.info("CLOB client initialized")

        # Components
        market_manager = MarketManager(config)
        price_analyzer = PriceAnalyzer(config)
        wallet_manager = WalletManager(config, clob_client, w3)
        sniper = Sniper(config, market_manager, price_analyzer, wallet_manager,
                        clob_client, w3, learning_engine=learning_engine)

    # Log balance
    balance = await wallet_manager.get_usdc_balance(force_refresh=True)
    log.info("Trading balance (USDC.e): $%.2f", balance)

    # Tasks
    tasks_to_run = []

    # Harvester only in live mode
    if not config.paper_trading and w3 is not None:
        from harvester import Harvester
        harvester = Harvester(config, market_manager, wallet_manager, w3,
                              learning_engine=learning_engine)
        tasks_to_run.append(asyncio.create_task(harvester.run(), name="harvester"))

        # Profit Taker : virement automatique vers le wallet personnel
        personal_wallet = os.environ.get("PERSONAL_WALLET", "")
        if personal_wallet:
            profit_taker = ProfitTaker(
                config=config,
                w3=w3,
                personal_wallet=personal_wallet,
                trigger_balance=float(os.environ.get("PROFIT_TRIGGER_BALANCE", "100.0")),
                transfer_amount=float(os.environ.get("PROFIT_TRANSFER_AMOUNT", "50.0")),
                trading_reserve=float(os.environ.get("PROFIT_TRADING_RESERVE", "40.0")),
            )
            tasks_to_run.append(asyncio.create_task(profit_taker.run(), name="profit_taker"))
            log.info("ProfitTaker enabled — trigger=$%.0f, transfer=$%.0f",
                     profit_taker._trigger_balance, profit_taker._transfer_amount)
        else:
            log.info("ProfitTaker disabled — set PERSONAL_WALLET in env to enable")

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
    log.info("POLYMARKET 5-MIN BTC SNIPER BOT v6 RUNNING")
    log.info("  Sizing: Kelly (fraction=%.0f%%, max=%.0f%%)",
             config.kelly_fraction * 100, config.max_kelly_size * 100)
    log.info("  Max buy price: $%.2f (min yield ~%.0f%%)",
             config.max_buy_price,
             ((1 - config.max_buy_price) / config.max_buy_price) * 100)
    log.info("  Entry window: last %ds", config.entry_window_seconds)
    log.info("  Ask buffer: +$%.2f | Order type: %s", config.ask_buffer, config.order_type)
    if "FILL_BUFFER" in os.environ:
        log.warning("FILL_BUFFER est ignoré (il n'a jamais été appliqué) : "
                    "utiliser ASK_BUFFER (défaut $0.02 au-dessus du meilleur vendeur)")
    log.info("  Signals: OBI + Momentum + Volume + RSI + VWAP + TradeRate")
    log.info("=" * 50)

    tasks_to_run.append(asyncio.create_task(sniper.run(), name="sniper"))
    tasks_to_run.append(asyncio.create_task(shutdown_event.wait(), name="shutdown"))
    tasks_to_run.append(asyncio.create_task(heartbeat(), name="heartbeat"))

    # Process D: Balance Guard (live mode seulement)
    if not config.paper_trading and w3 is not None:
        balance_guard = BalanceGuard(
            wallet_address=config.wallet_address,
            w3=w3,
            shutdown_event=shutdown_event,
            check_interval=60,
        )
        tasks_to_run.append(asyncio.create_task(balance_guard.run(), name="balance_guard"))
        log.info("BalanceGuard (Process D) enabled — arrêt automatique si solde < $0.50")

    # Process E: Live Optimizer (optimisation interne toutes les 30 minutes)
    live_optimizer = LiveOptimizer(learning_engine=learning_engine)
    tasks_to_run.append(asyncio.create_task(live_optimizer.run(), name="live_optimizer"))
    log.info("LiveOptimizer (Process E) enabled — recalibration toutes les 30min")

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

        if config.paper_trading and sniper._paper_trades:
            log.info("=" * 50)
            log.info("PAPER TRADING SUMMARY")
            log.info("  Total trades: %d", len(sniper._paper_trades))
            total_bet = sum(t["amount"] for t in sniper._paper_trades)
            total_potential = sum(t["potential_payout"] for t in sniper._paper_trades)
            log.info("  Total wagered: $%.2f", total_bet)
            log.info("  Total potential payout: $%.2f", total_potential)
            log.info("  Final balance: $%.2f", await wallet_manager.get_usdc_balance())
            log.info("=" * 50)

        log.info("Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
