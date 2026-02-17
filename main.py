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
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        deadline: Optional[int] = None,
        from_address: Optional[str] = None,
        gas_limit: int = DEFAULT_GAS_LIMIT_SWAP,
    ) -> dict[str, Any]:
        deadline = deadline or (int(time.time()) + DEFAULT_DEADLINE_OFFSET_SEC)
        token_in = to_checksum(token_in)
        token_out = to_checksum(token_out)
        fn = self._contract.functions.executeSwapExactIn(
            token_in, token_out, amount_in, amount_out_min, deadline
        )
        tx = fn.build_transaction(
            {
                "from": to_checksum(from_address) if from_address else None,
                "gas": gas_limit,
            }
        )
        return tx

    def build_swap_multihop_tx(
        self,
        path: list[str],
        amount_in: int,
        amount_out_min: int,
        deadline: Optional[int] = None,
        from_address: Optional[str] = None,
        gas_limit: int = DEFAULT_GAS_LIMIT_MULTIHOP,
    ) -> dict[str, Any]:
        if len(path) < MIN_PATH_LEN or len(path) > MAX_PATH_LEN:
            raise ValueError("path length must be between 2 and 6")
        deadline = deadline or (int(time.time()) + DEFAULT_DEADLINE_OFFSET_SEC)
        path = [to_checksum(p) for p in path]
        fn = self._contract.functions.executeSwapExactInMultiHop(
            path, amount_in, amount_out_min, deadline
        )
        tx = fn.build_transaction(
            {
                "from": to_checksum(from_address) if from_address else None,
                "gas": gas_limit,
            }
        )
        return tx

    def execute_swap(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        private_key: Optional[str] = None,
        account: Optional["LocalAccount"] = None,
        deadline: Optional[int] = None,
        gas_limit: int = DEFAULT_GAS_LIMIT_SWAP,
        gas_price: Optional[int] = None,
        max_fee_per_gas: Optional[int] = None,
        max_priority_fee_per_gas: Optional[int] = None,
    ) -> SwapReceipt:
        if account is None and private_key:
            if Account is None:
                raise RuntimeError("eth_account not installed")
            account = Account.from_key(private_key)
        if account is None:
            raise ValueError("provide either private_key or account")
        if self.is_paused():
            raise RuntimeError("aggregator is paused")
        tx = self.build_swap_tx(
            token_in, token_out, amount_in, amount_out_min,
            deadline=deadline, from_address=account.address, gas_limit=gas_limit,
        )
        tx.pop("from", None)
        if gas_price is not None:
            tx["gasPrice"] = gas_price
        if max_fee_per_gas is not None:
            tx["maxFeePerGas"] = max_fee_per_gas
        if max_priority_fee_per_gas is not None:
            tx["maxPriorityFeePerGas"] = max_priority_fee_per_gas
        signed = account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        success = receipt["status"] == 1
        amount_out = 0
        fee_wei = 0
        swap_id = self.get_swap_count()
        if success and receipt.get("logs"):
            # Parse logs if needed; here we just use swap count
            pass
        return SwapReceipt(
            tx_hash=tx_hash.hex(),
            amount_in=amount_in,
            amount_out=amount_out,
            fee_wei=fee_wei,
            swap_id=swap_id,
            success=success,
            block_number=receipt.get("blockNumber"),
            gas_used=receipt.get("gasUsed"),
        )

    def execute_swap_multihop(
        self,
        path: list[str],
        amount_in: int,
        amount_out_min: int,
        private_key: Optional[str] = None,
        account: Optional["LocalAccount"] = None,
        deadline: Optional[int] = None,
        gas_limit: int = DEFAULT_GAS_LIMIT_MULTIHOP,
    ) -> SwapReceipt:
        if account is None and private_key:
            if Account is None:
                raise RuntimeError("eth_account not installed")
            account = Account.from_key(private_key)
        if account is None:
            raise ValueError("provide either private_key or account")
        if self.is_paused():
            raise RuntimeError("aggregator is paused")
        tx = self.build_swap_multihop_tx(
            path, amount_in, amount_out_min,
            deadline=deadline, from_address=account.address, gas_limit=gas_limit,
        )
        tx.pop("from", None)
        signed = account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        success = receipt["status"] == 1
        return SwapReceipt(
            tx_hash=tx_hash.hex(),
            amount_in=amount_in,
            amount_out=0,
            fee_wei=0,
            swap_id=self.get_swap_count(),
            success=success,
            block_number=receipt.get("blockNumber"),
            gas_used=receipt.get("gasUsed"),
        )


# -----------------------------------------------------------------------------
# Router (getAmountsOut) helper for off-chain quote
# -----------------------------------------------------------------------------


def get_amounts_out_via_router(
    w3: "Web3",
    router_address: str,
    amount_in: int,
    path: list[str],
) -> list[int]:
    path = [to_checksum(p) for p in path]
    router_contract = get_contract(w3, router_address, ROUTER_GET_AMOUNTS_OUT_ABI)
    amounts = router_contract.functions.getAmountsOut(amount_in, path).call()
    return list(amounts)


def quote_via_router(
    w3: "Web3",
    router_address: str,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> int:
    path = [to_checksum(token_in), to_checksum(token_out)]
    amounts = get_amounts_out_via_router(w3, router_address, amount_in, path)
    return amounts[-1] if amounts else 0


# -----------------------------------------------------------------------------
# Token helpers
# -----------------------------------------------------------------------------


def get_token_decimals(w3: "Web3", token_address: str) -> int:
    try:
        c = get_erc20(w3, token_address)
        return c.functions.decimals().call()
    except Exception:
        return 18


def get_token_balance(w3: "Web3", token_address: str, account: str) -> int:
    c = get_erc20(w3, token_address)
    return c.functions.balanceOf(to_checksum(account)).call()


def format_amount(amount: int, decimals: int) -> str:
    return str(Decimal(amount) / (10**decimals))


def parse_amount(amount_human: str, decimals: int) -> int:
    return int(Decimal(amount_human) * (10**decimals))


# -----------------------------------------------------------------------------
# Event parsing (KiteSwapExecuted)
# -----------------------------------------------------------------------------


KITE_SWAP_EXECUTED_TOPIC = None

def _kite_swap_topic():
    global KITE_SWAP_EXECUTED_TOPIC
    if KITE_SWAP_EXECUTED_TOPIC is None and Web3 is not None:
        KITE_SWAP_EXECUTED_TOPIC = Web3.keccak(
            text="KiteSwapExecuted(address,address,address,uint256,uint256,uint256,uint256)"
        )
    return KITE_SWAP_EXECUTED_TOPIC


def get_kite_swap_topic() -> bytes:
    """Return the event topic for KiteSwapExecuted (for log filtering)."""
    t = _kite_swap_topic()
    return t if t is not None else b""


def parse_swap_log(log_entry: dict, aggregator_address: str) -> Optional[dict[str, Any]]:
    try:
        if log_entry.get("address", "").lower() != aggregator_address.lower():
            return None
        topics = log_entry.get("topics", [])
        if not topics or (HexBytes(topics[0]) if isinstance(topics[0], str) else topics[0]) != _kite_swap_topic():
            return None
        data = log_entry.get("data", "0x")
        if isinstance(data, str) and data.startswith("0x"):
            data = bytes.fromhex(data[2:])
        if len(data) < 3 * 32:
            return None
        amount_in = int.from_bytes(data[0:32], "big")
        amount_out = int.from_bytes(data[32:64], "big")
        fee_wei = int.from_bytes(data[64:96], "big")
        swap_id = int.from_bytes(data[96:128], "big")
        return {
            "trader": "0x" + (log_entry["topics"][1].hex()[-40:] if len(log_entry["topics"]) > 1 else ""),
            "amount_in": amount_in,
            "amount_out": amount_out,
            "fee_wei": fee_wei,
            "swap_id": swap_id,
        }
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Retry and backoff
# -----------------------------------------------------------------------------


def with_retry(
    fn: Callable[[], Any],
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Any:
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except exceptions as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (backoff ** attempt))
    raise last_err


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="EasySwap SDK — quote and swap via EasyTrade")
    parser.add_argument("--chain", type=int, default=1, help="Chain ID")
    parser.add_argument("--rpc", type=str, default=None, help="RPC URL")
    parser.add_argument("--aggregator", type=str, required=True, help="EasyTrade contract address")
    sub = parser.add_subparsers(dest="cmd", required=True)
    # quote
    p_quote = sub.add_parser("quote", help="Get quote for tokenIn -> tokenOut")
    p_quote.add_argument("token_in", type=str)
    p_quote.add_argument("token_out", type=str)
    p_quote.add_argument("amount_in", type=str, help="Human-readable amount, e.g. 1.5")
    p_quote.add_argument("--decimals-in", type=int, default=18)
    p_quote.add_argument("--slippage-bps", type=int, default=AGGREGATOR_SLIPPAGE_BPS)
    # swap-count
    p_count = sub.add_parser("swap-count", help="Get total swap count")
    # info
    p_info = sub.add_parser("info", help="Aggregator info (router, fee collector, paused)")
    args = parser.parse_args()

    w3 = get_w3(args.chain, args.rpc)
    client = EasySwapClient(w3, args.aggregator, args.chain)

    if args.cmd == "quote":
        amount_raw = parse_amount(args.amount_in, args.decimals_in)
        q = client.quote_exact_in_with_slippage(
            args.token_in, args.token_out, amount_raw, slippage_bps=args.slippage_bps
        )
        print(json.dumps(q.to_dict(), indent=2))
        decimals_out = get_token_decimals(w3, args.token_out)
        print("amount_out_min (human):", format_amount(q.amount_out_min_suggested, decimals_out))
    elif args.cmd == "swap-count":
        print(client.get_swap_count())
    elif args.cmd == "info":
        print("router:", client.get_router())
        print("fee_collector:", client.get_fee_collector())
        print("weth:", client.get_weth())
        print("paused:", client.is_paused())
        print("swap_count:", client.get_swap_count())


if __name__ == "__main__":
    main()


# -----------------------------------------------------------------------------
# Mock / testing (local simulation without chain)
# -----------------------------------------------------------------------------


class MockEasySwapClient:
    """In-memory mock for tests; no RPC."""

    def __init__(self, chain_id: int = 1):
        self._chain_id = chain_id
        self._swap_count = 0
        self._paused = False
        self._router = "0x" + "1" * 40
        self._fee_collector = "0x" + "2" * 40
        self._weth = "0x" + "3" * 40

    @property
    def chain_id(self) -> int:
        return self._chain_id

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def get_router(self) -> str:
        return self._router

    def get_fee_collector(self) -> str:
        return self._fee_collector

    def get_weth(self) -> str:
        return self._weth

    def get_swap_count(self) -> int:
        return self._swap_count

    def quote_exact_in(self, token_in: str, token_out: str, amount_in: int) -> int:
        if amount_in <= 0:
            return 0
        # Mock: 1:1 with 0.1% fee deducted from output
        fee = amount_in * FEE_BPS // BPS_DENOM
        return amount_in - fee

    def quote_exact_in_with_slippage(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        slippage_bps: int = AGGREGATOR_SLIPPAGE_BPS,
    ) -> QuoteResult:
        amount_out_est = self.quote_exact_in(token_in, token_out, amount_in)
        amount_out_min = apply_slippage_bps(amount_out_est, slippage_bps)
        return QuoteResult(
            amount_in=amount_in,
            amount_out_est=amount_out_est,
            amount_out_min_suggested=amount_out_min,
            fee_bps=FEE_BPS,
            path=[token_in, token_out],
            router_address=self._router,
            chain_id=self._chain_id,
        )

    def simulate_swap(self, amount_in: int) -> SwapReceipt:
        self._swap_count += 1
        fee = fee_from_amount_bps(amount_in)
        amount_out = self.quote_exact_in("0x0", "0x0", amount_in)  # dummy tokens
        return SwapReceipt(
            tx_hash="0x" + "0" * 64,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_wei=fee,
            swap_id=self._swap_count,
            success=True,
        )


# -----------------------------------------------------------------------------
# Config loader (env + file)
# -----------------------------------------------------------------------------


def load_config(
    config_path: Optional[str] = None,
    env_prefix: str = "EASYSWAP_",
) -> dict[str, Any]:
    out = {}
    if config_path and os.path.isfile(config_path):
        with open(config_path, "r") as f:
            try:
                out = json.load(f)
            except json.JSONDecodeError:
                pass
    for key, value in os.environ.items():
        if key.startswith(env_prefix):
            k = key[len(env_prefix):].lower()
            if value.isdigit():
                out[k] = int(value)
            elif value.lower() in ("true", "false"):
                out[k] = value.lower() == "true"
            else:
                out[k] = value
    return out


def create_client_from_config(config: Optional[dict[str, Any]] = None) -> EasySwapClient:
    config = config or load_config()
    chain_id = config.get("chain_id", 1)
    rpc = config.get("rpc_url") or CHAIN_RPC.get(chain_id)
    aggregator = config.get("aggregator_address")
    if not aggregator:
        raise ValueError("config must contain aggregator_address (or EASYSWAP_AGGREGATOR_ADDRESS)")
    w3 = get_w3(chain_id, rpc)
    return EasySwapClient(w3, aggregator, chain_id)


# -----------------------------------------------------------------------------
# Batch quote (multiple pairs)
# -----------------------------------------------------------------------------


def batch_quote(
    client: EasySwapClient,
    pairs: list[tuple[str, str]],
    amount_in: int,
    slippage_bps: int = AGGREGATOR_SLIPPAGE_BPS,
) -> list[QuoteResult]:
    results = []
    for token_in, token_out in pairs:
        try:
            q = client.quote_exact_in_with_slippage(
                token_in, token_out, amount_in, slippage_bps=slippage_bps
            )
            results.append(q)
        except Exception as e:
            logger.warning("quote failed for %s -> %s: %s", token_in, token_out, e)
    return results


# -----------------------------------------------------------------------------
# Price impact (simplified)
# -----------------------------------------------------------------------------


def price_impact_bps(amount_in: int, amount_out: int, reserve_in: int, reserve_out: int) -> int:
    if reserve_in == 0 or reserve_out == 0:
        return 0
    # constant product: amount_out ~ reserve_out * amount_in / (reserve_in + amount_in)
    expected_out = (reserve_out * amount_in) // (reserve_in + amount_in)
    if expected_out == 0:
        return 10000
    impact = (expected_out - amount_out) * BPS_DENOM // expected_out
    return max(0, impact)


# -----------------------------------------------------------------------------
# Deadline helpers
# -----------------------------------------------------------------------------


def deadline_from_now(offset_sec: int = DEFAULT_DEADLINE_OFFSET_SEC) -> int:
    return int(time.time()) + offset_sec


def deadline_from_block(w3: "Web3", block_offset: int = 20) -> int:
    if Web3 is None:
        return deadline_from_now(300)
    block = w3.eth.block_number
    return block + block_offset


# -----------------------------------------------------------------------------
# Gas estimation
# -----------------------------------------------------------------------------


def estimate_swap_gas(
    client: EasySwapClient,
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    from_address: str,
) -> int:
    try:
        tx = client.build_swap_tx(
            token_in, token_out, amount_in, amount_out_min,
            from_address=from_address,
        )
        tx["from"] = to_checksum(from_address)
        tx["gas"] = None
        est = client._w3.eth.estimate_gas(tx)
        return est
    except Exception as e:
        logger.warning("gas estimation failed: %s", e)
        return DEFAULT_GAS_LIMIT_SWAP


# -----------------------------------------------------------------------------
# Allowance check / approval
# -----------------------------------------------------------------------------


def check_allowance(w3: "Web3", token: str, owner: str, spender: str) -> int:
    c = get_erc20(w3, token)
    return c.functions.allowance(to_checksum(owner), to_checksum(spender)).call()


def ensure_allowance(
    w3: "Web3",
    token: str,
    owner: "LocalAccount",
    spender: str,
    amount: int,
    private_key: Optional[str] = None,
) -> bool:
    current = check_allowance(w3, token, owner.address, spender)
    if current >= amount:
        return True
    if Account is None:
        raise RuntimeError("eth_account required for approval tx")
    acc = owner if isinstance(owner, LocalAccount) else Account.from_key(private_key)
    c = get_erc20(w3, token)
    tx = c.functions.approve(to_checksum(spender), 2**256 - 1).build_transaction(
        {"from": acc.address}
    )
    tx.pop("from", None)
    signed = acc.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return True


# Add allowance to ERC20_ABI for check_allowance
ERC20_ABI.append(
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
)


# -----------------------------------------------------------------------------
# Network detection
# -----------------------------------------------------------------------------


def detect_chain_from_rpc(rpc_url: str) -> int:
    if Web3 is None:
        raise RuntimeError("web3 not installed")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"Could not connect to RPC: {rpc_url}")
    return w3.eth.chain_id


# -----------------------------------------------------------------------------
# Export list
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Chain-specific WETH and common token addresses (for reference only)
# -----------------------------------------------------------------------------

WETH_BY_CHAIN: dict[int, str] = {
    1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    5: "0xB4FBF271143F4FBf7B91A5ded31805e42b2208d6",
    10: "0x4200000000000000000000000000000000000006",
    137: "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    42161: "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    8453: "0x4200000000000000000000000000000000000006",
    56: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    43114: "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
}


def get_weth_for_chain(chain_id: int) -> Optional[str]:
    return WETH_BY_CHAIN.get(chain_id)


# -----------------------------------------------------------------------------
# Human-readable chain names
# -----------------------------------------------------------------------------

CHAIN_NAMES: dict[int, str] = {
    1: "Ethereum",
    5: "Goerli",
    10: "Optimism",
    137: "Polygon",
    42161: "Arbitrum One",
    8453: "Base",
    56: "BSC",
    43114: "Avalanche C-Chain",
}


def chain_name(chain_id: int) -> str:
    return CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------


def is_valid_evm_address(addr: str) -> bool:
    if not addr or len(addr) != 42:
        return False
    if addr[:2] != "0x":
        return False
    try:
        int(addr[2:], 16)
        return True
    except ValueError:
        return False


def validate_path(path: Sequence[str]) -> None:
    if len(path) < MIN_PATH_LEN or len(path) > MAX_PATH_LEN:
        raise ValueError(f"path length must be between {MIN_PATH_LEN} and {MAX_PATH_LEN}")
    for p in path:
        if not is_valid_evm_address(p):
            raise ValueError(f"invalid address in path: {p}")


# -----------------------------------------------------------------------------
# Log filtering (fetch KiteSwapExecuted in block range)
# -----------------------------------------------------------------------------


def fetch_swap_events(
    w3: "Web3",
    aggregator_address: str,
    from_block: BlockIdentifier,
    to_block: BlockIdentifier = "latest",
) -> list[dict[str, Any]]:
    if Web3 is None:
        return []
    topic = _kite_swap_topic()
    if topic is None:
        return []
    logs = w3.eth.get_logs(
        {
            "address": to_checksum(aggregator_address),
            "topics": [topic],
            "fromBlock": from_block,
            "toBlock": to_block,
        }
    )
    out = []
    for log_entry in logs:
        parsed = parse_swap_log(dict(log_entry), aggregator_address)
        if parsed:
            out.append(parsed)
    return out


# -----------------------------------------------------------------------------
# Exports
# -----------------------------------------------------------------------------

__all__ = [
    "EasySwapClient",
    "MockEasySwapClient",
    "QuoteResult",
