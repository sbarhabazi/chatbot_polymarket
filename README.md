# Polymarket 5-Min BTC Sniper Bot

Bot de trading automatise pour les marches "Bitcoin Up or Down - 5 Minutes" de Polymarket sur la blockchain Polygon.

## Fonctionnement

Le bot analyse le prix du BTC en temps reel via Binance WebSocket et place des paris sur Polymarket quand les conditions sont favorables.

### Architecture

Deux processus asynchrones tournent en parallele :

- **Process A (Sniper)** : Observe le prix BTC pendant la fenetre de 5 minutes, analyse la distance entre le prix actuel et le prix d'ouverture (via Chainlink Oracle), et place un ordre d'achat quand la confiance est suffisante.
- **Process B (Harvester)** : Surveille les marches resolus et reclame automatiquement les gains sur la blockchain Polygon.

### Strategie "Safe Scalper v4 - Early Entry"

1. **Observation** : Le bot ecoute le flux de prix BTC seconde par seconde
2. **Prix de reference** : Le prix d'ouverture est capture via l'oracle Chainlink (meme source que Polymarket utilise pour la resolution)
3. **Entree anticipee** : Le bot entre avec 60-120 secondes restantes (quand les shares sont encore a prix accessible)
4. **Modele de confiance** : Table 2D calibree sur la distance BTC et le temps restant
5. **Sizing dynamique** : Taille de position ajustee selon le niveau de confiance (HIGH: 30%, MEDIUM: 20%)
6. **Ordres limite GTC** : Evite le slippage des market orders

```
Fenetre de 5 min :
|--- Observation (3min) ---|--- Entry Window (2min) ---|
                           ^                           ^
                     Bot peut trader              Marche ferme
```

## Structure du projet

```
chatbot_polymarket/
  main.py                  # Point d'entree / orchestrateur async
  config.py                # BotConfig charge depuis .env
  models.py                # MarketInfo, PriceSnapshot, PriceSignal
  logger.py                # Logging structure
  market_manager.py        # Decouverte marche 5min via Gamma API
  price_analyzer.py        # Binance WebSocket + modele de confiance 2D
  sniper.py                # Process A : analyse + ordre limite GTC
  harvester.py             # Process B : redeem on-chain (CTF)
  wallet_manager.py        # Balance USDC + position sizing
  contracts/
    __init__.py
    addresses.py           # Adresses Polygon (CTF, USDC, Chainlink, Uniswap)
    abis.py                # ABI fragments (redeem, balanceOf, approve, Chainlink)
    allowances.py          # Setup one-shot des approbations (7 txs)
    swap.py                # Swap USDC natif -> USDC.e via Uniswap V3
  .env.example             # Template de configuration
  requirements.txt         # Dependances Python
```

## Prerequis

- Python 3.10+
- Un wallet Polygon avec :
  - USDC.e (capital de trading, minimum $20 recommande)
  - POL (frais de gas, ~0.5 POL suffisant)

## Installation

```bash
# Cloner le repo
git clone https://github.com/sbarhabazi/chatbot_polymarket.git
cd chatbot_polymarket

# Creer un environnement virtuel
python3 -m venv .venv
source .venv/bin/activate

# Installer les dependances
pip install -r requirements.txt
```

## Configuration

### 1. Creer le fichier .env

```bash
cp .env.example .env
chmod 600 .env   # Securiser les permissions
```

### 2. Renseigner les cles

Editer `.env` et remplir :

```
PAPER_TRADING=false
PRIVATE_KEY=0xVOTRE_CLE_PRIVEE
WALLET_ADDRESS=0xVOTRE_ADRESSE
```

### 3. Parametres de trading

| Parametre | Defaut | Description |
|-----------|--------|-------------|
| `ENTRY_WINDOW_SECONDS` | 120 | Fenetre d'entree (dernieres 120 secondes) |
| `MAX_BUY_PRICE` | 0.90 | Prix max par share ($0.90 = yield min ~11%) |
| `MIN_BTC_DISTANCE` | 12.0 | Distance min BTC vs prix d'ouverture ($) |
| `HIGH_CONFIDENCE_THRESHOLD` | 0.72 | Seuil confiance tier HIGH (30% du capital) |
| `MEDIUM_CONFIDENCE_THRESHOLD` | 0.60 | Seuil confiance tier MEDIUM (20% du capital) |
| `HIGH_CONFIDENCE_SIZE` | 0.30 | Taille position HIGH (30%) |
| `MEDIUM_CONFIDENCE_SIZE` | 0.20 | Taille position MEDIUM (20%) |

### 4. Setup des approbations blockchain (une seule fois)

```bash
python -m contracts.allowances
```

Cela envoie 7 transactions d'approbation sur Polygon :
- 3x USDC.e approve pour les contracts Polymarket
- 3x CTF setApprovalForAll pour les contracts Polymarket
- 1x USDC natif approve pour Uniswap (swap automatique)

## Utilisation

### Lancement en mode live

```bash
source .venv/bin/activate
python main.py
```

### Lancement en mode Paper Trading (simulation)

Mettre dans `.env` :
```
PAPER_TRADING=true
PAPER_BALANCE=100.00
```

### Lancement en arriere-plan

```bash
nohup python main.py > bot.log 2>&1 &
echo $! > bot.pid
```

- Voir les logs : `tail -f bot.log`
- Arreter : `kill $(cat bot.pid)`

## Comprendre les logs

### Observation
```
Observing 1775484000 | 176s left | BTC: $69,311.87 | beat=N/A | Up=$0.515 Down=$0.485
```
Le bot observe le marche. `beat=N/A` signifie que le prix de reference Chainlink n'est pas encore utilise (le sera a l'entree).

### Entree dans la fenetre
```
=== ENTRY WINDOW [EARLY] === 115s remaining | window 1775484000
Signal: Down | BTC=$69310.14 | beat=$69380.82 | dist=$-70.7 | conf=0.70
```
- `EARLY` : Entree anticipee (>60s restantes)
- `dist=$-70.7` : BTC est $70.7 en-dessous du prix d'ouverture
- `conf=0.70` : 70% de confiance

### Decisions
```
BUY [HIGH]: Down | $5.49 (30% of $18.30) @ $0.495     # Achat execute
SKIP: Distance $5.1 < min $12.0                        # Distance trop faible
SKIP: Confidence 0.43 < minimum 0.60                   # Confiance insuffisante
SKIP: Bet $3.87 < $5 min (balance=$12.91)              # Solde trop bas
BUY NOT FILLED (no sellers at $0.49)                    # Pas de vendeurs
```

### Harvester
```
Found redeemable position: window 1775482200
Redeem CONFIRMED: bcd6d435... (gas used: 74314)
Redeemed successfully: window 1775482200
```

## Modele de confiance

Le bot utilise une table 2D distance x temps pour estimer la probabilite que BTC reste du meme cote du prix d'ouverture :

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

Penalites appliquees :
- Volatilite haute (>0.5%) : -0.08
- Momentum oppose (dernieres 60s) : -0.05
- Volume en contradiction : -0.03

## Flux d'un trade

```
1. Chainlink Oracle → prix d'ouverture BTC ($69,380)
2. Binance WebSocket → prix actuel BTC ($69,310)
3. Distance = -$70 → confiance = 0.70
4. Confiance >= 0.60 (MEDIUM) → BET 20% du solde
5. Limit order GTC: 11 shares "Down" @ $0.49
6. Si rempli → attendre resolution
7. Si gagne → Harvester redeem on-chain → USDC.e retour wallet
8. Cycle suivant avec solde mis a jour
```

## Securite

- Ne jamais partager la cle privee
- Toujours `chmod 600 .env` apres modification
- Le fichier `.env` est exclu de Git via `.gitignore`
- Toutes les connexions utilisent TLS/SSL
- La cle privee n'apparait jamais dans les logs
- Utiliser un wallet dedie (ne pas utiliser son wallet principal)

## Adresses des contrats (Polygon Mainnet)

| Contrat | Adresse |
|---------|---------|
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| USDC natif | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` |
| CTF (Conditional Tokens) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Chainlink BTC/USD | `0xc907E116054Ad103354f2D350FD2514433D57F6f` |
| Uniswap V3 Router | `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` |

## API utilisees

| API | Usage | Auth |
|-----|-------|------|
| Gamma API | Decouverte des marches 5min | Aucune |
| CLOB API | Placement des ordres | Signature wallet |
| Binance WebSocket | Prix BTC en temps reel | Aucune |
| Chainlink Oracle | Prix de reference (on-chain) | Aucune |
| Polygon RPC | Transactions blockchain | Aucune |

## Avertissement

Ce bot est un outil experimental. Le trading de marches de prediction comporte des risques financiers. Utilisez-le a vos propres risques et ne tradez que ce que vous pouvez vous permettre de perdre.
