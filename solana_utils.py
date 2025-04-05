# solana_utils.py
from solana.rpc.async_api import AsyncClient
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.keypair import Keypair
from solana.system_program import CreateAccountParams, create_account
from solana.sysvar import SYSVAR_RENT_PUBKEY
from solana.instruction import Instruction, AccountMeta
from solana.system_program import SYS_PROGRAM_ID
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import create_mint, create_associated_token_account, mint_to, set_authority, AuthorityType
from spl.token.async_client import Token
import base64
import logging
import json
import aiohttp
import time
from functools import lru_cache
import asyncio
from typing import List, Optional, Dict, Any, Tuple, Union
from dataclasses import dataclass
from datetime import datetime

# Import sensitive data from config.py
from config import (
    SOLANA_RPC_URL,
    SMART_CONTRACT_PROGRAM_ID,
    WHITELISTED_PROGRAM_IDS,
    MAX_RETRIES,
    RETRY_DELAY,
    USER_RATE_LIMITS,
)

# Constants
DISABLE_SELLING_INSTRUCTION_DISCRIMINATOR = 0
MIN_RETRY_DELAY = 1  # seconds
MAX_RETRY_DELAY = 30  # seconds
RETRY_EXPONENT = 2
DEFAULT_RATE_LIMIT = USER_RATE_LIMITS.get('default', {'requests': 5, 'interval': 60})  # 5 requests per minute

logger = logging.getLogger(__name__)

@dataclass
class RateLimit:
    user_id: str
    requests: int
    interval: int
    timestamps: List[datetime]

class SolanaUtils:
    def __init__(self, rpc_url: str = SOLANA_RPC_URL):
        """
        Initialize SolanaUtils with RPC connection and configuration.
        
        Args:
            rpc_url: Solana RPC endpoint URL
        """
        self.client = AsyncClient(rpc_url, commitment="confirmed")
        self.max_retries = MAX_RETRIES
        self.retry_delay = RETRY_DELAY
        self.whitelisted_programs = [PublicKey(pid) for pid in WHITELISTED_PROGRAM_IDS]
        self.user_rate_limits: Dict[str, RateLimit] = {}
        logger.info("SolanaUtils initialized with RPC: %s", rpc_url)

    def check_rate_limit(self, user_id: str) -> Tuple[bool, Optional[int]]:
        """
        Check if user has exceeded rate limits.
        
        Args:
            user_id: Unique user identifier
            
        Returns:
            Tuple: (allowed, remaining_time)
        """
        now = datetime.now()
        rate_limit = self.user_rate_limits.get(user_id)
        
        if not rate_limit:
            # Initialize new rate limit for user
            rate_limit = RateLimit(
                user_id=user_id,
                requests=DEFAULT_RATE_LIMIT['requests'],
                interval=DEFAULT_RATE_LIMIT['interval'],
                timestamps=[]
            )
            self.user_rate_limits[user_id] = rate_limit
        
        # Remove old timestamps outside the interval window
        rate_limit.timestamps = [
            ts for ts in rate_limit.timestamps 
            if (now - ts).total_seconds() <= rate_limit.interval
        ]
        
        if len(rate_limit.timestamps) >= rate_limit.requests:
            oldest_request = rate_limit.timestamps[0]
            remaining_time = rate_limit.interval - (now - oldest_request).total_seconds()
            return False, int(remaining_time)
        
        rate_limit.timestamps.append(now)
        return True, None

    async def disable_selling(self, mint_address: PublicKey, wallet_address: PublicKey, days: int) -> Optional[str]:
        """[Previous implementation with added rate limiting]"""
        # Rate limit check
        allowed, remaining = self.check_rate_limit(str(wallet_address))
        if not allowed:
            logger.warning(f"Rate limit exceeded for {wallet_address}, please wait {remaining} seconds")
            return None
        
        # [Rest of existing implementation...]

    # [Previous methods with added type hints and docstrings...]

    async def confirm_transaction(self, txid: str) -> Dict[str, Any]:
        """[Previous implementation with progress tracking]"""
        log_context = {"txid": txid}
        
        for attempt in range(1, self.max_retries + 1):
            try:
                # Notify progress
                if attempt > 1:
                    logger.info(f"Retrying confirmation (attempt {attempt}/{self.max_retries})")
                
                result = await self.client.get_transaction(txid, encoding="jsonParsed")
                
                if result and result.get("result"):
                    logger.info("Transaction confirmed successfully")
                    return {
                        "confirmed": True,
                        "status": "confirmed",
                        "details": result["result"]
                    }
                
                if attempt < self.max_retries:
                    delay = min(MIN_RETRY_DELAY * (RETRY_EXPONENT ** (attempt - 1)), MAX_RETRY_DELAY)
                    await asyncio.sleep(delay)
                    
            except Exception as error:
                logger.error(f"Attempt {attempt} failed: {str(error)}")
                if attempt < self.max_retries:
                    delay = min(MIN_RETRY_DELAY * (RETRY_EXPONENT ** (attempt - 1)), MAX_RETRY_DELAY)
                    await asyncio.sleep(delay)
        
        logger.error("Failed to confirm transaction after retries")
        return {
            "confirmed": False,
            "status": "failed_after_retries",
            "error": "Max retries exceeded"
        }