# Polymarket BTC Sniper Bot

Bot de trading automatique sur Polymarket — marchés BTC 5 minutes "Up or Down".

## Architecture

- **Process A (Sniper):** Surveille le prix BTC, calcule un signal composite (7 indicateurs), place des ordres FOK dans les dernières 120s de chaque fenêtre 5 min
- **Process B (Harvester):** Réclame les positions gagnantes on-chain après résolution
- **Process C (ProfitTaker):** Transfert automatique de $50 vers le portefeuille personnel quand la balance atteint $100
- **LearningEngine:** Auto-calibration des seuils et du sizing après chaque trade résolu

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
