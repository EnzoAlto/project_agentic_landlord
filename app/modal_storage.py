"""Serialized database jobs backed by consistent SQLite snapshots.

This module is independent of Modal so persistence and retry behavior can be
tested locally. The deployed function must be the sole owner of storage_root.
"""

from dataclasses import asdict
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Callable
from uuid import uuid4

from app.billing import BillingService
from app.database import Database
from app.maintenance import MaintenanceService
from app.payment_history import group_payment_context
from app.payments import PaymentService
from app.reminders import ReminderService
from app.proof_cleanup import cleanup_reviewed_proofs


READ_OPERATIONS = {
    'health', 'list_units', 'list_groups', 'list_bills',
    'list_payments', 'payment_history', 'list_maintenance', 'dashboard_state', 'read_proof',
    'is_update_processed', 'pending_outbox', 'find_awaiting_proof', 'get_payment',
    'group_maintenance',
    'cloud_bot_runtime', 'next_telegram_update',
}
WRITE_OPERATIONS = {
    'save_month_table', 'register_group', 'create_maintenance',
    'set_maintenance_status', 'import_database', 'apply_payment', 'review_payment', 'queue_reminders',
    'mark_update_processed', 'mark_outbox_sent', 'mark_outbox_failed',
    'create_payment_request', 'set_request_message', 'upload_proof',
    'set_cloud_bot_runtime', 'enqueue_telegram_update', 'finish_telegram_update',
    'fail_telegram_update',
}


def _dispatch(database: Database, operation: str, payload: dict) -> object:
    billing = BillingService(database, cleanup_proofs=False)
    maintenance = MaintenanceService(database)
    payments = PaymentService(database, cleanup_proofs=False)
    if operation == 'cloud_bot_runtime':
        with database.connection() as connection:
            row = connection.execute('SELECT enabled FROM telegram_cloud_runtime WHERE bot_id = ?',
                                     (payload['bot_id'],)).fetchone()
        return {'enabled': bool(row and row['enabled'])}
    if operation == 'set_cloud_bot_runtime':
        with database.connection() as connection:
            connection.execute('''INSERT INTO telegram_cloud_runtime VALUES (?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET enabled = excluded.enabled''',
                (payload['bot_id'], bool(payload['enabled'])))
        return {'enabled': bool(payload['enabled'])}
    if operation == 'enqueue_telegram_update':
        update = payload['update']
        if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
            raise ValueError('Invalid Telegram update')
        with database.connection() as connection:
            connection.execute('''INSERT OR IGNORE INTO telegram_inbox(bot_id, update_id, payload_json)
                                  VALUES (?, ?, ?)''',
                               (payload['bot_id'], update['update_id'], json.dumps(update)))
        return {'queued': True}
    if operation == 'next_telegram_update':
        with database.connection() as connection:
            row = connection.execute('''SELECT *,
                retry_after IS NULL OR retry_after <= CURRENT_TIMESTAMP AS ready
                FROM telegram_inbox WHERE bot_id = ? AND state = 'pending'
                ORDER BY update_id LIMIT 1''', (payload['bot_id'],)).fetchone()
        return json.loads(row['payload_json']) if row and row['ready'] else None
    if operation == 'finish_telegram_update':
        with database.connection() as connection:
            connection.execute("UPDATE telegram_inbox SET state = 'done', last_error = NULL WHERE bot_id = ? AND update_id = ?",
                               (payload['bot_id'], payload['update_id']))
        return True
    if operation == 'fail_telegram_update':
        with database.connection() as connection:
            connection.execute('''UPDATE telegram_inbox SET attempts = attempts + 1,
                state = CASE WHEN attempts >= 4 THEN 'failed' ELSE 'pending' END,
                retry_after = datetime('now', '+1 minute'), last_error = ?
                WHERE bot_id = ? AND update_id = ?''',
                (payload['error'], payload['bot_id'], payload['update_id']))
        return True
    if operation == 'is_update_processed':
        return database.is_update_processed(**payload)
    if operation == 'mark_update_processed':
        database.mark_update_processed(**payload)
        return True
    if operation == 'pending_outbox':
        return [dict(item) for item in database.pending_outbox(**payload)]
    if operation == 'mark_outbox_sent':
        database.mark_outbox_sent(**payload)
        return True
    if operation == 'mark_outbox_failed':
        database.mark_outbox_failed(**payload)
        return True
    if operation == 'create_payment_request':
        return asdict(payments.create_request(**payload))
    if operation == 'set_request_message':
        payments.set_request_message(**payload)
        return True
    if operation == 'find_awaiting_proof':
        submission = payments.find_awaiting_proof(**payload)
        return asdict(submission) if submission else None
    if operation == 'get_payment':
        return asdict(payments.get(**payload))
    if operation == 'group_maintenance':
        return [asdict(item) for item in maintenance.list_for_group(**payload)]
    if operation == 'health':
        return {'status': 'ready', 'units': len(billing.list_units()),
                'storage_mode': 'serialized-snapshot', 'dataset': 'worker-test'}
    if operation == 'dashboard_state':
        with database.connection() as connection:
            rates = [dict(row) for row in connection.execute('SELECT * FROM rent_rates')]
        units = billing.list_units()
        return dict(
            units=[asdict(unit) for unit in units], groups=billing.list_telegram_groups(),
            bills=[asdict(bill) for bill in billing.list_bills()],
            payments=[asdict(item) for item in PaymentService(database).list_submissions()],
            maintenance=[asdict(item) for item in maintenance.list_all()],
            credits={str(unit.id): billing.available_credit_cents(unit.id) for unit in units},
            rent_rates=rates,
        )
    if operation == 'apply_payment':
        return asdict(billing.apply_payment(**payload))
    if operation == 'review_payment':
        return asdict(PaymentService(database, cleanup_proofs=False).review(**payload))
    if operation == 'queue_reminders':
        return ReminderService(database).queue_reminders(**payload)
    if operation == 'import_database':
        with database.connection() as connection:
            return {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('units', 'monthly_bills', 'payment_submissions', 'maintenance_requests')}
    if operation == 'list_units':
        return [asdict(unit) for unit in billing.list_units()]
    if operation == 'list_groups':
        return billing.list_telegram_groups()
    if operation == 'list_bills':
        return [asdict(bill) for bill in billing.list_bills(**payload)]
    if operation == 'list_payments':
        return [asdict(payment) for payment in PaymentService(database).list_submissions(**payload)]
    if operation == 'payment_history':
        return group_payment_context(database, **payload)
    if operation == 'list_maintenance':
        return [asdict(ticket) for ticket in maintenance.list_all()]
    if operation == 'save_month_table':
        billing.save_month_table(**payload)
        return {'saved': True}
    if operation == 'register_group':
        database.upsert_group(**payload)
        return {'saved': True}
    if operation == 'create_maintenance':
        return asdict(maintenance.create_ticket(**payload))
    if operation == 'set_maintenance_status':
        return asdict(maintenance.update_status(**payload))
    raise ValueError('Unsupported database operation')


class SnapshotWorker:
    def __init__(self, storage_root: Path, *, reload: Callable[[], None],
                 commit: Callable[[], None]):
        self.root = storage_root
        self.reload = reload
        self.commit = commit

    def execute(self, operation: str, payload: dict | None = None,
                request_id: str | None = None) -> dict:
        if operation not in READ_OPERATIONS | WRITE_OPERATIONS:
            raise ValueError('Unsupported database operation')
        if payload is not None and not isinstance(payload, dict):
            raise ValueError('Job payload must be an object')
        payload = {} if payload is None else payload
        writing = operation in WRITE_OPERATIONS
        if writing and (not isinstance(request_id, str) or not request_id.strip()
                        or len(request_id) > 200):
            raise ValueError('Writes require a unique request_id of at most 200 characters')
        fingerprint = hashlib.sha256(json.dumps(
            {'operation': operation, 'payload': payload}, sort_keys=True,
            allow_nan=False, separators=(',', ':'),
        ).encode()).hexdigest()

        # No files may be open on the Volume during reload.
        self.reload()
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / 'CURRENT.json'
        existing_snapshot = None
        if manifest.exists():
            name = json.loads(manifest.read_text())['snapshot']
            if (not isinstance(name, str) or len(name) != 35
                    or not name.endswith('.db')):
                raise ValueError('Invalid database snapshot manifest')
            try:
                int(name[:-3], 16)
            except ValueError as exc:
                raise ValueError('Invalid database snapshot name') from exc
            existing_snapshot = self.root / 'snapshots' / name
            if not existing_snapshot.is_file():
                raise RuntimeError('Committed database snapshot is missing; refusing to initialize over it')

        with tempfile.TemporaryDirectory(prefix='landlord-worker-') as temporary:
            working_path = Path(temporary) / 'landlord.db'
            if existing_snapshot is not None:
                shutil.copyfile(existing_snapshot, working_path)
            elif operation == 'import_database':
                working_path.write_bytes(base64.b64decode(payload['database_b64'], validate=True))
                # Reject malformed or foreign databases before migrations run.
                with sqlite3.connect(working_path) as connection:
                    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Imported SQLite database failed integrity check')
                    required = {'units', 'payment_submissions', 'monthly_bills', 'maintenance_requests'}
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not required.issubset(tables):
                        raise ValueError('Import is not a current landlord database')
                    if connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchone()[0]:
                        raise ValueError('Import must not contain custom triggers or views')
            elif self.root.name != 'worker-test':
                if operation == 'health':
                    return {'ok': True, 'replayed': False, 'result': {
                        'status': 'awaiting_import', 'dataset': self.root.name}}
                raise ValueError('Import a database before using this dataset')
            database = Database(working_path)
            database.initialize()
            with database.connection() as connection:
                connection.execute('''CREATE TABLE IF NOT EXISTS worker_requests (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )''')
                previous = connection.execute(
                    'SELECT fingerprint, result_json FROM worker_requests WHERE request_id = ?',
                    (request_id,),
                ).fetchone() if writing else None
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('request_id was already used for a different operation or payload')
                return {'ok': True, 'replayed': True, 'result': json.loads(previous['result_json'])}
            if operation == 'import_database':
                if existing_snapshot is not None:
                    raise ValueError('Dataset already exists; database import will not overwrite it')
                self._import_proofs(database, payload.get('proofs', []))
            if operation == 'read_proof':
                result = self._read_proof(database, **payload)
                return {'ok': True, 'replayed': False, 'result': result}

            # Service methods can commit locally. Nothing is published if any
            # part fails; only a complete job and its receipt become durable.
            result = (self._upload_proof(database, payload) if operation == 'upload_proof'
                      else _dispatch(database, operation, payload))
            if operation == 'health':
                result['dataset'] = self.root.name
            encoded = json.dumps(result, allow_nan=False)
            # An idle reminder check makes no changes and needs no receipt or
            # new snapshot. Avoid thousands of identical backups each day.
            no_change = operation == 'queue_reminders' and result == 0
            if writing and not no_change:
                with database.connection() as connection:
                    connection.execute('INSERT INTO worker_requests VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                                       (request_id, fingerprint, encoded))
            if (writing and not no_change) or existing_snapshot is None:
                self._publish(working_path, manifest)
            # Review and its cleanup task are durable BEFORE deleting a proof.
            with database.connection() as connection:
                pending = connection.execute('SELECT COUNT(*) FROM payment_proof_cleanup').fetchone()[0]
            if pending:
                cleanup_reviewed_proofs(database, self.root / 'payment_proofs')
                self._publish(working_path, manifest)
            return {'ok': True, 'replayed': False, 'result': json.loads(encoded)}

    def _import_proofs(self, database: Database, proofs: list[dict]) -> None:
        proof_root = self.root / 'payment_proofs'
        proof_root.mkdir(exist_ok=True)
        supplied = {int(item['submission_id']): item for item in proofs}
        with database.connection() as connection:
            rows = connection.execute('SELECT id, status FROM payment_submissions').fetchall()
            connection.execute('DELETE FROM payment_proof_cleanup')
            for row in rows:
                path = None
                item = supplied.get(row['id'])
                if item and row['status'] == 'pending_review':
                    suffix = item['suffix'].lower()
                    if suffix not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                        raise ValueError('Unsupported screenshot format in import')
                    data = base64.b64decode(item['data_b64'], validate=True)
                    if len(data) > 20 * 1024 * 1024:
                        raise ValueError('Screenshot exceeds 20 MiB import limit')
                    path = proof_root / f"payment_{row['id']}_{uuid4().hex}{suffix}"
                    path.write_bytes(data)
                connection.execute('UPDATE payment_submissions SET proof_path = ? WHERE id = ?',
                                   (str(path) if path else None, row['id']))

    def _read_proof(self, database: Database, submission_id: int) -> dict | None:
        submission = PaymentService(database).get(submission_id)
        if submission.status != 'pending_review' or not submission.proof_path:
            return None
        path = Path(submission.proof_path)
        path.resolve().relative_to((self.root / 'payment_proofs').resolve())
        if not path.is_file():
            return None
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('Screenshot exceeds display limit')
        return {'data_b64': base64.b64encode(path.read_bytes()).decode()}

    def _upload_proof(self, database: Database, payload: dict) -> dict:
        payments = PaymentService(database, cleanup_proofs=False)
        submission = payments.get(payload['submission_id'])
        if submission.status not in {'awaiting_proof', 'needs_information'}:
            raise ValueError('This submission is not awaiting a screenshot')
        if submission.group_chat_id != payload['chat_id']:
            raise ValueError('Screenshot belongs to a different chat')
        if submission.submitter_user_id != payload['sender_id']:
            raise ValueError('Screenshot belongs to a different sender')
        suffix = payload['suffix'].lower()
        if suffix not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
            raise ValueError('Upload a JPG, PNG, WebP or GIF payment screenshot')
        data = base64.b64decode(payload['data_b64'], validate=True)
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError('Screenshot must be nonempty and no larger than 20 MiB')
        root = self.root / 'payment_proofs'
        root.mkdir(exist_ok=True)
        path = root / f'payment_{submission.id}_{uuid4().hex}{suffix}'
        path.write_bytes(data)
        updated = payments.attach_proof(
            submission_id=submission.id, proof_message_id=payload['proof_message_id'],
            telegram_file_id=payload['telegram_file_id'], proof_path=str(path),
            caption=payload.get('caption'),
        )
        return asdict(updated)

    def _publish(self, working_path: Path, manifest: Path) -> None:
        snapshots = self.root / 'snapshots'
        snapshots.mkdir(exist_ok=True)
        snapshot_name = f'{uuid4().hex}.db'
        snapshot_path = snapshots / snapshot_name
        # SQLite backup includes committed WAL contents and produces a
        # standalone file. SQLite is never opened directly on a Modal Volume.
        backup_path = working_path.parent / 'snapshot.db'
        with sqlite3.connect(working_path) as source:
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)
        shutil.copyfile(backup_path, snapshot_path)
        self.commit()
        pending_manifest = self.root / f'{uuid4().hex}.json'
        pending_manifest.write_text(json.dumps({'snapshot': snapshot_name}))
        pending_manifest.replace(manifest)
        self.commit()
        # Snapshot history is retained for this initial test worker. A recovery
        # and retention policy must be added before production migration.
