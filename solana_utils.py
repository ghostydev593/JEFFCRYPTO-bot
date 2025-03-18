from solana.rpc.async_api import AsyncClient
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.keypair import Keypair
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import create_mint, create_associated_token_account, mint_to, set_authority, AuthorityType
from spl.token.async_client import Token
import base64
import logging
import json

logger = logging.getLogger(__name__)

class SolanaUtils:
    def __init__(self, rpc_url: str):
        self.client = AsyncClient(rpc_url, commitment="confirmed")  # Added commitment="confirmed"

    async def create_and_send_transaction(self, metadata: dict, payer_public_key: PublicKey) -> tuple:
        """Create and send a transaction for token creation."""
        try:
            # Validate metadata before proceeding
            required_fields = ["decimals", "initial_supply"]
            missing_fields = [field for field in required_fields if field not in metadata]

            if missing_fields:
                return None, f"Error: Missing metadata fields: {', '.join(missing_fields)}"

            # Check payer's SOL balance
            try:
                balance_response = await self.client.get_balance(payer_public_key)
                balance = balance_response.get("result", {}).get("value", 0)
            except Exception as error:
                logger.error(json.dumps({"error": "get_balance", "message": str(error)}))
                return None, "Error: Unable to connect to Solana RPC. Try again later."

            # Assuming minimum 0.002 SOL for transaction fees and account rent
            min_balance_required = 200_000  # 0.002 SOL in lamports
            if balance < min_balance_required:
                logger.error("Insufficient SOL balance for transaction.")
                return None, "Error: Insufficient SOL balance. Please fund your wallet."

            # Proceed with token creation
            mint = Keypair.generate()
            transaction = Transaction()
            transaction.add(
                create_mint(
                    mint=mint.public_key,
                    mint_authority=payer_public_key,
                    decimals=metadata["decimals"],
                    program_id=TOKEN_PROGRAM_ID,
                )
            )

            # Create associated token account for the user
            associated_token_account = create_associated_token_account(
                payer=payer_public_key,
                owner=payer_public_key,
                mint=mint.public_key,
                program_id=TOKEN_PROGRAM_ID,
            )
            transaction.add(associated_token_account)

            # Check if the associated token account exists
            token_client = Token(self.client, mint.public_key, TOKEN_PROGRAM_ID, payer_public_key)
            user_token_account = await token_client.get_associated_token_address(payer_public_key)

            account_info = await self.client.get_account_info(user_token_account)
            if account_info["result"]["value"] is None:
                return None, "Error: Associated token account does not exist. Please create one."

            # Mint tokens directly to the user's wallet
            transaction.add(
                mint_to(
                    mint=mint.public_key,
                    dest=payer_public_key,  # ✅ Sends tokens directly to user's Phantom wallet
                    mint_authority=payer_public_key,
                    amount=metadata["initial_supply"],
                )
            )

            # Generate Phantom deep link for the transaction
            deep_link = await self.generate_phantom_deep_link(transaction)
            if not deep_link:
                raise Exception("Failed to generate Phantom Deep Link")

            return mint.public_key, deep_link
        except Exception as error:
            logger.error(json.dumps({"error": "create_and_send_transaction", "message": str(error)}))
            return None, None

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

            return f"https://phantom.app/ul/v1/?tx={base64_txn}&type=transaction"  # Improved Phantom link format
        except ValueError as error:
            logger.error(json.dumps({"error": "phantom_deep_link", "message": str(error)}))
            return None

    async def revoke_authorities(self, mint_address: PublicKey, owner_address: PublicKey) -> str:
        """Generate a transaction to revoke mint, freeze, and update authority."""
        try:
            transaction = Transaction()
            transaction.add(
                set_authority(
                    mint=mint_address,
                    authority_type=AuthorityType.MintTokens,  # Corrected
                    new_authority=None,
                    current_authority=owner_address,
                )
            )
            transaction.add(
                set_authority(
                    mint=mint_address,
                    authority_type=AuthorityType.FreezeAccount,  # Corrected
                    new_authority=None,
                    current_authority=owner_address,
                )
            )

            # Check if the transaction is empty
            if not transaction.instructions:
                logger.error("Transaction is empty. Cannot revoke authorities.")
                return None

            return await self.generate_phantom_deep_link(transaction)
        except Exception as error:
            logger.error(json.dumps({"error": "revoke_authorities", "message": str(error)}))
            return None