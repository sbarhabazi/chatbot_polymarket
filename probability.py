"""Modèle de probabilité et frais réels pour les marchés BTC Up/Down 5 minutes.

Le prix final suit une marche aléatoire de volatilité σ (dollars par √seconde),
mesurée sur les dernières minutes. On en déduit P(final ≥ priceToBeat), puis la
valeur d'un achat au prix du carnet, frais Polymarket compris :

    EV par part = P(gain) − prix − frais(prix)
    frais(prix)  = 0,07 × prix × (1 − prix)     (taker, catégorie Crypto)

Les ordres ne sont passés que si EV ≥ MIN_EDGE. Ce modèle remplace la « table
de confiance » et ses bonus d'indicateurs : mesurée le 2026-11-08 sur 103
trades réels, cette confiance ne prédisait rien (44 % de gains quand elle
annonçait plus de 90 %).
"""

from __future__ import annotations

import math

TAKER_FEE_RATE = 0.07
TWAP_WINDOW = 60.0
PROB_FLOOR, PROB_CAP = 0.03, 0.97  # queues épaisses : ne jamais se croire sûr à 99 %


def taker_fee(price: float, rate: float = TAKER_FEE_RATE) -> float:
    """Frais taker par part (docs Polymarket : fee = C × feeRate × p × (1 − p))."""
    if price <= 0 or price >= 1:
        return 0.0
    return rate * price * (1.0 - price)


def cost_per_share(price: float) -> float:
    return price + taker_fee(price)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def final_distribution(
    current: float,
    seconds_left: float,
    sigma: float,
    *,
    twap: bool,
    observed_window_avg: float | None = None,
    basis_noise: float = 0.0,
) -> tuple[float, float]:
    """Moyenne et écart-type du prix final.

    - Spot (twap=False) : final = prix à la clôture ; variance σ²·t.
    - TWAP 60 s (twap=True) : final = moyenne des 60 dernières secondes.
      t ≥ 60 s : moyenne = prix actuel, variance σ²·(t − 60 + 60/3).
      t < 60 s : une partie de la fenêtre est déjà observée (moyenne
      `observed_window_avg` sur 60 − t secondes) ; le reste, futur, a une
      variance σ²·t/3 et pèse t/60.
    `basis_noise` ajoute une erreur indépendante (Binance ≠ Chainlink).
    """
    t = max(0.0, seconds_left)
    if not twap:
        mean, var = current, sigma ** 2 * t
    elif t >= TWAP_WINDOW:
        mean, var = current, sigma ** 2 * (t - TWAP_WINDOW + TWAP_WINDOW / 3.0)
    else:
        w_future = t / TWAP_WINDOW
        past = observed_window_avg if observed_window_avg is not None else current
        mean = (1.0 - w_future) * past + w_future * current
        var = sigma ** 2 * (w_future ** 2) * t / 3.0
    return mean, math.sqrt(var + basis_noise ** 2)


def prob_up(mean: float, sd: float, price_to_beat: float) -> float:
    """P(final ≥ priceToBeat), bornée à [3 %, 97 %]."""
    if sd <= 0:
        p = 1.0 if mean >= price_to_beat else 0.0
    else:
        p = normal_cdf((mean - price_to_beat) / sd)
    return min(PROB_CAP, max(PROB_FLOOR, p))


def edge(prob_win: float, ask: float) -> float:
    """Espérance de gain par part achetée à `ask`, frais compris."""
    return prob_win - cost_per_share(ask)


def kelly_fraction(prob_win: float, ask: float) -> float:
    """Fraction de Kelly complète du capital à engager (0 si EV ≤ 0)."""
    c = cost_per_share(ask)
    if c <= 0 or c >= 1:
        return 0.0
    b = (1.0 - c) / c
    f = (b * prob_win - (1.0 - prob_win)) / b
    return max(0.0, f)
