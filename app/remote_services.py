"""Dashboard service adapters; all cloud operations use the single Modal worker."""

import base64
from dataclasses import fields
import json
import time
from uuid import uuid4

from app.billing import MonthlyBill, UnitRecord, PaymentApplication
from app.maintenance import MaintenanceTicket
from app.payments import PaymentSubmission


class RemoteStorageError(RuntimeError):
    """A cloud job could not be confirmed; preserve its ID when retrying."""


class ModalStore:
    def __init__(self, app_name: str, dataset: str, pending_writes: dict):
        import modal
        self.app_name = app_name
        self.worker = modal.Function.from_name(app_name, 'database_job')
        self.dataset = dataset
        self.pending_writes = pending_writes
        self._state = None

    def call(self, operation: str, payload: dict | None = None, *, write: bool = False,
             request_id: str | None = None):
        payload = {} if payload is None else payload
        # Preserve the same ID across uncertain retries and Streamlit reruns.
        key = json.dumps([self.dataset, operation, payload], sort_keys=True, allow_nan=False)
        if write:
            request_id = self.pending_writes.setdefault(key, request_id or str(uuid4()))
        last_error = None
        for attempt in range(2):
            try:
                response = self.worker.remote(operation, payload, request_id=request_id,
                                              dataset=self.dataset, include_claim_details=True,
                                              include_others_bill=True)
                if not response.get('ok'):
                    raise RuntimeError('Database worker did not confirm the request')
                if write:
                    self.pending_writes.pop(key, None)
                    self._state = None
                return response['result']
            except (ValueError, LookupError, TypeError):
                # Rejected operations did not persist and may be corrected.
                if write:
                    self.pending_writes.pop(key, None)
                raise
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        raise RemoteStorageError(
            'Modal request could not be confirmed. Retry the same action without '
            'changing its inputs; the request ID is retained in this session.'
        ) from last_error

    @property
    def state(self):
        if self._state is None:
            self._state = self.call('dashboard_state')
        return self._state

    def proof_bytes(self, submission_id: int) -> bytes | None:
        value = self.call('read_proof', {'submission_id': submission_id})
        return base64.b64decode(value['data_b64']) if value else None

    def queue_reminders(self, *, test: bool = False) -> int:
        return self.call('queue_reminders', {'test': test}, write=True)

    def create_due_bills(self):
        return self.call('create_due_bills', write=True)


class RemoteBotDatabase:
    """Only explicit operations are available; no remote SQL connection."""
    def __init__(self, store: ModalStore):
        self.store = store

    def is_update_processed(self, update_id):
        return self.store.call('is_update_processed', {'update_id': update_id})

    def mark_update_processed(self, update_id):
        return self.store.call('mark_update_processed', {'update_id': update_id}, write=True)

    def upsert_group(self, chat_id, title, chat_type):
        return self.store.call('register_group', dict(chat_id=chat_id, title=title,
                                                     chat_type=chat_type), write=True)

    def pending_outbox(self):
        return self.store.call('pending_outbox')

    def mark_outbox_sent(self, outbox_id):
        return self.store.call('mark_outbox_sent', {'outbox_id': outbox_id}, write=True)

    def mark_outbox_failed(self, outbox_id, error):
        return self.store.call('mark_outbox_failed', dict(outbox_id=outbox_id, error=error), write=True)

    def payment_context(self, chat_id):
        return self.store.call('payment_history', {'chat_id': chat_id})

    def queue_reminders(self):
        return self.store.queue_reminders()

class RemoteBillingService:
    def __init__(self, store: ModalStore):
        self.store = store

    def list_units(self):
        return [UnitRecord(**row) for row in self.store.state['units']]

    def list_telegram_groups(self):
        return self.store.state['groups']

    def list_bills(self, group_chat_id=None, unit_id=None):
        return [MonthlyBill(**row) for row in self.store.state['bills']
                if (group_chat_id is None or row['group_chat_id'] == group_chat_id)
                and (unit_id is None or row['unit_id'] == unit_id)]

    def available_credit_cents(self, unit_id):
        return self.store.state['credits'].get(str(unit_id), 0)

    def rent_for_period(self, unit_id, period):
        rates = [row for row in self.store.state['rent_rates']
                 if row['unit_id'] == unit_id and row['effective_period'] <= period]
        if rates:
            return max(rates, key=lambda row: row['effective_period'])['amount_cents']
        return next(row['rent_parking_cents'] for row in self.store.state['units']
                    if row['id'] == unit_id)

    def save_month_table(self, period, records):
        import math
        records = [{key: None if isinstance(value, float) and math.isnan(value) else value
                    for key, value in record.items()} for record in records]
        return self.store.call('save_month_table', {'period': period, 'records': records}, write=True)

    def apply_payment(self, **payload):
        return PaymentApplication(**self.store.call('apply_payment', payload, write=True))

    def record_advance(self, **payload):
        return PaymentApplication(**self.store.call('record_advance', payload, write=True))

    def undo_approval(self, submission_id):
        return self.store.call('undo_approval', {'submission_id': submission_id}, write=True)


class RemotePaymentService:
    def __init__(self, store: ModalStore):
        self.store = store

    def list_submissions(self, statuses=None):
        return [self._submission(row)
                for row in self.store.state['payments']
                if not statuses or row['status'] in statuses]

    def review(self, **payload):
        row = self.store.call('review_payment', payload, write=True)
        return self._submission(row)

    @staticmethod
    def _submission(row):
        if row is None:
            return None
        # Worker and clients deploy independently. Ignore fields added by a
        # newer worker until this client's PaymentSubmission supports them.
        supported = {field.name for field in fields(PaymentSubmission)}
        values = {key: value for key, value in row.items() if key in supported}
        values['categories'] = tuple(row['categories'])
        return PaymentSubmission(**values)

    def create_request(self, **payload):
        payload = {**payload, 'categories': list(payload.get('categories', []))}
        return self._submission(self.store.call('create_payment_request', payload, write=True))

    def quote(self, group_chat_id, claim_lines, amount_claimed=None):
        return self.store.call('payment_quote', {'group_chat_id': group_chat_id, 'claim_lines': claim_lines,
                                               'amount_claimed': amount_claimed})

    def set_request_message(self, submission_id, request_message_id):
        return self.store.call('set_request_message', dict(submission_id=submission_id,
            request_message_id=request_message_id), write=True)

    def find_awaiting_proof(self, **payload):
        return self._submission(self.store.call('find_awaiting_proof', payload))

    def upload_proof(self, *, path, chat_id, sender_id, **payload):
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('Screenshot is larger than 20 MiB')
        payload = {**payload, 'chat_id': chat_id, 'sender_id': sender_id,
                   'suffix': path.suffix, 'data_b64': base64.b64encode(path.read_bytes()).decode()}
        return self._submission(self.store.call('upload_proof', payload, write=True,
            request_id=f"proof-{payload['submission_id']}-{payload['proof_message_id']}"))


class RemoteMaintenanceService:
    def __init__(self, store: ModalStore):
        self.store = store

    def list_all(self):
        return [MaintenanceTicket(**row) for row in self.store.state['maintenance']]

    def create_ticket(self, **payload):
        return MaintenanceTicket(**self.store.call('create_maintenance', payload, write=True))

    def list_for_group(self, group_chat_id):
        return [MaintenanceTicket(**row) for row in self.store.call('group_maintenance',
            {'group_chat_id': group_chat_id})]

    def update_status(self, ticket_id, status, *, notify_group=False):
        return MaintenanceTicket(**self.store.call('set_maintenance_status', dict(
            ticket_id=ticket_id, status=status, notify_group=notify_group,
        ), write=True))
