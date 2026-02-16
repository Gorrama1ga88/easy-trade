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
