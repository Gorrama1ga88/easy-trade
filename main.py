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
