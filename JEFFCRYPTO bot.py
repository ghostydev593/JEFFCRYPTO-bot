import os
import json
import logging
import time
import re
import base64
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple, Any, Union

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)
from aiohttp import ClientSession
from pinatapy import PinataPy
from solana_utils import SolanaUtils
from solana.publickey import PublicKey
from functools import lru_cache

# Import config
from config import (
    TELEGRAM_BOT_TOKEN,
    BOT_PASSWORD,
    SOLANA_RPC_URL,
    PINATA_API_KEY,
    PINATA_SECRET_API_KEY,
    ADMIN_IDS,
    USER_RATE_LIMITS
)

# Initialize logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Load ADMIN_IDS
if os.path.exists("admins.json"):
    with open("admins.json", "r") as f:
        ADMIN_IDS.extend(json.load(f))

# Initialize services
solana_utils = SolanaUtils(SOLANA_RPC_URL)
pinata = PinataPy(PINATA_API_KEY, PINATA_SECRET_API_KEY)

# State management
temporary_storage = {}
authenticated_users = {}
user_edit_states = {}  # Stores metadata being edited

# Conversation states
(
    PASSWORD, MENU, NAME, SYMBOL, DECIMALS, SUPPLY, 
    IMAGE, DESCRIPTION, PHANTOM_WALLET, DISABLE_SELLING, 
    REVOKE_AUTHORITIES, PREVIEW, EDIT_CHOICE, EDIT_FIELD
) = range(14)

# Constants
MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_IMAGE_SIZE_MB = 5
MAX_NAME_LENGTH = 30
MAX_SYMBOL_LENGTH = 10
MAX_DESCRIPTION_LENGTH = 200

# Helper Functions
async def show_progress(context: CallbackContext, chat_id: int, message: str) -> Any:
    """Show progress to user."""
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ {message}...",
    )
    return msg

async def update_progress(context: CallbackContext, msg: Any, text: str) -> None:
    """Update progress message."""
    await context.bot.edit_message_text(
        text=f"✅ {text}",
        chat_id=msg.chat_id,
        message_id=msg.message_id
    )

async def create_inline_keyboard(options: List[str], columns: int = 2) -> InlineKeyboardMarkup:
    """Create an inline keyboard from options."""
    buttons = []
    for i in range(0, len(options), columns):
        row = options[i:i + columns]
        buttons.append([InlineKeyboardButton(text=opt, callback_data=opt) for opt in row])
    return InlineKeyboardMarkup(buttons)

async def upload_to_ipfs(image_url: str, is_batch: bool = False) -> Optional[Union[str, List[str]]]:
    """Handle both single and batch image uploads to IPFS with retries."""
    async def _upload_single(url: str) -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                async with ClientSession() as session:
                    async with session.get(url, timeout=10) as response:
                        if response.status != 200:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                        
                        image_data = await response.read()
                        if len(image_data) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                            return None
                        
                        result = pinata.pin_file_to_ipfs(image_data)
                        return f"https://ipfs.io/ipfs/{result['IpfsHash']}"
            except Exception as e:
                logger.error(f"Upload attempt {attempt + 1} failed: {str(e)}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        return None

    if is_batch:
        results = []
        for url in image_url:
            result = await _upload_single(url)
            if result:
                results.append(result)
            await asyncio.sleep(1)  # Rate limit between uploads
        return results if results else None
    else:
        return await _upload_single(image_url)

def validate_metadata(metadata: Dict) -> Tuple[bool, Optional[str]]:
    """Validate token metadata with detailed error messages."""
    try:
        if not metadata.get("name"):
            return False, "Token name is required"
        if not re.match(r"^[a-zA-Z0-9 ]+$", metadata["name"]):
            return False, "Name can only contain letters, numbers and spaces"
        if len(metadata["name"]) > MAX_NAME_LENGTH:
            return False, f"Name too long (max {MAX_NAME_LENGTH} chars)"
        
        if not metadata.get("symbol"):
            return False, "Token symbol is required"
        if not re.match(r"^[a-zA-Z0-9]+$", metadata["symbol"]):
            return False, "Symbol can only contain letters and numbers"
        if len(metadata["symbol"]) > MAX_SYMBOL_LENGTH:
            return False, f"Symbol too long (max {MAX_SYMBOL_LENGTH} chars)"
        
        if not isinstance(metadata.get("decimals"), int):
            return False, "Decimals must be a number"
        if metadata["decimals"] < 0 or metadata["decimals"] > 18:
            return False, "Decimals must be between 0 and 18"
        
        if not isinstance(metadata.get("initial_supply"), int):
            return False, "Supply must be a number"
        if metadata["initial_supply"] <= 0:
            return False, "Supply must be positive"
        
        if "description" in metadata and len(metadata["description"]) > MAX_DESCRIPTION_LENGTH:
            return False, f"Description too long (max {MAX_DESCRIPTION_LENGTH} chars)"
        
        return True, None
    except Exception as error:
        logger.error(f"Validation error: {str(error)}")
        return False, "Invalid metadata format"

# Bot Handlers
async def start(update: Update, context: CallbackContext) -> int:
    """Start command with inline keyboard."""
    user_id = update.message.from_user.id
    
    if user_id in authenticated_users:
        keyboard = [
            [InlineKeyboardButton("Create Token", callback_data='create')],
            [InlineKeyboardButton("Copy Token", callback_data='copy')],
            [InlineKeyboardButton("Token Info", callback_data='info')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Welcome back! Choose an option:",
            reply_markup=reply_markup
        )
        return MENU
    else:
        await update.message.reply_text("🔒 Please enter the password to use this bot:")
        return PASSWORD

async def check_password(update: Update, context: CallbackContext) -> int:
    """Verify the user-provided password."""
    user_id = update.message.from_user.id
    password_attempt = update.message.text.strip()

    if password_attempt == BOT_PASSWORD or user_id in ADMIN_IDS:
        authenticated_users[user_id] = True

        if user_id not in ADMIN_IDS:
            ADMIN_IDS.append(user_id)
            with open("admins.json", "w") as f:
                json.dump(ADMIN_IDS, f)

        keyboard = [
            [InlineKeyboardButton("Create Token", callback_data='create')],
            [InlineKeyboardButton("Copy Token", callback_data='copy')],
            [InlineKeyboardButton("Token Info", callback_data='info')]
        ]
        await update.message.reply_text(
            "✅ Authentication successful! Choose an option:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return MENU
    else:
        await update.message.reply_text("❌ Incorrect password. Please try again.")
        return PASSWORD

async def menu_handler(update: Update, context: CallbackContext) -> int:
    """Handle menu selections."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'create':
        await query.edit_message_text("Let's create a new token! Please enter the token name:")
        return NAME
    elif query.data == 'copy':
        await query.edit_message_text("Please enter the token address you want to copy:")
        return await copy_token(update, context)
    elif query.data == 'info':
        await query.edit_message_text("Please enter the token address for info:")
        return await token_info(update, context)
    
    return MENU

async def create_token_name(update: Update, context: CallbackContext) -> int:
    """Handle token name input."""
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Please enter the token symbol (max 10 chars):")
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
        await update.message.reply_text("Please enter the initial supply:")
        return SUPPLY
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a number between 0 and 18.")
        return DECIMALS

async def create_token_supply(update: Update, context: CallbackContext) -> int:
    """Handle token supply input."""
    try:
        initial_supply = int(update.message.text)
        if initial_supply <= 0:
            await update.message.reply_text("Supply must be positive. Please try again.")
            return SUPPLY
        context.user_data["initial_supply"] = initial_supply
        await update.message.reply_text("Please enter the image URL (optional):")
        return IMAGE
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a positive number.")
        return SUPPLY

async def create_token_image(update: Update, context: CallbackContext) -> int:
    """Handle token image input."""
    image_url = update.message.text.strip()
    if image_url and image_url.lower() != 'skip':
        progress_msg = await show_progress(context, update.message.chat_id, "Uploading image to IPFS")
        context.user_data["image_url"] = await upload_to_ipfs(image_url)
        await update_progress(context, progress_msg, "Image uploaded")
    
    await update.message.reply_text("Please enter a description for the token (optional):")
    return DESCRIPTION

async def create_token_description(update: Update, context: CallbackContext) -> int:
    """Handle token description input."""
    context.user_data["description"] = update.message.text
    return await preview_metadata(update, context)

async def preview_metadata(update: Update, context: CallbackContext) -> int:
    """Show metadata preview before submission."""
    metadata = context.user_data
    valid, error = validate_metadata(metadata)
    if not valid:
        await update.message.reply_text(f"❌ Validation error: {error}")
        return ConversationHandler.END
    
    preview_text = (
        f"📝 Metadata Preview:\n\n"
        f"Name: {metadata.get('name')}\n"
        f"Symbol: {metadata.get('symbol')}\n"
        f"Decimals: {metadata.get('decimals')}\n"
        f"Supply: {metadata.get('initial_supply')}\n"
        f"Image: {metadata.get('image_url', 'None')}\n"
        f"Description: {metadata.get('description', 'None')}\n\n"
        f"Does this look correct?"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Confirm", callback_data='confirm')],
        [InlineKeyboardButton("✏️ Edit", callback_data='edit')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
    ]
    await update.message.reply_text(
        preview_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PREVIEW

async def handle_edit_choice(update: Update, context: CallbackContext) -> int:
    """Handle metadata editing choices."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'confirm':
        await query.edit_message_text("Great! Please provide your Phantom wallet address:")
        return PHANTOM_WALLET
    elif query.data == 'edit':
        fields = ["Name", "Symbol", "Decimals", "Supply", "Image", "Description"]
        keyboard = [[InlineKeyboardButton(f, callback_data=f.lower())] for f in fields]
        await query.edit_message_text(
            "Which field would you like to edit?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_CHOICE
    else:
        await query.edit_message_text("Token creation cancelled.")
        return ConversationHandler.END

async def edit_metadata_field(update: Update, context: CallbackContext) -> int:
    """Edit specific metadata field."""
    query = update.callback_query
    await query.answer()
    
    field = query.data
    context.user_data['editing_field'] = field
    prompt = f"Enter new value for {field}:"
    if field == 'image':
        prompt += "\n(Send 'skip' to remove image)"
    await query.edit_message_text(prompt)
    return EDIT_FIELD

async def save_edited_field(update: Update, context: CallbackContext) -> int:
    """Save edited field and return to preview."""
    field = context.user_data.pop('editing_field')
    value = update.message.text
    
    # Special handling for image field
    if field == 'image':
        if value.strip().lower() == 'skip':
            context.user_data['image_url'] = None
        else:
            progress_msg = await show_progress(context, update.message.chat_id, "Uploading new image")
            context.user_data['image_url'] = await upload_to_ipfs(value)
            await update_progress(context, progress_msg, "Image updated")
        return await preview_metadata(update, context)
    
    # Validation for other fields
    if field == 'decimals':
        try:
            value = int(value)
            if not 0 <= value <= 18:
                raise ValueError
            context.user_data['decimals'] = value
        except ValueError:
            await update.message.reply_text("Decimals must be 0-18. Try again:")
            return EDIT_FIELD
    elif field == 'supply':
        try:
            value = int(value)
            if value <= 0:
                raise ValueError
            context.user_data['initial_supply'] = value
        except ValueError:
            await update.message.reply_text("Supply must be positive integer. Try again:")
            return EDIT_FIELD
    else:
        context.user_data[field] = value
    
    return await preview_metadata(update, context)

async def create_token_phantom_wallet(update: Update, context: CallbackContext) -> int:
    """Handle Phantom wallet address input."""
    phantom_wallet_address = update.message.text.strip()
    context.user_data["phantom_wallet_address"] = phantom_wallet_address
    
    progress_msg = await show_progress(context, update.message.chat_id, "Creating token")
    
    try:
        mint_address, deep_link = await solana_utils.create_and_send_transaction(
            context.user_data, 
            PublicKey(phantom_wallet_address)
        )
        
        if not mint_address:
            error_msg = deep_link if deep_link else "Failed to create token"
            await update.message.reply_text(f"❌ Error: {error_msg}")
            return ConversationHandler.END
        
        await update_progress(context, progress_msg, "Token created")
        
        # Store transaction details
        context.user_data["mint_address"] = str(mint_address)
        temporary_storage[str(mint_address)] = {
            "user_id": update.message.from_user.id,
            "deep_link": deep_link,
            "metadata": context.user_data.copy(),
            "created_at": time.time(),
        }
        
        # Show success message
        await update.message.reply_text(
            f"🎉 Token created successfully!\n\n"
            f"Token Address: `{mint_address}`\n\n"
            f"Click below to sign the transaction:\n"
            f"{deep_link}",
            parse_mode="Markdown"
        )
        
        # Ask about disabling selling
        keyboard = [
            [InlineKeyboardButton("Disable Selling", callback_data='disable')],
            [InlineKeyboardButton("Skip", callback_data='skip_disable')]
        ]
        await update.message.reply_text(
            "Would you like to disable selling for a period?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return DISABLE_SELLING
        
    except Exception as e:
        logger.error(f"Token creation failed: {str(e)}")
        await update.message.reply_text("❌ Failed to create token. Please try again.")
        return ConversationHandler.END

async def disable_selling_duration(update: Update, context: CallbackContext) -> int:
    """Handle disable selling duration input."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'skip_disable':
        await query.edit_message_text("Skipped disabling selling.")
        return await prompt_revoke_authorities(update, context)
    
    await query.edit_message_text("How many days to disable selling? (1-7):")
    return DISABLE_SELLING

async def handle_disable_selling(update: Update, context: CallbackContext) -> int:
    """Handle disable selling days input."""
    try:
        days = int(update.message.text)
        if not 1 <= days <= 7:
            await update.message.reply_text("Please enter a number between 1 and 7:")
            return DISABLE_SELLING
        
        mint_address = context.user_data.get("mint_address")
        wallet_address = context.user_data.get("phantom_wallet_address")
        
        if not mint_address or not wallet_address:
            await update.message.reply_text("❌ Error: Missing token or wallet address")
            return ConversationHandler.END
        
        progress_msg = await show_progress(context, update.message.chat_id, "Disabling selling")
        deep_link = await solana_utils.disable_selling(
            PublicKey(mint_address),
            PublicKey(wallet_address),
            days
        )
        
        if not deep_link:
            await update.message.reply_text("❌ Failed to disable selling")
            return await prompt_revoke_authorities(update, context)
        
        await update_progress(context, progress_msg, "Selling disabled")
        await update.message.reply_text(
            f"✅ Selling disabled for {days} days\n\n"
            f"Sign the transaction:\n{deep_link}"
        )
        
        return await prompt_revoke_authorities(update, context)
    
    except ValueError:
        await update.message.reply_text("Please enter a valid number (1-7):")
        return DISABLE_SELLING

async def prompt_revoke_authorities(update: Update, context: CallbackContext) -> int:
    """Prompt user about revoking authorities."""
    keyboard = [
        [InlineKeyboardButton("Revoke Authorities", callback_data='revoke')],
        [InlineKeyboardButton("Skip", callback_data='skip_revoke')]
    ]
    await update.message.reply_text(
        "Revoke freeze/mint/update authorities?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return REVOKE_AUTHORITIES

async def handle_revoke_authorities(update: Update, context: CallbackContext) -> int:
    """Handle revoke authorities choice."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'skip_revoke':
        await query.edit_message_text("Token creation complete!")
        return ConversationHandler.END
    
    mint_address = context.user_data.get("mint_address")
    wallet_address = context.user_data.get("phantom_wallet_address")
    
    if not mint_address or not wallet_address:
        await query.edit_message_text("❌ Error: Missing token or wallet address")
        return ConversationHandler.END
    
    progress_msg = await show_progress(context, query.message.chat_id, "Preparing authority revocation")
    revoke_deep_link = await solana_utils.revoke_authorities(
        PublicKey(mint_address),
        PublicKey(wallet_address)
    )
    
    if not revoke_deep_link:
        await query.edit_message_text("❌ Failed to prepare revocation")
        return ConversationHandler.END
    
    await update_progress(context, progress_msg, "Ready to revoke authorities")
    await query.edit_message_text(
        f"Click to revoke authorities:\n{revoke_deep_link}\n\n"
        f"Token creation complete!"
    )
    return ConversationHandler.END

async def copy_token(update: Update, context: CallbackContext) -> int:
    """Handle copy token command."""
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Please enter the token address to copy:")
            return PHANTOM_WALLET
        
        token_address = update.message.text.strip()
        progress_msg = await show_progress(context, update.message.chat_id, "Fetching token metadata")
        
        metadata = await solana_utils.fetch_token_metadata(token_address)
        if not metadata:
            await update.message.reply_text("❌ Failed to fetch token metadata")
            return ConversationHandler.END
        
        await update_progress(context, progress_msg, "Metadata fetched")
        
        # Store metadata for later use
        context.user_data["metadata"] = metadata
        context.user_data["original_address"] = token_address
        
        # Ask for Phantom wallet
        await update.message.reply_text("Please provide your Phantom wallet address:")
        return PHANTOM_WALLET
    
    except Exception as e:
        logger.error(f"Copy token failed: {str(e)}")
        await update.message.reply_text("❌ Failed to copy token")
        return ConversationHandler.END

async def token_info(update: Update, context: CallbackContext) -> int:
    """Handle token info command."""
    try:
        token_address = update.message.text.strip()
        progress_msg = await show_progress(context, update.message.chat_id, "Fetching token info")
        
        metadata = await solana_utils.fetch_token_metadata(token_address)
        if not metadata:
            await update.message.reply_text("❌ Token not found or invalid address")
            return ConversationHandler.END
        
        await update_progress(context, progress_msg, "Info retrieved")
        
        info_text = (
            f"🔍 Token Info\n\n"
            f"Name: {metadata.get('name', 'Unknown')}\n"
            f"Symbol: {metadata.get('symbol', 'N/A')}\n"
            f"Decimals: {metadata.get('decimals', 0)}\n"
            f"Supply: {metadata.get('total_supply', 'N/A')}\n"
            f"Description: {metadata.get('description', 'None')}\n"
            f"Image: {metadata.get('image_url', 'None')}"
        )
        
        await update.message.reply_text(info_text)
        return ConversationHandler.END
    
    except Exception as e:
        logger.error(f"Token info failed: {str(e)}")
        await update.message.reply_text("❌ Failed to fetch token info")
        return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the current operation."""
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext) -> None:
    """Handle errors with user-friendly messages."""
    error = context.error
    logger.error(f"Error: {str(error)}", exc_info=True)
    
    user_message = "❌ An error occurred. Please try again later."
    if isinstance(error, asyncio.TimeoutError):
        user_message = "⌛ Operation timed out. Please try again."
    elif "rate limit" in str(error).lower():
        user_message = "⚠️ You're sending requests too fast. Please slow down."
    elif isinstance(error, ConnectionError):
        user_message = "🌐 Network error. Please check your connection."
    
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(user_message)
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=user_message
            )
    except Exception as e:
        logger.error(f"Failed to send error message: {str(e)}")

def main() -> None:
    """Start the bot with all handlers."""
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Conversation handler for token creation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)],
            MENU: [CallbackQueryHandler(menu_handler)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_name)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_symbol)],
            DECIMALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_decimals)],
            SUPPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_supply)],
            IMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_image)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_description)],
            PREVIEW: [CallbackQueryHandler(handle_edit_choice)],
            EDIT_CHOICE: [CallbackQueryHandler(edit_metadata_field)],
            EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_field)],
            PHANTOM_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_phantom_wallet)],
            DISABLE_SELLING: [
                CallbackQueryHandler(disable_selling_duration, pattern='^disable$|^skip_disable$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_disable_selling)
            ],
            REVOKE_AUTHORITIES: [
                CallbackQueryHandler(handle_revoke_authorities, pattern='^revoke$|^skip_revoke$')
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # Command handlers
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('copy', copy_token))
    app.add_handler(CommandHandler('tokeninfo', token_info))
    app.add_handler(CommandHandler('help', help_command))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Start the bot
    app.run_polling()

async def help_command(update: Update, context: CallbackContext) -> None:
    """Display help information."""
    help_text = (
        "🛠️ JEFFCRYPTO BOT Help\n\n"
        "• /start - Begin token creation\n"
        "• /copy <address> - Copy an existing token\n"
        "• /tokeninfo <address> - Get token info\n"
        "• /help - Show this message\n\n"
        "During creation you can:\n"
        "- Preview metadata before submission\n"
        "- Edit any field before finalizing\n"
        "- Disable selling for 1-7 days\n"
        "- Revoke authorities after creation"
    )
    await update.message.reply_text(help_text)

if __name__ == '__main__':
    main()