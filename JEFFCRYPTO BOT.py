import os
import logging
import time
import json
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)
from aiohttp import ClientSession
from pinatapy import PinataPy
from solana_utils import SolanaUtils
from solana.publickey import PublicKey

# Import settings from config.py
from config import (
    TELEGRAM_BOT_TOKEN,
    BOT_PASSWORD,
    SOLANA_RPC_URL,
    PINATA_API_KEY,
    PINATA_SECRET_API_KEY,
)

# Initialize logging with structured logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,  # Use INFO level for production
)
logger = logging.getLogger(__name__)

# Solana client setup
solana_utils = SolanaUtils(SOLANA_RPC_URL)

# Pinata (IPFS) setup
pinata = PinataPy(PINATA_API_KEY, PINATA_SECRET_API_KEY)

# Temporary data storage using a Python dictionary
temporary_storage = {}

# Conversation states
NAME, SYMBOL, DECIMALS, SUPPLY, IMAGE, PHANTOM_WALLET, REVOKE_AUTHORITIES = range(7)

# Dictionary to store authenticated users
authenticated_users = {}

# Utility function for retries with exponential backoff
async def retry_async(func, max_retries=3, delay=1, **kwargs):
    for attempt in range(max_retries):
        try:
            return await func(**kwargs)
        except Exception as error:
            logger.error(json.dumps({"error": func.__name__, "message": str(error), "attempt": attempt + 1}))
            if attempt == max_retries - 1:
                raise error
            await asyncio.sleep(delay * (2 ** attempt))  # Exponential backoff

# Validate metadata input
def validate_metadata(metadata: dict) -> bool:
    """Validate token metadata."""
    try:
        if (not metadata.get("name") or
            not re.match(r"^[a-zA-Z0-9 ]+$", metadata["name"]) or
            len(metadata["name"]) > 30):
            return False
        if (not metadata.get("symbol") or
            not re.match(r"^[a-zA-Z0-9]+$", metadata["symbol"]) or
            len(metadata["symbol"]) > 10):
            return False
        if (not isinstance(metadata.get("decimals"), int) or
            metadata["decimals"] < 0 or metadata["decimals"] > 18):
            return False
        if (not isinstance(metadata.get("initial_supply"), int) or
            metadata["initial_supply"] < 0 or metadata["initial_supply"] > 10**18):
            return False
        return True
    except Exception as error:
        logger.error(json.dumps({"error": "validate_metadata", "message": str(error)}))
        return False

# Fetch token metadata
async def fetch_token_metadata(token_address: str) -> dict:
    """Fetch token metadata from Metaplex API."""
    try:
        async with ClientSession() as session:
            async with session.post("https://api.mainnet-beta.solana.com", json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [token_address, {"encoding": "jsonParsed"}]
            }) as response:
                if response.status == 200:
                    data = await response.json()
                    value = data.get("result", {}).get("value", {})
                    metadata = value.get("data", [None])[0]  # Extract base64 metadata
                    if metadata:
                        if not isinstance(metadata, str) or not metadata.strip():
                            logger.error("Unexpected or empty metadata format received.")
                            return None
                        try:
                            decoded_metadata = base64.b64decode(metadata).decode()
                            parsed_metadata = json.loads(decoded_metadata)
                            return {
                                "name": parsed_metadata.get("name", "Unknown"),
                                "symbol": parsed_metadata.get("symbol", "N/A"),
                                "decimals": parsed_metadata.get("decimals", 0),
                                "total_supply": parsed_metadata.get("total_supply", 0),
                                "image_url": parsed_metadata.get("image_url", None),
                            }
                        except (json.JSONDecodeError, UnicodeDecodeError) as error:
                            logger.error(json.dumps({"error": "fetch_token_metadata", "message": str(error)}))
                            return {"raw_metadata": decoded_metadata}  # Return raw metadata if parsing fails
        return None
    except Exception as error:
        logger.error(json.dumps({"error": "fetch_token_metadata", "message": str(error)}))
        return None

# Upload image to IPFS with retries and fallback
async def upload_image_to_ipfs(image_url: str) -> str:
    """Upload an image to IPFS using Pinata."""
    async def _upload():
        try:
            async with ClientSession() as session:
                async with session.get(image_url, timeout=10) as response:
                    response.raise_for_status()
                    image_data = await response.read()
                    files = {"file": ("image.png", image_data)}
                    headers = {
                        "pinata_api_key": PINATA_API_KEY,
                        "pinata_secret_api_key": PINATA_SECRET_API_KEY,
                    }
                    async with session.post("https://api.pinata.cloud/pinning/pinFileToIPFS", data=files, headers=headers, timeout=10) as ipfs_response:
                        ipfs_response.raise_for_status()
                        ipfs_data = await ipfs_response.json()
                        return f"https://ipfs.io/ipfs/{ipfs_data.get('IpfsHash', '')}"
        except Exception as error:
            logger.error(f"Failed to upload image to IPFS: {error}")
            return None
    return await retry_async(_upload, max_retries=3, delay=2)

# Start command
async def start(update: Update, context: CallbackContext) -> int:
    """Start the bot and ask for a password."""
    user_id = update.message.from_user.id

    if user_id in authenticated_users:
        await update.message.reply_text("✅ You are already authenticated!")
        return MENU

    await update.message.reply_text("🔒 Please enter the password to use this bot:")
    return PASSWORD

# Password handler
async def check_password(update: Update, context: CallbackContext) -> int:
    """Verify the user-provided password."""
    user_id = update.message.from_user.id
    password_attempt = update.message.text.strip()

    if password_attempt == BOT_PASSWORD:
        authenticated_users[user_id] = True
        await update.message.reply_text("✅ Authentication successful! You can now use the bot.")
        return MENU
    else:
        await update.message.reply_text("❌ Incorrect password. Please try again.")
        return PASSWORD

# Step-by-step token creation
async def create_token_start(update: Update, context: CallbackContext) -> int:
    """Start the token creation process."""
    user_id = update.message.from_user.id
    if user_id not in authenticated_users:
        await update.message.reply_text("🔒 You must enter the password first! Use /start.")
        return PASSWORD

    await update.message.reply_text("Please enter the token name (max 30 characters):")
    return NAME

async def create_token_name(update: Update, context: CallbackContext) -> int:
    """Handle token name input."""
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Please enter the token symbol (max 10 characters):")
    return SYMBOL

async def create_token_symbol(update: Update, context: CallbackContext) -> int:
    """Handle token symbol input."""
    context.user_data["symbol"] = update.message.text
    await update.message.reply_text("Please enter the token decimals (0-18):")
    return DECIMALS

async def create_token_decimals(update: Update, context: CallbackContext) -> int:
    """Handle token decimals input."""
    try:
        decimals = int(update.message.text)
        if decimals < 0 or decimals > 18:
            await update.message.reply_text("Decimals must be between 0 and 18. Please try again.")
            return DECIMALS
        context.user_data["decimals"] = decimals
        await update.message.reply_text("Please enter the initial supply (max 1 quintillion):")
        return SUPPLY
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a valid number for decimals.")
        return DECIMALS

async def create_token_supply(update: Update, context: CallbackContext) -> int:
    """Handle token supply input."""
    try:
        initial_supply = int(update.message.text)
        if initial_supply < 0 or initial_supply > 10**18:
            await update.message.reply_text("Initial supply must be between 0 and 1 quintillion. Please try again.")
            return SUPPLY
        context.user_data["initial_supply"] = initial_supply
        await update.message.reply_text("Please enter the image URL (optional):")
        return IMAGE
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a valid number for initial supply.")
        return SUPPLY

async def create_token_image(update: Update, context: CallbackContext) -> int:
    """Handle token image input."""
    context.user_data["image_url"] = update.message.text
    metadata = context.user_data

    if not validate_metadata(metadata):
        await update.message.reply_text("Invalid metadata format. ❌")
        return ConversationHandler.END

    # Ask for the user's Phantom wallet address
    await update.message.reply_text("Please provide your Phantom wallet address:")
    return PHANTOM_WALLET

async def create_token_phantom_wallet(update: Update, context: CallbackContext) -> int:
    """Handle Phantom wallet address input."""
    user_id = update.message.from_user.id
    if user_id not in authenticated_users:
        await update.message.reply_text("🔒 You must enter the password first! Use /start.")
        return PASSWORD

    phantom_wallet_address = update.message.text
    context.user_data["phantom_wallet_address"] = phantom_wallet_address

    # Create and send transaction
    mint_address, deep_link = await solana_utils.create_and_send_transaction(
        context.user_data, PublicKey(phantom_wallet_address)
    if not mint_address:
        error_message = deep_link if deep_link else "Failed to create token. Possible reasons:\n- Insufficient SOL balance\n- RPC connection issues\n- Metadata errors"
        await update.message.reply_text(error_message)
        return ConversationHandler.END

    # Store mint_address in context.user_data
    context.user_data["mint_address"] = str(mint_address)

    # Log transaction hash
    logger.info(json.dumps({
        "action": "create_token",
        "token_address": str(mint_address),
        "deep_link": deep_link,
    }))

    # Save to temporary storage
    temporary_storage[str(mint_address)] = {
        "user_id": update.message.from_user.id,
        "deep_link": deep_link,
        "image_url": context.user_data.get("image_url"),
        "total_supply": context.user_data.get("initial_supply"),
        "created_at": time.time(),
    }

    # Reply with token address and Deep Link
    await update.message.reply_text(
        f"Token creation ready! 🎉\n\n"
        f"Name: {context.user_data['name']}\n"
        f"Symbol: {context.user_data['symbol']}\n"
        f"Decimals: {context.user_data['decimals']}\n"
        f"Initial Supply: {context.user_data['initial_supply']}\n"
        f"Token Address: {mint_address}\n\n"
        f"Click the link below to sign the transaction with Phantom:\n"
        f"{deep_link}",
        parse_mode="MarkdownV2",
    )

    # Prompt to revoke authorities
    await update.message.reply_text(
        "After deploying the token, do you want to revoke freeze authority, mint authority, and update authority? (yes/no)"
    )
    return REVOKE_AUTHORITIES

async def revoke_authorities_prompt(update: Update, context: CallbackContext) -> int:
    """Handle revoke authorities prompt."""
    user_response = update.message.text.lower()
    if user_response == "yes":
        mint_address = context.user_data.get("mint_address")
        phantom_wallet_address = context.user_data.get("phantom_wallet_address")

        # Check if mint_address and phantom_wallet_address are set
        if not mint_address or not phantom_wallet_address:
            await update.message.reply_text("Error: Missing mint address or Phantom wallet address.")
            return ConversationHandler.END

        # Generate revoke authorities Deep Link
        revoke_deep_link = await solana_utils.revoke_authorities(
            PublicKey(mint_address), PublicKey(phantom_wallet_address)
        )
        if not revoke_deep_link:
            await update.message.reply_text("Failed to generate revoke authorities transaction. ❌")
            return ConversationHandler.END

        # Send revoke authorities Deep Link
        await update.message.reply_text(
            f"Click the link below to revoke freeze authority, mint authority, and update authority:\n"
            f"{revoke_deep_link}",
            parse_mode="MarkdownV2",
        )
    else:
        await update.message.reply_text("Token creation completed without revoking authorities.")

    return ConversationHandler.END

# Copy token logic
async def copy_token(update: Update, context: CallbackContext) -> None:
    """Copy an existing token."""
    try:
        if not context.args:
            await update.message.reply_text("Usage: /copy <token_address>")
            return ConversationHandler.END
        token_address = context.args[0]

        # Fetch token metadata (including total supply)
        metadata = await fetch_token_metadata(token_address)
        if metadata is None:
            await update.message.reply_text("Error: Failed to retrieve token metadata. Please try again.")
            return ConversationHandler.END

        if not metadata.get("symbol") or not metadata.get("total_supply"):
            await update.message.reply_text("Error: Incomplete or missing token metadata. Please verify the token address.")
            return ConversationHandler.END

        # Check for missing required fields
        required_fields = ["total_supply", "decimals", "symbol"]
        missing_fields = [field for field in required_fields if field not in metadata]

        if missing_fields:
            await update.message.reply_text(f"Metadata is missing: {', '.join(missing_fields)}. Please verify the token address.")
            return ConversationHandler.END

        # Upload image to IPFS
        new_image_url = None
        if "image_url" in metadata:
            new_image_url = await upload_image_to_ipfs(metadata["image_url"])

        # Ask for the user's Phantom wallet address
        await update.message.reply_text("Please provide your Phantom wallet address:")
        context.user_data["phantom_wallet_address"] = None
        context.user_data["metadata"] = metadata
        context.user_data["new_image_url"] = new_image_url
        return PHANTOM_WALLET

    except Exception as error:
        logger.error(json.dumps({"error": "copy_token", "message": str(error)}))
        await update.message.reply_text("Failed to copy token. Please try again. ❌")

async def copy_token_phantom_wallet(update: Update, context: CallbackContext) -> int:
    """Handle Phantom wallet address input for copying a token."""
    user_id = update.message.from_user.id
    if user_id not in authenticated_users:
        await update.message.reply_text("🔒 You must enter the password first! Use /start.")
        return PASSWORD

    phantom_wallet_address = update.message.text
    context.user_data["phantom_wallet_address"] = phantom_wallet_address

    # Ensure metadata exists and is complete
    metadata = context.user_data.get("metadata")
    if metadata is None:
        await update.message.reply_text("Error: Failed to retrieve token metadata. Please try again.")
        return ConversationHandler.END

    if not metadata.get("symbol") or not metadata.get("total_supply"):
        await update.message.reply_text("Error: Incomplete or missing token metadata. Please verify the token address.")
        return ConversationHandler.END

    # Ensure metadata["total_supply"] exists
    required_fields = ["total_supply", "decimals", "symbol"]
    missing_fields = [field for field in required_fields if field not in metadata]

    if missing_fields:
        await update.message.reply_text(f"Metadata is missing: {', '.join(missing_fields)}. Please verify the token address.")
        return ConversationHandler.END

    # Create and send transaction
    mint_address, deep_link = await solana_utils.create_and_send_transaction(
        metadata, PublicKey(phantom_wallet_address)
    )
    if not mint_address:
        error_message = deep_link if deep_link else "Failed to copy token. Possible reasons:\n- Insufficient SOL balance\n- RPC connection issues\n- Metadata errors"
        await update.message.reply_text(error_message)
        return ConversationHandler.END

    # Wait for transaction confirmation
    await asyncio.sleep(5)  # Give time for confirmation

    # Fetch transaction info
    transaction_info = await solana_utils.client.get_confirmed_transaction(mint_address)
    if not transaction_info:
        await update.message.reply_text("Error: Transaction not confirmed. Please check your wallet.")
        return ConversationHandler.END

    # Store mint_address in context.user_data
    context.user_data["mint_address"] = str(mint_address)

    # Log transaction hash
    logger.info(json.dumps({
        "action": "copy_token",
        "token_address": str(mint_address),
        "deep_link": deep_link,
    }))

    # Save to temporary storage
    temporary_storage[str(mint_address)] = {
        "user_id": update.message.from_user.id,
        "deep_link": deep_link,
        "image_url": context.user_data.get("new_image_url"),
        "total_supply": metadata.get("total_supply"),
        "created_at": time.time(),
    }

    # Reply with token address and Deep Link
    await update.message.reply_text(
        f"Token copied successfully! 🎉\n\n"
        f"Name: {metadata['name']}\n"
        f"Symbol: {metadata['symbol']}\n"
        f"Decimals: {metadata['decimals']}\n"
        f"Initial Supply: {metadata['total_supply']}\n"
        f"Token Address: {mint_address}\n\n"
        f"Click the link below to sign the transaction with Phantom:\n"
        f"{deep_link}",
        parse_mode="MarkdownV2",
    )

    # Prompt to revoke authorities
    await update.message.reply_text(
        "After deploying the token, do you want to revoke freeze authority, mint authority, and update authority? (yes/no)"
    )
    return REVOKE_AUTHORITIES

# Token info command
async def token_info(update: Update, context: CallbackContext) -> None:
    """Fetch and display token metadata."""
    try:
        token_address = context.args[0]

        # Fetch token metadata
        metadata = await fetch_token_metadata(token_address)
        if not metadata:
            await update.message.reply_text("Failed to fetch token metadata. ❌")
            return

        # Send token info
        await update.message.reply_text(
            f"Token Info ℹ️\n\n"
            f"Name: {metadata.get('name')}\n"
            f"Symbol: {metadata.get('symbol')}\n"
            f"Decimals: {metadata.get('decimals')}\n"
            f"Total Supply: {metadata.get('total_supply')}\n"
            f"Image URL: {metadata.get('image_url', 'N/A')}",
            parse_mode="MarkdownV2",
        )
    except Exception as error:
        logger.error(json.dumps({"error": "token_info", "message": str(error)}))await update.message.reply_text("Failed to fetch token info. Please try again. ❌")

# Help command
async def help_command(update: Update, context: CallbackContext) -> None:
    """Display help information."""
    await update.message.reply_text(
        "How to use JEFFCRYPTO BOT 🤖:\n\n"
        "- Use the buttons to create, copy, or explore tokens.\n"
        "- Use /help for assistance.",
        parse_mode="MarkdownV2",
    )

# Error handler
async def error(update: Update, context: CallbackContext) -> None:
    """Handle errors."""
    logger.error(json.dumps({"error": "telegram_bot", "message": str(context.error)}))
    await update.message.reply_text("An error occurred. Please try again later. ❌")

# Main function
def main() -> None:
    """Start the bot."""
    # Build the application
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("copy", copy_token))
    application.add_handler(CommandHandler("tokeninfo", token_info))

    # Conversation handler for password check
    password_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            "PASSWORD": [MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)],
        },
        fallbacks=[],
    )
    application.add_handler(password_conv_handler)

    # Conversation handler for token creation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("create", create_token_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_name)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_symbol)],
            DECIMALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_decimals)],
            SUPPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_supply)],
            IMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_image)],
            PHANTOM_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_phantom_wallet)],
            REVOKE_AUTHORITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, revoke_authorities_prompt)],
        },
        fallbacks=[],
    )
    application.add_handler(conv_handler)

    # Conversation handler for copying a token
    copy_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("copy", copy_token)],
        states={
            PHANTOM_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_token_phantom_wallet)],
            REVOKE_AUTHORITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, revoke_authorities_prompt)],
        },
        fallbacks=[],
    )
    application.add_handler(copy_conv_handler)

    # Error handler
    application.add_error_handler(error)

    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()
       