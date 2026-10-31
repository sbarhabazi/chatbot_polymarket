"""Emplacement des fichiers d'état du bot.

Tous les fichiers persistants (historique des trades, paramètres appris, mode
du BalanceGuard, virements, heartbeat) sont écrits dans DATA_DIR. Dans Docker,
DATA_DIR=/app/data est le volume monté (-v /opt/polybot/data:/app/data) : les
données survivent à la reconstruction du conteneur. Par défaut (hors Docker),
le répertoire courant.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))


def data_path(name: str) -> str:
    return str(DATA_DIR / name)


HEARTBEAT_FILE = data_path("heartbeat")
