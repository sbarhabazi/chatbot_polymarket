"""
Optimisation automatique des paramètres du bot quand 50+ trades sont accumulés.
Lance optimizer.py --apply si le nombre de trades résolus a augmenté de 10+ depuis la dernière optimisation.
Exécuté toutes les 6h par Perplexity Computer (en parallèle avec la supervision).
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

TRADE_HISTORY_FILE = Path("/home/user/workspace/chatbot_polymarket/trade_history.json")
TRACKING_FILE = Path("/home/user/workspace/cron_tracking/auto_optimize.json")
OPTIMIZER_SCRIPT = Path("/home/user/workspace/chatbot_polymarket/optimizer.py")
MIN_TRADES_TOTAL = 50
MIN_NEW_TRADES_SINCE_LAST = 10


def load_state():
    if TRACKING_FILE.exists():
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {"last_trade_count": 0, "last_optimization": None, "optimizations_applied": 0}


def save_state(state):
    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(state, f, indent=2)


def count_resolved_trades():
    if not TRADE_HISTORY_FILE.exists():
        return 0
    with open(TRADE_HISTORY_FILE) as f:
        trades = json.load(f)
    return sum(
        1 for t in trades
        if t.get("outcome") in ("win", "loss") or t.get("result") in ("win", "loss")
    )


def run_optimizer():
    """Lance optimizer.py --apply depuis le répertoire du bot."""
    repo_dir = str(OPTIMIZER_SCRIPT.parent)
    result = subprocess.run(
        [sys.executable, str(OPTIMIZER_SCRIPT), "--apply"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=120
    )
    return result.returncode, result.stdout, result.stderr


def main():
    state = load_state()
    resolved_count = count_resolved_trades()

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resolved_trades": resolved_count,
        "last_trade_count": state["last_trade_count"],
        "action": "none",
        "reason": "",
    }

    # Vérifier si on doit optimiser
    if resolved_count < MIN_TRADES_TOTAL:
        result["reason"] = f"Pas assez de trades résolus: {resolved_count}/{MIN_TRADES_TOTAL}"
        result["action"] = "skipped"
        print(json.dumps(result))
        save_state(state)
        return result

    new_trades = resolved_count - state["last_trade_count"]
    if new_trades < MIN_NEW_TRADES_SINCE_LAST:
        result["reason"] = f"Seulement {new_trades} nouveaux trades depuis la dernière optimisation (seuil: {MIN_NEW_TRADES_SINCE_LAST})"
        result["action"] = "skipped"
        print(json.dumps(result))
        save_state(state)
        return result

    if not OPTIMIZER_SCRIPT.exists():
        result["reason"] = "optimizer.py introuvable dans le workspace"
        result["action"] = "error"
        print(json.dumps(result))
        return result

    # Lancer l'optimisation
    print(f"Lancement optimizer.py --apply ({resolved_count} trades résolus, {new_trades} nouveaux)...")
    returncode, stdout, stderr = run_optimizer()

    if returncode == 0:
        state["last_trade_count"] = resolved_count
        state["last_optimization"] = datetime.now(timezone.utc).isoformat()
        state["optimizations_applied"] = state.get("optimizations_applied", 0) + 1
        result["action"] = "optimized"
        result["reason"] = f"Optimisation appliquée avec succès. {new_trades} nouveaux trades traités."
        result["optimizer_output"] = stdout[:1000]
        result["notify"] = True
        result["notification"] = {
            "title": "Optimisation automatique appliquée",
            "body": f"Le bot a été optimisé automatiquement après {resolved_count} trades résolus (+{new_trades} nouveaux). Consultez optimizer.py pour les détails."
        }
    else:
        result["action"] = "error"
        result["reason"] = f"Optimizer a échoué (code {returncode}): {stderr[:500]}"
        result["notify"] = True
        result["notification"] = {
            "title": "Erreur optimisation automatique",
            "body": f"L'optimisation a échoué: {stderr[:200]}"
        }

    save_state(state)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
