"""
Vérification du capital critique et du solde POL (gas).
Lancé toutes les heures par Perplexity Computer.
Envoie une alerte si :
  - USDC.e < 15$ (capital critique)
  - POL < 1.0 (gas critique)
"""

from web3 import Web3
import json, os, sys
from pathlib import Path
from datetime import datetime

WALLET = "0x0f6768A0ff895433827531f83933D805CF979B02"
RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
TRACKING_FILE = "/home/user/workspace/cron_tracking/capital_check.json"

CRITICAL_USDC = 15.0
CRITICAL_POL = 1.0
WARNING_USDC = 20.0
WARNING_POL = 2.0

def load_last_state():
    if Path(TRACKING_FILE).exists():
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    Path(TRACKING_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(state, f, indent=2)

def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    abi = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]

    wallet = Web3.to_checksum_address(WALLET)
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=abi)

    usdc_balance = usdc_contract.functions.balanceOf(wallet).call() / 1e6
    pol_balance = w3.eth.get_balance(wallet) / 1e18

    last = load_last_state()
    last_usdc = last.get("usdc_balance", usdc_balance)
    change = usdc_balance - last_usdc

    state = {
        "timestamp": datetime.utcnow().isoformat(),
        "usdc_balance": usdc_balance,
        "pol_balance": pol_balance,
        "change_since_last": change,
    }
    save_state(state)

    alerts = []

    # Capital critique
    if usdc_balance < CRITICAL_USDC:
        alerts.append({
            "level": "CRITICAL",
            "type": "capital",
            "message": f"🚨 Capital critique : ${usdc_balance:.2f} USDC.e (seuil: ${CRITICAL_USDC}). Le bot ne peut plus trader efficacement. Recharge le wallet."
        })
    elif usdc_balance < WARNING_USDC:
        alerts.append({
            "level": "WARNING",
            "type": "capital",
            "message": f"⚠️ Capital bas : ${usdc_balance:.2f} USDC.e. Envisage de recharger le wallet de trading."
        })

    # Gas POL critique
    if pol_balance < CRITICAL_POL:
        alerts.append({
            "level": "CRITICAL",
            "type": "gas",
            "message": f"🚨 POL critique : {pol_balance:.4f} POL (seuil: {CRITICAL_POL}). Les transactions blockchain vont échouer. Envoie du POL au wallet {WALLET[:8]}...{WALLET[-4:]}."
        })
    elif pol_balance < WARNING_POL:
        alerts.append({
            "level": "WARNING",
            "type": "gas",
            "message": f"⚠️ POL bas : {pol_balance:.4f} POL. Pense à recharger le gas."
        })

    # Chute significative du capital
    if change < -5.0:
        alerts.append({
            "level": "WARNING",
            "type": "loss",
            "message": f"📉 Capital en baisse de ${abs(change):.2f} depuis la dernière vérification. Solde actuel: ${usdc_balance:.2f}."
        })

    # Output pour le cron
    print(json.dumps({
        "usdc_balance": usdc_balance,
        "pol_balance": pol_balance,
        "alerts": alerts
    }))

    return alerts

if __name__ == "__main__":
    alerts = main()
    if alerts:
        sys.exit(1)  # Signal au cron qu'il faut notifier
    sys.exit(0)
