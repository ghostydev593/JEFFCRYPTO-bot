import os
import json
import logging
import time
import re
import base64
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple, Any, Union

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)
from aiohttp import ClientSession, ClientWebSocketResponse
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
user_edit_states = {}
tx_monitor_tasks = {}

# Constants
MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_IMAGE_SIZE_MB = 5
MAX_NAME_LENGTH = 30
MAX_SYMBOL_LENGTH = 10
MAX_DESCRIPTION_LENGTH = 200

EXPLORER_URLS = {
    'Solscan': 'https://solscan.io/token/{}',
    'SolanaFM': 'https://solana.fm/address/{}',
    'Dexlab': 'https://dexlab.space/token/{}',
    'Raydium': 'https://raydium.io/swap/?inputCurrency={}'
}

# Conversation states
(
    PASSWORD, MENU, NAME, SYMBOL, DECIMALS_CHOICE, SUPPLY, 
    IMAGE, DESCRIPTION, PHANTOM_WALLET, DISABLE_SELLING_CHOICE,
    DISABLE_DAYS, REVOKE_AUTHORITIES_CHOICE, PREVIEW, 
    EDIT_CHOICE, EDIT_FIELD, POST_CREATION_ACTIONS
) = range(16)

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

async def upload_to_ipfs(image_url: str) -> Optional[str]:
    """Handle image uploads to IPFS with retries."""
    for attempt in range(MAX_RETRIES):
        try:
            async with ClientSession() as session:
                async with session.get(image_url, timeout=10) as response:
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

async def start(update: Update, context: CallbackContext) -> int:
    """Start command with enhanced inline keyboard."""
    user_id = update.message.from_user.id
    
    if user_id in authenticated_users:
        keyboard = [
            [InlineKeyboardButton("🆕 Create Token", callback_data='create')],
            [InlineKeyboardButton("©️ Copy Token", callback_data='copy')],
            [InlineKeyboardButton("ℹ️ Token Info", callback_data='info')],
            [InlineKeyboardButton("📊 My Tokens", callback_data='my_tokens')]
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
            [InlineKeyboardButton("🆕 Create Token", callback_data='create')],
            [InlineKeyboardButton("©️ Copy Token", callback_data='copy')],
            [InlineKeyboardButton("ℹ️ Token Info", callback_data='info')],
            [InlineKeyboardButton("📊 My Tokens", callback_data='my_tokens')]
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
    elif query.data == 'my_tokens':
        return await show_user_tokens(update, context)
    
    return MENU

async def create_token_name(update: Update, context: CallbackContext) -> int:
    """Handle token name input."""
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Please enter the token symbol (max 10 chars):")
    return SYMBOL

async def create_token_symbol(update: Update, context: CallbackContext) -> int:
    """Handle token symbol input."""
    context.user_data["symbol"] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton(str(i), callback_data=str(i)) for i in range(0, 6)],
        [InlineKeyboardButton(str(i), callback_data=str(i)) for i in range(6, 12)],
        [InlineKeyboardButton("Custom (0-18)", callback_data='custom')]
    ]
    
    await update.message.reply_text(
        "Select token decimals:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DECIMALS_CHOICE

async def handle_decimals_choice(update: Update, context: CallbackContext) -> int:
    """Handle decimals selection."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'custom':
        await query.edit_message_text("Enter custom decimals (0-18):")
        return DECIMALS_CHOICE
    
    try:
        decimals = int(query.data)
        if 0 <= decimals <= 18:
            context.user_data["decimals"] = decimals
            await query.edit_message_text(f"Selected decimals: {decimals}\n\nEnter initial supply:")
            return SUPPLY
        else:
            await query.edit_message_text("Invalid decimals. Please select 0-18:")
            return DECIMALS_CHOICE
    except ValueError:
        await query.edit_message_text("Invalid input. Please try again.")
        return DECIMALS_CHOICE

async def handle_decimals_input(update: Update, context: CallbackContext) -> int:
    """Handle custom decimals input."""
    try:
        decimals = int(update.message.text)
        if 0 <= decimals <= 18:
            context.user_data["decimals"] = decimals
            await update.message.reply_text(f"Set decimals to {decimals}\n\nEnter initial supply:")
            return SUPPLY
        else:
            await update.message.reply_text("Decimals must be 0-18. Please try again:")
            return DECIMALS_CHOICE
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a number 0-18:")
        return DECIMALS_CHOICE

async def create_token_supply(update: Update, context: CallbackContext) -> int:
    """Handle token supply input with validation."""
    try:
        initial_supply = int(update.message.text)
        if initial_supply <= 0:
            await update.message.reply_text("Supply must be positive. Please try again:")
            return SUPPLY
        
        context.user_data["initial_supply"] = initial_supply
        
        keyboard = [
            [InlineKeyboardButton("Add Image", callback_data='add_image')],
            [InlineKeyboardButton("Skip", callback_data='skip_image')]
        ]
        await update.message.reply_text(
            "Would you like to add a token image?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return IMAGE
        
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a positive number:")
        return SUPPLY

async def handle_image_choice(update: Update, context: CallbackContext) -> int:
    """Handle image choice selection."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'skip_image':
        context.user_data["image_url"] = None
        await query.edit_message_text("Skipped image. Please enter a description (optional):")
        return DESCRIPTION
    else:
        await query.edit_message_text("Please enter the image URL:")
        return IMAGE

async def create_token_image(update: Update, context: CallbackContext) -> int:
    """Handle token image input."""
    image_url = update.message.text.strip()
    if image_url.lower() in ['skip', 'none']:
        context.user_data["image_url"] = None
        await update.message.reply_text("Skipped image. Please enter a description (optional):")
        return DESCRIPTION
    
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
        f"📝 Token Metadata Preview:\n\n"
        f"🔹 Name: {metadata.get('name')}\n"
        f"🔸 Symbol: {metadata.get('symbol')}\n"
        f"🔢 Decimals: {metadata.get('decimals')}\n"
        f"💰 Initial Supply: {metadata.get('initial_supply'):,}\n"
        f"🖼️ Image: {metadata.get('image_url', 'None')}\n"
        f"📄 Description: {metadata.get('description', 'None')}\n\n"
        f"Does this look correct?"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Confirm & Create", callback_data='confirm')],
        [InlineKeyboardButton("✏️ Edit Metadata", callback_data='edit')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            preview_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
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
    
    if field == 'image':
        prompt = "Enter new image URL (or 'skip' to remove image):"
    elif field == 'decimals':
        prompt = "Enter new decimals (0-18):"
    elif field == 'supply':
        prompt = "Enter new initial supply:"
    else:
        prompt = f"Enter new {field}:"
    
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
        
        # Start monitoring transaction
        tx_monitor_tasks[str(mint_address)] = asyncio.create_task(
            monitor_transaction_status(context, deep_link.split('tx=')[1].split('&')[0], update.message.chat_id, str(mint_address))
        )
        
        # Show success message
        await update.message.reply_text(
            f"🎉 Token creation initiated!\n\n"
            f"Token Address: `{mint_address}`\n\n"
            f"Click below to sign the transaction:\n"
            f"{deep_link}\n\n"
            f"I'll notify you when it's confirmed.",
            parse_mode="Markdown"
        )
        
        return POST_CREATION_ACTIONS
        
    except Exception as e:
        logger.error(f"Token creation failed: {str(e)}")
        await update.message.reply_text("❌ Failed to create token. Please try again.")
        return ConversationHandler.END

async def monitor_transaction_status(context: CallbackContext, txid: str, chat_id: int, mint_address: str) -> None:
    """Monitor and update transaction status via websocket."""
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Waiting for transaction confirmation..."
    )
    
    result = await solana_utils.confirm_transaction(txid)
    
    explorer_links = "\n".join(
        f"• [{name}]({url.format(mint_address)})" 
        for name, url in EXPLORER_URLS.items()
    )
    
    if result['status'] == 'confirmed':
        await context.bot.edit_message_text(
            text=(
                f"✅ Transaction confirmed!\n\n"
                f"🔗 Explorer Links:\n"
                f"{explorer_links}\n\n"
                f"What would you like to do next?"
            ),
            chat_id=chat_id,
            message_id=status_msg.message_id,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        
        # Show post-creation actions
        keyboard = [
            [InlineKeyboardButton("➕ Add Liquidity", callback_data=f'add_liquidity_{mint_address}')],
            [
                InlineKeyboardButton("🔍 View Token", callback_data=f'view_{mint_address}'),
                InlineKeyboardButton("📊 My Tokens", callback_data='my_tokens')
            ],
            [InlineKeyboardButton("✅ Done", callback_data='done')]
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text="Select an option:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.bot.edit_message_text(
            text=f"❌ Transaction failed: {result.get('error', 'Unknown error')}",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )

async def handle_post_creation_actions(update: Update, context: CallbackContext) -> int:
    """Handle actions after token creation."""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('add_liquidity_'):
        mint_address = query.data.split('_')[2]
        await query.edit_message_text(
            f"📌 To add liquidity to Raydium:\n\n"
            f"1. Go to [Raydium Swap](https://raydium.io/swap/)\n"
            f"2. Select your token: `{mint_address}`\n"
            f"3. Follow the liquidity adding process\n\n"
            f"Need help? Check the [Raydium Guide](https://docs.raydium.io/raydium/)",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    elif query.data.startswith('view_'):
        mint_address = query.data.split('_')[1]
        explorer_links = "\n".join(
            f"• [{name}]({url.format(mint_address)})" 
            for name, url in EXPLORER_URLS.items()
        )
        await query.edit_message_text(
            f"🔍 Token Explorer Links:\n\n{explorer_links}",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    elif query.data == 'my_tokens':
        return await show_user_tokens(update, context)
    else:
        await query.edit_message_text("✅ Token creation process completed!")
    
    return ConversationHandler.END

async def show_user_tokens(update: Update, context: CallbackContext) -> int:
    """Show user's created tokens."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user_tokens = [
        token for token in temporary_storage.values() 
        if token['user_id'] == user_id
    ]
    
    if not user_tokens:
        await query.edit_message_text("You haven't created any tokens yet!")
        return ConversationHandler.END
    
    token_list = []
    for i, token in enumerate(user_tokens, 1):
        token_list.append(
            f"{i}. {token['metadata']['name']} ({token['metadata']['symbol']})\n"
            f"   Address: `{token['mint_address']}`\n"
            f"   Created: {datetime.fromtimestamp(token['created_at']).strftime('%Y-%m-%d %H:%M')}"
        )
    
    await query.edit_message_text(
        f"📊 Your Tokens:\n\n" + "\n\n".join(token_list),
        parse_mode="Markdown"
    )
    
    # Show actions for each token
    keyboard = []
    for token in user_tokens[:5]:  # Limit to 5 tokens to avoid message size limits
        keyboard.append([
            InlineKeyboardButton(
                f"View {token['metadata']['symbol']}",
                callback_data=f"view_{token['mint_address']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data='back')])
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Select a token to view:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return POST_CREATION_ACTIONS

async def copy_token(update: Update, context: CallbackContext) -> int:
    """Handle copy token command."""
    try:
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
            f"🔹 Name: {metadata.get('name', 'Unknown')}\n"
            f"🔸 Symbol: {metadata.get('symbol', 'N/A')}\n"
            f"🔢 Decimals: {metadata.get('decimals', 0)}\n"
            f"💰 Supply: {metadata.get('total_supply', 'N/A'):,}\n"
            f"📄 Description: {metadata.get('description', 'None')}\n"
            f"🖼️ Image: {metadata.get('image_url', 'None')}\n\n"
            f"🔗 Explorer Links:\n"
        )
        
        explorer_links = "\n".join(
            f"• [{name}]({url.format(token_address)})" 
            for name, url in EXPLORER_URLS.items()
        )
        
        await update.message.reply_text(
            info_text + explorer_links,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
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

async def help_command(update: Update, context: CallbackContext) -> None:
    """Display help information."""
    help_text = (
        "🛠️ JEFFCRYPTO BOT Help\n\n"
        "• /start - Begin token creation\n"
        "• /copy <address> - Copy an existing token\n"
        "• /tokeninfo <address> - Get token info\n"
        "• /help - Show this message\n\n"
        "📌 During creation you can:\n"
        "- Preview metadata before submission\n"
        "- Edit any field before finalizing\n"
        "- Get real-time transaction updates\n"
        "- View explorer links after creation\n\n"
        "💡 Pro Tip: Use the inline menus for faster navigation!"
    )
    await update.message.reply_text(help_text)

def main() -> None:
    """Start the bot with all handlers."""
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Enhanced conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)],
            MENU: [CallbackQueryHandler(menu_handler)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_name)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_symbol)],
            DECIMALS_CHOICE: [
                CallbackQueryHandler(handle_decimals_choice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_decimals_input)
            ],
            SUPPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_supply)],
            IMAGE: [
                CallbackQueryHandler(handle_image_choice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_image)
            ],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_description)],
            PREVIEW: [CallbackQueryHandler(handle_edit_choice)],
            EDIT_CHOICE: [CallbackQueryHandler(edit_metadata_field)],
            EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_field)],
            PHANTOM_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token_phantom_wallet)],
            POST_CREATION_ACTIONS: [CallbackQueryHandler(handle_post_creation_actions)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('copy', copy_token))
    app.add_handler(CommandHandler('tokeninfo', token_info))
    app.add_handler(CommandHandler('help', help_command))
    app.add_error_handler(error_handler)
    
    # Start the bot
    app.run_polling()

if __name__ == '__main__':
    main()