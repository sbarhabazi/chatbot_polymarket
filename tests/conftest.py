import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PAPER_TRADING", "true")


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Les fichiers d'état (historique, mode du BalanceGuard) sont écrits dans
    le répertoire courant : chaque test travaille dans un dossier temporaire."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
