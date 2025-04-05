import logging
from telegram.ext import ApplicationBuilder
from handlers import (
    get_password_conversation_handler,
    get_token_creation_conversation_handler,
    get_copy_token_conversation_handler,
    help_command,
    token_info,
    error
)
from config import TELEGRAM_BOT_TOKEN

# Initialize logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main() -> None:
    """Entry point for the JEFFCRYPTO BOT."""
    try:
        # Build the application
        application = ApplicationBuilder() \
            .token(TELEGRAM_BOT_TOKEN) \
            .build()

        # Register handlers
        application.add_handler(get_password_conversation_handler())
        application.add_handler(get_token_creation_conversation_handler())
        application.add_handler(get_copy_token_conversation_handler())
        
        # Register commands
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("tokeninfo", token_info))
        
        # Error handler
        application.add_error_handler(error)

        logger.info("Starting bot...")
        application.run_polling()

    except Exception as e:
        logger.critical(f"Failed to start bot: {str(e)}")
        raise

if __name__ == "__main__":
    main()