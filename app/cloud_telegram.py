"""Build the cloud bot without assuming a laptop-local model endpoint."""

import json
import os
from pathlib import Path

from app.ollama_client import AssistantUnavailableError
from app.ai_client import create_assistant
from app.config import Settings
from app.remote_services import ModalStore, RemoteBotDatabase, RemoteMaintenanceService, RemotePaymentService
from app.telegram_bot import LandlordBot, TelegramClient


class CommandOnlyAssistant:
    def classify_message(self, text, *, reference_date=None):
        raise AssistantUnavailableError('Remote inference has not been configured')

    def chat(self, messages):
        # /payments supplies only context scoped by Python to the incoming chat.
        value = json.loads(messages[-1]['content'])
        context = value['authorized_records']
        lines = ["This group's payment records:"]
        for bill in context['bills'][:10]:
            lines.append(f"{bill['period']} · due {bill['due_date']} · outstanding {bill['outstanding']}")
        for payment in context['submissions'][:10]:
            received = payment['amount_received_cents'] or 'not recorded'
            lines.append(f"Proof #{payment['id']} · {payment['status']} · received {received} · date {payment['received_date'] or 'not recorded'}")
        if not context['bills'] and not context['submissions']:
            lines.append('No payment records have been recorded for this chat.')
        lines.append(f"Available advance credit: {context['available_advance_credit']}")
        lines.append('Showing up to 10 recent bills and 10 submissions. Pending proof is not a confirmed payment.')
        return '\n'.join(lines)


def cloud_store() -> ModalStore:
    return ModalStore(os.environ.get('MODAL_WORKER_APP', 'landlord-db-worker-test'),
                      os.environ.get('MODAL_DATASET', 'production'), {})


def build_cloud_bot(store: ModalStore) -> LandlordBot:
    return LandlordBot(
        database=RemoteBotDatabase(store), maintenance=RemoteMaintenanceService(store),
        payments=RemotePaymentService(store),
        telegram=TelegramClient(os.environ['TELEGRAM_BOT_TOKEN']),
        assistant=(CommandOnlyAssistant() if os.getenv('AI_BACKEND', 'commands') == 'commands'
                   else create_assistant(Settings.from_env(require_token=True))),
        bot_username=os.environ['TELEGRAM_BOT_USERNAME'],
        bot_user_id=int(os.environ['TELEGRAM_BOT_ID']),
        proof_dir=Path('/tmp/telegram-proofs'),
    )
