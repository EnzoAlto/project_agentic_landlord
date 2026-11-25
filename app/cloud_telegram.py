"""Build the cloud bot without assuming a laptop-local model endpoint."""

import json
import os
from pathlib import Path

from app.ollama_client import AssistantUnavailableError
from app.ai_client import create_assistant
from app.config import Settings
from app.remote_services import ModalStore, RemoteBotDatabase, RemoteMaintenanceService, RemotePaymentService
from app.telegram_bot import LandlordBot, TelegramClient
from app.telegram_inbox import RequestSuperseded


class ActiveTelegram:
    """Check durable request ownership before sending a delayed response."""

    def __init__(self, client, active):
        self.client, self.active = client, active

    def __getattr__(self, name):
        return getattr(self.client, name)

    def send_message(self, chat_id, text):
        if not self.active():
            raise RequestSuperseded('Skipping a superseded Telegram reply')
        return self.client.send_message(chat_id, text)


class ActiveAssistant:
    def __init__(self, assistant, active):
        self.assistant, self.active = assistant, active

    def __getattr__(self, name):
        return getattr(self.assistant, name)

    def _call(self, method, *args, **kwargs):
        if not self.active():
            raise RequestSuperseded('Skipping superseded inference')
        result = getattr(self.assistant, method)(*args, **kwargs)
        # Do not cancel the GPU call; discard its result if a command replaced it.
        if not self.active():
            raise RequestSuperseded('Discarding superseded inference result')
        return result

    def classify_message(self, *args, **kwargs):
        return self._call('classify_message', *args, **kwargs)

    def chat(self, *args, **kwargs):
        return self._call('chat', *args, **kwargs)


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


def build_cloud_bot(store: ModalStore, context=None) -> LandlordBot:
    assistant = (CommandOnlyAssistant() if os.getenv('AI_BACKEND', 'commands') == 'commands'
                 else create_assistant(Settings.from_env(require_token=True)))
    telegram = TelegramClient(os.environ['TELEGRAM_BOT_TOKEN'])
    active, claim_notice = None, None
    if context is not None:
        store.telegram_context = context
        # Separate control clients also keep timer-thread calls out of the
        # main request's pending-write state.
        active = lambda: cloud_store().call('telegram_request_active', context)
        claim_notice = lambda: cloud_store().call('claim_telegram_notice', context, write=True)
        telegram = ActiveTelegram(telegram, active)
        assistant = ActiveAssistant(assistant, active)
    return LandlordBot(
        database=RemoteBotDatabase(store), maintenance=RemoteMaintenanceService(store),
        payments=RemotePaymentService(store),
        telegram=telegram, assistant=assistant,
        bot_username=os.environ['TELEGRAM_BOT_USERNAME'],
        bot_user_id=int(os.environ['TELEGRAM_BOT_ID']),
        proof_dir=Path('/tmp/telegram-proofs'),
        request_active=active, claim_notice=claim_notice,
    )
