import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.modal_storage import SnapshotWorker


class SnapshotWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'worker'
        self.commits = 0
        self.worker = SnapshotWorker(self.root, reload=lambda: None, commit=self.commit)

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self):
        self.commits += 1

    def snapshot(self):
        return self.root / 'snapshots' / json.loads((self.root / 'CURRENT.json').read_text())['snapshot']

    def test_snapshot_survives_a_fresh_worker_and_has_no_wal_dependency(self):
        self.worker.execute('register_group',
            {'chat_id': -1, 'title': 'Residents', 'chat_type': 'group'}, 'group-1')
        fresh = SnapshotWorker(self.root, reload=lambda: None, commit=self.commit)
        self.assertEqual(fresh.execute('list_groups')['result'][0]['chat_id'], -1)
        with sqlite3.connect(self.snapshot()) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.commits, 2)

    def test_duplicate_write_returns_original_result_without_duplicate_ticket(self):
        self.worker.execute('register_group',
            {'chat_id': -1, 'title': 'Residents', 'chat_type': 'group'}, 'group-1')
        payload = {'group_chat_id': -1, 'description': 'Leaking sink'}
        first = self.worker.execute('create_maintenance', payload, 'ticket-1')
        second = self.worker.execute('create_maintenance', payload, 'ticket-1')
        self.assertEqual(first['result'], second['result'])
        self.assertTrue(second['replayed'])
        self.assertEqual(len(self.worker.execute('list_maintenance')['result']), 1)
        with self.assertRaises(ValueError):
            self.worker.execute('create_maintenance', {**payload, 'description': 'Other'}, 'ticket-1')

    def test_failed_mutation_does_not_publish_local_commits(self):
        self.worker.execute('health')
        old_manifest = (self.root / 'CURRENT.json').read_text()
        with self.assertRaises(ValueError):
            self.worker.execute('create_maintenance', {'description': ''}, 'bad-1')
        self.assertEqual((self.root / 'CURRENT.json').read_text(), old_manifest)
        self.assertEqual(self.worker.execute('list_maintenance')['result'], [])

    def test_unrecognized_operation_or_missing_write_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.worker.execute('sql', {'query': 'DROP TABLE units'})
        with self.assertRaises(ValueError):
            self.worker.execute('register_group', {'chat_id': -1})
        self.assertFalse(self.root.exists())

    def test_missing_committed_snapshot_does_not_silently_start_empty(self):
        self.root.mkdir()
        (self.root / 'CURRENT.json').write_text(json.dumps({'snapshot': 'a' * 32 + '.db'}))
        with self.assertRaises(RuntimeError):
            self.worker.execute('health')
