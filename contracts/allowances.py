"""One-time approval setup for Polymarket trading.

Run this once per wallet before the bot can trade:
    python -m contracts.allowances
"""

import asyncio

from web3 import Web3

from config import BotConfig
from contracts.abis import ERC1155_SET_APPROVAL_ABI, ERC20_APPROVE_ABI
from contracts.addresses import CTF, CTF_EXCHANGE, NEG_RISK_ADAPTER, NEG_RISK_EXCHANGE, USDC_E
from logger import log

MAX_UINT256 = 2**256 - 1

OPERATORS = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]


def setup_allowances(config: BotConfig) -> None:
    w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
    wallet = Web3.to_checksum_address(config.wallet_address)
    nonce = w3.eth.get_transaction_count(wallet)

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_E),
        abi=ERC20_APPROVE_ABI,
    )
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF),
        abi=ERC1155_SET_APPROVAL_ABI,
    )

    for operator in OPERATORS:
        op = Web3.to_checksum_address(operator)

        # ERC20 USDC approval
        tx = usdc.functions.approve(op, MAX_UINT256).build_transaction({
            "chainId": 137,
            "from": wallet,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
            "gas": 60000,
        })
        signed = w3.eth.account.sign_transaction(tx, config.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        log.info("USDC approve %s -> %s: %s", USDC_E[:10], operator[:10], tx_hash.hex())
        nonce += 1

        # ERC1155 CTF approval
        tx = ctf.functions.setApprovalForAll(op, True).build_transaction({
            "chainId": 137,
            "from": wallet,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
            "gas": 60000,
        })
        signed = w3.eth.account.sign_transaction(tx, config.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        log.info("CTF setApprovalForAll %s -> %s: %s", CTF[:10], operator[:10], tx_hash.hex())
        nonce += 1

    log.info("All 6 allowances set successfully.")


if __name__ == "__main__":
    cfg = BotConfig.from_env()
    setup_allowances(cfg)
