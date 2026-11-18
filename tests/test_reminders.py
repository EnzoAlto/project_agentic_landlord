from datetime import date
from pathlib import Path
import tempfile
import unittest

from app.billing import BillingService
from app.database import Database
from app.reminders import ReminderService


class ReminderTests(unittest.TestCase):
    def test_due_date_deduplication_and_repeatable_test_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'test.db')
            db.initialize()
            db.upsert_group(-123, 'Residents', 'group')
            db.upsert_group(456, 'Landlord private', 'private')
            billing = BillingService(db)
            unit = billing.list_units()[0]
            billing.update_units([dict(
                id=unit.id, resident_name=unit.resident_name,
                due_day=9, rent_parking=7500, telegram_group_chat_id=-123,
            )])
            billing.upsert_bill(
                unit_id=unit.id, billing_period='2026-11', rent_parking=7500,
                water_previous=None, water_present=None, water_bill=0,
                electricity_previous=None, electricity_present=None, electricity_bill=0,
            )
            reminders = ReminderService(db)
            self.assertEqual(reminders.queue_reminders(today=date(2026, 11, 8)), 0)
            self.assertEqual(reminders.queue_reminders(today=date(2026, 11, 9)), 1)
            self.assertEqual(reminders.queue_reminders(today=date(2026, 11, 10)), 0)
            self.assertEqual(reminders.queue_reminders(test=True, today=date(2026, 11, 10)), 2)
            self.assertEqual(reminders.queue_reminders(test=True, today=date(2026, 11, 10)), 2)
            outbox = db.pending_outbox()
            self.assertEqual(len(outbox), 5)
            self.assertIn('₱7,500.00', outbox[0]['text'])
            self.assertEqual(outbox[0]['chat_id'], -123)


if __name__ == '__main__':
    unittest.main()
