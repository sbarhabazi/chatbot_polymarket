"""
Vérification du solde POL (gas) sur le wallet de trading.
Exécuté toutes les 6h par Perplexity Computer.
Alerte si POL < 1.0 (transactions blockchain impossibles).
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from web3 import Web3

WALLET = "0x0f6768A0ff895433827531f83933D805CF979B02"
RPC = "https://polygon-bor-rpc.publicnode.com"
TRACKING_FILE = Path("/home/user/workspace/cron_tracking/pol_balance.json")

CRITICAL_POL = 1.0
WARNING_POL = 2.0
RECOMMENDED_POL = 5.0


def load_state():
    if TRACKING_FILE.exists():
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    wallet = Web3.to_checksum_address(WALLET)

    pol_balance = w3.eth.get_balance(wallet) / 1e18

    state = load_state()
    last_pol = state.get("pol_balance", pol_balance)
    change = pol_balance - last_pol

    now = datetime.now(timezone.utc).isoformat()
    state.update({
        "timestamp": now,
        "pol_balance": pol_balance,
        "change_since_last": round(change, 6),
    })
    save_state(state)

    alerts = []

    if pol_balance < CRITICAL_POL:
        alerts.append({
            "level": "CRITICAL",
            "message": (
                f"🚨 POL CRITIQUE: {pol_balance:.4f} POL sur le wallet de trading.\n"
                f"Seuil critique: {CRITICAL_POL} POL.\n"
                f"Les transactions blockchain vont ÉCHOUER. "
                f"Envoie au moins {RECOMMENDED_POL} POL à: {WALLET}"
            )
        })
    elif pol_balance < WARNING_POL:
        alerts.append({
            "level": "WARNING",
            "message": (
                f"⚠️ POL bas: {pol_balance:.4f} POL (seuil warning: {WARNING_POL} POL).\n"
                f"Prévoir un rechargement prochainement. Recommandé: {RECOMMENDED_POL} POL minimum."
            )
        })

    result = {
        "timestamp": now,
        "pol_balance": round(pol_balance, 6),
        "change_since_last": round(change, 6),
        "status": "critical" if pol_balance < CRITICAL_POL else ("warning" if pol_balance < WARNING_POL else "ok"),
        "alerts": alerts,
        "notify": len(alerts) > 0,
    }

    if alerts:
        result["notification"] = {
            "title": "Alerte gas POL — wallet de trading",
            "body": alerts[0]["message"],
        }

    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
