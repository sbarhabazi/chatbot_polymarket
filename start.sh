#!/bin/bash
# Script de démarrage unifié
# Lance le bot principal + le monitor en parallèle

set -e

echo "=== Polymarket Bot — Démarrage ==="
echo "Mode: ${PAPER_TRADING:-false}"
echo "Wallet: ${WALLET_ADDRESS:-non configuré}"
echo "=================================="

# Lancer le bot principal
python main.py &
BOT_PID=$!

# Lancer le monitor en arrière-plan (rapport toutes les heures)
python monitor.py &
MONITOR_PID=$!

echo "Bot PID: $BOT_PID | Monitor PID: $MONITOR_PID"

# Attendre que l'un des deux se termine
wait $BOT_PID
echo "Bot terminé. Arrêt du monitor..."
kill $MONITOR_PID 2>/dev/null || true
