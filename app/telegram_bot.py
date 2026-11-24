"""Telegram transport and group-first landlord bot orchestration."""

from __future__ import annotations

from collections import defaultdict
import json
from uuid import uuid4
from pathlib import Path
import re
import time
import tempfile
from threading import Timer
from typing import Callable, Protocol, TypeVar

import requests

from app.database import Database
from app.maintenance import MaintenanceService
from app.ollama_client import IntentResult, OllamaResponseError, AssistantUnavailableError
from app.payments import PaymentService
from app.reminders import ReminderService
from app.payment_history import PAYMENT_HISTORY_PROMPT, group_payment_context
from app.proof_cleanup import cleanup_reviewed_proofs
from app.payment_claims import message_date, normalize_claims, period_heading
from app.remote_services import RemoteStorageError


class TelegramAPIError(RuntimeError):
    """A Telegram error that does not expose the bot token in its message."""


class TelegramTransport(Protocol):
    def get_updates(self, offset: int | None = None) -> list[dict]: ...

    def send_message(self, chat_id: int, text: str) -> dict: ...

    def download_file(self, file_id: str, destination: Path) -> Path: ...


_Result = TypeVar('_Result')


class _AIWaitNotice:
    """One delay notice per incoming message, counting time spent awaiting AI."""

    def __init__(self, telegram: TelegramTransport, chat_id: int):
        self.telegram = telegram
        self.chat_id = chat_id
        self.elapsed = 0.0
        self.attempted = False

    def _send(self) -> None:
        self.attempted = True
        try:
            self.telegram.send_message(
                self.chat_id,
                'Hello! I’m reading your message.\n'
                'For quick payments click /payment.',
            )
        except Exception as exc:
            # A failed progress notice must not prevent the actual response.
            print(f'AI delay notice failed: {type(exc).__name__}')

    def call(self, function: Callable[..., _Result], *args, **kwargs) -> _Result:
        if self.attempted:
            return function(*args, **kwargs)
        timer = Timer(max(0.0, 8.0 - self.elapsed), self._send)
        timer.daemon = True
        started = time.monotonic()
        timer.start()
        try:
            return function(*args, **kwargs)
        finally:
            self.elapsed += time.monotonic() - started
            timer.cancel()
            # Finish an already-started notice before sending the final reply.
            timer.join()


class AssistantClient(Protocol):
    def classify_message(self, text: str, *, reference_date: str | None = None) -> IntentResult: ...

    def chat(self, messages: list[dict[str, str]]) -> str: ...


class TelegramClient:
    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
    ):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"
        self.session = session or requests.Session()

    def _call(self, method: str, **payload):  # type: ignore[no-untyped-def]
        try:
            response = self.session.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=40,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" HTTP {status}" if status else ""
            raise TelegramAPIError(
                f"Telegram {method} request failed{suffix}"
            ) from None

        result = response.json()
        if not result.get("ok"):
            raise TelegramAPIError(
                f"Telegram {method} failed: "
                f"{result.get('description', 'unknown error')}"
            )
        return result["result"]

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_webhook_info(self) -> dict:
        return self._call('getWebhookInfo')

    def get_updates(self, offset: int | None = None) -> list[dict]:
        payload = {"timeout": 25, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", **payload)

    def send_message(self, chat_id: int, text: str) -> dict:
        text = text.strip() or "(No response generated.)"
        result: dict = {}
        for position in range(0, len(text), 4000):
            result = self._call(
                "sendMessage",
                chat_id=chat_id,
                text=text[position : position + 4000],
            )
        return result

    def download_file(self, file_id: str, destination: Path) -> Path:
        file_info = self._call("getFile", file_id=file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            raise TelegramAPIError("Telegram getFile returned no file path")

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self.session.get(
                f"{self.file_base_url}/{file_path}", timeout=60
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TelegramAPIError("Telegram file download failed") from None

        destination.write_bytes(response.content)
        return destination


class LandlordBot:
    def __init__(
        self,
        *,
        database: Database,
        maintenance: MaintenanceService,
        payments: PaymentService,
        telegram: TelegramTransport,
        assistant: AssistantClient,
        bot_username: str,
        bot_user_id: int,
        proof_dir: Path,
    ):
        self.database = database
        self.maintenance = maintenance
        self.payments = payments
        self.telegram = telegram
        self.assistant = assistant
        self.bot_username = bot_username.lstrip("@").lower()
        self.bot_user_id = bot_user_id
        self.proof_dir = proof_dir
        self.conversation_history: dict[int, list[dict[str, str]]] = defaultdict(list)

    @staticmethod
    def _sender_name(message: dict) -> str:
        sender = message.get("from") or {}
        parts = [sender.get("first_name"), sender.get("last_name")]
        name = " ".join(part for part in parts if part).strip()
        return name or sender.get("username") or "Telegram user"

    @staticmethod
    def _chat_title(message: dict) -> str:
        chat = message.get("chat") or {}
        if chat.get("title"):
            return str(chat["title"])
        parts = [chat.get("first_name"), chat.get("last_name")]
        return " ".join(part for part in parts if part).strip() or str(chat.get("id"))

    def _is_activated(self, message: dict, text: str) -> bool:
        chat_type = (message.get("chat") or {}).get("type")
        if chat_type == "private":
            return True

        lowered = text.lower()
        if f"@{self.bot_username}" in lowered:
            return True
        if lowered.startswith("/"):
            return True

        replied_to = message.get("reply_to_message") or {}
        replied_sender = replied_to.get("from") or {}
        return bool(
            replied_sender.get("id") == self.bot_user_id
            or replied_sender.get("is_bot")
        )

    def _clean_text(self, text: str) -> str:
        cleaned = re.sub(
            rf"@{re.escape(self.bot_username)}\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            rf"^(/\w+)@{re.escape(self.bot_username)}\b",
            r"\1",
            cleaned,
            flags=re.IGNORECASE,
        )
        return " ".join(cleaned.split()).strip()

    @staticmethod
    def _file_from_message(message: dict) -> tuple[str, str] | None:
        photos = message.get("photo") or []
        if photos:
            return photos[-1]["file_id"], ".jpg"

        document = message.get("document") or {}
        mime_type = str(document.get("mime_type", ""))
        if document.get("file_id") and mime_type.startswith("image/"):
            suffix = Path(str(document.get("file_name", "proof.jpg"))).suffix
            return document["file_id"], suffix or ".jpg"
        return None

    def _start_payment_request(
        self,
        *,
        update_id: int,
        chat_id: int,
        sender_id: int | None,
        sender_name: str,
        intent: IntentResult,
        original_text: str = '',
        reference_date=None,
    ) -> None:
        reference = reference_date or message_date()
        lines = normalize_claims(intent.payment_items, intent.payment_categories, reference, intent.billing_period)
        quote = self.payments.quote(chat_id, lines, amount_claimed=intent.amount_claimed)
        periods = list(dict.fromkeys(line['period'] for line in lines if line['period']))
        submission = self.payments.create_request(
            group_chat_id=chat_id,
            submitter_user_id=sender_id,
            submitter_name=sender_name,
            source_update_id=update_id,
            summary=original_text or intent.summary,
            resident_notes=original_text or intent.summary,
            claim_lines=lines,
            categories=intent.payment_categories,
            billing_period=', '.join(periods) or None,
            amount_claimed=intent.amount_claimed,
        )
        response = self.telegram.send_message(
            chat_id,
            f"Hello! Here is your payment request #{submission.id}\n"
            f"{period_heading(periods)}\n\n"
            f"{quote}\n\n"
            "Please send your payment screenshot here.\n"
            "Payment is subject to landlord approval.\nThank you!",
        )
        request_message_id = response.get("message_id")
        if isinstance(request_message_id, int):
            self.payments.set_request_message(submission.id, request_message_id)

    def _handle_payment_proof(
        self,
        *,
        message: dict,
        chat_id: int,
        sender_id: int | None,
        file_id: str,
        suffix: str,
    ) -> bool:
        reply_to_message_id = (
            (message.get("reply_to_message") or {}).get("message_id")
        )
        submission = self.payments.find_awaiting_proof(
            group_chat_id=chat_id,
            submitter_user_id=sender_id,
            reply_to_message_id=(
                reply_to_message_id
                if isinstance(reply_to_message_id, int)
                else None
            ),
        )
        if submission is None:
            self.telegram.send_message(
                chat_id,
                "No awaiting payment submission was found. "
                "Send /payment to start one.",
            )
            return True

        if hasattr(self.payments, 'upload_proof'):
            with tempfile.TemporaryDirectory(prefix='telegram-proof-') as temporary:
                destination = Path(temporary) / f'proof{suffix}'
                self.telegram.download_file(file_id, destination)
                updated = self.payments.upload_proof(
                    path=destination, chat_id=chat_id, sender_id=sender_id,
                    submission_id=submission.id,
                    proof_message_id=int(message.get('message_id', 0)),
                    telegram_file_id=file_id, caption=message.get('caption'),
                )
        else:
            destination = self.proof_dir / f"payment_{submission.id}_{uuid4().hex}{suffix}"
            self.telegram.download_file(file_id, destination)
            updated = self.payments.attach_proof(
                submission_id=submission.id,
                proof_message_id=int(message.get("message_id", 0)),
                telegram_file_id=file_id,
                proof_path=str(destination),
                caption=message.get("caption"),
            )
        self.telegram.send_message(
            chat_id,
            f"Thank you, Your Image has been Received!\n",
            "Payment proof #{updated.id} was submitted.\n",
            "Please wait for landlord review",
        )
        return True

    def _reply_with_payment_history(
        self, chat_id: int, question: str, notice: _AIWaitNotice | None = None,
    ) -> None:
        context = (self.database.payment_context(chat_id)
                   if hasattr(self.database, 'payment_context')
                   else group_payment_context(self.database, chat_id))
        notice = notice or _AIWaitNotice(self.telegram, chat_id)
        reply = notice.call(self.assistant.chat, [
            {'role': 'system', 'content': PAYMENT_HISTORY_PROMPT},
            {'role': 'user', 'content': json.dumps({
                'question': question, 'authorized_records': context,
            }, ensure_ascii=False)},
        ])
        self.telegram.send_message(chat_id, reply)

    def handle_update(self, update: dict) -> bool:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return False
        if self.database.is_update_processed(update_id):
            return False

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        sender_id = sender.get("id")
        if not isinstance(chat_id, int):
            self.database.mark_update_processed(update_id)
            return False

        text = str(message.get("text") or message.get("caption") or "").strip()
        file_info = self._file_from_message(message)
        is_activated = self._is_activated(message, text)

        # A resident may send the screenshot as the next group message instead
        # of using Telegram's Reply action. Accept that first image only when
        # the same sender has an awaiting payment in this exact group.
        has_matching_payment = False
        if (
            file_info is not None
            and not is_activated
            and isinstance(sender_id, int)
        ):
            reply_to_message_id = (
                (message.get("reply_to_message") or {}).get("message_id")
            )
            has_matching_payment = self.payments.find_awaiting_proof(
                group_chat_id=chat_id,
                submitter_user_id=sender_id,
                reply_to_message_id=(
                    reply_to_message_id
                    if isinstance(reply_to_message_id, int)
                    else None
                ),
            ) is not None

        if not is_activated and not has_matching_payment:
            self.database.mark_update_processed(update_id)
            return False

        self.database.upsert_group(
            chat_id,
            self._chat_title(message),
            str(chat.get("type") or "unknown"),
        )
        sender_name = self._sender_name(message)
        print(f"Processing Telegram update {update_id} from {sender_name} in {chat_id}")

        try:
            if file_info:
                handled = self._handle_payment_proof(
                    message=message,
                    chat_id=chat_id,
                    sender_id=sender_id if isinstance(sender_id, int) else None,
                    file_id=file_info[0],
                    suffix=file_info[1],
                )
                self.database.mark_update_processed(update_id)
                return handled

            cleaned = self._clean_text(text)
            command = cleaned.split(maxsplit=1)[0].lower() if cleaned else ""
            notice = _AIWaitNotice(self.telegram, chat_id)

            if command in {"/start", "/help"}:
                self.telegram.send_message(
                    chat_id,
                    "Mention me with a maintenance or payment request.\n"
                    "Commands: /tickets, /maintenance <issue>, /payment, /payments, /reset",
                )
            elif command == '/maintenance':
                description = cleaned[len(command):].strip()
                if not description:
                    self.telegram.send_message(chat_id, 'Use /maintenance followed by the issue, for example: /maintenance leaking sink')
                else:
                    ticket = self.maintenance.create_ticket(
                        group_chat_id=chat_id,
                        submitter_user_id=sender_id if isinstance(sender_id, int) else None,
                        submitter_name=sender_name, description=description,
                        telegram_update_id=update_id,
                    )
                    self.telegram.send_message(chat_id, f'Maintenance ticket #{ticket.id} created. Status: {ticket.status}')
            elif command == "/payments":
                self._reply_with_payment_history(chat_id, 'Show our payment history.', notice)
            elif command == "/reset":
                self.conversation_history.pop(chat_id, None)
                self.telegram.send_message(chat_id, "Conversation memory cleared.")
            elif command == "/tickets":
                tickets = self.maintenance.list_for_group(chat_id)
                reply = (
                    "You have no maintenance tickets."
                    if not tickets
                    else "\n".join(
                        f"#{ticket.id} [{ticket.status}] {ticket.description}"
                        for ticket in tickets[:10]
                    )
                )
                self.telegram.send_message(chat_id, reply)
            elif command == "/payment":
                self._start_payment_request(
                    update_id=update_id,
                    chat_id=chat_id,
                    sender_id=sender_id if isinstance(sender_id, int) else None,
                    sender_name=sender_name,
                    intent=IntentResult(
                        intent="payment_submission",
                        summary="Payment proof submission",
                        requires_attachment=True,
                    ),
                    original_text=text,
                    reference_date=message_date(message.get('date')),
                )
            elif command.startswith("/"):
                self.telegram.send_message(
                    chat_id,
                    "Unknown command. Use /help to see available commands.",
                )
            elif not cleaned:
                self.telegram.send_message(
                    chat_id,
                    "Please include a request after mentioning the bot.",
                )
            else:
                intent = notice.call(self.assistant.classify_message, cleaned,
                                     reference_date=message_date(message.get('date')).isoformat())
                print(f"Classified update {update_id} as {intent.intent}")
                if intent.intent == "maintenance_request":
                    ticket = self.maintenance.create_ticket(
                        group_chat_id=chat_id,
                        submitter_user_id=(
                            sender_id if isinstance(sender_id, int) else None
                        ),
                        submitter_name=sender_name,
                        description=intent.summary,
                        urgency=intent.urgency,
                        telegram_update_id=update_id,
                    )
                    self.telegram.send_message(
                        chat_id,
                        f"Maintenance ticket #{ticket.id} was created.\n"
                        f"Issue: {ticket.description}\n"
                        f"Urgency: {ticket.urgency}\n"
                        f"Status: {ticket.status}",
                    )
                elif intent.intent == "payment_submission":
                    self._start_payment_request(
                        update_id=update_id,
                        chat_id=chat_id,
                        sender_id=(
                            sender_id if isinstance(sender_id, int) else None
                        ),
                        sender_name=sender_name,
                        intent=intent,
                        original_text=text,
                        reference_date=message_date(message.get('date')),
                    )
                elif intent.intent in {"payment_history", "balance_inquiry", "billing_question"}:
                    self._reply_with_payment_history(chat_id, cleaned, notice)
                else:
                    history = self.conversation_history[chat_id]
                    history.append({"role": "user", "content": cleaned})
                    history[:] = history[-12:]
                    reply = notice.call(self.assistant.chat,
                        [
                            {
                                "role": "system",
                                "content": (
                                    "You are a concise assistant for residents of a "
                                    "small rental property. Do not invent balances, "
                                    "payments, schedules, or ticket statuses."
                                ),
                            },
                            *history,
                        ]
                    )
                    history.append({"role": "assistant", "content": reply})
                    history[:] = history[-12:]
                    self.telegram.send_message(chat_id, reply)
        except AssistantUnavailableError:
            self.telegram.send_message(chat_id,
                'AI replies are temporarily unavailable. Use /payment to send proof, '
                '/payments for history, /maintenance followed by your issue, or /tickets.')
        except (OllamaResponseError, TelegramAPIError, ValueError) as exc:
            print(f"Request error for update {update_id}: {exc}")
            self.telegram.send_message(
                chat_id,
                "I could not safely process that request. Please try again.",
            )

        self.database.mark_update_processed(update_id)
        return True

    def flush_outbox(self) -> None:
        for item in self.database.pending_outbox():
            try:
                self.telegram.send_message(item["chat_id"], item["text"])
                self.database.mark_outbox_sent(item["id"])
            except TelegramAPIError as exc:
                self.database.mark_outbox_failed(item["id"], str(exc))

    def run_forever(self) -> None:
        offset: int | None = None
        print("Landlord Telegram bot is online. Press Ctrl+C to stop.")
        while True:
            try:
                if hasattr(self.database, 'queue_reminders'):
                    self.database.store.create_due_bills()
                    self.database.queue_reminders()
                else:
                    from app.billing import BillingService
                    BillingService(self.database).create_due_bills()
                    cleanup_reviewed_proofs(self.database, self.proof_dir)
                    ReminderService(self.database).queue_reminders()
                self.flush_outbox()
                updates = self.telegram.get_updates(offset)
                for update in updates:
                    update_id = update.get("update_id")
                    self.handle_update(update)
                    if isinstance(update_id, int):
                        offset = update_id + 1
                self.flush_outbox()
            except KeyboardInterrupt:
                print("\nBot stopped.")
                return
            except (requests.RequestException, TelegramAPIError, RemoteStorageError) as exc:
                print(f"Telegram connection error: {exc}")
                time.sleep(3)

