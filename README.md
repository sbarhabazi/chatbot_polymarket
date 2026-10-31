# Polymarket BTC Sniper Bot

Bot de trading automatique sur Polymarket — marchés BTC 5 minutes "Up or Down".

## Architecture

- **Process A (Sniper):** Surveille le prix BTC, calcule un signal composite (7 indicateurs), place des ordres FOK dans les dernières 120s de chaque fenêtre 5 min
- **Process B (Harvester):** Réclame les positions gagnantes on-chain après résolution
- **Process C (ProfitTaker):** Transfert automatique de $50 vers le portefeuille personnel quand la balance atteint $100
- **LearningEngine:** Auto-calibration des seuils et du sizing après chaque trade résolu

## Gestion du risque (BalanceGuard)

| Mode | Solde | Kelly | Plafond par trade | Confiance min. |
|---|---|---|---|---|
| NORMAL | ≥ 15 $ | ½ | 20 % | 0,60 |
| SURVIE | 3 à 15 $ | ¼ | 10 % | 0,70 |
| DERNIER | 0,50 à 3 $ | — | aucun nouvel ordre | — |
| RUINE | < 0,50 $ | — | arrêt du bot | — |

Le coût d'un ordre (minimum Polymarket de 5 parts compris) ne dépasse jamais le plafond du mode : si 5 parts coûtent plus, la fenêtre est sautée. Le bot joue uniquement le côté recommandé par le signal.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests
```

## Infrastructure

- **VPS:** Vultr Madrid (ES) — région autorisée par Polymarket
- **Container:** Docker `polybot`
- **Réseau:** Polygon (USDC.e)

## Variables d'environnement

Voir `.env.example` pour la liste des variables requises.

## Déploiement

```bash
# Construire et lancer
docker build -t polybot:latest .
docker run -d --name polybot --restart unless-stopped --env-file .env \
  -v /opt/polybot/data:/app/data \
  --log-driver json-file --log-opt max-size=50m \
  polybot:latest

# Logs
docker logs polybot -f
```

## Supervision

8 crons automatiques gèrent:
- Supervision toutes les 6h (auto-fix si nécessaire)
- Alertes capital critique (toutes les heures)
- Sauvegarde learning data (quotidienne)
- Rapports hebdomadaires et mensuels
- Audit de sécurité hebdomadaire
- Détection Polymarket down

## Notes importantes

- GB (Royaume-Uni), FR (France), DE (Allemagne), NL (Pays-Bas) sont **bloqués** par Polymarket
- ES (Espagne) est **autorisé** — VPS Madrid utilisé
- Ne jamais utiliser de VPN (interdit par Polymarket)
