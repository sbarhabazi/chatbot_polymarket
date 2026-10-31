"""One-time approval setup for Polymarket trading.

Run this once per wallet before the bot can trade:
    python -m contracts.allowances

Les opérateurs Polymarket (échanges et adaptateur neg-risk) reçoivent une
approbation illimitée : ils prélèvent l'USDC.e à chaque ordre rempli, et un
plafond fixe bloquerait le trading une fois consommé. Le routeur Uniswap, lui,
n'a plus d'approbation permanente (montant exact à chaque swap).
"""

import time

from web3 import Web3

from config import BotConfig
from contracts.abis import ERC1155_SET_APPROVAL_ABI, ERC20_APPROVE_ABI
from contracts.addresses import (
    CTF, CTF_EXCHANGE, NEG_RISK_ADAPTER, NEG_RISK_EXCHANGE,
    UNISWAP_SWAP_ROUTER, USDC_E, USDC_NATIVE,
)
from logger import log

MAX_UINT256 = 2**256 - 1

OPERATORS = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]


def _send_tx(w3, wallet, private_key, tx_builder, label):
    """Build, sign, send a transaction and wait for confirmation."""
    nonce = w3.eth.get_transaction_count(wallet)
    tx = tx_builder.build_transaction({
        "chainId": 137,
        "from": wallet,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
        "gas": 80_000,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status = "OK" if receipt["status"] == 1 else "FAILED"
    log.info("[%s] %s | tx: %s | gas: %d", status, label, tx_hash.hex(), receipt["gasUsed"])
    time.sleep(4)  # Wait for RPC nonce sync
    return receipt["status"] == 1


def setup_allowances(config: BotConfig) -> None:
    w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
    wallet = Web3.to_checksum_address(config.wallet_address)

    usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_APPROVE_ABI)
    usdc_native = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=ERC1155_SET_APPROVAL_ABI)

    count = 0

    # 1. Approve USDC.e for Polymarket operators (3 txs)
    for op in OPERATORS:
        op_addr = Web3.to_checksum_address(op)
        ok = _send_tx(w3, wallet, config.private_key,
                      usdc_e.functions.approve(op_addr, MAX_UINT256),
                      f"USDC.e approve -> {op[:10]}")
        if ok:
            count += 1

    # 2. Approve CTF (ERC1155) for Polymarket operators (3 txs)
    for op in OPERATORS:
        op_addr = Web3.to_checksum_address(op)
        ok = _send_tx(w3, wallet, config.private_key,
                      ctf.functions.setApprovalForAll(op_addr, True),
                      f"CTF setApprovalForAll -> {op[:10]}")
        if ok:
            count += 1

    # 3. Révoquer l'ancienne approbation illimitée USDC native -> Uniswap.
    # contracts/swap.py approuve désormais le montant exact de chaque swap.
    router = Web3.to_checksum_address(UNISWAP_SWAP_ROUTER)
    ok = _send_tx(w3, wallet, config.private_key,
                  usdc_native.functions.approve(router, 0),
                  "USDC native approve -> Uniswap révoquée (0)")
    if ok:
        count += 1

    log.info("Allowances done: %d/7 succeeded.", count)


if __name__ == "__main__":
    cfg = BotConfig.from_env()
    setup_allowances(cfg)
