"""Run the Telegram landlord assistant."""

from app.config import Settings
from app.database import Database
from app.maintenance import MaintenanceService
from app.ai_client import create_assistant
from app.payments import PaymentService
from app.telegram_bot import LandlordBot, TelegramClient


def main() -> None:
    settings = Settings.from_env(require_token=True)
    if settings.telegram_bot_token is None:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing from .env")

    if settings.storage_backend == 'modal':
        from app.remote_services import (
            ModalStore, RemoteBotDatabase, RemoteMaintenanceService, RemotePaymentService,
        )
        store = ModalStore(settings.modal_worker_app, settings.modal_dataset, {})
        health = store.call('health')
        if health.get('status') != 'ready':
            raise RuntimeError('The selected Modal dataset has not been imported')
        database = RemoteBotDatabase(store)
        maintenance = RemoteMaintenanceService(store)
        payments = RemotePaymentService(store)
        print(f'Telegram storage: Modal / {settings.modal_dataset}')
    else:
        database = Database(settings.database_path)
        database.initialize()
        maintenance = MaintenanceService(database)
        payments = PaymentService(database)
        print('Telegram storage: local SQLite')

    telegram = TelegramClient(settings.telegram_bot_token)
    bot_profile = telegram.get_me()
    if telegram.get_webhook_info().get('url'):
        raise RuntimeError(
            'This bot has an active webhook. Deactivate the cloud webhook before '
            'starting local polling: python -m scripts.configure_modal_telegram --deactivate'
        )
    assistant = create_assistant(settings)

    installed_models = assistant.health_check()
    if settings.ai_backend == 'ollama' and settings.ollama_model not in installed_models:
        raise RuntimeError(
            f"{settings.ollama_model} is not installed. Found: {installed_models}"
        )
    print(f'Telegram AI: {settings.ai_backend} / {", ".join(installed_models)}')

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

