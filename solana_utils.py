from solana.rpc.async_api import AsyncClient
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.keypair import Keypair
from solana.system_program import CreateAccountParams, create_account
from solana.sysvar import SYSVAR_RENT_PUBKEY
from solana.instruction import Instruction, AccountMeta
from solana.system_program import SYS_PROGRAM_ID
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    create_mint,
    create_associated_token_account,
    mint_to,
    set_authority,
    AuthorityType
)
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
from datetime import datetime, timedelta
from enum import Enum

# Import sensitive data from config.py
from config import (
    SOLANA_RPC_URL,
    SMART_CONTRACT_PROGRAM_ID,
    WHITELISTED_PROGRAM_IDS,
    MAX_RETRIES,
    RETRY_DELAY,
    USER_RATE_LIMITS,
    DEFAULT_RATE_LIMIT
)

# Constants
DISABLE_SELLING_INSTRUCTION_DISCRIMINATOR = 0
MIN_RETRY_DELAY = 1  # seconds
MAX_RETRY_DELAY = 30  # seconds
RETRY_EXPONENT = 2
MAX_TRANSACTION_SIZE = 1232  # Solana transaction size limit
PHANTOM_DEEP_LINK_MAX_SIZE = 2000  # Phantom URL length limit

class TransactionStatus(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    PENDING = "pending"
    TIMEOUT = "timeout"

@dataclass
class RateLimit:
    """Track rate limits for a user."""
    user_id: str
    requests: int
    interval: int  # in seconds
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
            Tuple: (allowed, remaining_time_seconds)
                  allowed: True if request is allowed
                  remaining_time: Seconds until next allowed request if rate limited
        """
        now = datetime.now()
        rate_limit = self.user_rate_limits.get(user_id)
        
        if not rate_limit:
            # Initialize new rate limit for user from config or defaults
            rate_config = USER_RATE_LIMITS.get(user_id, DEFAULT_RATE_LIMIT)
            rate_limit = RateLimit(
                user_id=user_id,
                requests=rate_config['requests'],
                interval=rate_config['interval'],
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

    async def create_and_send_transaction(
        self,
        metadata: Dict[str, Any],
        wallet_address: PublicKey
    ) -> Tuple[Optional[PublicKey], Optional[str]]:
        """
        Create and send a token creation transaction.
        
        Args:
            metadata: Token metadata dictionary
            wallet_address: User's wallet PublicKey
            
        Returns:
            Tuple: (mint_address, deep_link)
                   mint_address: New token mint address if successful
                   deep_link: Phantom deep link for signing
        """
        # Rate limit check
        allowed, remaining = self.check_rate_limit(str(wallet_address))
        if not allowed:
            logger.warning(f"Rate limit exceeded for {wallet_address}")
            return None, f"Rate limit exceeded. Please wait {remaining} seconds"

        try:
            # Create mint account
            mint_keypair = Keypair()
            
            # Build transaction
            transaction = Transaction()
            transaction.add(
                create_account(
                    CreateAccountParams(
                        from_pubkey=wallet_address,
                        new_account_pubkey=mint_keypair.public_key,
                        lamports=await self.client.get_minimum_balance_for_rent_exemption(165),
                        space=82,
                        program_id=TOKEN_PROGRAM_ID
                    )
                )
            )
            
            # Create mint instruction
            transaction.add(
                create_mint(
                    payer=wallet_address,
                    mint_authority=wallet_address,
                    freeze_authority=wallet_address,
                    decimals=metadata['decimals'],
                    program_id=TOKEN_PROGRAM_ID,
                    mint=mint_keypair.public_key
                )
            )
            
            # Create associated token account
            transaction.add(
                create_associated_token_account(
                    payer=wallet_address,
                    owner=wallet_address,
                    mint=mint_keypair.public_key
                )
            )
            
            # Mint initial supply
            transaction.add(
                mint_to(
                    mint=mint_keypair.public_key,
                    dest=await self._get_associated_token_address(wallet_address, mint_keypair.public_key),
                    amount=metadata['initial_supply'],
                    mint_authority=wallet_address
                )
            )
            
            # Generate deep link
            deep_link = await self._generate_phantom_deep_link_with_retry(transaction)
            if not deep_link:
                logger.error("Failed to generate Phantom deep link")
                return None, "Failed to generate transaction"
            
            return mint_keypair.public_key, deep_link
            
        except Exception as error:
            logger.error(f"Transaction creation failed: {str(error)}")
            return None, f"Transaction failed: {str(error)}"

    async def disable_selling(
        self,
        mint_address: PublicKey,
        wallet_address: PublicKey,
        days: int
    ) -> Optional[str]:
        """
        Disable selling for a token by interacting with a custom smart contract.
        
        Args:
            mint_address: PublicKey of the token mint
            wallet_address: PublicKey of the wallet signing the transaction
            days: Number of days to disable selling (1-7)
            
        Returns:
            str: Phantom deep link for signing the transaction or None if failed
        """
        # Rate limit check
        allowed, remaining = self.check_rate_limit(str(wallet_address))
        if not allowed:
            logger.warning(f"Rate limit exceeded for {wallet_address}")
            return f"Rate limit exceeded. Please wait {remaining} seconds"

        if not 1 <= days <= 7:
            return "Invalid duration (1-7 days only)"

        try:
            # Build the instruction to call the smart contract
            instruction = Instruction(
                program_id=PublicKey(SMART_CONTRACT_PROGRAM_ID),
                data=self._serialize_disable_selling_data(days),
                keys=[
                    AccountMeta(pubkey=mint_address, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=wallet_address, is_signer=True, is_writable=False),
                    AccountMeta(pubkey=SYSVAR_RENT_PUBKEY, is_signer=False, is_writable=False),
                ]
            )
            
            # Create transaction
            transaction = Transaction()
            transaction.add(instruction)
            transaction.fee_payer = wallet_address
            
            # Generate deep link with retry logic
            return await self._generate_phantom_deep_link_with_retry(transaction)
            
        except Exception as error:
            logger.error(f"Disable selling failed: {str(error)}")
            return None

    async def revoke_authorities(
        self,
        mint_address: PublicKey,
        wallet_address: PublicKey
    ) -> Optional[str]:
        """
        Revoke mint, freeze, and update authorities from a token.
        
        Args:
            mint_address: PublicKey of the token mint
            wallet_address: PublicKey of the wallet signing the transaction
            
        Returns:
            str: Phantom deep link for signing the transaction or None if failed
        """
        # Rate limit check
        allowed, remaining = self.check_rate_limit(str(wallet_address))
        if not allowed:
            logger.warning(f"Rate limit exceeded for {wallet_address}")
            return f"Rate limit exceeded. Please wait {remaining} seconds"

        try:
            transaction = Transaction()
            
            # Revoke mint authority
            transaction.add(
                set_authority(
                    account=mint_address,
                    current_authority=wallet_address,
                    authority_type=AuthorityType.MINT_TOKENS,
                    new_authority=None,
                    program_id=TOKEN_PROGRAM_ID
                )
            )
            
            # Revoke freeze authority
            transaction.add(
                set_authority(
                    account=mint_address,
                    current_authority=wallet_address,
                    authority_type=AuthorityType.FREEZE_ACCOUNT,
                    new_authority=None,
                    program_id=TOKEN_PROGRAM_ID
                )
            )
            
            # Generate deep link
            return await self._generate_phantom_deep_link_with_retry(transaction)
            
        except Exception as error:
            logger.error(f"Revoke authorities failed: {str(error)}")
            return None

    async def fetch_token_metadata(
        self,
        token_address: str
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch token metadata from on-chain data with retry logic.
        
        Args:
            token_address: Token mint address as string
            
        Returns:
            Dict: Parsed token metadata or None if failed
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                async with ClientSession() as session:
                    async with session.post(
                        SOLANA_RPC_URL,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getAccountInfo",
                            "params": [
                                token_address,
                                {"encoding": "jsonParsed"}
                            ]
                        },
                        timeout=10
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            value = data.get("result", {}).get("value", {})
                            metadata = value.get("data", [None])[0]
                            
                            if not metadata:
                                continue
                                
                            try:
                                decoded_metadata = base64.b64decode(metadata).decode()
                                parsed_metadata = json.loads(decoded_metadata)
                                return {
                                    "name": parsed_metadata.get("name", "Unknown"),
                                    "symbol": parsed_metadata.get("symbol", "N/A"),
                                    "decimals": parsed_metadata.get("decimals", 0),
                                    "total_supply": parsed_metadata.get("total_supply", 0),
                                    "description": parsed_metadata.get("description", ""),
                                    "image_url": parsed_metadata.get("image_url", None),
                                }
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                return {"raw_metadata": metadata}
            
            except Exception as error:
                logger.warning(f"Metadata fetch attempt {attempt} failed: {str(error)}")
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * attempt)
        
        logger.error("Failed to fetch token metadata after retries")
        return None

    async def confirm_transaction(
        self,
        txid: str
    ) -> Dict[str, Any]:
        """
        Confirm transaction status with exponential backoff.
        
        Args:
            txid: Transaction ID as string
            
        Returns:
            Dict: {
                "status": TransactionStatus,
                "details": Optional[Dict],
                "error": Optional[str]
            }
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                # Use get_transaction instead of deprecated get_confirmed_transaction
                result = await self.client.get_transaction(
                    txid,
                    encoding="jsonParsed"
                )
                
                if result and result.get("result"):
                    return {
                        "status": TransactionStatus.CONFIRMED,
                        "details": result["result"]
                    }
                
                if attempt < self.max_retries:
                    delay = min(
                        MIN_RETRY_DELAY * (RETRY_EXPONENT ** (attempt - 1)),
                        MAX_RETRY_DELAY
                    )
                    await asyncio.sleep(delay)
                    
            except aiohttp.ClientError as error:
                logger.warning(f"Network error confirming transaction (attempt {attempt}): {str(error)}")
                if attempt < self.max_retries:
                    delay = min(
                        MIN_RETRY_DELAY * (RETRY_EXPONENT ** (attempt - 1)),
                        MAX_RETRY_DELAY
                    )
                    await asyncio.sleep(delay)
            except Exception as error:
                logger.error(f"Unexpected error confirming transaction: {str(error)}")
                return {
                    "status": TransactionStatus.FAILED,
                    "error": str(error)
                }
        
        return {
            "status": TransactionStatus.TIMEOUT,
            "error": "Max retries exceeded"
        }

    async def _generate_phantom_deep_link_with_retry(
        self,
        transaction: Transaction
    ) -> Optional[str]:
        """
        Generate Phantom deep link with exponential backoff retry logic.
        
        Args:
            transaction: Prepared Transaction object
            
        Returns:
            str: Phantom deep link URL or None if failed
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                if not self._validate_transaction_security(transaction):
                    logger.error(f"Transaction validation failed on attempt {attempt}")
                    continue
                
                deep_link = await self._generate_phantom_deep_link(transaction)
                if deep_link:
                    return deep_link
                
            except Exception as error:
                logger.warning(f"Deep link generation attempt {attempt} failed: {str(error)}")
            
            if attempt < self.max_retries:
                delay = min(
                    MIN_RETRY_DELAY * (RETRY_EXPONENT ** (attempt - 1)),
                    MAX_RETRY_DELAY
                )
                await asyncio.sleep(delay)
        
        return None

    def _validate_transaction_security(
        self,
        transaction: Transaction
    ) -> bool:
        """
        Validate transaction security before sending to Phantom.
        Note: Does not verify signatures since transaction isn't signed yet.
        
        Args:
            transaction: Transaction to validate
            
        Returns:
            bool: True if transaction passes all security checks
        """
        try:
            # Basic validation
            if not transaction.message.instructions:
                logger.error("Transaction has no instructions")
                return False

            # Program whitelist validation
            for instruction in transaction.message.instructions:
                program_id = transaction.message.account_keys[instruction.program_id_index]
                
                if program_id not in self.whitelisted_programs:
                    logger.error(f"Unauthorized program ID: {str(program_id)}")
                    return False

                # Check for suspicious account access
                for account_index in instruction.accounts:
                    account_key = transaction.message.account_keys[account_index]
                    if account_key == SYS_PROGRAM_ID and program_id != SYS_PROGRAM_ID:
                        logger.error(f"Suspicious system program access by {str(program_id)}")
                        return False

            # Size validation
            serialized = transaction.serialize()
            if len(serialized) > MAX_TRANSACTION_SIZE:
                logger.error(f"Transaction too large: {len(serialized)} bytes")
                return False
                
            return True
            
        except Exception as error:
            logger.error(f"Transaction validation error: {str(error)}")
            return False

    async def _generate_phantom_deep_link(
        self,
        transaction: Transaction
    ) -> Optional[str]:
        """
        Generate a Phantom Deep Link for the given transaction.
        
        Args:
            transaction: The Solana Transaction object
            
        Returns:
            str: The deep link URL or None if failed
        """
        try:
            serialized_txn = transaction.serialize()
            if not serialized_txn:
                raise ValueError("Transaction serialization failed")
            
            base64_txn = base64.b64encode(serialized_txn).decode("utf-8")
            if len(base64_txn) > PHANTOM_DEEP_LINK_MAX_SIZE:
                raise ValueError("Transaction too large for Phantom Deep Link")

            return f"https://phantom.app/ul/v1/?tx={base64_txn}&type=transaction"
            
        except Exception as error:
            logger.error(f"Deep link generation failed: {str(error)}")
            return None

    def _serialize_disable_selling_data(self, days: int) -> bytes:
        """
        Serialize the disable selling instruction data.
        
        Args:
            days: Number of days to disable selling
            
        Returns:
            bytes: Serialized instruction data
        """
        return bytes([DISABLE_SELLING_INSTRUCTION_DISCRIMINATOR]) + days.to_bytes(4, byteorder='little')

    async def _get_associated_token_address(
        self,
        wallet: PublicKey,
        mint: PublicKey
    ) -> PublicKey:
        """Get associated token account address."""
        return await Token.get_associated_token_address(
            owner=wallet,
            mint=mint
        )

    async def close(self):
        """Clean up resources and close connections."""
        try:
            await self.client.close()
            logger.info("Solana client closed successfully")
        except Exception as error:
            logger.error(f"Error closing Solana client: {str(error)}")

    async def __aenter__(self):
        """Support async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure cleanup on context exit."""
        await self.close()