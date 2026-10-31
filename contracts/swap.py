"""
Swap USDC native -> USDC.e via Uniswap V3 on Polygon.
Polymarket uses USDC.e as collateral, so native USDC must be converted.
"""

import time

from web3 import Web3

from contracts.abis import ERC20_APPROVE_ABI
from contracts.addresses import UNISWAP_SWAP_ROUTER, USDC_E, USDC_NATIVE
from logger import log


# Uniswap V3 SwapRouter02 - exactInputSingle
SWAP_ROUTER_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    }
]


def get_native_usdc_balance(w3: Web3, wallet: str) -> int:
    """Returns raw balance in wei (6 decimals)."""
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_NATIVE),
        abi=ERC20_APPROVE_ABI + [
            {
                "name": "balanceOf",
                "type": "function",
                "inputs": [{"name": "", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
            }
        ],
    )
    return usdc.functions.balanceOf(Web3.to_checksum_address(wallet)).call()


def get_usdc_e_balance(w3: Web3, wallet: str) -> int:
    """Returns raw balance in wei (6 decimals)."""
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_E),
        abi=[
            {
                "name": "balanceOf",
                "type": "function",
                "inputs": [{"name": "", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
            }
        ],
    )
    return usdc.functions.balanceOf(Web3.to_checksum_address(wallet)).call()


def swap_native_usdc_to_usdc_e(
    w3: Web3,
    private_key: str,
    wallet: str,
    amount_wei: int | None = None,
    slippage: float = 0.005,
) -> bool:
    """
    Swap USDC native -> USDC.e via Uniswap V3.

    Args:
        w3: Web3 instance connected to Polygon
        private_key: Wallet private key
        wallet: Wallet address
        amount_wei: Amount in wei (6 decimals). None = swap entire balance.
        slippage: Max slippage (0.005 = 0.5%)

    Returns:
        True if swap succeeded
    """
    wallet = Web3.to_checksum_address(wallet)

    # Get amount to swap
    if amount_wei is None:
        amount_wei = get_native_usdc_balance(w3, wallet)

    if amount_wei <= 0:
        log.info("No native USDC to swap")
        return True

    amount_usdc = amount_wei / 1e6
    log.info("Swapping $%.2f USDC native -> USDC.e", amount_usdc)

    nonce = w3.eth.get_transaction_count(wallet)

    # Step 1: Approve exactly the swapped amount for the Uniswap router
    # (jamais d'approbation illimitée : le routeur ne peut prélever que ce swap)
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_NATIVE),
        abi=ERC20_APPROVE_ABI,
    )
    router_addr = Web3.to_checksum_address(UNISWAP_SWAP_ROUTER)

    approve_tx = usdc_contract.functions.approve(
        router_addr, amount_wei
    ).build_transaction({
        "chainId": 137,
        "from": wallet,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
        "gas": 60_000,
    })
    signed_approve = w3.eth.account.sign_transaction(approve_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        log.error("USDC approve failed: %s", tx_hash.hex())
        return False
    log.info("USDC approve OK: %s", tx_hash.hex())
    nonce += 1

    # Step 2: Swap via Uniswap V3 exactInputSingle
    router = w3.eth.contract(address=router_addr, abi=SWAP_ROUTER_ABI)

    # USDC/USDC.e pool fee tier: 100 (0.01%) — stablecoin pair
    # Min output with slippage
    min_out = int(amount_wei * (1 - slippage))

    swap_params = (
        Web3.to_checksum_address(USDC_NATIVE),  # tokenIn
        Web3.to_checksum_address(USDC_E),        # tokenOut
        100,                                      # fee (0.01%)
        wallet,                                   # recipient
        amount_wei,                               # amountIn
        min_out,                                  # amountOutMinimum
        0,                                        # sqrtPriceLimitX96 (0 = no limit)
    )

    swap_tx = router.functions.exactInputSingle(swap_params).build_transaction({
        "chainId": 137,
        "from": wallet,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
        "gas": 300_000,
    })
    signed_swap = w3.eth.account.sign_transaction(swap_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
    log.info("Swap tx sent: %s", tx_hash.hex())

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] == 1:
        new_balance = get_usdc_e_balance(w3, wallet) / 1e6
        log.info("Swap OK! USDC.e balance: $%.2f (gas: %d)", new_balance, receipt["gasUsed"])
        return True
    else:
        log.error("Swap tx reverted: %s", tx_hash.hex())
        return False
