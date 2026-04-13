#!/bin/bash
# Sauvegarde automatique des fichiers d'apprentissage vers GitHub
# Exécuté quotidiennement par Perplexity Computer

set -e

REPO_DIR="/home/user/workspace/chatbot_polymarket"
DATA_BRANCH="learning-data"

cd "$REPO_DIR"

# Pull dernière version
git fetch origin
git checkout main
git pull origin main

# Vérifier si les fichiers existent dans le container Railway
# (ils sont copiés ici via l'API Railway ou Railway Volume)
FILES_CHANGED=false

for FILE in learned_params.json trade_history.json profit_history.json; do
    if [ -f "$FILE" ]; then
        git add "$FILE"
        if ! git diff --cached --quiet "$FILE"; then
            FILES_CHANGED=true
        fi
    fi
done

if [ "$FILES_CHANGED" = true ]; then
    TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")
    git commit -m "data: sauvegarde automatique données apprentissage — $TIMESTAMP"
    git push origin main
    echo "✓ Données sauvegardées: $TIMESTAMP"
else
    echo "✓ Aucun changement à sauvegarder"
fi
