import tempfile
from pathlib import Path
import unittest

from app.database import Database
from app.maintenance import MaintenanceService
from app.ollama_client import IntentResult
from app.payments import PaymentService
from app.telegram_bot import LandlordBot


class FakeTelegram:
    def __init__(self):
        self.sent_messages = []

    def get_updates(self, offset=None):
        return []

    def send_message(self, chat_id, text):
        result = {
            "message_id": 9000 + len(self.sent_messages),
            "chat": {"id": chat_id},
            "text": text,
        }
        self.sent_messages.append(result)
        return result

    def download_file(self, file_id, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake image")
        return destination


class FakeAssistant:
    def __init__(self, intent="maintenance_request"):
        self.intent = intent
        self.classified_messages = []

    def classify_message(self, text):
        self.classified_messages.append(text)
        if self.intent == "payment_submission":
            return IntentResult(
                intent=self.intent,
                summary="June rent and electricity payment",
                payment_categories=("rent", "electricity"),
                billing_period="June 2026",
                amount_claimed="5000",
                requires_attachment=True,
            )
        return IntentResult(
            intent=self.intent,
            summary="Air conditioner is leaking",
            urgency="normal",
        )

    def chat(self, messages):
        return "This is a simulated assistant reply."


def group_update(
    *,
    update_id=1001,
    chat_id=-100123,
    sender_id=501,
    text="@test_landlord_bot My aircon is leaking",
    message_id=55,
    reply_to=None,
    photo=False,
):
    message = {
        "message_id": message_id,
        "from": {"id": sender_id, "first_name": "Test Resident"},
        "chat": {"id": chat_id, "type": "supergroup", "title": "Unit 2A"},
    }
    if text is not None:
        message["text"] = text
    if reply_to is not None:
        message["reply_to_message"] = reply_to
    if photo:
        message["photo"] = [
            {"file_id": "small", "width": 90, "height": 90},
            {"file_id": "large", "width": 900, "height": 900},
        ]
    return {"update_id": update_id, "message": message}


class MessageFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database = Database(root / "test.db")
        self.database.initialize()
        self.maintenance = MaintenanceService(self.database)
        self.payments = PaymentService(self.database)
        self.telegram = FakeTelegram()
        self.assistant = FakeAssistant()
        self.bot = LandlordBot(
            database=self.database,
            maintenance=self.maintenance,
            payments=self.payments,
            telegram=self.telegram,
            assistant=self.assistant,
            bot_username="test_landlord_bot",
            bot_user_id=42,
            proof_dir=root / "proofs",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_mentioned_group_message_creates_maintenance_ticket(self):
        self.assertTrue(self.bot.handle_update(group_update()))

        tickets = self.maintenance.list_for_group(-100123)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].description, "Air conditioner is leaking")
        self.assertIn("ticket #1", self.telegram.sent_messages[0]["text"])

    def test_unmentioned_group_message_is_ignored(self):
        handled = self.bot.handle_update(group_update(text="My aircon is leaking"))

        self.assertFalse(handled)
        self.assertEqual(self.assistant.classified_messages, [])
        self.assertEqual(self.maintenance.list_for_group(-100123), [])
        self.assertEqual(self.telegram.sent_messages, [])

    def test_any_sender_can_activate_bot_in_group(self):
        self.bot.handle_update(group_update(sender_id=777))

        self.assertEqual(len(self.maintenance.list_for_group(-100123)), 1)
        self.assertEqual(len(self.assistant.classified_messages), 1)

    def test_duplicate_update_is_ignored(self):
        update = group_update()
        self.bot.handle_update(update)
        self.bot.handle_update(update)

        self.assertEqual(len(self.maintenance.list_for_group(-100123)), 1)
        self.assertEqual(len(self.telegram.sent_messages), 1)

    def test_payment_request_and_screenshot_become_pending_review(self):
        self.assistant.intent = "payment_submission"
        self.bot.handle_update(
            group_update(text="@test_landlord_bot I paid June rent and power")
        )
        request_message = self.telegram.sent_messages[-1]

        self.bot.handle_update(
            group_update(
                update_id=1002,
                text=None,
                message_id=56,
                reply_to={
                    "message_id": request_message["message_id"],
                    "from": {"id": 42, "is_bot": True},
                },
                photo=True,
            )
        )

        submissions = self.payments.list_submissions()
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0].status, "pending_review")
        self.assertEqual(submissions[0].categories, ("rent", "electricity"))
        self.assertTrue(Path(submissions[0].proof_path).is_file())

    def test_first_unmentioned_image_completes_awaiting_payment(self):
        self.assistant.intent = "payment_submission"
        self.bot.handle_update(
            group_update(text="@test_landlord_bot I paid June rent")
        )

        handled = self.bot.handle_update(
            group_update(
                update_id=1002,
                text=None,
                message_id=56,
                photo=True,
            )
        )

        submission = self.payments.list_submissions()[0]
        self.assertTrue(handled)
        self.assertEqual(submission.status, "pending_review")
        self.assertEqual(submission.proof_message_id, 56)
        self.assertIn(
            "Awaiting landlord review",
            self.telegram.sent_messages[-1]["text"],
        )

        second_image_handled = self.bot.handle_update(
            group_update(
                update_id=1003,
                text=None,
                message_id=57,
                photo=True,
            )
        )
        self.assertFalse(second_image_handled)

    def test_unmentioned_image_cannot_complete_another_senders_payment(self):
        self.assistant.intent = "payment_submission"
        self.bot.handle_update(
            group_update(
                sender_id=501,
                text="@test_landlord_bot I paid June rent",
            )
        )

        handled = self.bot.handle_update(
            group_update(
                update_id=1002,
                sender_id=777,
                text=None,
                message_id=56,
                photo=True,
            )
        )

        self.assertFalse(handled)
        submission = self.payments.list_submissions()[0]
        self.assertEqual(submission.status, "awaiting_proof")
        self.assertIsNone(submission.proof_message_id)

    def test_dashboard_review_is_sent_from_outbox(self):
        self.assistant.intent = "payment_submission"
        self.bot.handle_update(
            group_update(text="@test_landlord_bot I paid rent")
        )
        submission = self.payments.list_submissions()[0]
        self.payments.attach_proof(
            submission_id=submission.id,
            proof_message_id=56,
            telegram_file_id="proof",
            proof_path=str(Path(self.temporary_directory.name) / "proof.jpg"),
            caption=None,
        )
        self.payments.review(
            submission_id=submission.id,
            status="approved",
            note="Receipt matched.",
        )

        self.bot.flush_outbox()

        self.assertIn(
            "approved by the landlord",
            self.telegram.sent_messages[-1]["text"],
        )
        self.assertEqual(self.database.pending_outbox(), [])


if __name__ == "__main__":
    unittest.main()
