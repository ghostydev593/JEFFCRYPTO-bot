import os
import json
import logging
import time
import re
import base64
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple, Any

import telebot
from telebot import types
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_handler_backends import State, StatesGroup
from aiohttp import ClientSession
from pinata_python.pinning import Pinning
from solana_utils import SolanaUtils
from solana.publickey import PublicKey

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
pinata = Pinning(PINATA_API_KEY=PINATA_API_KEY, PINATA_API_SECRET=PINATA_SECRET_API_KEY)
solana_utils = SolanaUtils(SOLANA_RPC_URL)

# Initialize bot
bot = AsyncTeleBot(TELEGRAM_BOT_TOKEN)

# States
class BotStates(StatesGroup):
    password = State()
    menu = State()
    name = State()
    symbol = State()
    decimals_choice = State()
    supply = State()
    image = State()
    description = State()
    phantom_wallet = State()
    preview = State()
    edit_choice = State()
    edit_field = State()
    post_creation = State()

# State management
temporary_storage = {}
authenticated_users = {}
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

# Helper functions
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
                    
                    temp_file = f"temp_{int(time.time())}.jpg"
                    with open(temp_file, 'wb') as f:
                        f.write(image_data)
                    
                    try:
                        response = pinata.pin_file_to_ipfs(temp_file)
                        os.remove(temp_file)
                        return f"https://ipfs.io/ipfs/{response['IpfsHash']}"
                    except Exception as e:
                        logger.error(f"Pinata upload failed: {str(e)}")
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                        return None
        except Exception as e:
            logger.error(f"Upload attempt {attempt + 1} failed: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    return None

def validate_metadata(metadata: Dict) -> Tuple[bool, Optional[str]]:
    """Validate token metadata."""
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

async def show_progress(chat_id: int, message: str) -> Any:
    """Show progress to user."""
    msg = await bot.send_message(
        chat_id=chat_id,
        text=f"⏳ {message}..."
    )
    return msg

async def update_progress(msg: types.Message, text: str) -> None:
    """Update progress message."""
    await bot.edit_message_text(
        text=f"✅ {text}",
        chat_id=msg.chat.id,
        message_id=msg.message_id
    )

# Handlers
@bot.message_handler(commands=['start'])
async def start(message: types.Message):
    """Start command handler."""
    user_id = message.from_user.id
    
    if user_id in authenticated_users:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("🆕 Create Token", callback_data='create'),
            types.InlineKeyboardButton("©️ Copy Token", callback_data='copy')
        )
        keyboard.row(
            types.InlineKeyboardButton("ℹ️ Token Info", callback_data='info'),
            types.InlineKeyboardButton("📊 My Tokens", callback_data='my_tokens')
        )
        
        await bot.send_message(
            message.chat.id,
            "Welcome back! Choose an option:",
            reply_markup=keyboard
        )
        await bot.set_state(message.from_user.id, BotStates.menu, message.chat.id)
    else:
        await bot.send_message(message.chat.id, "🔒 Please enter the password to use this bot:")
        await bot.set_state(message.from_user.id, BotStates.password, message.chat.id)

@bot.message_handler(state=BotStates.password)
async def check_password(message: types.Message):
    """Password verification handler."""
    user_id = message.from_user.id
    password_attempt = message.text.strip()

    if password_attempt == BOT_PASSWORD or user_id in ADMIN_IDS:
        authenticated_users[user_id] = True

        if user_id not in ADMIN_IDS:
            ADMIN_IDS.append(user_id)
            with open("admins.json", "w") as f:
                json.dump(ADMIN_IDS, f)

        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("🆕 Create Token", callback_data='create'),
            types.InlineKeyboardButton("©️ Copy Token", callback_data='copy')
        )
        keyboard.row(
            types.InlineKeyboardButton("ℹ️ Token Info", callback_data='info'),
            types.InlineKeyboardButton("📊 My Tokens", callback_data='my_tokens')
        )
        
        await bot.send_message(
            message.chat.id,
            "✅ Authentication successful! Choose an option:",
            reply_markup=keyboard
        )
        await bot.set_state(message.from_user.id, BotStates.menu, message.chat.id)
    else:
        await bot.send_message(message.chat.id, "❌ Incorrect password. Please try again.")

@bot.callback_query_handler(func=lambda call: True, state=BotStates.menu)
async def menu_handler(call: types.CallbackQuery):
    """Menu selection handler."""
    await bot.answer_callback_query(call.id)
    
    if call.data == 'create':
        await bot.send_message(call.message.chat.id, "Let's create a new token! Please enter the token name:")
        await bot.set_state(call.from_user.id, BotStates.name, call.message.chat.id)
    elif call.data == 'copy':
        await bot.send_message(call.message.chat.id, "Please enter the token address you want to copy:")
        await bot.set_state(call.from_user.id, BotStates.name, call.message.chat.id)
    elif call.data == 'info':
        await bot.send_message(call.message.chat.id, "Please enter the token address for info:")
        await bot.set_state(call.from_user.id, BotStates.name, call.message.chat.id)
    elif call.data == 'my_tokens':
        await show_user_tokens(call.message)

@bot.message_handler(state=BotStates.name)
async def create_token_name(message: types.Message):
    """Handle token name input."""
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['name'] = message.text
    
    await bot.send_message(message.chat.id, "Please enter the token symbol (max 10 chars):")
    await bot.set_state(message.from_user.id, BotStates.symbol, message.chat.id)

@bot.message_handler(state=BotStates.symbol)
async def create_token_symbol(message: types.Message):
    """Handle token symbol input."""
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['symbol'] = message.text
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        *[types.InlineKeyboardButton(str(i), callback_data=str(i)) for i in range(0, 6)]
    )
    keyboard.row(
        *[types.InlineKeyboardButton(str(i), callback_data=str(i)) for i in range(6, 12)]
    )
    keyboard.row(
        types.InlineKeyboardButton("Custom (0-18)", callback_data='custom')
    )
    
    await bot.send_message(
        message.chat.id,
        "Select token decimals:",
        reply_markup=keyboard
    )
    await bot.set_state(message.from_user.id, BotStates.decimals_choice, message.chat.id)

@bot.callback_query_handler(func=lambda call: True, state=BotStates.decimals_choice)
async def handle_decimals_choice(call: types.CallbackQuery):
    """Handle decimals selection."""
    await bot.answer_callback_query(call.id)
    
    if call.data == 'custom':
        await bot.send_message(call.message.chat.id, "Enter custom decimals (0-18):")
        return
    
    try:
        decimals = int(call.data)
        if 0 <= decimals <= 18:
            async with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
                data['decimals'] = decimals
            
            await bot.send_message(
                call.message.chat.id,
                f"Selected decimals: {decimals}\n\nEnter initial supply:"
            )
            await bot.set_state(call.from_user.id, BotStates.supply, call.message.chat.id)
        else:
            await bot.send_message(call.message.chat.id, "Invalid decimals. Please select 0-18:")
    except ValueError:
        await bot.send_message(call.message.chat.id, "Invalid input. Please try again.")

@bot.message_handler(state=BotStates.decimals_choice)
async def handle_decimals_input(message: types.Message):
    """Handle custom decimals input."""
    try:
        decimals = int(message.text)
        if 0 <= decimals <= 18:
            async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                data['decimals'] = decimals
            
            await bot.send_message(
                message.chat.id,
                f"Set decimals to {decimals}\n\nEnter initial supply:"
            )
            await bot.set_state(message.from_user.id, BotStates.supply, message.chat.id)
        else:
            await bot.send_message(message.chat.id, "Decimals must be 0-18. Please try again:")
    except ValueError:
        await bot.send_message(message.chat.id, "Invalid input. Please enter a number 0-18:")

@bot.message_handler(state=BotStates.supply)
async def create_token_supply(message: types.Message):
    """Handle token supply input with validation."""
    try:
        initial_supply = int(message.text)
        if initial_supply <= 0:
            await bot.send_message(message.chat.id, "Supply must be positive. Please try again:")
            return
        
        async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data['initial_supply'] = initial_supply
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("Add Image", callback_data='add_image'),
            types.InlineKeyboardButton("Skip", callback_data='skip_image')
        )
        
        await bot.send_message(
            message.chat.id,
            "Would you like to add a token image?",
            reply_markup=keyboard
        )
        await bot.set_state(message.from_user.id, BotStates.image, message.chat.id)
        
    except ValueError:
        await bot.send_message(message.chat.id, "Invalid input. Please enter a positive number:")

@bot.callback_query_handler(func=lambda call: True, state=BotStates.image)
async def handle_image_choice(call: types.CallbackQuery):
    """Handle image choice selection."""
    await bot.answer_callback_query(call.id)
    
    if call.data == 'skip_image':
        async with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            data['image_url'] = None
        
        await bot.send_message(call.message.chat.id, "Skipped image. Please enter a description (optional):")
        await bot.set_state(call.from_user.id, BotStates.description, call.message.chat.id)
    else:
        await bot.send_message(call.message.chat.id, "Please enter the image URL:")
        await bot.set_state(call.from_user.id, BotStates.image, call.message.chat.id)

@bot.message_handler(state=BotStates.image)
async def create_token_image(message: types.Message):
    """Handle token image input."""
    image_url = message.text.strip()
    if image_url.lower() in ['skip', 'none']:
        async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data['image_url'] = None
        
        await bot.send_message(message.chat.id, "Skipped image. Please enter a description (optional):")
        await bot.set_state(message.from_user.id, BotStates.description, message.chat.id)
        return
    
    progress_msg = await show_progress(message.chat.id, "Uploading image to IPFS")
    
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['image_url'] = await upload_to_ipfs(image_url)
    
    await update_progress(progress_msg, "Image uploaded")
    await bot.send_message(message.chat.id, "Please enter a description for the token (optional):")
    await bot.set_state(message.from_user.id, BotStates.description, message.chat.id)

@bot.message_handler(state=BotStates.description)
async def create_token_description(message: types.Message):
    """Handle token description input."""
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['description'] = message.text
    
    await preview_metadata(message)

async def preview_metadata(message: types.Message):
    """Show metadata preview before submission."""
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        metadata = data.copy()
    
    valid, error = validate_metadata(metadata)
    if not valid:
        await bot.send_message(message.chat.id, f"❌ Validation error: {error}")
        await bot.delete_state(message.from_user.id, message.chat.id)
        return
    
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
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("✅ Confirm & Create", callback_data='confirm')
    )
    keyboard.row(
        types.InlineKeyboardButton("✏️ Edit Metadata", callback_data='edit'),
        types.InlineKeyboardButton("❌ Cancel", callback_data='cancel')
    )
    
    await bot.send_message(
        message.chat.id,
        preview_text,
        reply_markup=keyboard
    )
    await bot.set_state(message.from_user.id, BotStates.preview, message.chat.id)

@bot.callback_query_handler(func=lambda call: True, state=BotStates.preview)
async def handle_edit_choice(call: types.CallbackQuery):
    """Handle metadata editing choices."""
    await bot.answer_callback_query(call.id)
    
    if call.data == 'confirm':
        await bot.send_message(call.message.chat.id, "Great! Please provide your Phantom wallet address:")
        await bot.set_state(call.from_user.id, BotStates.phantom_wallet, call.message.chat.id)
    elif call.data == 'edit':
        fields = ["Name", "Symbol", "Decimals", "Supply", "Image", "Description"]
        keyboard = types.InlineKeyboardMarkup()
        for field in fields:
            keyboard.row(types.InlineKeyboardButton(field, callback_data=field.lower()))
        
        await bot.send_message(
            call.message.chat.id,
            "Which field would you like to edit?",
            reply_markup=keyboard
        )
        await bot.set_state(call.from_user.id, BotStates.edit_choice, call.message.chat.id)
    else:
        await bot.send_message(call.message.chat.id, "Token creation cancelled.")
        await bot.delete_state(call.from_user.id, call.message.chat.id)

@bot.callback_query_handler(func=lambda call: True, state=BotStates.edit_choice)
async def edit_metadata_field(call: types.CallbackQuery):
    """Edit specific metadata field."""
    await bot.answer_callback_query(call.id)
    
    field = call.data
    async with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        data['editing_field'] = field
    
    if field == 'image':
        prompt = "Enter new image URL (or 'skip' to remove image):"
    elif field == 'decimals':
        prompt = "Enter new decimals (0-18):"
    elif field == 'supply':
        prompt = "Enter new initial supply:"
    else:
        prompt = f"Enter new {field}:"
    
    await bot.send_message(call.message.chat.id, prompt)
    await bot.set_state(call.from_user.id, BotStates.edit_field, call.message.chat.id)

@bot.message_handler(state=BotStates.edit_field)
async def save_edited_field(message: types.Message):
    """Save edited field and return to preview."""
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        field = data.pop('editing_field')
        value = message.text
        
        if field == 'image':
            if value.strip().lower() == 'skip':
                data['image_url'] = None
            else:
                progress_msg = await show_progress(message.chat.id, "Uploading new image")
                data['image_url'] = await upload_to_ipfs(value)
                await update_progress(progress_msg, "Image updated")
        elif field == 'decimals':
            try:
                value = int(value)
                if not 0 <= value <= 18:
                    raise ValueError
                data['decimals'] = value
            except ValueError:
                await bot.send_message(message.chat.id, "Decimals must be 0-18. Try again:")
                return
        elif field == 'supply':
            try:
                value = int(value)
                if value <= 0:
                    raise ValueError
                data['initial_supply'] = value
            except ValueError:
                await bot.send_message(message.chat.id, "Supply must be positive integer. Try again:")
                return
        else:
            data[field] = value
    
    await preview_metadata(message)

@bot.message_handler(state=BotStates.phantom_wallet)
async def create_token_phantom_wallet(message: types.Message):
    """Handle Phantom wallet address input."""
    phantom_wallet_address = message.text.strip()
    async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['phantom_wallet_address'] = phantom_wallet_address
        
        progress_msg = await show_progress(message.chat.id, "Creating token")
        
        try:
            mint_address, deep_link = await solana_utils.create_and_send_transaction(
                data.copy(),
                PublicKey(phantom_wallet_address)
            )
            
            if not mint_address:
                error_msg = deep_link if deep_link else "Failed to create token"
                await bot.send_message(message.chat.id, f"❌ Error: {error_msg}")
                await bot.delete_state(message.from_user.id, message.chat.id)
                return
            
            await update_progress(progress_msg, "Token created")
            
            # Store transaction details
            data["mint_address"] = str(mint_address)
            temporary_storage[str(mint_address)] = {
                "user_id": message.from_user.id,
                "deep_link": deep_link,
                "metadata": data.copy(),
                "created_at": time.time(),
            }
            
            # Start monitoring transaction
            tx_monitor_tasks[str(mint_address)] = asyncio.create_task(
                monitor_transaction_status(message.chat.id, deep_link.split('tx=')[1].split('&')[0], str(mint_address))
            )
            
            # Show success message
            await bot.send_message(
                message.chat.id,
                f"🎉 Token creation initiated!\n\n"
                f"Token Address: `{mint_address}`\n\n"
                f"Click below to sign the transaction:\n"
                f"{deep_link}\n\n"
                f"I'll notify you when it's confirmed.",
                parse_mode="Markdown"
            )
            
            await bot.set_state(message.from_user.id, BotStates.post_creation, message.chat.id)
            
        except Exception as e:
            logger.error(f"Token creation failed: {str(e)}")
            await bot.send_message(message.chat.id, "❌ Failed to create token. Please try again.")
            await bot.delete_state(message.from_user.id, message.chat.id)

async def monitor_transaction_status(chat_id: int, txid: str, mint_address: str):
    """Monitor and update transaction status."""
    status_msg = await bot.send_message(
        chat_id=chat_id,
        text="⏳ Waiting for transaction confirmation..."
    )
    
    result = await solana_utils.confirm_transaction(txid)
    
    explorer_links = "\n".join(
        f"• [{name}]({url.format(mint_address)})" 
        for name, url in EXPLORER_URLS.items()
    )
    
    if result['status'] == 'confirmed':
        await bot.edit_message_text(
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
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("➕ Add Liquidity", callback_data=f'add_liquidity_{mint_address}')
        )
        keyboard.row(
            types.InlineKeyboardButton("🔍 View Token", callback_data=f'view_{mint_address}'),
            types.InlineKeyboardButton("📊 My Tokens", callback_data='my_tokens')
        )
        keyboard.row(
            types.InlineKeyboardButton("✅ Done", callback_data='done')
        )
        
        await bot.send_message(
            chat_id=chat_id,
            text="Select an option:",
            reply_markup=keyboard
        )
    else:
        await bot.edit_message_text(
            text=f"❌ Transaction failed: {result.get('error', 'Unknown error')}",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )

@bot.callback_query_handler(func=lambda call: True, state=BotStates.post_creation)
async def handle_post_creation_actions(call: types.CallbackQuery):
    """Handle actions after token creation."""
    await bot.answer_callback_query(call.id)
    
    if call.data.startswith('add_liquidity_'):
        mint_address = call.data.split('_')[2]
        await bot.send_message(
            call.message.chat.id,
            f"📌 To add liquidity to Raydium:\n\n"
            f"1. Go to [Raydium Swap](https://raydium.io/swap/)\n"
            f"2. Select your token: `{mint_address}`\n"
            f"3. Follow the liquidity adding process\n\n"
            f"Need help? Check the [Raydium Guide](https://docs.raydium.io/raydium/)",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    elif call.data.startswith('view_'):
        mint_address = call.data.split('_')[1]
        explorer_links = "\n".join(
            f"• [{name}]({url.format(mint_address)})" 
            for name, url in EXPLORER_URLS.items()
        )
        await bot.send_message(
            call.message.chat.id,
            f"🔍 Token Explorer Links:\n\n{explorer_links}",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    elif call.data == 'my_tokens':
        await show_user_tokens(call.message)
    else:
        await bot.send_message(call.message.chat.id, "✅ Token creation process completed!")
    
    await bot.delete_state(call.from_user.id, call.message.chat.id)

async def show_user_tokens(message: types.Message):
    """Show user's created tokens."""
    user_id = message.from_user.id
    user_tokens = [
        token for token in temporary_storage.values() 
        if token['user_id'] == user_id
    ]
    
    if not user_tokens:
        await bot.send_message(message.chat.id, "You haven't created any tokens yet!")
        return
    
    token_list = []
    for i, token in enumerate(user_tokens, 1):
        token_list.append(
            f"{i}. {token['metadata']['name']} ({token['metadata']['symbol']})\n"
            f"   Address: `{token['mint_address']}`\n"
            f"   Created: {datetime.fromtimestamp(token['created_at']).strftime('%Y-%m-%d %H:%M')}"
        )
    
    await bot.send_message(
        message.chat.id,
        f"📊 Your Tokens:\n\n" + "\n\n".join(token_list),
        parse_mode="Markdown"
    )
    
    # Show actions for each token
    keyboard = types.InlineKeyboardMarkup()
    for token in user_tokens[:5]:  # Limit to 5 tokens
        keyboard.row(
            types.InlineKeyboardButton(
                f"View {token['metadata']['symbol']}",
                callback_data=f"view_{token['mint_address']}"
            )
        )
    
    keyboard.row(
        types.InlineKeyboardButton("Back to Menu", callback_data='back')
    )
    
    await bot.send_message(
        message.chat.id,
        "Select a token to view:",
        reply_markup=keyboard
    )

@bot.message_handler(commands=['copy'])
async def copy_token(message: types.Message):
    """Handle copy token command."""
    try:
        token_address = message.text.split(' ')[1] if len(message.text.split(' ')) > 1 else None
        if not token_address:
            await bot.send_message(message.chat.id, "Please provide a token address after /copy command")
            return
        
        progress_msg = await show_progress(message.chat.id, "Fetching token metadata")
        
        metadata = await solana_utils.fetch_token_metadata(token_address)
        if not metadata:
            await bot.send_message(message.chat.id, "❌ Failed to fetch token metadata")
            return
        
        await update_progress(progress_msg, "Metadata fetched")
        
        # Store metadata for later use
        async with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data['metadata'] = metadata
            data['original_address'] = token_address
        
        # Ask for Phantom wallet
        await bot.send_message(message.chat.id, "Please provide your Phantom wallet address:")
        await bot.set_state(message.from_user.id, BotStates.phantom_wallet, message.chat.id)
    
    except Exception as e:
        logger.error(f"Copy token failed: {str(e)}")
        await bot.send_message(message.chat.id, "❌ Failed to copy token")

@bot.message_handler(commands=['tokeninfo'])
async def token_info(message: types.Message):
    """Handle token info command."""
    try:
        token_address = message.text.split(' ')[1] if len(message.text.split(' ')) > 1 else None
        if not token_address:
            await bot.send_message(message.chat.id, "Please provide a token address after /tokeninfo command")
            return
        
        progress_msg = await show_progress(message.chat.id, "Fetching token info")
        
        metadata = await solana_utils.fetch_token_metadata(token_address)
        if not metadata:
            await bot.send_message(message.chat.id, "❌ Token not found or invalid address")
            return
        
        await update_progress(progress_msg, "Info retrieved")
        
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
        
        await bot.send_message(
            message.chat.id,
            info_text + explorer_links,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    
    except Exception as e:
        logger.error(f"Token info failed: {str(e)}")
        await bot.send_message(message.chat.id, "❌ Failed to fetch token info")

@bot.message_handler(commands=['cancel'])
async def cancel(message: types.Message):
    """Cancel the current operation."""
    await bot.send_message(message.chat.id, "Operation cancelled.")
    await bot.delete_state(message.from_user.id, message.chat.id)

@bot.message_handler(commands=['help'])
async def help_command(message: types.Message):
    """Help command handler."""
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
        "- View explorer links after creation"
    )
    await bot.send_message(message.chat.id, help_text)

@bot.message_handler(func=lambda message: True)
async def handle_unexpected_messages(message: types.Message):
    """Handle unexpected messages."""
    await bot.send_message(message.chat.id, "I didn't understand that command. Type /help for instructions.")

async def main():
    """Start the bot."""
    await bot.polling(non_stop=True)

if __name__ == '__main__':
    asyncio.run(main())