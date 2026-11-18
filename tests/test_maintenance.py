import tempfile
from pathlib import Path
import unittest

from app.database import Database
from app.maintenance import MaintenanceService
from app.residents import ResidentService


class MaintenanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "test.db"
        self.database = Database(database_path)
        self.database.initialize()
        self.residents = ResidentService(self.database)
        self.maintenance = MaintenanceService(self.database)
        self.resident = self.residents.register(
            telegram_user_id=501,
            telegram_chat_id=101,
            name="Test Resident",
            unit="2A",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_create_and_update_ticket(self):
        ticket = self.maintenance.create_ticket(
            resident_id=self.resident.id,
            description="Air conditioner is leaking",
            urgency="high",
            telegram_update_id=1001,
        )

        self.assertEqual(ticket.status, "open")
        self.assertEqual(ticket.urgency, "high")

        updated = self.maintenance.update_status(ticket.id, "in_progress")
        self.assertEqual(updated.status, "in_progress")

    def test_same_telegram_update_does_not_duplicate_ticket(self):
        first = self.maintenance.create_ticket(
            resident_id=self.resident.id,
            description="Leaking sink",
            telegram_update_id=1001,
        )
        duplicate = self.maintenance.create_ticket(
            resident_id=self.resident.id,
            description="Leaking sink",
            telegram_update_id=1001,
        )

        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(len(self.maintenance.list_for_resident(self.resident.id)), 1)


if __name__ == "__main__":
    unittest.main()

