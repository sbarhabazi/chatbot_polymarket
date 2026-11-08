# Polymarket BTC Sniper Bot

Bot de trading automatique sur Polymarket — marchés BTC 5 minutes "Up or Down".

## Architecture

- **Process A (Sniper):** Surveille le prix BTC, calcule un signal composite (7 indicateurs), place des ordres FOK dans les dernières 120s de chaque fenêtre 5 min
- **Process B (Harvester):** Réclame les positions gagnantes on-chain après résolution
- **Process C (ProfitTaker):** Transfert automatique de $50 vers le portefeuille personnel quand la balance atteint $100
- **LearningEngine:** Auto-calibration des seuils et du sizing après chaque trade résolu

## Stratégie (v7)

À chaque scan de la fenêtre d'entrée (2 dernières minutes), le bot :

1. **Suit le prix qui tranche le marché.** Polymarket règle les marchés Up/Down avec un TWAP Chainlink (docs : « The TWAP at the start sets the price to beat. When the duration ends, the TWAP at that time sets the final price »). Le bot le reçoit en temps réel via PolyBolt (`wss://ws-live-v2.polymarket.com/ws`, canaux `price.crypto` et `price.crypto.twap`), avec les identifiants API CLOB dérivés de `PRIVATE_KEY`. Binance (USDT) diffère du prix officiel de 55 à 175 $, et de 21 $ en médiane sur la variation d'une fenêtre.
2. **Calcule une probabilité** : P(final ≥ priceToBeat), avec l'écart actuel, le temps restant et la volatilité mesurée sur 5 minutes (marche aléatoire ; pour le TWAP, la partie déjà observée de la dernière minute est prise en compte). Probabilité bornée à [3 %, 97 %].
3. **Compare au prix réel du carnet CLOB, frais compris** : frais taker `0,07 × p × (1 − p)` par part (docs Polymarket). Il achète le côté dont l'espérance `P − prix − frais` est la plus haute, seulement si elle dépasse `MIN_EDGE` (0,05 $ par part par défaut), évaluée au prix limite `ask + ASK_BUFFER`.
4. **Dimensionne avec Kelly** sur cette probabilité, plafonné par le mode du BalanceGuard, frais compris.

| Variable | Défaut | Rôle |
|---|---|---|
| `REFERENCE_FEED` | `chainlink_twap` | `chainlink_twap`, `chainlink` (spot) ou `binance` |
| `MIN_EDGE` | `0.05` | Espérance minimale par part, frais compris |
| `BASIS_NOISE_USD` | `25` | Erreur Binance/Chainlink ajoutée au modèle (mode `binance`) |
| `MAX_BUY_PRICE` | `0.90` | Prix maximal d'une part |

Sources de prix Binance : en cas de coupure, le bot réessaie Binance 5 fois, puis passe 5 minutes au plus sur Kraken ou Coinbase avant de revenir à Binance. Un flux muet pendant 30 s est considéré comme mort. CoinGecko n'est plus utilisé.

## Calibration : le journal fantôme

À chaque fenêtre, le bot note sans trader son estimation du priceToBeat selon chaque source (`open`), puis à 120, 90, 60, 30 et 15 s de la fin la probabilité du modèle et les prix du carnet (`quote`), dans `shadow_log.jsonl`. L'**OutcomeRecorder** ajoute 7 minutes après la clôture le priceToBeat, le finalPrice et le gagnant officiels (`window_outcomes.jsonl`). Rapport :

```bash
docker exec polybot python scripts/shadow_report.py
```

Il répond à trois questions sur toutes les fenêtres, environ 288 par jour :
1. Quelle source reproduit le priceToBeat officiel (écart médian proche de 0 $) ?
2. Le modèle est-il calibré, et prédit-il mieux que le prix du marché (score de Brier) ?
3. Quel `MIN_EDGE` aurait gagné de l'argent, frais compris ?

Ne passer en réel que si la réponse à la question 3 est positive sur au moins quelques centaines de fenêtres.

## Mode papier et mesure de la stratégie

Avec `PAPER_TRADING=true`, le bot simule chaque trade comme en réel : prix lu dans le carnet d'ordres CLOB du token joué, prix limite `ask + ASK_BUFFER`, nombre entier de parts, minimum de 5 parts, plafond du mode, frais taker payés à l'achat. Le trade n'est compté que si le carnet permet de l'exécuter entièrement à ce prix, au prix moyen réel des niveaux consommés. La mise est calculée sur au plus `PAPER_BALANCE` : les gains ne sont pas réinvestis, pour que chaque trade pèse autant dans la mesure. Une minute après la fin de chaque fenêtre, le **PaperResolver** lit le résultat officiel du marché, enregistre gain ou perte dans `trade_history.json` et crédite le solde papier de 1 $ par part gagnante.

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
