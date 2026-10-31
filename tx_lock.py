"""Verrou partagé pour les transactions signées par le wallet de trading.

Les appels web3 tournent dans des threads (asyncio.to_thread) pour ne pas bloquer
la boucle du Sniper. Le Harvester et le ProfitTaker lisent le nonce « pending »
puis envoient une transaction : sans verrou, deux envois simultanés pourraient
réutiliser le même nonce. Toute transaction du wallet passe donc par ce verrou.
"""

import asyncio

WALLET_TX_LOCK = asyncio.Lock()
