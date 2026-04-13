#!/bin/bash
# Script de démarrage unifié
# Lance le bot principal + le monitor en parallèle
# Les logs vont dans bot.log, consultable via Railway dashboard

set -e

export BOT_LOG_FILE="bot.log"

echo "=== Polymarket Bot — Démarrage ==="
echo "Mode: ${PAPER_TRADING:-false}"
echo "Wallet: ${WALLET_ADDRESS:-non configuré}"
echo "=================================="

# Lancer le bot principal avec logs dans bot.log
python main.py 2>&1 | tee "$BOT_LOG_FILE" &
BOT_PID=$!

# Lancer le monitor en arrière-plan (rapport toutes les heures)
python monitor.py &
MONITOR_PID=$!

echo "Bot PID: $BOT_PID | Monitor PID: $MONITOR_PID"

# Attendre que l'un des deux se termine
wait $BOT_PID
echo "Bot terminé. Arrêt du monitor..."
kill $MONITOR_PID 2>/dev/null || true
