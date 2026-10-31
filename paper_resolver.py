"""Paper Resolver — résultats des trades simulés (mode papier uniquement).

En réel, le Harvester enregistre gain ou perte au moment du redeem. En papier,
rien ne le faisait : les trades simulés restaient « en attente » pour toujours
et le mode papier ne mesurait rien. Ce processus :

  - toutes les 30 s, prend les trades papier en attente dont la fenêtre est finie ;
  - lit le marché sur Gamma ; une fois résolu (outcomePrices 1/0), compare le
    côté joué au côté gagnant ;
  - enregistre le résultat dans le LearningEngine (trade_history.json) et
    crédite le solde papier du montant qu'aurait rapporté le redeem.

Les statistiques se lisent avec `python scripts/paper_report.py`.
"""

from __future__ import annotations

import asyncio
import time

from learning_engine import LearningEngine
from logger import log
from market_manager import MarketManager
from models import MarketInfo
from wallet_manager import WalletManager, paper_payout
from config import MARKET_INTERVAL_SECONDS

POLL_INTERVAL = 30            # secondes entre deux passages
RESOLUTION_GRACE = 60         # attendre 1 min après la fin de fenêtre
GIVE_UP_AFTER = 24 * 3600     # trade non résolu après 24 h : abandonné


def resolved_winner(market: MarketInfo | None) -> str | None:
    """Côté gagnant d'un marché résolu ou fermé avec des prix 1/0, sinon None."""
    if market is None or not (market.is_resolved or market.is_closed):
        return None
    if market.up_price >= 0.99 and market.down_price <= 0.01:
        return "Up"
    if market.down_price >= 0.99 and market.up_price <= 0.01:
        return "Down"
    return None


class PaperResolver:
    def __init__(self, market_manager: MarketManager, learning_engine: LearningEngine,
                 wallet_manager: WalletManager):
        self._mm = market_manager
        self._le = learning_engine
        self._wm = wallet_manager

    async def run(self) -> None:
        log.info("PaperResolver started — résultats des trades papier toutes les %ds",
                 POLL_INTERVAL)
        while True:
            try:
                await self.resolve_pending()
            except Exception as e:
                log.error("PaperResolver error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def resolve_pending(self, now: float | None = None) -> int:
        """Résout les trades papier prêts. Retourne le nombre de trades résolus."""
        now = time.time() if now is None else now
        resolved = 0
        for trade in self._le.pending_trades(source="paper"):
            window_ts = trade["window_ts"]
            window_end = window_ts + MARKET_INTERVAL_SECONDS
            if now < window_end + RESOLUTION_GRACE:
                continue
            if now > window_end + GIVE_UP_AFTER:
                log.warning("PaperResolver: window %d jamais résolue après 24 h — abandon",
                            window_ts)
                self._le.discard_pending_trade(window_ts)
                continue

            winner = resolved_winner(await self._mm.get_market_by_window_ts(window_ts))
            if winner is None:
                continue  # pas encore résolu : réessayer au prochain passage

            cost = float(trade["amount_usdc"])
            shares = round(cost / trade["share_price"]) if trade["share_price"] > 0 else 0
            won = trade["side"] == winner
            payout = paper_payout(shares, cost) if won else 0.0
            if payout:
                self._wm.paper_add(payout)
            self._le.record_result(window_ts, won=won, payout=payout)
            resolved += 1
            log.info(
                "PAPER %s: window %d | joué %s, gagnant %s | coût $%.2f → reçu $%.2f "
                "(P&L %+.2f) | solde papier $%.2f",
                "WIN" if won else "LOSS", window_ts, trade["side"], winner, cost, payout,
                payout - cost, await self._wm.get_usdc_balance(),
            )
        return resolved
