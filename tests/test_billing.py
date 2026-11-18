import tempfile
import math
from pathlib import Path
import unittest

from app.billing import BillingService
from app.database import Database
from app.payments import PaymentService


class BillingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "test.db")
        self.database.initialize()
        self.database.upsert_group(-100123, "Unit 102 Residents", "supergroup")
        self.billing = BillingService(self.database)
        self.payments = PaymentService(self.database)
        self.unit = next(
            unit for unit in self.billing.list_units()
            if unit.unit_number == "102"
        )
        self.billing.update_units(
            [
                {
                    "id": self.unit.id,
                    "resident_name": self.unit.resident_name,
                    "due_day": self.unit.due_day,
                    "rent_parking": "7500.00",
                    "telegram_group_chat_id": -100123,
                }
            ]
        )
        self.unit = self.billing.get_unit(self.unit.id)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _bill(self, period="2026-11"):
        return self.billing.upsert_bill(
            unit_id=self.unit.id,
            billing_period=period,
            rent_parking="7500.00",
            water_previous=100,
            water_present=110,
            water_bill="500.00",
            electricity_previous=None,
            electricity_present=None,
            electricity_bill="0.00",
            notes="",
        )

    def _submission(self, update_id=1001):
        submission = self.payments.create_request(
            group_chat_id=-100123,
            submitter_user_id=501,
            submitter_name="Test Resident",
            source_update_id=update_id,
            summary="Monthly payment",
            categories=("rent", "water"),
        )
        return self.payments.attach_proof(
            submission_id=submission.id,
            proof_message_id=update_id + 10,
            telegram_file_id=f"file-{update_id}",
            proof_path=f"/tmp/proof-{update_id}.jpg",
            caption=None,
        )

    def test_manual_readings_calculate_difference_and_total(self):
        bill = self._bill()

        self.assertEqual(bill.water_difference, 10)
        self.assertEqual(bill.water_bill_cents, 50_000)
        self.assertEqual(bill.total_due_cents, 800_000)
        self.assertEqual(bill.outstanding_cents, 800_000)
        self.assertEqual(bill.due_date, "2026-11-09")

    def test_partial_payment_leaves_remaining_balance(self):
        bill = self._bill()
        submission = self._submission()

        result = self.billing.apply_payment(
            submission_id=submission.id,
            bill_id=bill.id,
            amount_received="5000.00",
            fully_paid=False,
            received_date="2026-11-09",
        )

        self.assertEqual(result.payment_state, "partial")
        self.assertEqual(result.applied_cents, 500_000)
        self.assertEqual(result.remaining_cents, 300_000)
        self.assertEqual(self.billing.get_bill(bill.id).outstanding_cents, 300_000)

    def test_fully_paid_uses_exact_outstanding_balance(self):
        bill = self._bill()
        submission = self._submission()

        result = self.billing.apply_payment(
            submission_id=submission.id,
            bill_id=bill.id,
            amount_received=None,
            fully_paid=True,
            received_date="2026-11-09",
            or_number="OR-100",
        )

        self.assertEqual(result.payment_state, "paid")
        self.assertEqual(result.amount_received_cents, 800_000)
        self.assertEqual(result.remaining_cents, 0)
        reviewed = self.payments.get(submission.id)
        self.assertEqual(reviewed.or_number, "OR-100")
        self.assertEqual(reviewed.status, "approved")

    def test_advance_payment_becomes_credit_on_next_bill(self):
        november = self._bill("2026-11")
        submission = self._submission()

        result = self.billing.apply_payment(
            submission_id=submission.id,
            bill_id=november.id,
            amount_received="10000.00",
            fully_paid=False,
            received_date="2026-11-09",
        )

        self.assertEqual(result.payment_state, "advance")
        self.assertEqual(result.applied_cents, 800_000)
        self.assertEqual(result.credit_cents, 200_000)
        self.assertEqual(
            self.billing.available_credit_cents(self.unit.id), 200_000
        )

        december = self._bill("2026-12")
        self.assertEqual(december.total_due_cents, 800_000)
        self.assertEqual(december.allocated_cents, 200_000)
        self.assertEqual(december.outstanding_cents, 600_000)
        self.assertEqual(self.billing.available_credit_cents(self.unit.id), 0)

    def test_seeded_unit_directory_matches_source_total(self):
        units = self.billing.list_units()

        self.assertEqual(len(units), 23)
        self.assertEqual(
            sum(unit.rent_parking_cents for unit in units),
            18_950_000,
        )
        self.assertEqual(
            next(unit for unit in units if unit.unit_number == "207").occupancy_status,
            "vacant",
        )

    def test_group_assignment_appears_on_unit(self):
        unit = self.billing.get_unit(self.unit.id)

        self.assertEqual(unit.chatbot_status, "Connected")
        self.assertEqual(unit.telegram_group_chat_id, -100123)
        self.assertEqual(unit.telegram_group_name, "Unit 102 Residents")

    def test_directory_save_accepts_blank_due_day_for_vacant_unit(self):
        vacant = next(
            unit for unit in self.billing.list_units()
            if unit.unit_number == "207"
        )

        self.billing.update_units(
            [
                {
                    "id": vacant.id,
                    "resident_name": "Vacant",
                    "due_day": math.nan,
                    "rent_parking": "7000.00",
                    "telegram_group_chat_id": None,
                }
            ]
        )

        self.assertIsNone(self.billing.get_unit(vacant.id).due_day)

    def test_payment_cannot_be_applied_twice(self):
        bill = self._bill()
        submission = self._submission()
        self.billing.apply_payment(
            submission_id=submission.id,
            bill_id=bill.id,
            amount_received=None,
            fully_paid=True,
            received_date="2026-11-09",
        )

        with self.assertRaises(ValueError):
            self.billing.apply_payment(
                submission_id=submission.id,
                bill_id=bill.id,
                amount_received="100.00",
                fully_paid=False,
                received_date="2026-11-09",
            )


if __name__ == "__main__":
    unittest.main()
