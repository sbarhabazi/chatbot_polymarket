# Polymarket 5-Min BTC Sniper Bot

Bot de trading automatisé pour les marchés "Bitcoin Up or Down - 5 Minutes" de Polymarket sur la blockchain Polygon.

## Statut

- **Déployé sur Railway** (projet `resplendent-magic`) — actif 24h/24
- **Source de prix** : Kraken WS (fallback automatique depuis Binance, geo-bloqué sur Railway)
- **Blockchain** : Polygon Mainnet

## Fonctionnement

Le bot analyse le prix du BTC en temps réel et place des paris sur Polymarket quand les conditions sont favorables.

### Architecture

Deux processus asynchrones tournent en parallèle :

- **Process A (Sniper)** : Observe le prix BTC, analyse la distance entre le prix actuel et le prix d'ouverture, et place un ordre quand la confiance est suffisante.
- **Process B (Harvester)** : Surveille les marchés résolus et réclame automatiquement les gains sur la blockchain Polygon.

### Stratégie "Safe Scalper v5 — Composite Signal"

1. **Observation** : Le bot écoute le flux de prix BTC seconde par seconde
2. **Prix de référence** : Capturé via l'API Gamma/Polymarket (finalPrice de la fenêtre précédente) ou via le flux live en approximation
3. **Entrée anticipée** : Le bot entre dans les 60-120 secondes restantes
4. **Modèle de confiance** : Table 2D calibrée (distance BTC × temps restant) + 7 indicateurs composites
5. **Sizing dynamique** : Critère de Kelly fractionné (fraction=50%, cap=20%)
6. **Ordres FOK** : Fill-Or-Kill pour éviter les ordres en suspens

### Indicateurs composites (v5)

| # | Indicateur | Impact |
|---|---|---|
| 1 | Distance confidence (table 2D) | Base |
| 2 | Order Book Imbalance (OBI) | +0.08 / -0.10 |
| 3 | Multi-TF Momentum (10s/30s/60s) | +0.05 / -0.08 |
| 4 | Volume Acceleration (5s/15s/30s/60s) | +0.04 / -0.04 |
| 5 | RSI (fast, 14 périodes sur ticks) | +0.03 / -0.04 |
| 6 | VWAP déviation | +0.03 / -0.03 |
| 7 | Trade Rate Spike | +0.03 / -0.03 |
| 8 | Volatilité graduée | 0 / -0.03 / -0.06 / SKIP |

## Structure du projet

```
chatbot_polymarket/
  main.py                  # Point d'entrée / orchestrateur async
  config.py                # BotConfig + URLs des sources de prix
  models.py                # MarketInfo, PriceSnapshot, PriceSignal
  logger.py                # Logging structuré
  market_manager.py        # Découverte marché 5min via Gamma API
  price_analyzer.py        # Multi-source BTC (Binance → Kraken → Coinbase → CoinGecko)
  sniper.py                # Process A : analyse + ordre FOK/GTC
  harvester.py             # Process B : redeem on-chain (CTF)
  wallet_manager.py        # Balance USDC + position sizing Kelly
  monitor.py               # Analyse logs toutes les heures, healthcheck
  optimizer.py             # Ajustements automatiques des paramètres
  start.sh                 # Lance bot + monitor en parallèle
  Dockerfile               # Image Python 3.11 slim, user non-root
  railway.toml             # Config Railway (restart automatique)
  contracts/
    __init__.py
    addresses.py           # Adresses Polygon (CTF, USDC, Chainlink, Uniswap)
    abis.py                # ABI fragments
    allowances.py          # Setup one-shot des approbations (7 txs)
    swap.py                # Swap USDC natif → USDC.e via Uniswap V3
  .env.example             # Template de configuration
  requirements.txt         # Dépendances Python
```

## Sources de prix BTC (fallback automatique)

Le bot essaie les sources dans l'ordre suivant :

| Priorité | Source | Type | Latence |
|---|---|---|---|
| 1 | **Binance** | WebSocket | ~10ms |
| 2 | **Kraken** | WebSocket | ~15ms |
| 3 | **Coinbase** | WebSocket | ~20ms |
| 4 | **CoinGecko** | REST (polling 2s) | ~2s |

> Note : Binance est geo-bloqué (HTTP 451) depuis les serveurs Railway US. Le bot utilise automatiquement Kraken comme source principale.

## Prérequis

- Python 3.10+
- Un wallet Polygon avec :
  - USDC.e (capital de trading, minimum $20 recommandé)
  - POL (frais de gas, ~1 POL suffisant)

## Installation

```bash
git clone https://github.com/sbarhabazi/chatbot_polymarket.git
cd chatbot_polymarket
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

```bash
cp .env.example .env
chmod 600 .env
```

Remplir `.env` :

```
PAPER_TRADING=false
PRIVATE_KEY=0xVOTRE_CLE_PRIVEE
WALLET_ADDRESS=0xVOTRE_ADRESSE
```

### Setup des approbations blockchain (une seule fois)

```bash
python -m contracts.allowances
```

Envoie 7 transactions d'approbation sur Polygon (USDC.e + CTF + Uniswap).

## Déploiement Railway (recommandé)

1. Connecter le repo GitHub à Railway
2. Ajouter les variables d'environnement dans l'onglet **Variables**
3. Railway détecte le `Dockerfile` et déploie automatiquement
4. Chaque `git push` sur `main` déclenche un redéploiement automatique

### Variables d'environnement Railway

| Variable | Description |
|---|---|
| `PAPER_TRADING` | `false` pour le live |
| `PRIVATE_KEY` | Clé privée du wallet |
| `WALLET_ADDRESS` | Adresse du wallet |
| `POLYGON_RPC_URL` | RPC Polygon (défaut: publicnode) |
| `MIN_BTC_DISTANCE` | Distance min BTC (défaut: 12.0) |
| `HIGH_CONFIDENCE_THRESHOLD` | Seuil confiance HIGH (défaut: 0.72) |
| `MEDIUM_CONFIDENCE_THRESHOLD` | Seuil confiance MEDIUM (défaut: 0.60) |
| `HIGH_CONFIDENCE_SIZE` | Taille position HIGH (défaut: 0.30) |
| `MEDIUM_CONFIDENCE_SIZE` | Taille position MEDIUM (défaut: 0.20) |

## Monitoring et optimisation

### Logs en temps réel
```bash
# Railway : onglet Deployments → View Logs
# Local :
tail -f bot.log
```

### Rapport de performance (toutes les heures)
Le `monitor.py` génère automatiquement un rapport dans les logs avec :
- Nombre de trades, win rate par bucket
- Capital engagé, positions remboursées
- Raisons de SKIP les plus fréquentes

### Optimiseur automatique
```bash
python optimizer.py          # Analyse et suggestions
python optimizer.py --apply  # Applique les ajustements dans .env
```

## Paramètres de trading

| Paramètre | Défaut | Description |
|---|---|---|
| `ENTRY_WINDOW_SECONDS` | 120 | Fenêtre d'entrée (dernières 120s) |
| `MAX_BUY_PRICE` | 0.90 | Prix max par share (~11% yield min) |
| `MIN_BTC_DISTANCE` | 12.0 | Distance min BTC vs prix d'ouverture ($) |
| `HIGH_CONFIDENCE_THRESHOLD` | 0.72 | Seuil confiance tier HIGH |
| `MEDIUM_CONFIDENCE_THRESHOLD` | 0.60 | Seuil confiance tier MEDIUM |
| `KELLY_FRACTION` | 0.50 | Fraction Kelly |
| `MAX_KELLY_SIZE` | 0.20 | Cap taille position (20% du capital) |
| `ORDER_TYPE` | FOK | Fill-Or-Kill |

## Modèle de confiance (table 2D)

```
| Distance | 120s | 90s  | 60s  | 40s  | 25s  | 10s  |
|----------|------|------|------|------|------|------|
| $50+     | 0.72 | 0.78 | 0.82 | 0.88 | 0.92 | 0.98 |
| $30-50   | 0.65 | 0.72 | 0.78 | 0.82 | 0.85 | 0.96 |
| $20-30   | 0.58 | 0.65 | 0.72 | 0.76 | 0.78 | 0.90 |
| $15-20   | 0.50 | 0.58 | 0.65 | 0.70 | 0.72 | 0.86 |
| $12-15   | 0.45 | 0.52 | 0.58 | 0.64 | 0.65 | 0.80 |
| < $12    | SKIP | SKIP | SKIP | SKIP | SKIP | SKIP |
```

## Adresses des contrats (Polygon Mainnet)

| Contrat | Adresse |
|---------|---------|
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| USDC natif | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` |
| CTF (Conditional Tokens) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Chainlink BTC/USD | `0xc907E116054Ad103354f2D350FD2514433D57F6f` |
| Uniswap V3 Router | `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` |

## Sécurité

- Ne jamais partager la clé privée
- Toujours `chmod 600 .env` après modification
- `.env` est exclu de Git via `.gitignore`
- Utiliser un wallet dédié (jamais le wallet principal)
- Toutes les connexions utilisent TLS/SSL

## Avertissement

Ce bot est un outil expérimental. Le trading de marchés de prédiction comporte des risques financiers. Utilisez-le à vos propres risques et ne tradez que ce que vous pouvez vous permettre de perdre.
