import tempfile
from pathlib import Path
import unittest

from app.database import Database
from app.residents import ResidentService


class ResidentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "test.db"
        self.database = Database(database_path)
        self.database.initialize()
        self.residents = ResidentService(self.database)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_register_authorize_and_deactivate_resident(self):
        resident = self.residents.register(
            telegram_user_id=501,
            telegram_chat_id=101,
            name="Test Resident",
            unit="2A",
        )

        self.assertEqual(resident.unit, "2A")
        self.assertTrue(self.residents.is_authorized(101))

        self.residents.deactivate(101)
        self.assertFalse(self.residents.is_authorized(101))

    def test_registering_same_chat_updates_existing_resident(self):
        first = self.residents.register(
            telegram_user_id=501,
            telegram_chat_id=101,
            name="Old Name",
            unit="1A",
        )
        updated = self.residents.register(
            telegram_user_id=501,
            telegram_chat_id=101,
            name="New Name",
            unit="3C",
        )

        self.assertEqual(first.id, updated.id)
        self.assertEqual(updated.name, "New Name")
        self.assertEqual(updated.unit, "3C")


if __name__ == "__main__":
    unittest.main()

