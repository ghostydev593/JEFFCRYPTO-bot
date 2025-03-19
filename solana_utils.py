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

logger = logging.getLogger(__name__)

class SolanaUtils:
    def __init__(self, rpc_url: str):
        self.client = AsyncClient(rpc_url, commitment="confirmed")

    async def blacklist_on_raydium(self, mint_address: PublicKey) -> bool:
        """Blacklist the token on Raydium to prevent selling."""
        endpoints = [
            "https://api.raydium.io/v1/blacklist",  # Primary endpoint
            "https://backup.raydium.io/v1/blacklist",  # Fallback endpoint
        ]
        for endpoint in endpoints:
            try:
                async with aiohttp.ClientSession() as session:
                    response = await session.post(
                        endpoint,
                        json={"token_address": str(mint_address)},
                        timeout=10
                    )
                    if response.status == 200:
                        logger.info(f"Token {mint_address} blacklisted on Raydium.")
                        return True
                    else:
                        logger.error(f"Failed to blacklist token on Raydium ({endpoint}): {response.status}")
                        await asyncio.sleep(2)  # Delay before retrying with the next endpoint
            except Exception as error:
                logger.error(json.dumps({"error": "blacklist_on_raydium", "message": str(error)}))
                await asyncio.sleep(2)  # Delay before retrying with the next endpoint
        return False

    async def generate_phantom_deep_link(self, transaction: Transaction) -> str:
        """Generate a Phantom Deep Link with validation."""
        try:
            if transaction.message.instructions == []:
                logger.error("Transaction is empty. Cannot generate a Phantom Deep Link.")
                return None

            serialized_txn = transaction.serialize()
            if not serialized_txn:
                raise ValueError("Transaction serialization failed")
            base64_txn = base64.b64encode(serialized_txn).decode("utf-8")

            # Validate URL length
            if len(base64_txn) > 2000:
                logger.error("Transaction too large for Phantom Deep Link.")
                return None  # Return None instead of a string

            # Additional security checks
            if not self.validate_transaction(transaction):
                logger.error("Transaction failed security checks.")
                return None

            return f"https://phantom.app/ul/v1/?tx={base64_txn}&type=transaction"  # Improved Phantom link format
        except ValueError as error:
            logger.error(json.dumps({"error": "phantom_deep_link", "message": str(error)}))
            return None

    def validate_transaction(self, transaction: Transaction) -> bool:
        """Validate transaction for security."""
        try:
            # Check if the transaction has valid instructions
            if not transaction.message.instructions:
                return False

            # Verify signatures
            if not transaction.verify_signatures():
                logger.error(f"Transaction signature verification failed. Details: {transaction.signatures}")
                return False

            # Add additional checks here (e.g., valid program IDs, etc.)
            return True
        except Exception as error:
            logger.error(json.dumps({"error": "validate_transaction", "message": str(error)}))
            return False

    async def confirm_transaction(self, txid: str) -> bool:
        """Confirm that a transaction is on-chain."""
        try:
            # Check transaction status
            confirmation = await self.client.get_confirmed_transaction(txid)
            if confirmation and confirmation.get("result"):
                return True
            return False
        except Exception as error:
            logger.error(json.dumps({"error": "confirm_transaction", "message": str(error)}))
            return False