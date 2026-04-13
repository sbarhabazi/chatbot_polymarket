# Polymarket BTC 5-min Sniper Bot

Bot de trading automatique sur les marchés Polymarket "BTC Up/Down" en fenêtres de 5 minutes.

## Infrastructure

- **Déployé sur Vultr VPS** (Londres, GB) — actif 24h/24
- **Source de prix** : Binance WS (principal) → Kraken WS → Coinbase WS → CoinGecko REST
- **Blockchain** : Polygon (USDC.e)
- **Supervision** : Perplexity Computer (toutes les 6h, auto-correction)

## Architecture

```
Process A — Sniper     : surveille BTC, signal composite v5, place ordres FOK
Process B — Harvester  : réclame les positions gagnantes on-chain
Process C — ProfitTaker: transfère $50 vers wallet personnel quand solde ≥ $100
LearningEngine         : calibre automatiquement les seuils après chaque trade résolu
```

## Fichiers principaux

```
main.py                  — orchestrateur
config.py                — BotConfig, variables d'environnement
models.py                — MarketInfo, PriceSnapshot, PriceSignal
logger.py                — logging structuré
market_manager.py        — découverte marchés via Gamma API
price_analyzer.py        — source prix multi-fallback
sniper.py                — Process A, signal composite v5
harvester.py             — Process B, rachat on-chain
wallet_manager.py        — solde USDC, Kelly sizing
learning_engine.py       — calibration auto (learned_params.json, trade_history.json)
profit_taker.py          — transfert auto vers wallet personnel
monitor.py               — analyse logs, healthcheck
optimizer.py             — ajustement paramètres (--apply)
Dockerfile               — Python 3.11 slim, non-root
scripts/                 — automatisations de maintenance
```

## Déploiement Vultr (actuel)

```bash
# Connexion au serveur
ssh root@45.76.130.234

# Logs en direct
docker logs polybot -f

# Redémarrage manuel
docker restart polybot

# Mise à jour depuis GitHub
cd /opt/polybot/chatbot_polymarket
git pull
docker build -t polybot:latest .
docker stop polybot && docker rm polybot
docker run -d --name polybot --restart unless-stopped \
  --env-file .env \
  -v /opt/polybot/data:/app/data \
  --log-driver json-file --log-opt max-size=50m \
  polybot:latest
```

## Variables d'environnement (.env sur le serveur)

| Variable | Description |
|---|---|
| `PAPER_TRADING` | `false` (live) |
| `PRIVATE_KEY` | Clé privée wallet Polygon |
| `WALLET_ADDRESS` | Wallet de trading |
| `PERSONAL_WALLET` | Wallet de destination (profit) |
| `POLYGON_RPC_URL` | RPC Polygon |
| `MIN_BET_AMOUNT` | Mise minimale ($3.0) |
| `PROFIT_TRIGGER_BALANCE` | Déclenchement transfert ($100) |
| `PROFIT_TRANSFER_AMOUNT` | Montant transféré ($50) |

## Automatisations (Perplexity Computer)

| Tâche | Fréquence |
|---|---|
| Supervision + auto-fix | Toutes les 6h |
| Alerte capital critique | Toutes les heures |
| Détection Polymarket down | Toutes les 6h |
| Auto-optimisation + check POL | Toutes les 6h |
| Sauvegarde données apprentissage | Chaque jour 23h EDT |
| Rapport hebdomadaire | Lundi 8h EDT |
| Audit sécurité | Dimanche 23h EDT |
| Rapport mensuel | 1er du mois 8h EDT |

## Wallets

- Trading : `0x0f6768A0ff895433827531f83933D805CF979B02`
- Personnel : `0x18f390858565B76c395b983531C659fAf67E4De8`
- Réseau : Polygon (chain 137)
- Token : USDC.e (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
