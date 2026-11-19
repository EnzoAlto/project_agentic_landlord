from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app.database import Database
from app.payments import PaymentService
from app.proof_cleanup import cleanup_reviewed_proofs


class ProofCleanupTests(unittest.TestCase):
    def test_each_review_deletes_file_and_preserves_telegram_references(self):
        for status in ('approved', 'rejected', 'needs_information'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                db = Database(root / 'test.db')
                db.initialize()
                db.upsert_group(-123, 'Residents', 'group')
                service = PaymentService(db)
                submission = service.create_request(group_chat_id=-123,
                    submitter_user_id=1, submitter_name='Resident', source_update_id=1,
                    summary='Payment')
                proof_root = root / 'proofs'
                proof_root.mkdir()
                proof = proof_root / 'payment_1.jpg'
                proof.write_bytes(b'image')
                service.attach_proof(submission_id=submission.id, proof_message_id=42,
                    telegram_file_id='file-42', proof_path=str(proof), caption=None)
                with patch('app.proof_cleanup.Settings.from_env', return_value=SimpleNamespace(payment_proof_dir=proof_root)):
                    reviewed = service.review(submission_id=submission.id, status=status, note='Reviewed')
                self.assertFalse(proof.exists())
                self.assertIsNone(reviewed.proof_path)
                self.assertEqual(reviewed.status, status)
                self.assertEqual(reviewed.telegram_file_id, 'file-42')
                self.assertEqual(reviewed.proof_message_id, 42)
                if status == 'needs_information':
                    replacement = proof_root / 'payment_1_new.jpg'
                    replacement.write_bytes(b'new image')
                    updated = service.attach_proof(submission_id=submission.id,
                        proof_message_id=43, telegram_file_id='file-43',
                        proof_path=str(replacement), caption=None)
                    cleanup_reviewed_proofs(db, proof_root)
                    self.assertEqual(updated.status, 'pending_review')
                    self.assertTrue(replacement.exists())

    def test_failed_cleanup_keeps_review_and_retries_without_deleting_new_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / 'test.db')
            db.initialize()
            db.upsert_group(-123, 'Residents', 'group')
            service = PaymentService(db)
            submission = service.create_request(group_chat_id=-123,
                submitter_user_id=1, submitter_name='Resident', source_update_id=1, summary='Payment')
            proof_root = root / 'proofs'
            proof_root.mkdir()
            old = proof_root / 'old.jpg'
            old.write_bytes(b'old')
            service.attach_proof(submission_id=submission.id, proof_message_id=1,
                telegram_file_id='old', proof_path=str(old), caption=None)
            with patch('app.proof_cleanup.Settings.from_env', return_value=SimpleNamespace(payment_proof_dir=proof_root)), patch.object(Path, 'unlink', side_effect=PermissionError):
                service.review(submission_id=submission.id, status='needs_information', note='New proof please')
            self.assertEqual(service.get(submission.id).status, 'needs_information')
            new = proof_root / 'new.jpg'
            new.write_bytes(b'new')
            service.attach_proof(submission_id=submission.id, proof_message_id=2,
                telegram_file_id='new', proof_path=str(new), caption=None)
            cleanup_reviewed_proofs(db, proof_root)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertEqual(service.get(submission.id).proof_path, str(new))
