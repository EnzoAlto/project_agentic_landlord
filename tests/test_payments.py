import tempfile
from pathlib import Path
import unittest

from app.database import Database
from app.payments import PaymentService


class PaymentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "test.db")
        self.database.initialize()
        self.database.upsert_group(-100123, "Unit 2A", "supergroup")
        self.payments = PaymentService(self.database)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _pending_submission(self):
        submission = self.payments.create_request(
            group_chat_id=-100123,
            submitter_user_id=501,
            submitter_name="Test Resident",
            source_update_id=1001,
            summary="June rent payment",
            categories=("rent",),
            billing_period="June 2026",
            amount_claimed="5000",
        )
        return self.payments.attach_proof(
            submission_id=submission.id,
            proof_message_id=55,
            telegram_file_id="file-1",
            proof_path="/tmp/proof.jpg",
            caption="June rent",
        )

    def test_review_updates_status_and_queues_notification(self):
        submission = self._pending_submission()

        reviewed = self.payments.review(
            submission_id=submission.id,
            status="approved",
            note="Receipt matched.",
        )

        self.assertEqual(reviewed.status, "approved")
        outbox = self.database.pending_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["chat_id"], -100123)
        self.assertIn("approved", outbox[0]["text"])

    def test_reviewed_submission_cannot_be_reviewed_twice(self):
        submission = self._pending_submission()
        self.payments.review(
            submission_id=submission.id,
            status="rejected",
            note="Unreadable receipt.",
        )

        with self.assertRaises(ValueError):
            self.payments.review(
                submission_id=submission.id,
                status="approved",
                note="",
            )

    def test_needs_information_accepts_a_replacement_screenshot(self):
        submission = self._pending_submission()
        self.payments.review(
            submission_id=submission.id,
            status="needs_information",
            note="Send a clearer image.",
        )

        awaiting = self.payments.find_awaiting_proof(
            group_chat_id=-100123,
            submitter_user_id=501,
            reply_to_message_id=None,
        )
        self.assertIsNotNone(awaiting)
        updated = self.payments.attach_proof(
            submission_id=awaiting.id,
            proof_message_id=56,
            telegram_file_id="file-2",
            proof_path="/tmp/proof-2.jpg",
            caption=None,
        )
        self.assertEqual(updated.status, "pending_review")


if __name__ == "__main__":
    unittest.main()
