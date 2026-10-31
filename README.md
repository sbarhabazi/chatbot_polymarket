# Polymarket BTC Sniper Bot

Bot de trading automatique sur Polymarket — marchés BTC 5 minutes "Up or Down".

## Architecture

- **Process A (Sniper):** Surveille le prix BTC, calcule un signal composite (7 indicateurs), place des ordres FOK dans les dernières 120s de chaque fenêtre 5 min
- **Process B (Harvester):** Réclame les positions gagnantes on-chain après résolution
- **Process C (ProfitTaker):** Transfert automatique de $50 vers le portefeuille personnel quand la balance atteint $100
- **LearningEngine:** Auto-calibration des seuils et du sizing après chaque trade résolu

## Mode papier et mesure de la stratégie

Avec `PAPER_TRADING=true`, le bot simule chaque trade comme en réel : même prix offert (`ASK_BUFFER`), nombre entier de parts, minimum de 5 parts, plafond du mode calculé sur le solde simulé. L'ordre est supposé rempli au prix offert, ce qui est une hypothèse optimiste. Une minute après la fin de chaque fenêtre, le **PaperResolver** lit le résultat officiel du marché, enregistre gain ou perte dans `trade_history.json` et crédite le solde papier (frais de 7,2 % sur le gain déduits).

Rapport de performance :

```bash
docker exec polybot python scripts/paper_report.py
```

Pour chaque tranche de confiance, le rapport compare le taux de gain observé au seuil de rentabilité (le prix moyen payé, frais compris). Une tranche n'est rentable que si elle gagne plus souvent que ce seuil. Il faut au moins 100 à 200 trades résolus avant de conclure.

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

Les fichiers d'état (`trade_history.json`, `learned_params.json`, `balance_guard_state.json`, `profit_history.json`, `heartbeat`) sont écrits dans `DATA_DIR`, soit `/app/data` dans l'image : le volume `/opt/polybot/data` les conserve d'une reconstruction à l'autre. Le conteneur tourne avec l'UID 1000, qui doit pouvoir écrire dans ce dossier :

```bash
sudo chown -R 1000:1000 /opt/polybot/data
```

Lors de la première mise à jour vers cette version, copier les anciens fichiers depuis le conteneur actuel avant de le supprimer :

```bash
for f in trade_history.json learned_params.json profit_history.json; do docker cp polybot:/app/$f /opt/polybot/data/ 2>/dev/null; done
```

Le healthcheck Docker est sain tant que le bot réécrit `/app/data/heartbeat` (toutes les 30 s, tolérance 180 s).

### Prix des ordres

Le bot offre `max(meilleur vendeur, prix du marché) + ASK_BUFFER` (défaut 0,02 $), plafonné à `MAX_BUY_PRICE`. L'ancienne variable `FILL_BUFFER` n'était pas appliquée ; elle est ignorée et signalée au démarrage.

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
