"""Run the Telegram landlord assistant."""

from app.config import Settings
from app.database import Database
from app.maintenance import MaintenanceService
from app.ollama_client import OllamaClient
from app.payments import PaymentService
from app.telegram_bot import LandlordBot, TelegramClient


def main() -> None:
    settings = Settings.from_env(require_token=True)
    if settings.telegram_bot_token is None:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing from .env")

    database = Database(settings.database_path)
    database.initialize()
    maintenance = MaintenanceService(database)
    payments = PaymentService(database)

    telegram = TelegramClient(settings.telegram_bot_token)
    bot_profile = telegram.get_me()
    assistant = OllamaClient(
        base_url=settings.ollama_url,
        model=settings.ollama_model,
    )

    installed_models = assistant.health_check()
    if settings.ollama_model not in installed_models:
        raise RuntimeError(
            f"{settings.ollama_model} is not installed. Found: {installed_models}"
        )

    bot = LandlordBot(
        database=database,
        maintenance=maintenance,
        payments=payments,
        telegram=telegram,
        assistant=assistant,
        bot_username=str(bot_profile["username"]),
        bot_user_id=int(bot_profile["id"]),
        proof_dir=settings.payment_proof_dir,
    )
    bot.run_forever()


if __name__ == "__main__":
    main()

