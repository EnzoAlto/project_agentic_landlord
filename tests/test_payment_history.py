import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.payments import PaymentService
from app.payment_history import group_payment_context


class PaymentHistoryTests(unittest.TestCase):
    def test_context_only_contains_requesting_chats_records(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / 'history.db')
            database.initialize()
            payments = PaymentService(database)
            for chat_id, period in ((-111, '2026-01'), (-222, '2026-02')):
                database.upsert_group(chat_id, str(chat_id), 'group')
                payments.create_request(
                    group_chat_id=chat_id, submitter_user_id=42,
                    submitter_name='Resident', source_update_id=abs(chat_id),
                    summary='Private claim', billing_period=period,
                )
            context = group_payment_context(database, -111)
            self.assertEqual(len(context['submissions']), 1)
            self.assertEqual(context['submissions'][0]['claimed_period'], '2026-01')
            self.assertNotIn('2026-02', str(context))
            self.assertNotIn('Private claim', str(context))
            self.assertEqual(group_payment_context(database, 999)['submissions'], [])
