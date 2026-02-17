# EasySwap SDK — Single-file client for EasyTrade (Kite) swap aggregator.
# SPDX-License-Identifier: MIT
# No configuration required; defaults work for local/testing. Override via env or args.

from __future__ import annotations

import os
import json
import time
import hashlib
import logging
import argparse
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Optional, Sequence

try:
    from web3 import Web3
    from web3.contract import Contract
    from web3.types import TxParams, Wei, BlockIdentifier
    from eth_account import Account
    from eth_account.signers.local import LocalAccount
    from hexbytes import HexBytes
except ImportError:
    Web3 = None
    Contract = None
    TxParams = None
    Account = None
    LocalAccount = None
    HexBytes = None

logger = logging.getLogger("easyswap")

# -----------------------------------------------------------------------------
# Constants (unique to EasySwap SDK; not shared with other projects)
# -----------------------------------------------------------------------------

AGGREGATOR_SLIPPAGE_BPS = 50
FEE_BPS = 10
BPS_DENOM = 10000
MIN_PATH_LEN = 2
MAX_PATH_LEN = 6
DEFAULT_DEADLINE_OFFSET_SEC = 300
DEFAULT_GAS_LIMIT_SWAP = 350_000
DEFAULT_GAS_LIMIT_MULTIHOP = 500_000
KITE_DOMAIN_SEED_HEX = "7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b"

# EasyTrade contract ABI (minimal for swap + quote)
EASYTRADE_ABI = [
    {
        "inputs": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "executeSwapExactIn",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "feeWei", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "path", "type": "address[]"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ],
        "name": "executeSwapExactInMultiHop",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "feeWei", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
        ],
        "name": "quoteExactIn",
        "outputs": [{"name": "amountOutEst", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getSwapCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "router",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "feeCollector",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "weth",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "kitePaused",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
]

ROUTER_GET_AMOUNTS_OUT_ABI = [
    {
        "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Chain IDs and default RPC (random-looking endpoints for demo; replace in prod)
CHAIN_RPC = {
    1: os.environ.get("ETHEREUM_RPC", "https://eth.llamarpc.com"),
    5: os.environ.get("GOERLI_RPC", "https://rpc.ankr.com/eth_goerli"),
    10: os.environ.get("OPTIMISM_RPC", "https://mainnet.optimism.io"),
    137: os.environ.get("POLYGON_RPC", "https://polygon-rpc.com"),
    42161: os.environ.get("ARBITRUM_RPC", "https://arb1.arbitrum.io/rpc"),
    8453: os.environ.get("BASE_RPC", "https://mainnet.base.org"),
    56: os.environ.get("BSC_RPC", "https://bsc-dataseed.binance.org"),
    43114: os.environ.get("AVAX_RPC", "https://api.avax.network/ext/bc/C/rpc"),
}


class Chain(Enum):
    MAINNET = 1
    GOERLI = 5
    OPTIMISM = 10
    POLYGON = 137
    ARBITRUM = 42161
    BASE = 8453
    BSC = 56
    AVALANCHE = 43114


# -----------------------------------------------------------------------------
# Data types
# -----------------------------------------------------------------------------


@dataclass
class QuoteResult:
    amount_in: int
    amount_out_est: int
    amount_out_min_suggested: int
    fee_bps: int
    path: list[str]
    router_address: str
    chain_id: int
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount_in": self.amount_in,
            "amount_out_est": self.amount_out_est,
            "amount_out_min_suggested": self.amount_out_min_suggested,
            "fee_bps": self.fee_bps,
            "path": self.path,
            "router_address": self.router_address,
            "chain_id": self.chain_id,
            "timestamp": self.timestamp,
        }


@dataclass
class SwapReceipt:
    tx_hash: str
    amount_in: int
    amount_out: int
    fee_wei: int
    swap_id: int
    success: bool
    block_number: Optional[int] = None
    gas_used: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_hash": self.tx_hash,
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
            "fee_wei": self.fee_wei,
            "swap_id": self.swap_id,
            "success": self.success,
            "block_number": self.block_number,
            "gas_used": self.gas_used,
        }


# -----------------------------------------------------------------------------
# Encoding / hashing (unique names)
# -----------------------------------------------------------------------------


def kite_domain_hash(chain_id: int, contract_address: str) -> bytes:
    payload = f"EasyTrade_Kite_{chain_id}_{contract_address}"
    return hashlib.sha256(payload.encode()).digest()


def encode_path(path: list[str]) -> bytes:
    if not path or len(path) < MIN_PATH_LEN or len(path) > MAX_PATH_LEN:
        raise ValueError("path length must be between 2 and 6")
    return b"".join(bytes.fromhex(addr[2:].lower().zfill(40)) if addr.startswith("0x") else bytes.fromhex(addr.lower().zfill(40)) for addr in path)


def decode_uint256_list(data: bytes) -> list[int]:
    if len(data) < 32:
        return []
    try:
        return [int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data), 32)]
    except Exception:
        return []


def apply_slippage_bps(amount: int, bps: int, denom: int = BPS_DENOM) -> int:
    return amount * (denom - bps) // denom


def fee_from_amount_bps(amount: int, bps: int = FEE_BPS, denom: int = BPS_DENOM) -> int:
    return amount * bps // denom


# -----------------------------------------------------------------------------
# Web3 / contract helpers
# -----------------------------------------------------------------------------


def get_w3(chain_id: int, rpc_url: Optional[str] = None) -> "Web3":
    if Web3 is None:
        raise RuntimeError("web3 not installed; pip install web3")
    url = rpc_url or CHAIN_RPC.get(chain_id, "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(url))
    if not w3.is_connected():
        raise ConnectionError(f"Could not connect to RPC: {url}")
    return w3


def to_checksum(addr: str) -> str:
    if Web3 is None:
        return addr
    return Web3.to_checksum_address(addr)


def get_contract(w3: "Web3", address: str, abi: list) -> "Contract":
    if Contract is None:
        raise RuntimeError("web3 not installed")
    return w3.eth.contract(address=to_checksum(address), abi=abi)


def get_erc20(w3: "Web3", token_address: str) -> "Contract":
    return get_contract(w3, token_address, ERC20_ABI)


def get_easytrade(w3: "Web3", aggregator_address: str) -> "Contract":
    return get_contract(w3, aggregator_address, EASYTRADE_ABI)


# -----------------------------------------------------------------------------
# EasySwap client
# -----------------------------------------------------------------------------


class EasySwapClient:
    """Single-file client for EasyTrade (Kite) aggregator."""

    def __init__(
        self,
        w3: "Web3",
        aggregator_address: str,
        chain_id: Optional[int] = None,
    ):
        self._w3 = w3
        self._chain_id = chain_id or w3.eth.chain_id
        self._aggregator_address = to_checksum(aggregator_address)
        self._contract = get_easytrade(w3, aggregator_address)

    @property
    def chain_id(self) -> int:
        return self._chain_id

    @property
    def aggregator_address(self) -> str:
        return self._aggregator_address

    def is_paused(self) -> bool:
        return self._contract.functions.kitePaused().call()

    def get_router(self) -> str:
        return self._contract.functions.router().call()

    def get_fee_collector(self) -> str:
        return self._contract.functions.feeCollector().call()

    def get_weth(self) -> str:
        return self._contract.functions.weth().call()

    def get_swap_count(self) -> int:
        return self._contract.functions.getSwapCount().call()

    def quote_exact_in(self, token_in: str, token_out: str, amount_in: int) -> int:
        token_in = to_checksum(token_in)
        token_out = to_checksum(token_out)
        return self._contract.functions.quoteExactIn(token_in, token_out, amount_in).call()

    def quote_exact_in_with_slippage(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        slippage_bps: int = AGGREGATOR_SLIPPAGE_BPS,
    ) -> QuoteResult:
        amount_out_est = self.quote_exact_in(token_in, token_out, amount_in)
        amount_out_min = apply_slippage_bps(amount_out_est, slippage_bps)
        fee = fee_from_amount_bps(amount_in)
        return QuoteResult(
            amount_in=amount_in,
            amount_out_est=amount_out_est,
            amount_out_min_suggested=amount_out_min,
            fee_bps=FEE_BPS,
            path=[to_checksum(token_in), to_checksum(token_out)],
            router_address=self.get_router(),
            chain_id=self._chain_id,
        )

    def build_swap_tx(
        self,
